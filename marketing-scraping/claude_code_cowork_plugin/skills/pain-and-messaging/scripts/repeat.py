#!/usr/bin/env python3
"""
repeat.py -- manage repeat runs for marketing-scraping skills.

This file is byte-identical in ad-intelligence, pain-and-messaging and
search-intelligence (FROZEN: each skill ships standalone; they drift apart
like apify_client.py once did, so a test pins the sha256 hashes of all three copies).

Commands (all print JSON; errors print {"error": "..."} and exit 2):

  list          --parent <dir> [--tool pain|ad|search]
  prepare       --from <previous-run-dir> [--date YYYY-MM-DD] [--parent <dir>]
                --job   <job-file>         [--date YYYY-MM-DD] [--parent <dir>]
  schedule      --from <run-dir> --ceiling <usd> [--cadence monthly]
                [--mode local|cloud]
  fingerprint   --run <dir>
  run-scheduled --job <job-file> [--publish git]
  wait          --job <job-file>

Usage (full path each time; shell variables do not carry between commands
and zsh does not word-split them):

  python3 "${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py" <command> ...

Self-test (in-memory, no files, no network):

  python3 repeat.py

list
----
Lists all run folders under --parent, grouped by (tool, subject), newest first
within each group.  Optional --tool filters to one tool only.

prepare
-------
Creates a new run folder from a previous one.  Copies the config (with
repeat_of and subject added) and any reusable resolution artifacts.
Refuses when the previous run has no config file.
Accepts --from <dir> (direct) or --job <file>: reads last_run_dir from the
job file, then follows the repeat_of chain from that run to the newest
completed run, and prepares from that one.  The job file is never written
(it stays byte-identical, FROZEN); see cmd_prepare and _newest_in_chain.

schedule
--------
Writes a job file to <parent>/.repeat_jobs/<tool>_<slug>.json recording
the ceiling, cadence, config fingerprint, and last known run directory.
Entry scripts that receive --standing-approval <job> re-verify the
fingerprint and forecast before spending anything.
When an existing job file for the same tool+subject is overwritten, the
output includes "replaced": true, "previous_ceiling" and "previous_mode".
last_run_dir is stored as the run folder's NAME, resolved next to the job's
.repeat_jobs/ folder, so the approval keeps working when the research folder
is copied or cloned to another path.  Job files written before this stored an
absolute path; those keep working exactly as before.
--mode records where the repeat is approved to run: "local" (this computer,
the default) or "cloud" (Claude's cloud).  run-scheduled refuses at $0 in the
other place, so one month is never paid for twice.  --mode cloud refuses unless
the research folder has had its cloud setup (research_sync.py cloud-setup
writes .marketing-scraping/plugin/VERSION): a cloud routine runs the tools from
that copy, so without it every cloud month would fail.

fingerprint
-----------

run-scheduled
-------------
Runs a full scheduled repeat unattended: calls prepare --job, then the entry
script with --standing-approval, then reads notification.json.  Prints one
NOTIFY line then one final JSON {status, message, report, run_dir, exit_code}.
Exits with the entry script's code (0 done, 3 refused, 4 incomplete) or 4 when
no notification was written.  Refuses with exit 2 if the job's tool does not
match this skill's tool; exit 3 if the source run folder was moved, or if the
job's mode does not match where it runs (CLAUDE_CODE_REMOTE == "true" means
the cloud; an unknown mode also refuses).
Never writes the job file, never adds flags that widen spending.
--publish git (a cloud job needs it, and only a cloud job may use it; either
mismatch refuses at $0): before the lock and prepare, research_sync.claim()
brings the clone up to date, refuses when the job or the settings differ from
the version saved in the repository, and pushes a claim commit; a refused push
refuses at $0.  After the entry script, research_sync.save() commits the new
run folder, removes the claim and pushes (one pull --rebase and push again).
A month collected but not saved ends "incomplete", exit 4, and says so.
While it runs, run-scheduled keeps <job>.status.json next to the job file
(its process id, then the final NOTIFY line and JSON exactly as printed).

wait
----
For a run-scheduled that was moved to the background because it takes long.
Each call waits up to about 90 seconds and answers in plain words:
STILL RUNNING (exit 0; call again), FINISHED (exit 0; the run's NOTIFY line and
final JSON exactly as it printed them), DIED (exit 1; its process is gone
without a final result) or NO RUN FOUND (exit 2).  Waiting in short calls is
not a time limit: the run itself is never stopped, however long it takes.

Computes a sha256 over the run's config, excluding keys that do not change
what is scraped, so scheduled repeats can check whether settings changed.

Reusable artifacts (WHY this list lives in every copy):
  ad-intelligence creates competitors/resolution.json and domains.json.
  pain-and-messaging and search-intelligence do not currently produce any
  cross-run reusable artifact: their per-source resolution files (e.g.
  trustpilot_reviews/resolution.json) are always regenerated from the config
  before they are read, so copying them has no effect.
  The list is exhaustive; prepare copies only those that exist in the source run.

Fingerprint exclusions (WHY direction is FROZEN -- see constant docstring):
  subject, repeat_of, budget_caps, resolution_confirmed, _* keys.
"""

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date as _date_cls
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

# ---------------------------------------------------------------------------
# argparse error routing (F4 fix)
# WHY: the documented contract is JSON {"error": ...} plus exit 2 for all
# error paths; argparse's default prints plain text to stderr.  A subclass
# that overrides error() routes parse failures through _fail (defined below),
# keeping the contract uniform.  _fail calls sys.exit, which argparse also
# does — the behaviour for the caller is identical, but the shape is JSON.
# ---------------------------------------------------------------------------

class _SilentParser(argparse.ArgumentParser):
    """Route argparse errors through _fail (JSON output, exit 2)."""
    def error(self, message):
        _fail(f"{self.prog}: {message}")


# ---------------------------------------------------------------------------
# Run-folder conventions (mirrors viewer_server.py exactly; must not import it
# because each skill ships standalone and marketing-viewer is a separate skill)
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE   = re.compile(r'^v\d+$', re.IGNORECASE)

# A directory is a run when it contains one of these markers.
_RUN_MARKERS = frozenset(("frontend_index.json", "AD_INTEL_CONFIG.json",
                           "PAIN_CONFIG.json", "SEARCH_CONFIG.json"))

# Folder prefix -> tool id  (same as viewer_server._TOOL_BY_PREFIX)
_TOOL_BY_PREFIX = {"adintel": "ad", "ad": "ad", "pain": "pain", "search": "search"}

# All known config file names across the three skills.
_CONFIG_NAMES = ("PAIN_CONFIG.json", "AD_INTEL_CONFIG.json", "SEARCH_CONFIG.json")

# Dirs to skip when scanning for run folders.
_IGNORE_DIRS = frozenset((".git", ".tokensave", "node_modules", "__pycache__", ".viewer"))

# Reusable resolution artifacts (one list for all three copies of this file).
#
# WHY this exact list:
#   ad-intelligence: competitors/resolution.json is an explicit reuse gate in
#     ad_intel.py (`if resolution_path.exists() and not args.force: reused=True`).
#     When present, the entire B1 competitor-resolution stage (SERP + LinkedIn
#     enrichment) is skipped.  domains.json is always written alongside it and
#     is read by build_B5 for Google Ads targets.
#   pain-and-messaging: per-source resolution files (trustpilot_reviews/
#     resolution.json, appstore_reviews/resolution.json, etc.) are always
#     regenerated by pain_intel.py BEFORE the corresponding stage reads them.
#     Copying them cross-run would be immediately overwritten; they are not
#     candidates.
#   search-intelligence: seeds come from the config directly; no resolution
#     artifacts are produced that survive across runs.
#
# WHY direction is FROZEN: the list must only contain files the pipeline already
# reuses when present.  Adding a file that is not checked by the pipeline would
# copy stale data silently.  Adding a file that IS checked but always regenerated
# would waste a copy operation and confuse the report.  Extend only in P3 when
# the pipeline is changed to reuse new things (the correctness rules apply).
_REUSABLE_ARTIFACTS = [
    "competitors/resolution.json",
    "competitors/domains.json",
]

# Keys excluded from the config fingerprint.
#
# WHY direction is FROZEN: a false "changed" is safe (incremental falls back to
# a full scrape; scheduled repeats stop and ask the user).  A false "unchanged"
# is dangerous (incremental merge of unlike corpora, or unattended spending on
# changed settings).  When in doubt, INCLUDE the key in the hash.
# These keys are excluded ONLY because they provably do not change what is
# scraped:
#   subject          -- labelling only; does not reach any actor input
#   repeat_of        -- bookkeeping written by prepare; not an actor input
#   _*               -- any key starting with underscore (internal notes,
#                       e.g. _source in the reconstructed SEARCH_CONFIG)
#   budget_caps      -- a spending ceiling, not a scraping selector
#   resolution_confirmed -- a confirmation flag, not a scraping selector
_FINGERPRINT_EXCLUDE = frozenset(("subject", "repeat_of", "budget_caps",
                                  "resolution_confirmed"))


# Skill-directory-name to tool id.  Used by run-scheduled to validate the job's
# tool against this copy's owning skill, and to derive the entry script path.
# WHY at module level: the self-tests read it directly to pick a mismatched tool.
_SKILL_TO_TOOL = {
    "pain-and-messaging": "pain",
    "ad-intelligence":    "ad",
    "search-intelligence": "search",
}


def _own_skill_tool():
    """Return the tool id for this script's skill directory, or None.

    WHY: each copy of repeat.py lives in exactly one skill folder.  The folder
    name is the authoritative source for which tool it serves.  Accepting the
    tool id from a caller argument would let an agent substitute the wrong copy.
    Returns None when run outside a known skill directory (e.g. tests in /tmp).
    """
    return _SKILL_TO_TOOL.get(Path(__file__).resolve().parent.parent.name)


def _write_fallback_notification(run_dir, message):
    """Write a fallback notification.json and INCOMPLETE marker.

    WHY: notification.json is the caller's signal for run outcome.  When the
    entry script crashes before writing it, a missing file would silently hide
    the failure.  The INCOMPLETE marker prevents this folder from being treated
    as a completed run by _run_completed and _newest_in_chain.
    """
    _p = Path(run_dir)
    _notif = {
        "status":  "incomplete",
        "message": str(message)[:200],
        "report":  (
            "The entry script stopped before writing a result file. "
            "Run this job once manually to see the error output and fix it."
        ),
    }
    try:
        (_p / "notification.json").write_text(
            json.dumps(_notif, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (_p / "INCOMPLETE").write_text("", encoding="utf-8")
    except OSError:
        pass  # non-fatal; the caller reports the failure via exit code


# ---------------------------------------------------------------------------
# Shared parsing helpers (must agree with viewer_server.py on every name;
# a development drift test verifies this on a shared name set)
# ---------------------------------------------------------------------------

def _parse_run_folder(name):
    """Parse <prefix>_<slug>_<YYYY-MM-DD>[_v<N>].  Returns (date_str, version).
    Version is 1 when there is no _vN suffix.  Returns (None, 1) for
    unconventional names.  Authority for run_date and run_version."""
    parts = name.split("_")
    if len(parts) < 3 or parts[0] not in _TOOL_BY_PREFIX:
        return None, 1
    for i, part in enumerate(parts):
        if _DATE_RE.match(part):
            after = parts[i + 1:]
            version = 1
            if after and _VN_RE.match(after[0]):
                try:
                    version = int(after[0][1:])
                except ValueError:
                    pass
            return part, version
    return None, 1


def _subject_from_dirname(name):
    """Best-effort subject from <prefix>_<slug>_<YYYY-MM-DD>[_vN].
    Unconventional names (no date, empty slug, unknown prefix) return as-is."""
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] in _TOOL_BY_PREFIX:
        for i, part in enumerate(parts):
            if _DATE_RE.match(part):
                slug_parts = parts[1:i]
                if slug_parts:
                    return " ".join(slug_parts).replace("-", " ").title()
                break
    return name


def _read_config_subject(run_dir):
    """Read 'subject' from the run's config file.  Returns the string or None.
    Tries each name in _CONFIG_NAMES; returns None when no config is present,
    the file is unreadable, or the subject field is missing or empty."""
    for cname in _CONFIG_NAMES:
        p = Path(run_dir) / cname
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        s = cfg.get("subject") if isinstance(cfg, dict) else None
        return s if (s and isinstance(s, str)) else None
    return None


def _corrected_subject(dirname, index, config_subject=None):
    """Subject with the buggy-index correction applied.
    Precedence: index (corrected) > config_subject > folder derivation.
    The correction only fires on _vN runs whose stored subject matches the old
    buggy output AND that have no explicit config_subject."""
    stored = index.get("subject") if isinstance(index, dict) else None
    if stored:
        run_date, _ = _parse_run_folder(dirname)
        last_part = dirname.split("_")[-1]
        has_vn = run_date is not None and bool(_VN_RE.match(last_part))
        if has_vn:
            if config_subject:
                return stored
            parts = dirname.split("_")
            old_buggy = (" ".join(parts[1:-1]).replace("-", " ").title()
                         if len(parts) >= 3 else dirname)
            if stored == old_buggy:
                return _subject_from_dirname(dirname)
        return stored
    if config_subject:
        return config_subject
    return _subject_from_dirname(dirname)


def _tool_of(dirname, index):
    """Tool id from index (authoritative) or folder prefix (fallback)."""
    if isinstance(index, dict) and index.get("tool"):
        return index["tool"]
    return _TOOL_BY_PREFIX.get(dirname.split("_", 1)[0], "unknown")


def _run_sort_key(row):
    """Ordering key: (run_date, run_version, generated_at, run_id).
    Null date sorts before any real date.  run_id is the final tie-breaker."""
    date = row.get("run_date") or "0000-00-00"
    ver  = row.get("run_version") or 1
    gen  = row.get("generated_at") or ""
    rid  = row.get("run_id") or ""
    return (date, ver, gen, rid)


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


# The text of the last JSON object printed.  WHY: run-scheduled records its
# final JSON in its status file exactly as printed, so `wait` can give it back
# verbatim (a model quotes it; it never re-assembles it from parts).
_LAST_OUT = [None]


def _out(obj):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    _LAST_OUT[0] = text
    print(text)


def _fail(msg, code=2):
    _out({"error": msg})
    sys.exit(code)


def _probe_path(raw):
    """Return (resolved path, exists, is a folder) for a path the user gave,
    or (None, False, False) when the value cannot be a path at all.

    WHY: depending on the Python version, a value that cannot be a folder
    raises instead of answering.  A NUL makes resolve() raise ValueError.  On
    Python 3.12 and older (macOS's own python3 is 3.9), a symbolic link loop
    makes resolve() raise RuntimeError, and a name too long for the file
    system makes exists() and is_dir() raise OSError.  Uncaught, the user sees
    a traceback instead of the command's plain error.  WHY only these three and
    only around resolve() and the existence probes: they are what those calls
    raise for a value they cannot use; a broader catch would hide real bugs.
    """
    try:
        path = Path(raw).resolve()
        return path, path.exists(), path.is_dir()
    except (ValueError, OSError, RuntimeError):
        return None, False, False


def _shown(path, raw):
    """The path for an error message: resolved when it could be, else as given."""
    return path if path is not None else repr(raw)


# ---------------------------------------------------------------------------
# Run discovery helper (subset of viewer_server.discover_runs, no run_seq)
# ---------------------------------------------------------------------------

def _discover_runs_under(parent, tool_filter=None):
    """Find run dirs under parent.  Returns a list of dicts with the fields
    needed by list and prepare.  Does not descend into a run dir."""
    parent = Path(parent).resolve()
    rows = []
    for dirpath, dirnames, filenames in os.walk(parent):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        fset = set(filenames)
        if not _RUN_MARKERS.intersection(fset):
            continue
        d = Path(dirpath)
        run_id = str(d.relative_to(parent)) if d != parent else "."
        indexed = "frontend_index.json" in fset
        index = _read_json(d / "frontend_index.json", {}) if indexed else {}

        run_date, run_version = _parse_run_folder(d.name)
        if run_date is None and indexed:
            gen = (index.get("generated_at") or "") if isinstance(index, dict) else ""
            if gen and len(gen) >= 10 and _DATE_RE.match(gen[:10]):
                run_date = gen[:10]

        config_subject = _read_config_subject(d)
        tool = _tool_of(d.name, index)

        if tool_filter and tool != tool_filter:
            dirnames[:] = []
            continue

        if tool == "unknown":
            dirnames[:] = []
            continue

        # repeat_of: index first, then config
        repeat_of = None
        if isinstance(index, dict):
            repeat_of = index.get("repeat_of") or None
        if repeat_of is None:
            cfg = _read_config_if_present(d)
            if isinstance(cfg, dict):
                repeat_of = cfg.get("repeat_of") or None

        rows.append({
            "run_id":      run_id,
            "tool":        tool,
            "subject":     _corrected_subject(d.name, index if indexed else {}, config_subject),
            "run_date":    run_date,
            "run_version": run_version,
            "generated_at": (index.get("generated_at") if indexed and isinstance(index, dict) else None),
            "has_config":  _has_repeatable_config(d),
            "indexed":     indexed,
            "repeat_of":   repeat_of,
        })
        dirnames[:] = []

    return rows


def _read_config_if_present(run_dir):
    """Read and return the config dict for a run, or None."""
    for cname in _CONFIG_NAMES:
        p = Path(run_dir) / cname
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


def _run_completed(run_dir):
    """Return True when run_dir holds a complete, successful run.

    frontend_index.json is written by IDX.build_index() only after a full
    successful run.  Refused (exit 3) and INCOMPLETE (exit 4) paths both exit
    before that call, so the file's presence is necessary -- but not sufficient.

    WHY needed: _newest_in_chain must skip refused / mid-run-stopped months
    so that the next month's prepare chains from the last *completed* run, not
    from an empty or partial workdir.  Without this check, month N refused ->
    month N+1 prepare -> repeat from refused folder -> empty corpus.

    WHY: a folder may have frontend_index.json from a previous successful
    run AND an INCOMPLETE marker or refused/incomplete notification from a
    re-run that was stopped.  The old state (index) must not shadow the new
    state (stop).  Check: if INCOMPLETE exists, or notification.json says
    'refused' or 'incomplete', the run's current state is not complete.
    """
    p = Path(run_dir)
    if not (p / "frontend_index.json").exists():
        return False
    # Check for stop markers that override the index
    if (p / "INCOMPLETE").exists():
        return False
    notif_path = p / "notification.json"
    if notif_path.exists():
        try:
            notif = json.loads(notif_path.read_text(encoding="utf-8"))
            if notif.get("status") in ("refused", "incomplete"):
                return False
        except (OSError, ValueError):
            pass  # unreadable notification: assume completed (conservative)
    return True


def _newest_in_chain(start_dir):
    """Walk the repeat_of chain from start_dir by discovery, return the newest run.

    Scans the parent directory for any run whose config has 'repeat_of' pointing
    at the current run's folder name, advancing until no further run is found.

    WHY discovery, not job-file mutation: the job file must be byte-identical
    before and after any prepare call (FROZEN).  Storing state in the file
    requires mutation.  Scanning the parent folder is cheap and correct.

    Tiebreaker when multiple candidates share a repeat_of: folder date, then
    _vN version, then folder name (final alphabetic tiebreaker).

    Only COMPLETED runs (those with frontend_index.json) are considered
    as candidates.  A refused or INCOMPLETE run is not advanced past -- the
    chain stops at the last completed run so prepare does not chain from an
    empty workdir.
    """
    parent = start_dir.parent
    current = start_dir
    while True:
        current_name = current.name
        candidates = []
        try:
            entries = list(parent.iterdir())
        except OSError:
            break
        for d in entries:
            if not d.is_dir() or d == current:
                continue
            if not _run_completed(d):  # Skip refused/INCOMPLETE runs
                continue
            cfg = _read_config_if_present(d)
            if isinstance(cfg, dict) and cfg.get("repeat_of") == current_name:
                candidates.append(d)
        if not candidates:
            break

        def _dir_key(d):
            run_date, run_ver = _parse_run_folder(d.name)
            return (run_date or "0000-00-00", run_ver or 1, d.name)

        current = max(candidates, key=_dir_key)
    return current


def _has_repeatable_config(run_dir):
    """Returns True only when the run has a config file that parses as a JSON
    object (dict).  WHY: 'has_config: true' means 'can be repeated'; prepare
    refuses a config that is not a JSON object (it would _fail with a clear
    message), so we surface that up-front here too so the agent does not offer
    to repeat a run it cannot actually repeat."""
    for cname in _CONFIG_NAMES:
        p = Path(run_dir) / cname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        return isinstance(data, dict)
    return False


_VN_MAX = 1000   # enough for any realistic day; _fail when exhausted


def _claim_new_dir(base):
    """Atomically claim a new run folder.  Tries <base>, then <base>_v2, ...,
    <base>_v{_VN_MAX}.  Returns the Path that was successfully created.

    WHY mkdir inside the loop instead of exists() + mkdir:
      The exists() + mkdir sequence is a TOCTOU race: two concurrent prepare
      calls both see not exists() and then both call mkdir(exist_ok=False).
      The loser gets a raw FileExistsError traceback (exit 1), which is
      unreadable to the agent (Step 0 reads JSON).  Calling mkdir inside the
      loop and catching FileExistsError is atomic on POSIX: only one caller
      can create any given directory name, so the loser simply moves to the
      next _vN and succeeds.  A bounded loop prevents an infinite spin.
    """
    for n in range(1, _VN_MAX + 2):
        if n > _VN_MAX:
            _fail(
                f"Too many same-day runs (reached v{_VN_MAX}) for "
                f"{base.name}: use a different date with --date."
            )
        candidate = base if n == 1 else base.parent / f"{base.name}_v{n}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    # unreachable; satisfies type checkers
    _fail(f"Internal error: _claim_new_dir exhausted for {base.name}")


def _os_reason(exc):
    """The short reason of a failed folder step, for an error message."""
    return getattr(exc, "strerror", None) or str(exc)


# Every refusal of prepare because the new run folder or its setup could not be
# made starts with this.  run-scheduled recognises it and tells the user that
# reason, instead of saying the approved folder was not found (it was found).
_SETUP_FAILED = "The new run folder could not be "


def _plain_os_reason(exc, denied):
    """The reason a folder step failed, in the user's words.  denied is what a
    permission error means at that step."""
    code = getattr(exc, "errno", None)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return denied
    if code in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return "the disk is full"
    if code == errno.ENAMETOOLONG:
        return "its name would be too long for the file system"
    if code == errno.EFBIG:
        return "a file would be too large"
    return _os_reason(exc)


def _remove_empty(folders):
    """Remove folders this call made, in the order given (deepest first).
    WHY rmdir and never a recursive delete: rmdir removes only an empty folder,
    so it can never take anything that was put there by someone else."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass


def _make_parent(parent, raw):
    """Make a missing --parent and its missing ancestors.  Returns the folders
    that did not exist before, deepest first, for _remove_empty.

    WHY: mkdir(parents=True) makes the ancestors before it reaches the last
    name.  A name too long, a read-only folder or a full disk then raised a
    traceback and left the ancestors it had made in the user's project.  Now
    the failure gives the command's JSON error and those folders are removed.
    WHY a catch and not a name-length check: the limit differs per file
    system, and a check would not cover a read-only or full disk.  WHY only
    these three and only around this step: they are what making a folder
    raises for a value it cannot use; nothing here spends, locks or writes an
    approval.
    """
    missing = []
    step = parent
    while step != step.parent and not os.path.lexists(step):
        missing.append(step)
        step = step.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, RuntimeError) as exc:
        _remove_empty(missing)
        _fail(f"--parent cannot be made as a folder: {raw!r} ({_os_reason(exc)})")
    return missing


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------

def cmd_list(args):
    parent, _, _parent_is_dir = _probe_path(args.parent)
    if not _parent_is_dir:
        _fail(f"--parent is not a folder: {_shown(parent, args.parent)}")

    tool_filter = args.tool or None
    rows = _discover_runs_under(parent, tool_filter=tool_filter)

    # Group by (tool, subject)
    groups = {}
    for row in rows:
        key = (row["tool"], row["subject"])
        groups.setdefault(key, []).append(row)

    # Sort within each group by run order, then reverse for newest-first display
    result = []
    # Group order: stable deterministic — tool asc, then subject asc
    for key in sorted(groups.keys()):
        group_rows = groups[key]
        ordered = sorted(group_rows, key=_run_sort_key, reverse=True)  # newest first
        runs_out = []
        for r in ordered:
            runs_out.append({
                "run_id":      r["run_id"],
                "run_date":    r["run_date"],
                "run_version": r["run_version"],
                "has_config":  r["has_config"],
                "indexed":     r["indexed"],
                "repeat_of":   r["repeat_of"],
            })
        result.append({
            "tool":    key[0],
            "subject": key[1],
            "runs":    runs_out,
        })

    _out(result)
    return 0


# ---------------------------------------------------------------------------
# Job file: where its approved run folder is
# ---------------------------------------------------------------------------

def _resolve_job_run_dir(job_path, raw):
    """Return (folder, None) for a job's last_run_dir, or (None, plain reason).

    Never raises for a bad value; see _resolve_job_run_dir_unchecked for the
    rules.  WHY the catch: a value that cannot be a path at all (a NUL
    character, which JSON can carry as \\u0000, or a name the file system
    rejects) makes resolve() raise ValueError or OSError.  Uncaught, that
    crashes run-scheduled with no NOTIFY line and no final JSON, so the
    unattended caller and the notification learn nothing.  RuntimeError too:
    on Python 3.12 and older (macOS's own python3 is 3.9), resolve() raises it
    for a symbolic link loop, its only documented cause there.  WHY only these
    three and only around this step: they are what resolve() raises for a
    value it cannot use; a broader catch would hide real bugs.
    WHY the exists() probe inside the catch: on Python 3.12 and older, a name
    too long for the file system resolves without error but then makes
    stat() raise OSError (exists() and is_dir() only swallow "not found"
    errors there), so the crash would just move to the caller's is_dir().
    A folder that is simply missing still returns normally, so its own
    refusal ("was not found") is unchanged.
    """
    try:
        folder, why = _resolve_job_run_dir_unchecked(job_path, raw)
        if folder is not None:
            folder.exists()
        return folder, why
    except (ValueError, OSError, RuntimeError):
        return None, (f"The monthly repeat's approval names its research folder as "
                      f"{raw!r}, which cannot be used as a folder.")


def _resolve_job_run_dir_unchecked(job_path, raw):
    """Return (folder, None) for a job's last_run_dir, or (None, plain reason).

    job_path must already be resolved.  Two forms are accepted:
      - an absolute path (every job written before approvals became portable):
        used as-is, exactly as before.  WHY no fallback to its folder name when
        it is missing: that would silently move an approval to a folder the
        user never approved.  Refusing is safe (it spends nothing).
      - a single folder name, resolved next to the job's .repeat_jobs/ folder
        (job_path.parent.parent).  WHY: a copied or cloned research folder has
        another absolute path, so only a name travels with it.
    WHY a name must be one plain folder name (no / or \\, not . or .., not
    empty) and must stay inside the research folder after links are followed:
    a job file can be shared or committed to a repository, and a shared file
    must never be able to point a paid run at an arbitrary folder.
    """
    if not isinstance(raw, str) or raw == "":
        return None, (f"The monthly repeat's approval does not name its research "
                      f"folder ({raw!r}).")
    if Path(raw).is_absolute():
        return Path(raw).resolve(), None
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        return None, (f"The monthly repeat's approval names its research folder as "
                      f"{raw!r}, which is not a single folder name inside the "
                      f"research folder.")
    root = job_path.parent.parent.resolve()
    resolved = (root / raw).resolve()
    if resolved.parent != root:
        return None, (f"The monthly repeat's approval names the folder {raw!r}, "
                      f"which leads outside the research folder.")
    return resolved, None


# ---------------------------------------------------------------------------
# prepare command
# ---------------------------------------------------------------------------

def cmd_prepare(args):
    # --job mode: read last_run_dir (the scheduled run) from the job file, then
    # walk the repeat_of chain to find the newest prepared run.
    # WHY discovery not mutation: the job file must remain byte-identical across
    # any number of prepare calls (FROZEN).  Scanning the parent folder for
    # the newest repeat_of successor is cheap and correct by construction; no
    # state needs to be stored back into the file.
    _job_path = None
    if getattr(args, "job", None) is not None:
        _job_path, _job_found, _ = _probe_path(args.job)
        if not _job_found:
            _fail(f"--job file not found: {_shown(_job_path, args.job)}")
        try:
            _job_raw = json.loads(_job_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            _fail(f"Could not read job file {_job_path}: {exc}")
        if not isinstance(_job_raw, dict) or "last_run_dir" not in _job_raw:
            _fail(f"Invalid job file (missing last_run_dir): {_job_path}")
        _scheduled_dir, _why = _resolve_job_run_dir(_job_path, _job_raw["last_run_dir"])
        if _why is not None:
            _fail(_why)
        args.from_dir = str(_newest_in_chain(_scheduled_dir))
    elif getattr(args, "from_dir", None) is None:
        _fail("Either --from or --job is required")

    prev_dir, _, _prev_is_dir = _probe_path(args.from_dir)
    if not _prev_is_dir:
        _fail(f"--from is not a folder: {_shown(prev_dir, args.from_dir)}")

    # ---- Check that the previous run has a config -------------------------
    config_name = None
    for cname in _CONFIG_NAMES:
        if (prev_dir / cname).exists():
            config_name = cname
            break
    if config_name is None:
        _fail(
            f"This run has no saved settings "
            f"(no {' / '.join(_CONFIG_NAMES)} found in {prev_dir.name}), "
            f"so it cannot be repeated exactly. Start a new run instead."
        )

    # ---- Resolve the previous run's subject (explicit from now on) ----
    indexed = (prev_dir / "frontend_index.json").exists()
    index = _read_json(prev_dir / "frontend_index.json", {}) if indexed else {}
    config_subject = _read_config_subject(prev_dir)
    subject = _corrected_subject(prev_dir.name, index if indexed else {}, config_subject)

    # ---- Determine parent and date ----------------------------------------
    parent = prev_dir.parent
    if args.parent:
        # A missing --parent is created below, as before.  One that exists but
        # is not a folder, or cannot be a path (a loop, a name too long), is
        # refused.  WHY stat() and not _probe_path: on Python 3.13 and newer
        # exists() answers False for a loop or a name too long, which would
        # read as "missing" and crash later when the folder is made.
        try:
            parent = Path(args.parent).resolve()
            parent.stat()
            _parent_ok = parent.is_dir()
        except FileNotFoundError:
            _parent_ok = True
        except (ValueError, OSError, RuntimeError):
            _parent_ok = False
        if not _parent_ok:
            _fail(f"--parent cannot be used as a folder: {args.parent!r}")
    raw_date = args.date
    if raw_date is None:
        run_date = str(_date_cls.today())
    else:
        # Validate: must match YYYY-MM-DD AND be a real calendar date.
        # WHY fromisoformat: the regex _DATE_RE accepts 2026-13-40 (valid
        # pattern, impossible date); an impossible date sorts forever wrong
        # because the app orders runs by folder date.
        if not raw_date or not _DATE_RE.match(raw_date):
            _fail(f"--date must be YYYY-MM-DD, got: {raw_date!r}")
        try:
            _date_cls.fromisoformat(raw_date)
        except ValueError as exc:
            _fail(f"--date {raw_date!r} is not a valid calendar date: {exc}")
        run_date = raw_date

    # ---- Build new folder name from previous run's prefix + slug + date ---
    prev_name = prev_dir.name
    prev_date, _ = _parse_run_folder(prev_name)
    if prev_date is not None:
        # Strip _vN suffix and the date; prefix_slug is what remains
        parts = prev_name.split("_")
        # Find date position
        date_i = next(i for i, p in enumerate(parts) if _DATE_RE.match(p))
        prefix_slug = "_".join(parts[:date_i])  # e.g. "pain_acme-fitness"
    else:
        # Unconventional name: use the whole name as prefix_slug
        prefix_slug = prev_name

    base_name = f"{prefix_slug}_{run_date}"
    new_dir = parent / base_name

    # ---- Atomically claim a new folder (race-safe _vN bump) -----------
    # A --parent the user chose is made first, on its own, so a value that
    # cannot be made refuses cleanly and leaves nothing behind (_make_parent).
    # The run folder inside it can still fail (a read-only --parent, a name too
    # long); that refuses too, and removes what _make_parent made.  Without
    # --parent the folder is the previous run's own parent: unchanged.
    _made = []
    if args.parent:
        _made = _make_parent(parent, args.parent)
        try:
            new_dir = _claim_new_dir(parent / base_name)
        except (OSError, ValueError, RuntimeError) as exc:
            _remove_empty(_made)
            _fail(f"The new run folder cannot be made in --parent {args.parent!r} "
                  f"({_os_reason(exc)})")
    else:
        # WHY a catch here too: this is the path run-scheduled uses.  A folder
        # that became read-only, a full disk, or a new name too long (the date
        # is added to the previous run's name) raised a traceback, so an
        # unattended month ended with no NOTIFY line and no final JSON and the
        # user never learned it was skipped.  WHY only these three and only
        # around making the folder: they are what making a folder raises;
        # nothing here spends, locks or writes an approval.  mkdir makes one
        # folder here (the parent is the previous run's own parent, which
        # exists), so a failure leaves nothing to remove.
        try:
            new_dir = _claim_new_dir(parent / base_name)
        except (OSError, ValueError, RuntimeError) as exc:
            _fail(f"{_SETUP_FAILED}made in {parent} "
                  f"({_plain_os_reason(exc, 'this folder cannot be written to')}).")

    # ---- Copy the config into the new folder ------------------------------

    # Read the previous config, add repeat_of and subject
    try:
        prev_cfg = json.loads((prev_dir / config_name).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        shutil.rmtree(new_dir, ignore_errors=True)
        _remove_empty(_made)
        _fail(f"Could not read config {config_name} from {prev_dir.name}: {exc}")

    if not isinstance(prev_cfg, dict):
        shutil.rmtree(new_dir, ignore_errors=True)
        _remove_empty(_made)
        _fail(f"Config {config_name} in {prev_dir.name} is not a JSON object")

    new_cfg = dict(prev_cfg)
    new_cfg["repeat_of"] = prev_dir.name   # FROZEN
    new_cfg["subject"]   = subject          # FROZEN

    config_out = new_dir / config_name
    # WHY a catch around the setup writes: a full disk or a file that cannot
    # be read or written raised a traceback after the run folder was made, and
    # left that half-made folder behind (a later month could chain from it).
    # Now the folder this call claimed is removed and prepare refuses with its
    # JSON error, which run-scheduled turns into its NOTIFY line.  WHY only
    # these three and only these writes: nothing here spends, locks or writes
    # an approval; the entry script starts only after prepare returns.
    try:
        config_out.write_text(
            json.dumps(new_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ---- Copy reusable resolution artifacts (if they exist) -----------
        copied_artifacts = []
        for rel in _REUSABLE_ARTIFACTS:
            src = prev_dir / rel
            if not src.exists():
                continue
            dst = new_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_artifacts.append(rel)
    except (OSError, ValueError, RuntimeError) as exc:
        shutil.rmtree(new_dir, ignore_errors=True)
        _remove_empty(_made)
        _fail(f"{_SETUP_FAILED}set up in {parent} "
              f"({_plain_os_reason(exc, 'a file could not be read or written')}).")

    _out({
        "new_run_dir":         str(new_dir),
        "run_id":              new_dir.name,
        "repeat_of":           prev_dir.name,
        "subject":             subject,
        "config":              str(config_out),
        "copied_artifacts":    copied_artifacts,
    })
    # FROZEN: the job file is never written here.  The next prepare call
    # will rediscover the newest run via _newest_in_chain.

    return 0


# ---------------------------------------------------------------------------
# schedule command
# ---------------------------------------------------------------------------

def _approval_forecast(from_dir):
    """The forecast `forecast --from from_dir` prints, or None when it cannot
    be computed.  WHY cmd_forecast itself, in-process: the stored figure must
    be the very number the user was shown before approving; it spends
    nothing and writes only into a temporary folder it removes."""
    import io as _io
    from contextlib import redirect_stdout as _redir

    class _FcArgs:
        pass
    _a = _FcArgs()
    _a.from_dir = str(from_dir)
    _buf = _io.StringIO()
    try:
        with _redir(_buf):
            cmd_forecast(_a)
    except (SystemExit, Exception):  # noqa: BLE001 - shown as null, never a crash
        return None
    try:
        usd = json.loads(_buf.getvalue()).get("forecast_usd")
    except (ValueError, AttributeError):
        return None
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not (0 <= usd < float("inf")):
        return None
    return float(usd)


# What `schedule` says when the forecast could not be worked out at approval
# (see the end of cmd_schedule).
SCHEDULE_NO_FORECAST_NOTE = (
    "No cost forecast could be worked out just now, so none was recorded with this "
    "approval (the approval itself holds), and if a later month is stopped because its "
    "forecast is above your ceiling, that message cannot say whether the forecast rose "
    "since you approved it.")


def cmd_schedule(args):
    """Write a job file recording approval: tool, ceiling, fingerprint, cadence.

    WHY atomic write: a crash during the write would leave a partial JSON file
    that the entry scripts would try to parse and fail on -- better to either
    have the old file or the new file, never a half-written one.
    """
    # WHY _probe_path: a value that cannot be a folder must refuse like any
    # other path that is not a folder, not crash (see _probe_path).
    from_dir, _, _from_ok = _probe_path(args.from_dir)
    if not _from_ok:
        _fail(f"--from is not a folder: {_shown(from_dir, args.from_dir)}")

    # Tool detection (index or prefix)
    indexed = (from_dir / "frontend_index.json").exists()
    index = _read_json(from_dir / "frontend_index.json", {}) if indexed else {}
    tool = _tool_of(from_dir.name, index if indexed else {})
    if tool not in ("pain", "ad", "search"):
        _fail(f"Cannot determine tool from folder name: {from_dir.name!r}")

    # Subject
    config_subject = _read_config_subject(from_dir)
    subject = _corrected_subject(from_dir.name, index if indexed else {}, config_subject)

    # Fingerprint (same logic as cmd_fingerprint)
    cfg = _read_config_if_present(from_dir)
    if cfg is None:
        _fail(f"No config file found in {from_dir.name}. "
              f"A job requires one of: {', '.join(_CONFIG_NAMES)}")
    if not isinstance(cfg, dict):
        _fail(f"Config in {from_dir.name} is not a JSON object")

    filtered = {k: v for k, v in cfg.items()
                if k not in _FINGERPRINT_EXCLUDE and not k.startswith("_")}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # Validate ceiling
    try:
        ceiling = float(args.ceiling)
    except (TypeError, ValueError):
        _fail(f"--ceiling must be a positive number, got: {args.ceiling!r}")
    # NaN passed "ceiling <= 0" and 1e999 became
    # infinity. The job file then held NaN or Infinity (not valid JSON), every
    # "forecast > ceiling" refusal was False for it, and the user read "Ceiling
    # $nan". A ceiling is a finite number of dollars above zero.
    if ceiling != ceiling or ceiling in (float("inf"), float("-inf")):
        _fail(f"--ceiling must be a positive number, got: {args.ceiling!r}")
    if ceiling <= 0:
        _fail(f"--ceiling must be positive, got: {ceiling}")

    # Slug from subject for job filename: lowercase, non-alphanumeric -> hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-") or "run"

    # Job file location: <parent>/.repeat_jobs/<tool>_<subject-slug>.json
    parent = from_dir.parent

    # WHY a cloud approval needs the cloud setup first: the cloud routine runs
    # the tools from the copy research_sync.py cloud-setup puts in the research
    # folder.  Without it every cloud month would fail, after the user had
    # already been told the repeat was set up.  Checked before anything is
    # written, so a refusal leaves the folder exactly as it was.
    if (getattr(args, "mode", None) == "cloud"
            and not (parent / ".marketing-scraping" / "plugin" / "VERSION").is_file()):
        _fail("A repeat in the cloud needs the cloud setup of this research folder "
              "first (research_sync.py cloud-setup). Nothing was changed.")

    jobs_dir = parent / ".repeat_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_file = jobs_dir / f"{tool}_{slug}.json"

    # Where the repeat is approved to run (see cmd_run_scheduled's mode check).
    # WHY getattr: callers that build args by hand predate --mode; no mode
    # means this computer, which is what every earlier approval meant.
    mode = getattr(args, "mode", None) or "local"
    if mode not in ("local", "cloud"):
        _fail(f"--mode must be local or cloud, got: {mode!r}")

    # WHY the folder's name, not its absolute path: the approval must keep
    # working when the research folder is copied or cloned to another path.
    # Checked with the same rule run-scheduled applies, so a folder name that
    # could not be resolved later is refused now instead of every month.
    _resolved_check, _why = _resolve_job_run_dir(job_file.resolve(), from_dir.name)
    if _why is not None or _resolved_check != from_dir:
        _fail(f"Cannot record this run folder in the approval: "
              f"{_why or from_dir.name!r}")

    job = {
        "tool":         tool,
        "subject":      subject,
        "subject_slug": slug,
        "ceiling":      ceiling,
        "cadence":      args.cadence,
        "fingerprint":  fingerprint,
        "last_run_dir": from_dir.name,
        "mode":         mode,
    }
    # WHY the forecast in effect now is kept with the approval: a
    # month later refused because its forecast is above the ceiling can then
    # say the forecast rose since approval, with both figures, instead of
    # leaving the user to think the ceiling was set wrong.  It is computed the
    # way `forecast` computes it (the number the user was shown).  When it
    # cannot be computed, nothing is stored and the approval still holds: a
    # refusal then never states an earlier figure.
    approved_forecast = _approval_forecast(from_dir)
    if approved_forecast is not None:
        job["approved_forecast_usd"] = approved_forecast

    # Check if replacing an existing approval.
    # WHY report it: two schedule calls for the same tool and subject in the same
    # parent folder share one job file.  The second silently replaces the first
    # approval.  That is intentional (re-approving IS how you change a ceiling),
    # but the user must be told so they know the earlier approval no longer holds.
    _replaced = False
    _prev_ceiling = None
    _prev_mode = None
    if job_file.exists():
        _old = _read_json(job_file, {})
        if isinstance(_old, dict) and "ceiling" in _old:
            try:
                _prev_ceiling = float(_old["ceiling"])
                _replaced = True
                if _prev_ceiling != _prev_ceiling or _prev_ceiling in (float("inf"),
                                                                        float("-inf")):
                    _prev_ceiling = None  # An old NaN/Infinity is no amount
            except (TypeError, ValueError):
                pass
            # No mode in the old file meant this computer.
            _prev_mode = _old["mode"] if "mode" in _old else "local"

    # Atomic write (temp + rename)
    tmp = job_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(job_file)

    _sched_out = {
        "job_file":    str(job_file),
        "tool":        tool,
        "subject":     subject,
        "ceiling":     ceiling,
        "cadence":     args.cadence,
        "fingerprint": fingerprint,
        "mode":        mode,
        "approved_forecast_usd": approved_forecast,
    }
    if _replaced:
        _sched_out["replaced"]         = True
        _sched_out["previous_ceiling"] = _prev_ceiling
        _sched_out["previous_mode"]    = _prev_mode
    # WHY one plain sentence when no forecast was recorded: a null
    # figure alone tells the user nothing, and they would later expect a
    # refusal to say whether the forecast rose since approval, which it then
    # cannot.  Information only: the approval above is written either way,
    # because the ceiling, not this figure, is what limits spending.
    if approved_forecast is None:
        _sched_out["forecast_note"] = SCHEDULE_NO_FORECAST_NOTE

    _out(_sched_out)
    return 0


# ---------------------------------------------------------------------------
# fingerprint command
# ---------------------------------------------------------------------------

def cmd_fingerprint(args):
    run_dir, _, _run_is_dir = _probe_path(args.run)
    if not _run_is_dir:
        _fail(f"--run is not a folder: {_shown(run_dir, args.run)}")

    cfg = _read_config_if_present(run_dir)
    if cfg is None:
        _fail(
            f"No config file found in {run_dir.name}. "
            f"A fingerprint requires one of: {', '.join(_CONFIG_NAMES)}"
        )
    if not isinstance(cfg, dict):
        _fail(f"Config in {run_dir.name} is not a JSON object")

    included_keys = []
    excluded_keys = []
    filtered = {}
    for k, v in cfg.items():
        if k in _FINGERPRINT_EXCLUDE or k.startswith("_"):
            excluded_keys.append(k)
        else:
            included_keys.append(k)
            filtered[k] = v

    # Canonical JSON: sorted keys, no whitespace, stable encoding
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    _out({
        "fingerprint":    digest,
        "included_keys":  sorted(included_keys),
        "excluded_keys":  sorted(excluded_keys),
    })
    return 0


# ---------------------------------------------------------------------------
# run-scheduled lock (never run two unattended runs of one repeat at once)
# ---------------------------------------------------------------------------
#
# WHY a lock: without one, two run-scheduled calls on the same job (a scheduler
# that fires twice, a manual run while the scheduled one is going) each pass the
# spending gate, claim separate run folders, and each spend up to the ceiling.
# Only SIMULTANEOUS runs are blocked: a second run the next
# day is allowed, so there is no cadence window here on purpose.
#
# WHY a stale lock is taken over (FROZEN): a crashed run leaves its lock behind.
# If that blocked every later month, a monthly job would silently refuse forever
# after one crash, which is the silent zero again, only slower.
#
# Staleness rule (adjustable, chosen here):
#   - the holder's pid is not alive                      -> stale;
#   - the lock is older than _LOCK_STALE_SECONDS (24 h)  -> stale, even if a
#     process with that pid exists. Covers pid reuse (a month later the number
#     may belong to some other program) and systems where liveness cannot be
#     probed safely (Windows: os.kill(pid, 0) is not a probe there). A real run
#     takes minutes to a few hours, so 24 h never cuts a live run short in
#     practice, while a month-old lock can never block a month;
#   - an unreadable lock (a racing run between creating the file and writing
#     it) is held for _LOCK_UNREADABLE_GRACE seconds, then stale.

_LOCK_STALE_SECONDS = 24 * 3600
_LOCK_UNREADABLE_GRACE = 60


def _pid_alive(pid):
    """True when a process with this pid exists (or cannot be probed safely)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False  # never probe 0 / negatives: they address process groups
    if os.name == "nt":
        return True   # no safe probe; the 24 h age rule breaks stale locks here
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    except OSError:
        return False
    return True


def _lock_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _lock_info(raw):
    try:
        info = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return info if isinstance(info, dict) else None


def _lock_started_text(info):
    """Plain start time for the user, e.g. '2026-09-25 14:03 UTC'."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        return _dt.fromtimestamp(float(info["started_epoch"]), _tz.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


try:
    import fcntl as _fcntl
except ImportError:   # Windows: no file locks; the process-id checks are used
    _fcntl = None


def _flock_state(path):
    """"held" when a process of a run holds this lock file's flock, "free" when
    none does, "unknown" when it cannot be checked (no such file, Windows, a
    file system without locks).  Takes the flock shared for an instant and lets
    it go; never blocks.

    WHY flock as well as the pid in the file (the same idea as the entry
    scripts' run folder lock): run-scheduled passes its flock to the entry
    script it starts, and the system drops it only when the last of them ends.
    If run-scheduled itself is killed while the entry script is still paying
    for a month, its pid is gone but the run is not: the flock says so."""
    if _fcntl is None:
        return "unknown"
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        _fcntl.flock(fd, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    except OSError:
        return "unknown"
    finally:
        os.close(fd)


def _hold_flock(path):
    """Take this run's lock file's flock exclusively; return the fd or None."""
    if _fcntl is None:
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return None
    try:
        # Blocking: only a `wait` or another run's staleness check can hold it,
        # and each holds it shared for an instant.
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        return fd
    except OSError:
        os.close(fd)
        return None


def _lock_stale_reason(info, lock_path, now):
    """None when the lock is held by a live run, else a plain reason it is stale."""
    # A held flock means a process of that run is still alive (see _flock_state),
    # whatever its pid or age says.  WHY it outranks the 24 h rule: taking the lock
    # over while any process of the run is alive lets two runs pay for one month.
    if _flock_state(lock_path) == "held":
        return None
    started = (info or {}).get("started_epoch")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        try:
            started = Path(lock_path).stat().st_mtime
        except OSError:
            return "the lock file vanished"
    age = now - float(started)
    if info is None or not isinstance(info.get("pid"), int):
        if age > _LOCK_UNREADABLE_GRACE:
            return "its lock file could not be read"
        return None
    if age > _LOCK_STALE_SECONDS:
        return "it started more than 24 hours ago"
    if not _pid_alive(info["pid"]):
        return "its process is no longer running"
    return None


def _acquire_job_lock(job_path):
    """Take <job file>.lock atomically (O_CREAT|O_EXCL), holding pid and start time.

    Returns a dict: {"path", "held_by" (the live holder's info, or None when we
    hold the lock), "data" (the bytes we wrote), "taken_over" (the stale lock's
    info and reason, or None)}.

    A stale lock is moved aside under a name unique to this process; os.rename is
    atomic, so when two runs find the same stale lock only one moves it. The
    mover then checks that it moved the stale bytes it judged, not a fresh lock
    another run created in between; if not, it puts that lock back and tries
    again, and then sees a live lock and refuses. Contended three times: refuse
    (spending nothing is the safe side)."""
    import time as _time
    lock = Path(str(job_path) + ".lock")
    taken_over = None
    info = None
    for _attempt in range(3):
        now = _time.time()
        mine = {"pid": os.getpid(), "started_epoch": now,
                "started": _lock_started_text({"started_epoch": now})}
        data = json.dumps(mine).encode("utf-8")
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raw = _lock_bytes(lock)
            if raw is None:
                continue  # released between our create and our read
            info = _lock_info(raw)
            reason = _lock_stale_reason(info, lock, now)
            if reason is None:
                return {"path": lock, "held_by": info or {}, "data": None,
                        "taken_over": None}
            aside = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
            try:
                os.rename(str(lock), str(aside))
            except FileNotFoundError:
                continue  # another run moved it first
            if _lock_bytes(aside) != raw:
                # We moved a fresh lock another run just took: put it back.
                try:
                    os.link(str(aside), str(lock))
                except OSError:
                    if not lock.exists():
                        try:
                            os.rename(str(aside), str(lock))
                        except OSError:
                            pass
                try:
                    aside.unlink()
                except OSError:
                    pass
                continue
            try:
                aside.unlink()
            except OSError:
                pass
            taken_over = {"info": info or {}, "reason": reason}
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return {"path": lock, "held_by": None, "data": data, "taken_over": taken_over}
    return {"path": lock, "held_by": info or {}, "data": None, "taken_over": None}


def _release_job_lock(lock):
    """Remove the lock only if it is still ours (after a 24 h takeover it is not)."""
    if lock.get("data") is None:
        return
    if _lock_bytes(lock["path"]) == lock["data"]:
        try:
            Path(lock["path"]).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# run-scheduled: the status file `wait` reads, and the one way it ends
# ---------------------------------------------------------------------------
#
# WHY a status file: a scheduled run can take longer than
# one command may last, so the agent may move it to the background.  Nothing
# then told the agent when it ended, and in the cloud an agent that ends its
# turn lets the machine be reclaimed with a paid month in it.  run-scheduled
# writes <job>.status.json next to the job file: "running" with its process id
# at the start, and the final NOTIFY line and JSON, exactly as printed, at the
# end.  `repeat.py wait --job <job>` reads it.  The file is a convenience: a
# failure to write it never stops or changes the run.

_RS = {"status_path": None, "status_doc": None, "publish": None, "flock": None,
       "ended": False}

_NOTIFY = "NOTIFY (send as Claude app notification): "


def _status_path_for(job_path):
    name = Path(job_path).name
    stem = name[:-5] if name.endswith(".json") else name
    return Path(job_path).with_name(stem + ".status.json")


def _given_through_link(raw):
    """True when the job path as given reaches its file through a link: the
    file itself, or the folder it is in (.repeat_jobs).  WHY only these two:
    they are the two parts research_sync.claim checks between the research
    folder and the job; links above the research folder are the user's own
    choice of place (on macOS /tmp is one)."""
    try:
        p = Path(os.path.abspath(str(raw)))
        return p.is_symlink() or p.parent.is_symlink()
    except (OSError, ValueError):
        return False


def _write_status(doc):
    path = _RS["status_path"]
    if path is None:
        return
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass  # a convenience for `wait`, never the run


def _status_end(exit_code, notify=None, final=None, crashed=False):
    import time as _time
    doc = _RS.get("status_doc")
    if doc is None or _RS.get("ended"):
        return
    _RS["ended"] = True
    now = _time.time()
    _write_status(dict(doc, state="failed" if crashed else "finished",
                       finished_epoch=now, finished=_lock_started_text({"started_epoch": now}),
                       exit_code=exit_code, notify=notify, final=final))


def _finish_run(status, message, report, run_dir, exit_code, troubleshooting=None):
    """Print the NOTIFY line and the final JSON, record them for `wait`, exit.

    Every end of run-scheduled after the job was read goes through here, so a
    cloud run saves (or releases its claim) on every way out, and `wait` gets
    the same words the run printed.

    troubleshooting: git's own words about the research repository, kept apart
    from message and report.  WHY apart: message and report are quoted to the
    user word for word, and git's text names addresses and folders of this
    machine in words a user cannot act on.  Nothing asks Claude to quote it."""
    trouble = list(troubleshooting or [])
    pub = _RS.get("publish")
    saved = None
    if pub is not None:
        _RS["publish"] = None
        status, message, report, exit_code, saved = _save_month(
            pub, status, message, report, run_dir, exit_code)
        trouble += list(pub["ctx"].get("troubleshooting") or [])
    notify = f"{_NOTIFY}{message}"
    print(notify)
    final = {
        "status":    status,
        "message":   message,
        "report":    report,
        "run_dir":   run_dir,
        "exit_code": exit_code,
    }
    if saved is not None:
        final["saved"] = saved
    if trouble:
        final["troubleshooting"] = trouble
    _out(final)
    _status_end(exit_code, notify=notify, final=_LAST_OUT[0])
    sys.exit(exit_code)


def _save_month(pub, status, message, report, run_dir, exit_code):
    """Save the month to the research repository and say plainly if it was not.

    WHY a collected month that is not saved is never reported as done: in the
    cloud the machine is thrown away after the run, so an unsaved month is lost
    although it was paid for.  The user must hear that, not "done"."""
    saved, why = pub["mod"].save(pub["ctx"], run_dir)
    # WHY added to the report in every case: a claim left in the repository
    # skips the next cloud run for 24 hours, and the user must know why.
    note = pub["ctx"].get("claim_note")
    if note:
        report = ((report + "\n\n") if report else "") + note
    if saved:
        if run_dir is not None:
            report = ((report + "\n\n") if report else "") + (
                "This month's folder was saved to your research repository.")
            left = pub["mod"].left_out_words(pub["ctx"].get("left_out") or [])
            if left:
                report += " " + left
        return status, message, report, exit_code, True
    why = str(why or "no reason was given").rstrip(".")
    if run_dir is not None and exit_code == 0:
        # WHY the time and not "can be run again": this run's claim on the
        # month stays in the repository (research_sync.claim_kept_until says
        # why), and a new run before then is skipped.
        until = pub["mod"].claim_kept_until(pub["ctx"])
        if until is None:
            again = "this month can be run again; a new run is charged again."
        elif pub["ctx"].get("claim_clash"):
            # WHY not this run's time: the update before the second
            # try found this month's reservation changed in the repository
            # while this run was going, so it is no longer the one this run
            # made, and this run does not know when the new one was made.
            again = ("this month can be run again once the reservation of this month in "
                     "your research repository is 24 hours old. It was changed there "
                     "while this run was going, so this run cannot tell when that is. "
                     "Until then a new run of this repeat is skipped and spends nothing, "
                     "so that two runs never pay for the same month at the same time. A "
                     "new run is charged again.")
        elif pub["ctx"].get("claim_mine") is False:
            # WHY not "this run's claim", and not "another run's" either: the
            # claim in this run's copy was not its own (or could not be read),
            # so it was left as it is and never sent.  The repository then
            # still holds the claim this run sent at its start, unless
            # something replaced it there meanwhile; the run cannot tell which.
            # WHY a link is named as a link: "could not be read"
            # hid that the file is a shortcut to another place, which the
            # user acts on differently from a file whose content is damaged.
            link = pub["ctx"].get("claim_link")
            if link and link == pub["ctx"].get("claim_rel"):
                found = "was a link (a shortcut to another place on this computer)"
            elif link:
                found = ("was reached through a link (a shortcut to another place on "
                         "this computer)")
            elif pub["ctx"].get("claim_unreadable"):
                found = "could not be read"
            else:
                found = "did not hold this run's reservation"
            again = (f"this month can be run again once the reservation of this month in "
                     f"your research repository is 24 hours old: {until} if it is still the "
                     f"reservation this run made when it started. At the end of the run, "
                     f"the file that holds this month's reservation in the research folder "
                     f"on the machine this run used {found}, so this run left it as it is "
                     f"and cannot tell which reservation is in your research repository "
                     f"now. Until then a new run of this repeat is skipped and spends "
                     f"nothing, so that two runs never pay for the same month at the same "
                     f"time. A new run is charged again.")
        else:
            again = (f"this month can be run again {until}. Until then a new run of "
                     f"this repeat is skipped and spends nothing: this run's reservation "
                     f"of the month stays in your research repository for 24 hours, so "
                     f"that two runs never pay for the same month at the same time. A new "
                     f"run is charged again.")
        report = (f"Why it was not saved: {why}. What to do next: once that is sorted "
                  f"out, {again} What the run itself reported: {message} "
                  f"{report}").strip()
        message = ("This month's research was collected, but it could not be saved to "
                   "your research repository, so it is not in your research and will be "
                   "lost when this cloud session ends. Nothing more was spent.")
        return "incomplete", message, report, 4, False
    if run_dir is not None:
        extra = f"This run's folder could not be saved to your research repository: {why}."
    else:
        extra = (f"Its reservation of this month could not be removed from your research "
                 f"repository, so another cloud run of this repeat in the next 24 hours "
                 f"would be skipped. Why: {why}.")
    report = ((report + "\n\n") if report else "") + extra
    return status, message, report, exit_code, False


def cmd_run_scheduled(args):
    """See _run_scheduled.  This wrapper records how the run ended for `wait`,
    whatever way it ends (a crash is recorded as one)."""
    try:
        _run_scheduled(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        _status_end(code)
        raise
    except BaseException:
        _status_end(None, crashed=True)
        raise


def _run_scheduled(args):
    """Run a scheduled repeat unattended: prepare + entry script + notification.

    This is the single command an unattended agent should call.  It:
      1. Validates the job's tool against this skill's own tool (exit 2 on mismatch).
      2. Calls prepare --job in-process; exits 3 (refused) if the source folder
         is gone, spending nothing.
      3. Runs the entry script with --standing-approval; inherits its own exit
         code (0 done, 3 refused, 4 incomplete).
      4. Reads notification.json; writes a fallback and exits 4 if the entry
         script crashed before writing it.
      5. Prints ONE NOTIFY line then ONE final JSON object, then exits.

    WHY one command for the whole chain:
      If an agent had to chain prepare -> entry script -> read notification,
      every step it decides is a step where it could improvise: resume a stopped
      run, retry, drop the standing-approval flag, edit a config to "fix" a
      refusal.  The reason -- an unattended agent must not be the thing deciding
      whether it may spend money -- applies to the whole chain, not only to the
      spend check inside the entry script.

    WHY prepare is called in-process:
      Avoids spawning a second Python subprocess.  All failure paths still
      produce JSON via _fail; we capture stdout with redirect_stdout to read the
      result.

    WHY sys.executable and no shell:
      Ensures the same Python interpreter, avoids shell injection, and ensures
      --standing-approval is passed verbatim without shell quoting risks.

    WHY no timeout:
      The report-writing step runs a local claude subagent and may take minutes.
      No timeouts on model steps: a slow model step is normal, not a hang.

    WHY a lock around steps 2-5: see _acquire_job_lock.  It is taken
    before prepare, and released in a finally block, so a done, refused,
    incomplete or crashed run all leave no lock behind (a killed process cannot
    run finally; its lock is stale by pid and the next run takes it over).

    WHY --publish git claims before the lock (cloud only): the lock is a file
    on one machine, and each cloud run starts in a fresh machine.  The claim is
    a commit pushed to the research repository: only one of two runs can push
    it, and a run that cannot push could not save its month either, so it stops
    before spending anything.
    """
    job_path, _job_found, _ = _probe_path(args.job)
    if not _job_found:
        _fail(f"--job file not found: {_shown(job_path, args.job)}")

    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        _fail(f"Could not read job file {job_path}: {exc}")

    if not isinstance(job, dict):
        _fail(f"Invalid job file (not a JSON object): {job_path}")

    # ---- Tool mismatch check -----------------------------------------------
    # WHY from __file__, not from a caller argument: each skill ships its own
    # copy.  Running an ad job through the pain copy would run pain_intel.py
    # (wrong entry script, wrong config format).
    _own_tool = _own_skill_tool()
    if _own_tool is None:
        _fail(
            f"run-scheduled: cannot determine tool from skill directory: "
            f"{Path(__file__).resolve().parent.parent.name!r}"
        )

    job_tool = str(job.get("tool", ""))
    if job_tool != _own_tool:
        _fail(
            f"Job is for tool {job_tool!r} but this is the {_own_tool!r} skill. "
            f"Use the {job_tool!r} skill's repeat.py for this job."
        )

    # ---- Status file for `wait` --------------------------------------------
    import time as _time
    _now = _time.time()
    _publish = getattr(args, "publish", None)
    # WHY not while another run of this job is live: the status file then
    # describes that run, and this run's refusal written over it would tell
    # `wait` the run had ended while its entry script is still paying.
    # WHY not for a run that saves to the research repository when its job is
    # reached through a link: such a run refuses at $0 (research_sync.claim
    # never writes through a link), and the status file written next to the
    # resolved job landed where the link points, possibly another copy of
    # the repository, where `wait` there would read this run as its own.
    _RS["status_path"] = (None if (_flock_state(Path(str(job_path) + ".lock")) == "held"
                                   or (_publish == "git" and _given_through_link(args.job)))
                          else _status_path_for(job_path))
    _RS["status_doc"] = {"state": "running", "job": job_path.name, "pid": os.getpid(),
                         "started_epoch": _now,
                         "started": _lock_started_text({"started_epoch": _now}),
                         "publish": _publish}
    _write_status(_RS["status_doc"])

    # ---- Run only in the place this repeat was approved for -----------------
    # WHY: the one-run lock below is a file on one machine, so it cannot stop a
    # run on this computer and a run in Claude's cloud from both paying for the
    # same month.  The approval records one place; any other place refuses
    # here, before the lock and before prepare, so it spends nothing, creates
    # no folder and leaves no lock.  Claude's cloud sets CLAUDE_CODE_REMOTE to
    # "true" and it is never "true" on a user's computer (Claude Code's cloud
    # environment documentation).  No mode means this computer (every
    # approval written before modes existed).  Any other value refuses:
    # a typo must never become "runs everywhere".
    _here = "cloud" if os.environ.get("CLAUDE_CODE_REMOTE") == "true" else "local"
    _job_mode = job["mode"] if "mode" in job else "local"
    _mode_msg = None
    if _job_mode not in ("local", "cloud"):
        _mode_msg = (
            f"This repeat's approval names a place to run that is not recognised "
            f"({_job_mode!r}), so this run was skipped and spent nothing. "
            f"Set up the monthly repeat again."
        )
    elif _job_mode == "cloud" and _here == "local":
        _mode_msg = ("This repeat is set to run in the cloud, so this run on your "
                     "computer was skipped and spent nothing.")
    elif _job_mode == "local" and _here == "cloud":
        _mode_msg = ("This repeat is set to run on your computer, so this run in "
                     "the cloud was skipped and spent nothing.")
    if _mode_msg is not None:
        _finish_run("refused", _mode_msg,
                    ("A monthly repeat runs in one place only, so the same month "
                     "is never paid for twice. Nothing was collected by this run."),
                    None, 3)

    # ---- A cloud month is always saved; only a cloud month is saved ----------
    # WHY: a cloud machine is thrown away after the run, so a cloud month run
    # without saving would be paid for and then lost.  And saving to the
    # repository from this computer is the user's own step (publish), never an
    # unattended one.  Either mismatch refuses before anything else happens.
    if _job_mode == "cloud" and _publish != "git":
        _finish_run(
            "refused",
            ("This repeat is set to run in the cloud, but this run was started without "
             "saving to the research repository, so it was skipped and spent nothing."),
            ("A cloud month that is not saved is lost when the cloud session ends, so a "
             "cloud run only starts with --publish git. Nothing was collected by this run."),
            None, 3)
    if _publish == "git" and _job_mode != "cloud":
        _finish_run(
            "refused",
            ("Saving to the research repository is only for a repeat set to run in the "
             "cloud, and this one is set to run on your computer, so this run was "
             "skipped and spent nothing."),
            "Nothing was collected by this run.", None, 3)

    if _publish == "git":
        job = _claim_cloud_month(job_path, _own_tool, Path(args.job))

    # ---- One run per job at a time ---------------------------------
    _lock = _acquire_job_lock(job_path)
    if _lock["held_by"] is not None:
        _busy_msg = (
            f"This repeat is already running (started "
            f"{_lock_started_text(_lock['held_by'])}). This run was skipped so "
            f"it would not spend twice."
        )
        # The status file belongs to the run that holds the lock (see above).
        _RS["status_path"] = None
        _finish_run(
            "refused", _busy_msg,
            ("Another scheduled run of this repeat was still going, so "
             "this one did not start and spent nothing. Later scheduled "
             "runs go ahead as normal."),
            None, 3)
    # WHY a flock on the lock file, passed to the entry script: `wait` and a
    # later run's staleness check then see this run as alive while ANY of its
    # processes lives (see _flock_state).
    _RS["flock"] = _hold_flock(_lock["path"])
    if _lock["taken_over"] is not None:
        _old = _lock["taken_over"]
        print(f"[lock] An earlier run of this repeat (process "
              f"{_old['info'].get('pid', 'unknown')}, started "
              f"{_lock_started_text(_old['info'])}) left its lock behind: "
              f"{_old['reason']}. The lock was taken over and this run goes ahead.",
              flush=True)
    try:
        _run_scheduled_locked(job_path, job, _own_tool)
    finally:
        _release_job_lock(_lock)


def _claim_cloud_month(job_path, own_tool, given):
    """Claim this month in the research repository before anything is spent
    (research_sync.claim).  Returns the job as saved in the repository.

    given: the job path exactly as the command received it.  WHY not the
    resolved job_path: resolving follows a link on the way (for example a
    folder .repeat_jobs that is a link), and the claim must see that link to
    refuse it; resolved, the place the link points to would silently become
    the research folder."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import research_sync as _rsync
    except ImportError:
        _finish_run("refused",
                    ("The part of this tool that saves to the research repository is "
                     "missing, so this cloud month did not run and spent nothing."),
                    "Install the plugin again, then run the cloud setup again.", None, 3)

    def _check():
        # Read after the update: the saved version is what was approved.
        try:
            saved = json.loads(job_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            saved = None
        if not isinstance(saved, dict):
            return [job_path], ("This repeat's approval could not be read after the "
                                "update, so this run was skipped and spent nothing.")
        if saved.get("mode") != "cloud":
            return [job_path], ("The saved version of this repeat is no longer set to run "
                                "in the cloud, so this run was skipped and spent nothing.")
        if str(saved.get("tool", "")) != own_tool:
            return [job_path], ("The saved version of this repeat is for another research "
                                "tool, so this run was skipped and spent nothing.")
        paths = [job_path]
        folder, why = _resolve_job_run_dir(job_path, saved.get("last_run_dir"))
        if why is None and folder is not None and folder.is_dir():
            newest = _newest_in_chain(folder)
            for cname in _CONFIG_NAMES:
                if (newest / cname).is_file():
                    paths.append(newest / cname)
                    break
        return paths, None

    try:
        ctx = _rsync.claim(given, _check)
    except _rsync.SyncError as exc:
        _finish_run("refused", str(exc),
                    ("A cloud month runs only when it can first prove it will be able to "
                     "save its result. Nothing was collected by this run."), None, 3,
                    troubleshooting=exc.troubleshooting)
    _RS["publish"] = {"mod": _rsync, "ctx": ctx}
    if ctx.get("took_over"):
        print(f"[reservation] An earlier cloud run of this repeat (started "
              f"{ctx['took_over']['started']}) left its reservation of this month behind "
              f"more than 24 hours ago. The reservation was taken over and this run goes "
              f"ahead.", flush=True)
    try:
        return json.loads(job_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        _finish_run("refused", ("This repeat's approval could not be read, so this run "
                                "was skipped and spent nothing."), "", None, 3)


def _run_scheduled_locked(job_path, job, _own_tool):
    """Steps 2-5 of cmd_run_scheduled, run while holding the job's lock."""
    import io as _io
    from contextlib import redirect_stdout as _redir

    # ---- Call prepare --job in-process -------------------------------------
    class _PrepArgs:
        job      = str(job_path)
        from_dir = None
        date     = None
        parent   = None

    _buf = _io.StringIO()
    _prepare_exit = 0
    with _redir(_buf):
        try:
            cmd_prepare(_PrepArgs())
        except SystemExit as _e:
            _prepare_exit = int(_e.code) if _e.code is not None else 2

    if _prepare_exit != 0:
        # Name the folder the approval resolves to (not the raw stored value,
        # which may be a bare folder name), or say why the value was refused.
        _where, _why = _resolve_job_run_dir(job_path, job.get("last_run_dir"))
        try:
            _prep_err = str(json.loads(_buf.getvalue()).get("error", ""))
        except (ValueError, AttributeError):
            _prep_err = ""
        if _why is not None:
            _refused_msg = f"{_why} Nothing was run. Set up the monthly repeat again."
        elif _prep_err.startswith(_SETUP_FAILED):
            # The folder was found; the new month's folder could not be made or
            # set up (read-only folder, full disk, name too long).  The approval
            # is still good, so it is not "set up again".
            _refused_msg = (
                f"{_prep_err} This month's repeat did not run and spent nothing. "
                f"Once this is fixed, the next scheduled run goes ahead as normal."
            )
        else:
            _refused_msg = (
                f"The approved research folder was not found: {_where}. "
                f"Set up the monthly repeat again."
            )
        _finish_run("refused", _refused_msg, "", None, 3)

    try:
        _prep_result = json.loads(_buf.getvalue())
        _new_run_dir = Path(_prep_result["new_run_dir"])
    except (ValueError, KeyError) as exc:
        _refused_msg = (
            f"Prepare step failed unexpectedly: {exc}. "
            f"Set up the monthly repeat again."
        )
        _finish_run("refused", _refused_msg, "", None, 3)

    # ---- Find config in new run dir ----------------------------------------
    _config_path = None
    for _cname in _CONFIG_NAMES:
        _cp = _new_run_dir / _cname
        if _cp.exists():
            _config_path = _cp
            break

    if _config_path is None:
        _err_msg = (
            "Prepared run folder has no config file. "
            "Set up the monthly repeat again."
        )
        _write_fallback_notification(_new_run_dir, _err_msg)
        _finish_run("incomplete", _err_msg, "", str(_new_run_dir), 4)

    # ---- Entry script path -------------------------------------------------
    # WHY derived from this file's location, not a caller argument:
    #   each copy of this file is authoritative for exactly one entry script.
    #   Accepting the path from outside would let an agent substitute a
    #   different script.
    _skill_dir = Path(__file__).resolve().parent.parent
    _ENTRY_SCRIPTS = {
        "pain":   _skill_dir / "pain_intel.py",
        "ad":     _skill_dir / "ad_intel.py",
        "search": _skill_dir / "scripts" / "search_intel.py",
    }
    _entry = _ENTRY_SCRIPTS[_own_tool]

    # ---- Run entry script --------------------------------------------------
    # WHY --standing-approval and nothing else that could widen spending:
    #   The approval already contains the ceiling; additional flags could bypass
    #   spending checks.
    # WHY no --auto-confirm for the ad tool:
    #   prepare copies competitors/resolution.json (via _REUSABLE_ARTIFACTS).
    #   ad_intel.py then sets reused=True, and the confirmation gate evaluates
    #   confirmed = (auto_confirm OR reused OR ...) which is already True.
    #   Adding --auto-confirm would let an unattended run proceed on unreviewed
    #   competitors -- a spending check bypass that is not allowed.
    _cmd = [
        sys.executable, str(_entry),
        "--config",            str(_config_path),
        "--workdir",           str(_new_run_dir),
        "--standing-approval", str(job_path),
    ]
    _kw = {"pass_fds": (_RS["flock"],)} if _RS.get("flock") is not None else {}
    try:
        _result = subprocess.run(_cmd, check=False, **_kw)
        _entry_exit = _result.returncode
    except Exception as _exc:
        _err_msg = "The run stopped unexpectedly. Run it once by hand to check."
        _write_fallback_notification(_new_run_dir, _err_msg)
        _finish_run("incomplete", _err_msg, "", str(_new_run_dir), 4)

    # ---- Read notification.json; write fallback if missing or unreadable ---
    _notif_path = _new_run_dir / "notification.json"
    _notif = None
    if _notif_path.exists():
        try:
            _notif = json.loads(_notif_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass

    if not isinstance(_notif, dict):
        _err_msg = (
            "The run stopped unexpectedly and wrote no result. "
            "Run it once by hand to check."
        )
        _write_fallback_notification(_new_run_dir, _err_msg)
        _notif = {"status": "incomplete", "message": _err_msg, "report": ""}
        _entry_exit = 4

    _status  = str(_notif.get("status",  "incomplete"))
    _message = str(_notif.get("message", ""))
    _report  = str(_notif.get("report",  ""))

    # ---- Print NOTIFY line then final JSON ---------------------------------
    _finish_run(_status, _message, _report, str(_new_run_dir), _entry_exit)


# ---------------------------------------------------------------------------
# wait command
# ---------------------------------------------------------------------------
#
# WHY this shape (taken from the entry scripts' own `run_status.py wait`, and
# reimplemented here because each skill ships standalone and repeat.py must not
# depend on a file it does not own): each call waits a short while and answers
# in plain words, and the agent calls it again.  A short call is not a time
# limit on the run; the run is never stopped.
# WHY 90 seconds per call and not 5 minutes: the agent's command tool stops a
# command after two minutes unless told otherwise, and a wait that is itself
# stopped tells the agent nothing.
# WHY a settle of 5 seconds before reporting an ended run: a run started in the
# background a moment ago may not have written its status yet, and the file
# still holds last month's end.  A run writes its status within a fraction of a
# second of starting; 5 seconds leaves a wide margin.

_WAIT_PER_CALL = 90
_WAIT_POLL = 2
_WAIT_SETTLE = 5
_WAIT_EARLIER = 60


def _run_alive(job_path, doc):
    """True while any process of the run recorded in doc may still be alive."""
    import time as _time
    if _flock_state(Path(str(job_path) + ".lock")) == "held":
        return True
    started = doc.get("started_epoch")
    if (isinstance(started, (int, float)) and not isinstance(started, bool)
            and _time.time() - float(started) > _LOCK_STALE_SECONDS):
        return False  # the same 24 h rule as the lock: covers a reused pid
    return _pid_alive(doc.get("pid"))


def _mins(seconds):
    s = max(0, int(seconds))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _earlier_note(doc, t0):
    ended = doc.get("finished_epoch")
    if not isinstance(ended, (int, float)) or t0 - float(ended) < _WAIT_EARLIER:
        return []
    return [f"That run had already ended {_mins(t0 - float(ended))} before this check "
            f"began. If a run of this repeat was started after that, it has not started: "
            f"read that command's own output to see why."]


def cmd_wait(args):
    import time as _time
    job_path, _, _ = _probe_path(args.job)
    if job_path is None:
        _fail(f"--job cannot be used as a file: {args.job!r}")
    status_path = _status_path_for(job_path)
    name = job_path.name
    # WHY before reading any status: a cloud run whose job is reached through
    # a link keeps no status (see _run_scheduled), and a status file found
    # where the link points belongs to another place, maybe another copy of
    # the research, so reading it would report a run that is not this one.
    _job = _read_json(job_path)
    if (isinstance(_job, dict) and _job.get("mode") == "cloud"
            and _given_through_link(args.job)):
        print(f"NO STATUS KEPT: the path given for {name} goes through a link to another "
              f"place on this computer (the approval itself, or .repeat_jobs, the folder "
              f"where the approvals of your monthly repeats are kept, is a link). A cloud "
              f"run never writes through a link: it stops at once, spends nothing and "
              f"keeps no status for this command. Read that run's own output: it says why "
              f"it stopped.")
        return 2
    t0 = _time.time()
    seen_live = False
    doc = None
    while True:
        doc = _read_json(status_path)
        if not isinstance(doc, dict):
            doc = None
        settled = seen_live or _time.time() - t0 >= _WAIT_SETTLE
        state = doc.get("state") if doc else None
        if state == "running":
            if _run_alive(job_path, doc):
                seen_live = True
            elif settled:
                again = _read_json(status_path)
                if (isinstance(again, dict) and again.get("state") == "running"
                        and again.get("started_epoch") == doc.get("started_epoch")):
                    print("\n".join([
                        f"DIED: the scheduled run of {name} (started "
                        f"{doc.get('started', '?')}) stopped without printing its result: "
                        f"its process is gone. It was killed, it crashed, or the machine "
                        f"it ran on stopped.",
                        "Tell the user this in plain words. A paid step that was running "
                        "may still have been charged: the exact amount is in the user's "
                        "Apify console. If it was a cloud run, what it collected may not "
                        "have been saved to the research repository.",
                        "Do not start it again without the user's go-ahead."]))
                    return 1
                continue
        elif state in ("finished", "failed") and settled:
            lines = [f"FINISHED: the scheduled run of {name} has ended (started "
                     f"{doc.get('started', '?')}, ended {doc.get('finished', '?')}, exit "
                     f"code {doc.get('exit_code')})."]
            lines += _earlier_note(doc, t0)
            if doc.get("final"):
                lines.append("Its result, exactly as the run printed it. Quote the message "
                             "and the report as they are; do not reword them. A "
                             "\"troubleshooting\" part, when there is one, is for "
                             "troubleshooting only: do not quote it or read it out.")
                lines.append("-----")
                if doc.get("notify"):
                    lines.append(doc["notify"])
                lines.append(doc["final"])
                lines.append("-----")
                print("\n".join(lines))
                return 0
            lines[0] = (f"ENDED WITHOUT A RESULT: the scheduled run of {name} (started "
                        f"{doc.get('started', '?')}) ended without printing its result"
                        + (" because of an error." if state == "failed" else "."))
            lines.append("Tell the user this in plain words, and do not start it again "
                         "without their go-ahead.")
            print("\n".join(lines))
            return 1
        elif doc is None and _time.time() - t0 >= _WAIT_SETTLE:
            print(f"NO RUN FOUND: nothing says a scheduled run of {name} has started. If "
                  f"one was started in the background, read that command's own output.")
            return 2
        if _time.time() - t0 >= _WAIT_PER_CALL:
            break
        _time.sleep(_WAIT_POLL if settled else 0.1)
    started = (doc or {}).get("started_epoch") or t0
    print(f"STILL RUNNING: the scheduled run of {name} started "
          f"{(doc or {}).get('started', '?')} and is still working "
          f"({_mins(_time.time() - float(started))} so far). Call this same command again "
          f"now to keep waiting. Do not end your turn while the run is going, and do not "
          f"stop it because it is slow: slow is normal.")
    return 0


# ---------------------------------------------------------------------------
# forecast command
# ---------------------------------------------------------------------------

def cmd_forecast(args):
    """Compute the cost forecast for the next repeat of a completed run.

    Prepares the next run into a temp folder (never touching the user's folder),
    runs the entry script with --forecast-only, prints the forecast as JSON, and
    removes the temp folder.

    WHY --forecast-only instead of --dry-run:
      --dry-run only shows some stages' costs.  --forecast-only calls the exact
      same _*_forecast_usd function the standing-approval gate calls, so the
      number shown here IS the number the gate will check.  A ceiling set from
      this output will not cause unexpected refusals.

    WHY prepare into a temp dir:
      The gate runs on the prepared (next) run folder, not the reference folder.
      To match its context exactly, we prepare an identical temp run and call
      --forecast-only on that.  The user's folders are never touched.
    """
    import io as _io
    import shutil as _shutil
    import tempfile as _tempfile
    from contextlib import redirect_stdout as _redir

    from_dir, _, _from_is_dir = _probe_path(args.from_dir)
    if not _from_is_dir:
        _fail(f"--from is not a folder: {_shown(from_dir, args.from_dir)}")

    # Tool detection (same as cmd_schedule)
    indexed = (from_dir / "frontend_index.json").exists()
    index = _read_json(from_dir / "frontend_index.json", {}) if indexed else {}
    tool = _tool_of(from_dir.name, index if indexed else {})
    if tool not in ("pain", "ad", "search"):
        _fail(f"Cannot determine tool from folder name: {from_dir.name!r}")

    # Tool match: each copy is authoritative for exactly one entry script.
    _own_tool = _own_skill_tool()
    if _own_tool is None:
        _fail(
            f"forecast: cannot determine tool from skill directory: "
            f"{Path(__file__).resolve().parent.parent.name!r}"
        )
    if tool != _own_tool:
        _fail(
            f"Run is for tool {tool!r} but this is the {_own_tool!r} skill. "
            f"Use the {tool!r} skill's repeat.py to forecast this run."
        )

    # Subject
    config_subject = _read_config_subject(from_dir)
    subject = _corrected_subject(from_dir.name, index if indexed else {}, config_subject)

    # Prepare a temp next-run dir
    _tmp_parent = Path(_tempfile.mkdtemp(prefix="repeat_forecast_"))
    try:
        # WHY _fc_from and _fc_parent: class bodies do not close over enclosing
        # locals when the same name is assigned in the class body.
        # Using distinct names avoids the shadowing.
        _fc_from   = str(from_dir)
        _fc_parent = str(_tmp_parent)

        class _PrepArgs:
            from_dir = _fc_from
            job      = None
            date     = None
            parent   = _fc_parent

        _buf = _io.StringIO()
        _prepare_exit = 0
        with _redir(_buf):
            try:
                cmd_prepare(_PrepArgs())
            except SystemExit as _e:
                _prepare_exit = int(_e.code) if _e.code is not None else 2

        if _prepare_exit != 0:
            _fail(
                f"forecast: prepare failed (exit {_prepare_exit}): "
                f"{_buf.getvalue()[:200]}"
            )

        try:
            _prep_result = json.loads(_buf.getvalue())
            _new_run_dir = Path(_prep_result["new_run_dir"])
        except (ValueError, KeyError) as exc:
            _fail(
                f"forecast: could not parse prepare output: {exc}: "
                f"{_buf.getvalue()[:200]}"
            )

        # Find config in prepared run
        _config_path = None
        for _cname in _CONFIG_NAMES:
            _cp = _new_run_dir / _cname
            if _cp.exists():
                _config_path = _cp
                break
        if _config_path is None:
            _fail("forecast: prepared run folder has no config file")

        # Entry script (same derivation as cmd_run_scheduled)
        _skill_dir = Path(__file__).resolve().parent.parent
        _ENTRY_SCRIPTS = {
            "pain":   _skill_dir / "pain_intel.py",
            "ad":     _skill_dir / "ad_intel.py",
            "search": _skill_dir / "scripts" / "search_intel.py",
        }
        _entry = _ENTRY_SCRIPTS[_own_tool]

        _cmd = [
            sys.executable, str(_entry),
            "--config",         str(_config_path),
            "--workdir",        str(_new_run_dir),
            "--forecast-only",
        ]
        try:
            _res = subprocess.run(_cmd, capture_output=True, text=True, check=False)
        except Exception as exc:
            _fail(f"forecast: entry script failed to start: {exc}")

        # Parse {"forecast_usd": <float>} from stdout (single JSON line)
        _forecast_usd = None
        for _line in _res.stdout.splitlines():
            _line = _line.strip()
            if _line.startswith("{"):
                try:
                    _parsed = json.loads(_line)
                    if "forecast_usd" in _parsed:
                        _forecast_usd = float(_parsed["forecast_usd"])
                        break
                except (ValueError, TypeError):
                    pass
        if _forecast_usd is None:
            _fail(
                f"forecast: entry script (exit {_res.returncode}) did not print "
                f"a forecast_usd JSON line. stdout: {_res.stdout[:300]!r}"
            )

    finally:
        _shutil.rmtree(_tmp_parent, ignore_errors=True)

    _out({"forecast_usd": _forecast_usd, "tool": tool, "subject": subject})
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main(argv):
    if not argv:
        return _run_self_tests()

    ap = _SilentParser(
        prog="repeat.py",
        description="Manage repeat runs (list / prepare / schedule / fingerprint).",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="List run groups under --parent")
    p_list.add_argument("--parent", required=True)
    p_list.add_argument("--tool", choices=("pain", "ad", "search"), default=None)

    p_prep = sub.add_parser("prepare", help="Create a repeat run from a previous one")
    # --from and --job are mutually exclusive; exactly one is required.
    # cmd_prepare enforces this because argparse mutually exclusive groups
    # cannot mark both as optional.
    p_prep.add_argument("--from", dest="from_dir", default=None,
                        metavar="PREV_RUN_DIR",
                        help="Previous run folder (use --job for job-file mode)")
    p_prep.add_argument("--job", default=None,
                        help="Job file written by `schedule`; resolves --from automatically")
    p_prep.add_argument("--date", default=None,
                        help="YYYY-MM-DD date for the new run (default: today)")
    p_prep.add_argument("--parent", default=None,
                        help="Parent folder (default: previous run's parent)")

    p_sched = sub.add_parser("schedule",
                              help="Write a job file approving repeated unattended runs")
    p_sched.add_argument("--from", dest="from_dir", required=True,
                         metavar="RUN_DIR",
                         help="Reference run to fingerprint and approve")
    p_sched.add_argument("--ceiling", required=True,
                         help="Maximum USD spend per run (e.g. 5.0)")
    p_sched.add_argument("--cadence", default="monthly",
                         help="Schedule cadence hint (default: monthly)")
    p_sched.add_argument("--mode", choices=("local", "cloud"), default="local",
                         help="Where the repeat is approved to run: local (this "
                              "computer, default) or cloud (Claude's cloud)")

    p_fpr = sub.add_parser("fingerprint",
                            help="SHA-256 fingerprint of a run's config")
    p_fpr.add_argument("--run", required=True)

    p_fcast = sub.add_parser(
        "forecast",
        help="Forecast the cost of the next repeat without spending anything",
    )
    p_fcast.add_argument("--from", dest="from_dir", required=True,
                         metavar="RUN_DIR",
                         help="Completed run folder to forecast from")

    p_rs = sub.add_parser(
        "run-scheduled",
        help="Run a scheduled repeat unattended: prepare + entry script + notification",
    )
    p_rs.add_argument("--job", required=True,
                      help="Job file written by `schedule`")
    p_rs.add_argument("--publish", choices=("git",), default=None,
                      help="Cloud runs only: reserve the month in the research repository "
                           "first and save the new month to it afterwards")

    p_wait = sub.add_parser(
        "wait",
        help="Wait a short while for a run-scheduled of this job to end (call again "
             "until it has ended)",
    )
    p_wait.add_argument("--job", required=True, help="Job file written by `schedule`")

    a = ap.parse_args(argv)
    if a.cmd == "list":
        return cmd_list(a)
    elif a.cmd == "prepare":
        return cmd_prepare(a)
    elif a.cmd == "schedule":
        return cmd_schedule(a)
    elif a.cmd == "fingerprint":
        return cmd_fingerprint(a)
    elif a.cmd == "forecast":
        return cmd_forecast(a)
    elif a.cmd == "run-scheduled":
        return cmd_run_scheduled(a)
    elif a.cmd == "wait":
        return cmd_wait(a)
    else:
        ap.print_help()
        return 1


# ---------------------------------------------------------------------------
# Self-tests (python3 repeat.py -- no arguments)
# ---------------------------------------------------------------------------

def _run_self_tests():
    PASS = FAIL = SKIP = 0
    # WHY: a few cases make a folder read-only or a file unreadable with chmod,
    # so that a real refusal happens.  Root ignores those permissions (cloud
    # machines often run as root): there the refusal cannot happen and the case
    # would fail although the product is fine.  As root, exactly those cases
    # are skipped with a visible line and counted, never passed or failed.
    _as_root = hasattr(os, "geteuid") and os.geteuid() == 0

    def skip_as_root(label):
        nonlocal SKIP
        print(f"  SKIP  {label}: running as root, file permissions are not enforced")
        SKIP += 1

    def check(label, got, expected):
        nonlocal PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")
            FAIL += 1

    # ---- _parse_run_folder --------------------------------------------------
    print("\n-- _parse_run_folder --")
    check("normal run",          _parse_run_folder("pain_acme-fitness_2026-09-28"),           ("2026-09-28", 1))
    check("_v2 suffix",          _parse_run_folder("pain_acme-fitness_2026-09-28_v2"),        ("2026-09-28", 2))
    check("_v12 suffix",         _parse_run_folder("pain_acme-fitness_2026-09-28_v12"),       ("2026-09-28", 12))
    check("slug with underscores", _parse_run_folder("pain_acme-fitness_multi_2026-08-28"),   ("2026-08-28", 1))
    check("adintel prefix",      _parse_run_folder("adintel_hubspot_2026-09-28"),        ("2026-09-28", 1))
    check("search prefix",       _parse_run_folder("search_pm-saas_2026-09-19"),         ("2026-09-19", 1))
    check("no date -> (None,1)", _parse_run_folder("pain_acme-fitness_nodatehereXYZ"),        (None, 1))
    check("unknown prefix",      _parse_run_folder("custom_run_2026-09-28"),             (None, 1))

    # ---- _subject_from_dirname ----------------------------------------------
    print("\n-- _subject_from_dirname --")
    check("normal pain",         _subject_from_dirname("pain_acme-fitness_2026-09-28"),       "Acme Fitness")
    check("_v2 stripped",        _subject_from_dirname("pain_acme-fitness_2026-09-28_v2"),    "Acme Fitness")
    check("_v12 stripped",       _subject_from_dirname("pain_acme-fitness_2026-09-28_v12"),   "Acme Fitness")
    check("slug with underscores", _subject_from_dirname("pain_acme-fitness_multi_2026-08-28"), "Acme Fitness Multi")
    check("adintel prefix",      _subject_from_dirname("adintel_hubspot_2026-09-28"),    "Hubspot")
    check("search prefix",       _subject_from_dirname("search_pm-saas_2026-09-19"),     "Pm Saas")
    check("no date -> as-is",    _subject_from_dirname("pain_acme-fitness_nodatehereXYZ"),    "pain_acme-fitness_nodatehereXYZ")
    # WHY these tests: subject grouping drives the "same project" detection.
    # If _v2 returns a different subject than the base run, repeat.py list would
    # show them as separate projects and the user would never be offered a repeat.
    check("same subject v1 and v2",
          _subject_from_dirname("adintel_acme-plumbing_2026-08-27"),
          _subject_from_dirname("adintel_acme-plumbing_2026-08-27_v2"))

    # ---- _corrected_subject -------------------------------------------------
    print("\n-- _corrected_subject --")
    buggy = {"subject": "Acme Fitness 2026 09 28"}
    check("buggy _v2 subject corrected",
          _corrected_subject("pain_acme-fitness_2026-09-28_v2", buggy), "Acme Fitness")
    check("user-set config prevents correction",
          _corrected_subject("pain_acme-fitness_2026-09-28_v2", buggy, "My Brand"), "Acme Fitness 2026 09 28")
    check("no stored -> config subject",
          _corrected_subject("pain_acme-fitness_2026-09-28_v2", {}, "My Brand"), "My Brand")
    check("no stored, no config -> folder name",
          _corrected_subject("pain_acme-fitness_2026-09-28_v2", {}, None), "Acme Fitness")
    check("base run not corrected",
          _corrected_subject("pain_acme-fitness_2026-09-28", {"subject": "X"}, None), "X")

    # ---- _run_sort_key ------------------------------------------------------
    print("\n-- _run_sort_key --")
    r1 = {"run_date": "2026-08-28", "run_version": 1, "generated_at": "", "run_id": "a"}
    r2 = {"run_date": "2026-09-28", "run_version": 1, "generated_at": "", "run_id": "a"}
    r3 = {"run_date": "2026-09-28", "run_version": 2, "generated_at": "", "run_id": "a"}
    rn = {"run_date": None,          "run_version": 1, "generated_at": "2026-07-01T00:00:00Z", "run_id": "a"}
    check("earlier date < later date",  _run_sort_key(r1) < _run_sort_key(r2), True)
    check("same date v1 < v2",          _run_sort_key(r2) < _run_sort_key(r3), True)
    check("null date sorts first",      _run_sort_key(rn) < _run_sort_key(r1), True)
    check("run_id is 4th element",      len(_run_sort_key(r1)), 4)
    # WHY: sort order drives which run is "newest" for repeat offers.
    # A broken sort here would show the user an older run as the repeat source.

    # ---- list command -------------------------------------------------------
    print("\n-- list command (filesystem) --")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Two pain runs for Acme Fitness + one ad run
        for name in ("pain_acme-fitness_2026-08-28", "pain_acme-fitness_2026-09-28",
                     "adintel_acme-plumbing_2026-08-27"):
            d = root / name
            d.mkdir()
            (d / "PAIN_CONFIG.json" if name.startswith("pain") else d / "AD_INTEL_CONFIG.json").touch()

        # WHY: list must group by (tool, subject) and order newest-first.
        # If a repeat run appears as a new project, the agent cannot offer to
        # continue from the previous month.
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()

        class FakeArgs:
            parent = str(root)
            tool = None

        with redirect_stdout(buf):
            cmd_list(FakeArgs())
        groups = json.loads(buf.getvalue())

        # Should have 2 groups: (ad, Acme Plumbing) and (pain, Acme Fitness)
        check("list: 2 groups", len(groups), 2)
        pain_grp = next(g for g in groups if g["tool"] == "pain")
        ad_grp   = next(g for g in groups if g["tool"] == "ad")
        check("list: pain group subject", pain_grp["subject"], "Acme Fitness")
        check("list: pain group has 2 runs", len(pain_grp["runs"]), 2)
        # Newest first: 2026-09-28 before 2026-08-28
        check("list: newest run first", pain_grp["runs"][0]["run_date"], "2026-09-28")
        check("list: oldest run second", pain_grp["runs"][1]["run_date"], "2026-08-28")
        check("list: ad group has 1 run", len(ad_grp["runs"]), 1)

        # Tool filter
        buf2 = io.StringIO()

        class FakeArgsPain:
            parent = str(root)
            tool = "pain"

        with redirect_stdout(buf2):
            cmd_list(FakeArgsPain())
        groups2 = json.loads(buf2.getvalue())
        check("list: --tool pain shows only pain groups", len(groups2), 1)
        check("list: filtered group is pain", groups2[0]["tool"], "pain")

    # ---- prepare command ----------------------------------------------------
    print("\n-- prepare command (filesystem) --")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prev = root / "pain_acme-fitness_2026-08-28"
        prev.mkdir()
        cfg_data = {"country_code": "us", "sources": ["maps", "trustpilot"],
                    "competitor_names": ["Acme Fitness"]}
        (prev / "PAIN_CONFIG.json").write_text(
            json.dumps(cfg_data), encoding="utf-8")

        class Prep:
            from_dir = str(prev)
            date = "2026-09-28"
            parent = None

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_prepare(Prep())
        result = json.loads(buf.getvalue())

        new_dir = Path(result["new_run_dir"])
        # WHY: the new folder must exist with the date in the name
        check("prepare: new folder created", new_dir.exists(), True)
        check("prepare: new folder name", new_dir.name, "pain_acme-fitness_2026-09-28")
        check("prepare: repeat_of set", result["repeat_of"], "pain_acme-fitness_2026-08-28")
        check("prepare: subject set", result["subject"], "Acme Fitness")
        # Read the new config
        new_cfg = json.loads((new_dir / "PAIN_CONFIG.json").read_text(encoding="utf-8"))
        check("prepare: config has repeat_of", new_cfg.get("repeat_of"), "pain_acme-fitness_2026-08-28")
        check("prepare: config has subject", new_cfg.get("subject"), "Acme Fitness")
        check("prepare: original keys preserved", new_cfg.get("country_code"), "us")

        # _vN bump: same date again -> _v2
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cmd_prepare(Prep())
        result2 = json.loads(buf2.getvalue())
        check("prepare: _v2 on collision", Path(result2["new_run_dir"]).name, "pain_acme-fitness_2026-09-28_v2")
        # WHY: two same-day repeats must not collide; the skill already documents _vN.

        # _v3 on double collision
        buf3 = io.StringIO()
        with redirect_stdout(buf3):
            cmd_prepare(Prep())
        result3 = json.loads(buf3.getvalue())
        check("prepare: _v3 on double collision", Path(result3["new_run_dir"]).name, "pain_acme-fitness_2026-09-28_v3")

        # Original folder was NOT touched
        check("prepare: original folder untouched",
              json.loads((prev / "PAIN_CONFIG.json").read_text(encoding="utf-8")),
              cfg_data)

        # Resolution artifact copy: add competitors/resolution.json to prev
        comp_dir = prev / "competitors"
        comp_dir.mkdir()
        res_data = [{"input": "acme-plumbing.com", "domain": {"use": "acme-plumbing.com"}}]
        (comp_dir / "resolution.json").write_text(json.dumps(res_data), encoding="utf-8")
        (comp_dir / "domains.json").write_text(json.dumps(["acme-plumbing.com"]), encoding="utf-8")

        class Prep2:
            from_dir = str(prev)
            date = "2026-10-28"
            parent = None

        buf4 = io.StringIO()
        with redirect_stdout(buf4):
            cmd_prepare(Prep2())
        result4 = json.loads(buf4.getvalue())
        new_dir4 = Path(result4["new_run_dir"])
        check("prepare: resolution.json copied when present",
              "competitors/resolution.json" in result4["copied_artifacts"], True)
        check("prepare: domains.json copied when present",
              "competitors/domains.json" in result4["copied_artifacts"], True)
        check("prepare: copied file exists in new folder",
              (new_dir4 / "competitors" / "resolution.json").exists(), True)
        # WHY: copying resolution saves the B1 SERP stage cost on the repeat run
        # (ad_intel.py skips B1 when competitors/resolution.json exists and --force
        # is not passed).

    # ---- prepare refusal: no config -----------------------------------------
    print("\n-- prepare: refusal without config --")
    with tempfile.TemporaryDirectory() as td:
        no_cfg_dir = Path(td) / "pain_acme-fitness_2026-08-28"
        no_cfg_dir.mkdir()
        (no_cfg_dir / "frontend_index.json").write_text("{}", encoding="utf-8")

        class PrepNoConfig:
            from_dir = str(no_cfg_dir)
            date = "2026-09-28"
            parent = None

        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                cmd_prepare(PrepNoConfig())
            except SystemExit:
                pass
        err = json.loads(buf.getvalue())
        check("prepare: refuses without config (error key)", "error" in err, True)
        check("prepare: refusal mentions no saved settings",
              "no saved settings" in err["error"], True)
        # WHY: a repeat must be like for like; without the config the new
        # run's settings would differ from the previous one silently.

    # ---- fingerprint command ------------------------------------------------
    print("\n-- fingerprint command (filesystem) --")
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td) / "pain_acme-fitness_2026-09-28"
        run_dir.mkdir()
        cfg = {
            "country_code": "us",
            "sources": ["maps", "trustpilot"],
            "budget_caps": {"phase1": 2.0},
            "subject": "Acme Fitness",
            "repeat_of": "pain_acme-fitness_2026-08-28",
            "_note": "internal",
            "resolution_confirmed": True,
        }
        (run_dir / "PAIN_CONFIG.json").write_text(json.dumps(cfg), encoding="utf-8")

        class FPArgs:
            run = str(run_dir)

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_fingerprint(FPArgs())
        fp1 = json.loads(buf.getvalue())
        check("fingerprint: has fingerprint key", "fingerprint" in fp1, True)
        check("fingerprint: excluded subject", "subject" in fp1["excluded_keys"], True)
        check("fingerprint: excluded repeat_of", "repeat_of" in fp1["excluded_keys"], True)
        check("fingerprint: excluded budget_caps", "budget_caps" in fp1["excluded_keys"], True)
        check("fingerprint: excluded resolution_confirmed", "resolution_confirmed" in fp1["excluded_keys"], True)
        check("fingerprint: excluded _note (underscore)", "_note" in fp1["excluded_keys"], True)
        check("fingerprint: included country_code", "country_code" in fp1["included_keys"], True)
        check("fingerprint: included sources", "sources" in fp1["included_keys"], True)

        # Stable across key-order and whitespace variance
        cfg_reordered = {"sources": ["maps", "trustpilot"], "country_code": "us",
                         "budget_caps": {"phase1": 2.0}, "subject": "Acme Fitness",
                         "repeat_of": "pain_acme-fitness_2026-08-28",
                         "_note": "internal", "resolution_confirmed": True}
        (run_dir / "PAIN_CONFIG.json").write_text(
            json.dumps(cfg_reordered, indent=4), encoding="utf-8")
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cmd_fingerprint(FPArgs())
        fp2 = json.loads(buf2.getvalue())
        # WHY: fingerprints must be stable across cosmetic changes so scheduled
        # repeats do not falsely flag an unchanged config as changed.
        check("fingerprint: stable across key order", fp1["fingerprint"], fp2["fingerprint"])
        check("fingerprint: stable across whitespace", fp1["fingerprint"], fp2["fingerprint"])

        # Excluded-only change does not alter fingerprint
        cfg3 = dict(cfg)
        cfg3["repeat_of"] = "different_run"
        cfg3["subject"] = "Other Brand"
        (run_dir / "PAIN_CONFIG.json").write_text(json.dumps(cfg3), encoding="utf-8")
        buf3 = io.StringIO()
        with redirect_stdout(buf3):
            cmd_fingerprint(FPArgs())
        fp3 = json.loads(buf3.getvalue())
        check("fingerprint: excluded key change does not alter hash",
              fp1["fingerprint"], fp3["fingerprint"])

        # Included key change alters fingerprint
        cfg4 = dict(cfg)
        cfg4["sources"] = ["maps"]   # removed trustpilot
        (run_dir / "PAIN_CONFIG.json").write_text(json.dumps(cfg4), encoding="utf-8")
        buf4 = io.StringIO()
        with redirect_stdout(buf4):
            cmd_fingerprint(FPArgs())
        fp4 = json.loads(buf4.getvalue())
        # WHY: a source change means the corpora differ; a scheduled repeat with
        # a changed config must stop and ask the user.
        check("fingerprint: included key change alters hash",
              fp1["fingerprint"] != fp4["fingerprint"], True)

    # ---- Date validation ------------------------------------------------
    print("\n-- prepare: date validation --")
    with tempfile.TemporaryDirectory() as td:
        prev_d = Path(td) / "pain_acme-fitness_2026-08-28"
        prev_d.mkdir()
        (prev_d / "PAIN_CONFIG.json").write_text('{"x": 1}', encoding="utf-8")

        # Impossible date: valid YYYY-MM-DD pattern but not a real date
        class _PrepImpossible:
            from_dir = str(prev_d)
            date = "2026-13-40"
            parent = None

        buf_imp = io.StringIO()
        with redirect_stdout(buf_imp):
            try:
                cmd_prepare(_PrepImpossible())
            except SystemExit:
                pass
        err_imp = json.loads(buf_imp.getvalue())
        check("prepare: impossible date gives JSON error", "error" in err_imp, True)
        check("prepare: impossible date -> no folder created",
              not (prev_d.parent / "pain_acme-fitness_2026-13-40").exists(), True)

        # Empty string: also invalid
        class _PrepEmpty:
            from_dir = str(prev_d)
            date = ""
            parent = None

        buf_emp = io.StringIO()
        with redirect_stdout(buf_emp):
            try:
                cmd_prepare(_PrepEmpty())
            except SystemExit:
                pass
        err_emp = json.loads(buf_emp.getvalue())
        check("prepare: empty date string gives JSON error", "error" in err_emp, True)

    # ---- Argparse errors through _fail ----------------------------------
    print("\n-- argparse error routing --")

    # 'list' with no --parent: argparse error (required arg missing)
    buf_f4a = io.StringIO()
    with redirect_stdout(buf_f4a):
        try:
            _main(["list"])
        except SystemExit:
            pass
    err_f4a = json.loads(buf_f4a.getvalue())
    check("argparse: list --parent missing -> JSON error key", "error" in err_f4a, True)

    # 'list' with invalid --tool value
    buf_f4b = io.StringIO()
    with redirect_stdout(buf_f4b):
        try:
            _main(["list", "--parent", "/tmp", "--tool", "invalid_choice"])
        except SystemExit:
            pass
    err_f4b = json.loads(buf_f4b.getvalue())
    check("argparse: bad --tool value -> JSON error key", "error" in err_f4b, True)

    # 'fingerprint' with no --run: argparse error
    buf_f4c = io.StringIO()
    with redirect_stdout(buf_f4c):
        try:
            _main(["fingerprint"])
        except SystemExit:
            pass
    err_f4c = json.loads(buf_f4c.getvalue())
    check("argparse: fingerprint --run missing -> JSON error key", "error" in err_f4c, True)

    # ---- has_config false for non-object config -------------------------
    print("\n-- has_config: non-object config --")
    with tempfile.TemporaryDirectory() as td:
        root_f5 = Path(td)
        # Object config: has_config True
        d_obj = root_f5 / "pain_acme-fitness_2026-09-01"
        d_obj.mkdir()
        (d_obj / "PAIN_CONFIG.json").write_text('{"x": 1}', encoding="utf-8")

        # List config: has_config False
        d_lst = root_f5 / "pain_acme-fitness_2026-09-02"
        d_lst.mkdir()
        (d_lst / "PAIN_CONFIG.json").write_text('[1, 2, 3]', encoding="utf-8")

        check("_has_repeatable_config: object config -> True",
              _has_repeatable_config(d_obj), True)
        check("_has_repeatable_config: list config -> False",
              _has_repeatable_config(d_lst), False)
        check("_has_repeatable_config: no config -> False",
              _has_repeatable_config(root_f5), False)

        # Verify the list command reports has_config correctly
        buf_f5 = io.StringIO()

        class _FakeArgsAll:
            parent = str(root_f5)
            tool = None

        with redirect_stdout(buf_f5):
            cmd_list(_FakeArgsAll())
        groups_f5 = json.loads(buf_f5.getvalue())
        all_runs_f5 = {r["run_id"]: r for g in groups_f5 for r in g["runs"]}
        # WHY: an agent that trusts has_config to decide whether to offer a
        # repeat will confuse the user if has_config=True for a run that
        # prepare would actually refuse.
        check("list: object config run has_config=True",
              all_runs_f5.get("pain_acme-fitness_2026-09-01", {}).get("has_config"), True)
        check("list: list-config run has_config=False",
              all_runs_f5.get("pain_acme-fitness_2026-09-02", {}).get("has_config"), False)

    # ---- [unit] run-scheduled: tool mismatch refuses with exit 2 ------------
    print("\n-- [unit] run-scheduled: tool mismatch --")
    with tempfile.TemporaryDirectory() as td:
        # Determine this copy's own tool, then pick a different one for the job.
        _sk_name = Path(__file__).resolve().parent.parent.name
        _own = _SKILL_TO_TOOL.get(_sk_name, "pain")
        _other = "ad" if _own != "ad" else "search"

        _mm_job = Path(td) / "mismatch.json"
        _mm_job.write_text(json.dumps({
            "tool": _other, "subject": "Test", "subject_slug": "test",
            "ceiling": 5.0, "cadence": "monthly",
            "fingerprint": "abc123", "last_run_dir": td,
        }), encoding="utf-8")

        _buf_mm = io.StringIO()
        _mm_exit = 0
        with redirect_stdout(_buf_mm):
            try:
                _main(["run-scheduled", "--job", str(_mm_job)])
            except SystemExit as _e:
                _mm_exit = int(_e.code) if _e.code is not None else -1
        _err_mm = json.loads(_buf_mm.getvalue())
        check("[unit] run-scheduled: tool mismatch exits 2", _mm_exit, 2)
        check("[unit] run-scheduled: tool mismatch gives JSON error", "error" in _err_mm, True)

    # ---- [unit] schedule: replaced: true and previous_ceiling ---------------
    print("\n-- [unit] schedule: replaced: true --")
    with tempfile.TemporaryDirectory() as td:
        _sched_root = Path(td)
        _sched_run = _sched_root / "pain_acme-fitness_2026-08-28"
        _sched_run.mkdir()
        _sched_cfg = {"country_code": "us", "sources": ["maps"],
                      "competitor_names": ["Acme Fitness"]}
        (_sched_run / "PAIN_CONFIG.json").write_text(
            json.dumps(_sched_cfg), encoding="utf-8")

        class _SA1:
            from_dir = str(_sched_run)
            ceiling  = "3.0"
            cadence  = "monthly"

        _buf_s1 = io.StringIO()
        with redirect_stdout(_buf_s1):
            cmd_schedule(_SA1())
        _s1 = json.loads(_buf_s1.getvalue())
        check("[unit] schedule 1st: no replaced key", _s1.get("replaced"), None)
        check("[unit] schedule 1st: ceiling", _s1.get("ceiling"), 3.0)

        class _SA2:
            from_dir = str(_sched_run)
            ceiling  = "7.5"
            cadence  = "monthly"

        _buf_s2 = io.StringIO()
        with redirect_stdout(_buf_s2):
            cmd_schedule(_SA2())
        _s2 = json.loads(_buf_s2.getvalue())
        check("[unit] schedule 2nd: replaced = True", _s2.get("replaced"), True)
        check("[unit] schedule 2nd: previous_ceiling = 3.0",
              _s2.get("previous_ceiling"), 3.0)
        check("[unit] schedule 2nd: new ceiling = 7.5", _s2.get("ceiling"), 7.5)

    # ---- [unit] run-scheduled subprocess-based tests -------------------------
    # These tests create a temporary skill layout so __file__ in the subprocess
    # resolves to a known skill directory name.  A stub entry script replaces the
    # real entry script, keeping the tests fast and free of Apify calls.
    print("\n-- [unit] run-scheduled subprocess tests --")
    with tempfile.TemporaryDirectory() as td:
        _tmp = Path(td)
        # Build a minimal pain-and-messaging layout:
        # .../pain-and-messaging/scripts/repeat.py   (copy of this file)
        # .../pain-and-messaging/pain_intel.py       (stub)
        _skill_tmp  = _tmp / "pain-and-messaging"
        _scripts_tmp = _skill_tmp / "scripts"
        _scripts_tmp.mkdir(parents=True)
        _repeat_tmp = _scripts_tmp / "repeat.py"
        shutil.copy2(__file__, _repeat_tmp)

        # ---- test 1: job file bytes unchanged --------------------------------
        _stub1 = _skill_tmp / "pain_intel.py"
        _stub1.write_text(
            "import sys, json; import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--config'); ap.add_argument('--workdir')\n"
            "ap.add_argument('--standing-approval')\n"
            "a = ap.parse_args()\n"
            "import pathlib\n"
            "notif = {'status':'done','message':'stub done','report':'stub report'}\n"
            "pathlib.Path(a.workdir).mkdir(parents=True, exist_ok=True)\n"
            "(pathlib.Path(a.workdir)/'notification.json').write_text(\n"
            "    __import__('json').dumps(notif))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        _prev1 = _tmp / "pain_test1_2026-01-01"
        _prev1.mkdir()
        (_prev1 / "PAIN_CONFIG.json").write_text(
            json.dumps({"country_code": "us", "sources": ["maps"],
                        "competitor_names": ["Test"]}), encoding="utf-8")
        (_prev1 / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "subject": "Test1"}), encoding="utf-8")

        class _S1sc:
            from_dir = str(_prev1)
            ceiling  = "5.0"
            cadence  = "monthly"

        _buf_sc1 = io.StringIO()
        with redirect_stdout(_buf_sc1):
            cmd_schedule(_S1sc())
        _sc1 = json.loads(_buf_sc1.getvalue())
        _jf1 = Path(_sc1["job_file"])
        _bytes_before = _jf1.read_bytes()

        _rs1 = subprocess.run(
            [sys.executable, str(_repeat_tmp), "run-scheduled", "--job", str(_jf1)],
            capture_output=True, text=True,
        )
        _bytes_after = _jf1.read_bytes()
        check("[unit] run-scheduled: job file bytes unchanged",
              _bytes_before, _bytes_after)
        check("[unit] run-scheduled: stub exits 0", _rs1.returncode, 0)

        # ---- test 2: missing notification fallback --------------------------
        _stub2 = _skill_tmp / "pain_intel.py"
        _stub2.write_text(
            "import sys, argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--config'); ap.add_argument('--workdir')\n"
            "ap.add_argument('--standing-approval')\n"
            "ap.parse_args()\n"
            "# deliberately does NOT write notification.json\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        _prev2 = _tmp / "pain_test2_2026-01-01"
        _prev2.mkdir()
        (_prev2 / "PAIN_CONFIG.json").write_text(
            json.dumps({"country_code": "us", "sources": ["maps"],
                        "competitor_names": ["Test2"]}), encoding="utf-8")
        (_prev2 / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "subject": "Test2"}), encoding="utf-8")

        class _S2sc:
            from_dir = str(_prev2)
            ceiling  = "5.0"
            cadence  = "monthly"

        _buf_sc2 = io.StringIO()
        with redirect_stdout(_buf_sc2):
            cmd_schedule(_S2sc())
        _sc2 = json.loads(_buf_sc2.getvalue())
        _jf2 = Path(_sc2["job_file"])

        _rs2 = subprocess.run(
            [sys.executable, str(_repeat_tmp), "run-scheduled", "--job", str(_jf2)],
            capture_output=True, text=True,
        )
        # _out() pretty-prints JSON (multi-line); parse from the first '{' onward.
        _json2_start = _rs2.stdout.find("{")
        _final2 = json.loads(_rs2.stdout[_json2_start:]) if _json2_start >= 0 else {}
        _new2 = Path(_final2.get("run_dir", ""))
        _notif2 = json.loads((_new2 / "notification.json").read_text()) if _new2.is_dir() else {}
        _incomplete2 = (_new2 / "INCOMPLETE").exists() if _new2.is_dir() else False
        check("[unit] run-scheduled: fallback notification status = incomplete",
              _notif2.get("status"), "incomplete")
        check("[unit] run-scheduled: fallback notification message non-empty",
              bool(_notif2.get("message", "").strip()), True)
        check("[unit] run-scheduled: fallback INCOMPLETE marker written",
              _incomplete2, True)
        check("[unit] run-scheduled: missing notification -> exits 4",
              _rs2.returncode, 4)

        # ---- test 3: prepare fails (last_run_dir missing) -------------------
        _job3 = _tmp / ".repeat_jobs" / "pain_missing.json"
        _job3.parent.mkdir(parents=True, exist_ok=True)
        _job3.write_text(json.dumps({
            "tool": "pain",
            "subject": "Missing",
            "subject_slug": "missing",
            "ceiling": 5.0,
            "cadence": "monthly",
            "fingerprint": "abc",
            "last_run_dir": str(_tmp / "pain_missing_2026-01-01"),  # does not exist
        }), encoding="utf-8")

        _before3 = set(d.name for d in _tmp.iterdir() if d.is_dir())
        _rs3 = subprocess.run(
            [sys.executable, str(_repeat_tmp), "run-scheduled", "--job", str(_job3)],
            capture_output=True, text=True,
        )
        _after3 = set(d.name for d in _tmp.iterdir() if d.is_dir())
        _json3_start = _rs3.stdout.find("{")
        _final3 = json.loads(_rs3.stdout[_json3_start:]) if _json3_start >= 0 else {}
        check("[unit] run-scheduled: prepare fails -> exits 3", _rs3.returncode, 3)
        check("[unit] run-scheduled: prepare fails -> status refused",
              _final3.get("status"), "refused")
        check("[unit] run-scheduled: prepare fails -> no new folder",
              _after3 - _before3 - {".repeat_jobs"}, set())

        # ---- test 4: the job lock -----------------------------------
        # Released after a done (test 1), an exit-4 (test 2) and a refused
        # (test 3) run; a live lock refuses with exit 3 and no new folder; a
        # dead pid's lock is taken over.
        for _lbl, _jf in (("done", _jf1), ("exit 4", _jf2), ("refused", _job3)):
            check(f"[unit] run-scheduled lock: released after the {_lbl} run",
                  Path(str(_jf) + ".lock").exists(), False)
        import time as _time4
        _lk = Path(str(_jf2) + ".lock")
        _lk.write_text(json.dumps({"pid": os.getpid(), "started_epoch": _time4.time()}),
                       encoding="utf-8")
        _before4 = set(d.name for d in _tmp.iterdir() if d.is_dir())
        _rs4 = subprocess.run(
            [sys.executable, str(_repeat_tmp), "run-scheduled", "--job", str(_jf2)],
            capture_output=True, text=True,
        )
        _after4 = set(d.name for d in _tmp.iterdir() if d.is_dir())
        _f4 = json.loads(_rs4.stdout[_rs4.stdout.find("{"):]) if "{" in _rs4.stdout else {}
        check("[unit] run-scheduled lock: live lock -> exits 3", _rs4.returncode, 3)
        check("[unit] run-scheduled lock: live lock -> status refused",
              _f4.get("status"), "refused")
        check("[unit] run-scheduled lock: live lock -> plain message",
              str(_f4.get("message", "")).startswith("This repeat is already running"), True)
        check("[unit] run-scheduled lock: live lock -> no new folder", _after4 - _before4, set())
        check("[unit] run-scheduled lock: live lock left untouched", _lk.exists(), True)
        _dead = subprocess.Popen([sys.executable, "-c", "pass"])
        _dead.wait()
        _lk.write_text(json.dumps({"pid": _dead.pid, "started_epoch": _time4.time()}),
                       encoding="utf-8")
        _rs5 = subprocess.run(
            [sys.executable, str(_repeat_tmp), "run-scheduled", "--job", str(_jf2)],
            capture_output=True, text=True,
        )
        check("[unit] run-scheduled lock: dead pid's lock taken over (said so)",
              "[lock]" in _rs5.stdout and "taken over" in _rs5.stdout, True)
        check("[unit] run-scheduled lock: taken-over run went ahead (stub exit 4)",
              _rs5.returncode, 4)
        check("[unit] run-scheduled lock: released after the taken-over run",
              _lk.exists(), False)

    # ---- [unit] portable approvals and the mode guard -----------------------
    # WHY: a monthly repeat may run on this computer or in Claude's cloud, where
    # the research is a clone at another path.  The approval must travel with
    # the research (folder name, not absolute path), old absolute approvals
    # must keep working, a stored name must never lead outside the research
    # folder, and a job must run only in the place it was approved for, or one
    # month could be paid for twice.
    print("\n-- [unit] portable approvals and mode guard --")
    with tempfile.TemporaryDirectory() as td:
        _pt = Path(td).resolve()
        _pskill = _pt / "pain-and-messaging"
        (_pskill / "scripts").mkdir(parents=True)
        _prep = _pskill / "scripts" / "repeat.py"
        shutil.copy2(__file__, _prep)
        _calls = _pt / "stub_calls.log"
        (_pskill / "pain_intel.py").write_text(
            "import argparse, json, pathlib, sys\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--config'); ap.add_argument('--workdir')\n"
            "ap.add_argument('--standing-approval')\n"
            "a = ap.parse_args()\n"
            f"open({str(_calls)!r}, 'a').write(a.workdir + '\\n')\n"
            "w = pathlib.Path(a.workdir)\n"
            "(w / 'frontend_index.json').write_text('{}')\n"
            "(w / 'notification.json').write_text(json.dumps("
            "{'status': 'done', 'message': 'stub done', 'report': ''}))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        _rootA = _pt / "A" / "marketing-research"
        _m1 = _rootA / "pain_port_2026-01-01"
        _m1.mkdir(parents=True)
        (_m1 / "PAIN_CONFIG.json").write_text(
            json.dumps({"country_code": "us", "sources": ["maps"],
                        "competitor_names": ["Port"]}), encoding="utf-8")
        (_m1 / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "subject": "Port"}), encoding="utf-8")

        def _rs(job, remote=None):
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_REMOTE"}
            if remote is not None:
                env["CLAUDE_CODE_REMOTE"] = remote
            r = subprocess.run([sys.executable, str(_prep), "run-scheduled",
                                "--job", str(job)],
                               capture_output=True, text=True, env=env)
            i = r.stdout.rfind("\n{")
            try:
                fin = json.loads(r.stdout[i + 1:] if i >= 0 else r.stdout)
            except ValueError:
                fin = {}
            return r.returncode, fin, r.stdout

        def _dirs(p):
            return sorted(d.name for d in p.iterdir() if d.is_dir())

        _sp = subprocess.run([sys.executable, str(_prep), "schedule", "--from",
                              str(_m1), "--ceiling", "5"],
                             capture_output=True, text=True)
        _so = json.loads(_sp.stdout)
        _jobA = Path(_so["job_file"])
        _jA = json.loads(_jobA.read_text(encoding="utf-8"))
        check("[unit] portable: job stores the folder name", _jA["last_run_dir"],
              "pain_port_2026-01-01")
        check("[unit] portable: default mode is local (job)", _jA.get("mode"), "local")
        check("[unit] portable: default mode is local (output)", _so.get("mode"), "local")

        # Copy the whole research folder to another path; the job runs there.
        _rootB = _pt / "B" / "marketing-research"
        shutil.copytree(_rootA, _rootB)
        _jobB = _rootB / ".repeat_jobs" / _jobA.name
        _before_A = _dirs(_rootA)
        _rc, _fin, _ = _rs(_jobB)
        check("[unit] portable: copied job runs at the new path (exit 0)", _rc, 0)
        check("[unit] portable: new run folder is inside the copy",
              Path(_fin.get("run_dir") or "/").parent, _rootB)
        check("[unit] portable: original research folder untouched",
              _dirs(_rootA), _before_A)

        # An absolute job as written before this change still runs.
        _jobAbs = _rootA / ".repeat_jobs" / "pain_abs.json"
        _jobAbs.write_text(json.dumps(dict(
            {k: v for k, v in _jA.items() if k != "mode"},
            last_run_dir=str(_m1))), encoding="utf-8")
        _rc, _fin, _ = _rs(_jobAbs)
        check("[unit] portable: old absolute job still runs (exit 0)", _rc, 0)
        check("[unit] portable: old absolute job's run lands next to its folder",
              Path(_fin.get("run_dir") or "/").parent, _rootA)

        # A missing absolute path is NOT retried by its folder name.
        _jobGone = _rootB / ".repeat_jobs" / "pain_gone.json"
        _jobGone.write_text(json.dumps(dict(_jA, last_run_dir=str(
            _pt / "elsewhere" / "pain_port_2026-01-01"))), encoding="utf-8")
        _before_B = _dirs(_rootB)
        _rc, _fin, _ = _rs(_jobGone)
        check("[unit] portable: missing absolute path refuses (exit 3)", _rc, 3)
        check("[unit] portable: missing absolute path, no new folder",
              _dirs(_rootB), _before_B)

        # Values that are not one plain folder name refuse at $0.
        _outside = _pt / "A" / "pain_out_2026-01-01"
        shutil.copytree(_m1, _outside)
        (_rootA / "sub").mkdir()
        shutil.copytree(_m1, _rootA / "sub" / "pain_in_2026-01-01")
        os.symlink(_outside, _rootA / "pain_link_2026-01-01")
        for _bad in ("../pain_out_2026-01-01", "sub/pain_in_2026-01-01",
                     "sub\\pain_in_2026-01-01", ".", "..", "", None, 5,
                     "pain_link_2026-01-01"):
            _jb = _rootA / ".repeat_jobs" / "pain_bad.json"
            _jb.write_text(json.dumps(dict(_jA, last_run_dir=_bad)), encoding="utf-8")
            _raw = _jb.read_bytes()
            _snap = (_dirs(_rootA), _dirs(_pt / "A"), _dirs(_rootA / "sub"))
            _rc, _fin, _ = _rs(_jb)
            check(f"[unit] portable: last_run_dir {_bad!r} refuses (exit 3)", _rc, 3)
            check(f"[unit] portable: last_run_dir {_bad!r} creates no folder",
                  (_dirs(_rootA), _dirs(_pt / "A"), _dirs(_rootA / "sub")), _snap)
            check(f"[unit] portable: last_run_dir {_bad!r} leaves no lock",
                  Path(str(_jb) + ".lock").exists(), False)
            check(f"[unit] portable: last_run_dir {_bad!r} job bytes unchanged",
                  _jb.read_bytes(), _raw)

        # Values that cannot be a folder at all (a NUL character, a name too
        # long for the file system), relative and absolute: every command
        # refuses cleanly instead of crashing.  WHY: the unattended caller and
        # the notification depend on the NOTIFY line and the final JSON.
        _jb = _rootA / ".repeat_jobs" / "pain_unusable.json"
        for _lbl, _bad in (
                ("NUL, relative", "pain_port\u00002026-01-01"),
                ("NUL, absolute", str(_rootA) + "/pain_port\u00002026-01-01"),
                ("name too long, relative", "pain_port_" + "x" * 300),
                ("name too long, absolute", str(_rootA / ("pain_port_" + "x" * 300)))):
            _jb.write_text(json.dumps(dict(_jA, last_run_dir=_bad)), encoding="utf-8")
            _raw = _jb.read_bytes()
            _snap = (_dirs(_rootA), _dirs(_pt / "A"))
            _n_calls = len(_calls.read_text().splitlines()) if _calls.exists() else 0
            _rc, _fin, _out_txt = _rs(_jb)
            check(f"[unit] unusable name ({_lbl}): run-scheduled exit 3", _rc, 3)
            check(f"[unit] unusable name ({_lbl}): status refused",
                  _fin.get("status"), "refused")
            check(f"[unit] unusable name ({_lbl}): NOTIFY line printed",
                  "NOTIFY (send as Claude app notification): " in _out_txt, True)
            check(f"[unit] unusable name ({_lbl}): no new folder",
                  (_dirs(_rootA), _dirs(_pt / "A")), _snap)
            check(f"[unit] unusable name ({_lbl}): entry script never ran",
                  len(_calls.read_text().splitlines()) if _calls.exists() else 0,
                  _n_calls)
            check(f"[unit] unusable name ({_lbl}): no lock left",
                  Path(str(_jb) + ".lock").exists(), False)
            check(f"[unit] unusable name ({_lbl}): job bytes unchanged",
                  _jb.read_bytes(), _raw)
            _pp = subprocess.run([sys.executable, str(_prep), "prepare", "--job",
                                  str(_jb), "--date", "2026-04-01"],
                                 capture_output=True, text=True)
            check(f"[unit] unusable name ({_lbl}): prepare --job exit 2",
                  _pp.returncode, 2)
            check(f"[unit] unusable name ({_lbl}): prepare --job made no folder",
                  (_dirs(_rootA), _dirs(_pt / "A")), _snap)
            # The resolver schedule uses: answers, never raises.  A NUL is never
            # a folder; a name too long may resolve to a missing path.
            try:
                _res = _resolve_job_run_dir(_jb.resolve(), _bad)
                _ok = (_res[0] is None and bool(_res[1])) or (
                    "NUL" not in _lbl and _res[1] is None)
            except (ValueError, OSError) as _exc:
                _ok = f"raised {type(_exc).__name__}"
            check(f"[unit] unusable name ({_lbl}): resolver answers without raising",
                  _ok, True)
        _sl = subprocess.run([sys.executable, str(_prep), "schedule", "--from",
                              str(_rootA / ("pain_port_" + "x" * 300)),
                              "--ceiling", "5"], capture_output=True, text=True)
        check("[unit] unusable name: schedule --from a name too long exits 2",
              _sl.returncode, 2)

        # A symbolic link loop as a job value, relative and absolute.  WHY: on
        # Python 3.12 and older resolve() raises RuntimeError for it, and an
        # unattended month that crashes prints no NOTIFY line, so the user is
        # never told the month was skipped.
        os.symlink("pain_loop_b", _rootA / "pain_loop_a")
        os.symlink("pain_loop_a", _rootA / "pain_loop_b")
        _loop = str(_rootA / "pain_loop_a")
        for _lbl, _bad in (("loop, relative", "pain_loop_a"),
                           ("loop, absolute", _loop)):
            _jb.write_text(json.dumps(dict(_jA, last_run_dir=_bad)), encoding="utf-8")
            _snap = (_dirs(_rootA), _dirs(_pt / "A"))
            _n_calls = len(_calls.read_text().splitlines()) if _calls.exists() else 0
            _rc, _fin, _out_txt = _rs(_jb)
            check(f"[unit] unusable name ({_lbl}): run-scheduled exit 3", _rc, 3)
            check(f"[unit] unusable name ({_lbl}): status refused",
                  _fin.get("status"), "refused")
            check(f"[unit] unusable name ({_lbl}): NOTIFY line printed",
                  "NOTIFY (send as Claude app notification): " in _out_txt, True)
            check(f"[unit] unusable name ({_lbl}): no new folder",
                  (_dirs(_rootA), _dirs(_pt / "A")), _snap)
            check(f"[unit] unusable name ({_lbl}): entry script never ran",
                  len(_calls.read_text().splitlines()) if _calls.exists() else 0,
                  _n_calls)
            check(f"[unit] unusable name ({_lbl}): no lock left",
                  Path(str(_jb) + ".lock").exists(), False)
            _pp = subprocess.run([sys.executable, str(_prep), "prepare", "--job",
                                  str(_jb), "--date", "2026-04-01"],
                                 capture_output=True, text=True)
            check(f"[unit] unusable name ({_lbl}): prepare --job exit 2, no traceback",
                  (_pp.returncode, "Traceback" in _pp.stderr), (2, False))

        # Every command's folder or file argument with such a value: the
        # command's plain JSON error (exit 2), never a traceback.  WHY: the
        # model runs these while the user watches.
        _long = str(_rootA / ("pain_port_" + "x" * 300))
        for _vlbl, _v in (("a loop", _loop), ("a name too long", _long)):
            _snap = (_dirs(_rootA), _dirs(_pt / "A"))
            for _argv in (("list", "--parent", _v),
                          ("prepare", "--from", _v, "--date", "2026-04-01"),
                          ("prepare", "--from", str(_m1), "--parent", _v,
                           "--date", "2026-04-01"),
                          ("prepare", "--job", _v),
                          ("fingerprint", "--run", _v),
                          ("forecast", "--from", _v),
                          ("schedule", "--from", _v, "--ceiling", "5"),
                          ("run-scheduled", "--job", _v)):
                _r = subprocess.run([sys.executable, str(_prep), *_argv],
                                    capture_output=True, text=True)
                try:
                    _err = "error" in json.loads(_r.stdout)
                except ValueError:
                    _err = False
                check(f"[unit] {_argv[0]} {_argv[_argv.index(_v) - 1]} with {_vlbl}: "
                      f"exit 2, JSON error, "
                      f"no traceback",
                      (_r.returncode, _err, "Traceback" in _r.stderr), (2, True, False))
            check(f"[unit] every command with {_vlbl}: no new folder",
                  (_dirs(_rootA), _dirs(_pt / "A")), _snap)
        (_rootA / "pain_loop_a").unlink()
        (_rootA / "pain_loop_b").unlink()

        # prepare --parent that cannot be made: the JSON error (exit 2), never
        # a traceback, and every folder it made on the way removed again.  WHY:
        # making a missing --parent makes its ancestors first, so a name too
        # long or a read-only folder left them behind in the user's project.
        def _all_dirs():
            return sorted(str(d.relative_to(_pt)) for d in _pt.rglob("*")
                          if d.is_dir() and "pain-and-messaging" not in d.parts)
        _ro = _pt / "A" / "read_only"
        _ro.mkdir()
        _afile = _pt / "A" / "a_file"
        _afile.write_text("x")
        os.chmod(_ro, 0o555)
        try:
            for _vlbl, _v, _perm in (
                    ("a name too long at the first missing level",
                     _pt / "A" / "miss1" / ("x" * 300), False),
                    ("a name too long at a deeper missing level",
                     _pt / "A" / "miss2" / "ok" / ("x" * 300), False),
                    ("a missing folder inside a read-only folder", _ro / "a" / "b", True),
                    ("a read-only folder", _ro, True),
                    ("a path through a file", _afile / "sub", False)):
                if _perm and _as_root:
                    skip_as_root(f"[unit] prepare --parent, {_vlbl}")
                    continue
                _snap = _all_dirs()
                _r = subprocess.run([sys.executable, str(_prep), "prepare", "--from",
                                     str(_m1), "--parent", str(_v), "--date",
                                     "2026-04-01"], capture_output=True, text=True)
                try:
                    _err = "error" in json.loads(_r.stdout)
                except ValueError:
                    _err = False
                check(f"[unit] prepare --parent, {_vlbl}: exit 2, JSON error, "
                      f"no traceback",
                      (_r.returncode, _err, "Traceback" in _r.stderr), (2, True, False))
                check(f"[unit] prepare --parent, {_vlbl}: nothing new left on disk",
                      _all_dirs(), _snap)
        finally:
            os.chmod(_ro, 0o755)
        # Guard: a --parent that can be made is still made, two levels deep.
        _two = _pt / "A" / "fresh2" / "inner"
        _r = subprocess.run([sys.executable, str(_prep), "prepare", "--from", str(_m1),
                             "--parent", str(_two), "--date", "2026-04-01"],
                            capture_output=True, text=True)
        check("[unit] prepare --parent two missing levels: exit 0, run made inside",
              (_r.returncode, Path(json.loads(_r.stdout or "{}").get(
                  "new_run_dir", "/")).parent), (0, _two))

        # The new month's folder or its setup cannot be made, without --parent
        # (the path run-scheduled uses): a read-only research folder, a new
        # name too long, a reusable file that cannot be copied after the folder
        # was made, and a settings write that fails after it (a file size limit
        # stands in for a full disk).  WHY: the unattended month crashed with a
        # traceback and no NOTIFY line, so the user never learned the month was
        # skipped; attended prepare crashed the same way; and the two later
        # failures left a half-made run folder behind.
        _longrun = _rootA / ("p" * 248)
        _longrun.mkdir()
        (_longrun / "PAIN_CONFIG.json").write_text(
            json.dumps({"competitor_names": ["P"]}), encoding="utf-8")
        (_longrun / "frontend_index.json").write_text("{}", encoding="utf-8")
        _big = _rootA / "pain_big_2026-01-01"
        _big.mkdir()
        (_big / "PAIN_CONFIG.json").write_text(json.dumps(
            {"competitor_names": ["Big"], "notes": "n" * 10000}), encoding="utf-8")
        (_big / "frontend_index.json").write_text("{}", encoding="utf-8")
        _art = _rootA / "pain_art_2026-01-01"
        (_art / "competitors").mkdir(parents=True)
        (_art / "PAIN_CONFIG.json").write_text(
            json.dumps({"competitor_names": ["Art"]}), encoding="utf-8")
        (_art / "frontend_index.json").write_text("{}", encoding="utf-8")
        _unread = _art / "competitors" / "resolution.json"
        _unread.write_text("{}", encoding="utf-8")
        try:
            import resource as _resource
        except ImportError:
            _resource = None

        def _fsize():
            _resource.setrlimit(_resource.RLIMIT_FSIZE, (4096, 4096))

        _jset = _rootA / ".repeat_jobs" / "pain_setup.json"
        _setup_cases = [("a read-only research folder", _m1, "ro", None,
                         "this folder cannot be written to"),
                        ("a new run folder name too long", _longrun, None, None,
                         "its name would be too long"),
                        ("a reusable file that cannot be copied", _art, "art", None,
                         "a file could not be read or written")]
        if _resource is not None:
            _setup_cases.append(("a settings write that fails", _big, None, _fsize,
                                 "a file would be too large"))
        _root_mode = _rootA.stat().st_mode & 0o7777
        _unread_mode = _unread.stat().st_mode & 0o7777
        _env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_REMOTE"}
        for _lbl, _from, _chm, _pre, _reason in _setup_cases:
            _jset.write_text(json.dumps(dict(_jA, last_run_dir=_from.name)),
                             encoding="utf-8")
            for _argv in (("run-scheduled", "--job", str(_jset)),
                          ("prepare", "--job", str(_jset)),
                          ("prepare", "--from", str(_from))):
                if _chm is not None and _as_root:
                    skip_as_root(f"[unit] setup fails, {_lbl}: {_argv[0]} {_argv[1]}")
                    continue
                _snap = _all_dirs()
                _n_calls = len(_calls.read_text().splitlines()) if _calls.exists() else 0
                try:
                    if _chm == "ro":
                        os.chmod(_rootA, 0o555)
                    if _chm == "art":
                        os.chmod(_unread, 0o000)
                    _r = subprocess.run([sys.executable, str(_prep), *_argv],
                                        capture_output=True, text=True, env=_env,
                                        preexec_fn=_pre)
                finally:
                    os.chmod(_rootA, _root_mode)
                    os.chmod(_unread, _unread_mode)
                _i = _r.stdout.rfind("\n{")
                try:
                    _fin = json.loads(_r.stdout[_i + 1:] if _i >= 0 else _r.stdout)
                except ValueError:
                    _fin = {}
                _tag = f"[unit] setup fails, {_lbl}: {_argv[0]} {_argv[1]}"
                if _argv[0] == "run-scheduled":
                    check(f"{_tag}: exit 3, refused, NOTIFY line, no traceback",
                          (_r.returncode, _fin.get("status"), _fin.get("exit_code"),
                           "NOTIFY (send as Claude app notification): " in _r.stdout,
                           "Traceback" in _r.stderr),
                          (3, "refused", 3, True, False))
                    check(f"{_tag}: the message gives the reason and says nothing was spent",
                          (_fin.get("message", "").startswith(_SETUP_FAILED),
                           _reason in _fin.get("message", ""),
                           "spent nothing" in _fin.get("message", "")), (True, True, True))
                    check(f"{_tag}: no lock left", Path(str(_jset) + ".lock").exists(),
                          False)
                else:
                    check(f"{_tag}: exit 2, JSON error with the reason, no traceback",
                          (_r.returncode, _reason in _fin.get("error", ""),
                           "Traceback" in _r.stderr),
                          (2, True, False))
                check(f"{_tag}: nothing new left on disk", _all_dirs(), _snap)
                check(f"{_tag}: entry script never ran",
                      len(_calls.read_text().splitlines()) if _calls.exists() else 0,
                      _n_calls)
        shutil.rmtree(_longrun)
        shutil.rmtree(_big)
        shutil.rmtree(_art)
        _jset.unlink()

        # Mode guard, both directions, missing and unknown values.
        _cases = (("local", None, 0), ("local", "true", 3),
                  ("cloud", None, 3), ("cloud", "true", 3),
                  ("cloud", "TRUE", 3), ("cloud", "1", 3),
                  (None, None, 0), (None, "true", 3),
                  ("Cloud", None, 3), ("Cloud", "true", 3), ("", None, 3))
        for _mode, _remote, _want in _cases:
            _jm = _rootB / ".repeat_jobs" / "pain_mode.json"
            _jd = {k: v for k, v in _jA.items() if k != "mode"}
            if _mode is not None:
                _jd["mode"] = _mode
            _jm.write_text(json.dumps(_jd), encoding="utf-8")
            _raw = _jm.read_bytes()
            _n_calls = len(_calls.read_text().splitlines()) if _calls.exists() else 0
            _snap = _dirs(_rootB)
            _rc, _fin, _out_txt = _rs(_jm, _remote)
            _lbl = f"mode {_mode!r}, CLAUDE_CODE_REMOTE {_remote!r}"
            check(f"[unit] mode guard: {_lbl} -> exit {_want}", _rc, _want)
            if _want == 3:
                _n_after = len(_calls.read_text().splitlines()) if _calls.exists() else 0
                check(f"[unit] mode guard: {_lbl} -> status refused",
                      _fin.get("status"), "refused")
                check(f"[unit] mode guard: {_lbl} -> NOTIFY line printed",
                      "NOTIFY (send as Claude app notification): " in _out_txt, True)
                check(f"[unit] mode guard: {_lbl} -> entry script never ran",
                      _n_after, _n_calls)
                check(f"[unit] mode guard: {_lbl} -> no new folder",
                      _dirs(_rootB), _snap)
                check(f"[unit] mode guard: {_lbl} -> no lock left",
                      Path(str(_jm) + ".lock").exists(), False)
            if _mode == "cloud" and _remote == "true":
                # WHY: a cloud month run without saving would be paid for and
                # then lost with the cloud machine, so it refuses at $0.
                check(f"[unit] mode guard: {_lbl} without --publish git -> "
                      f"says it was not saving", "without saving" in _fin.get("message", ""),
                      True)
            check(f"[unit] mode guard: {_lbl} -> job bytes unchanged",
                  _jm.read_bytes(), _raw)

        # schedule --mode cloud needs the cloud setup first (VERSION); without
        # it, it refuses and writes nothing.
        _jraw = _jobA.read_bytes()
        _s1 = subprocess.run(
            [sys.executable, str(_prep), "schedule", "--from", str(_m1),
             "--ceiling", "5", "--mode", "cloud"], capture_output=True, text=True)
        check("[unit] schedule --mode cloud without the cloud setup: exit 2, "
              "approval unchanged", (_s1.returncode, _jobA.read_bytes()), (2, _jraw))
        (_rootA / ".marketing-scraping" / "plugin").mkdir(parents=True)
        (_rootA / ".marketing-scraping" / "plugin" / "VERSION").write_text("1\n")
        # schedule --mode cloud, then re-approve as local: reported.
        _s2 = json.loads(subprocess.run(
            [sys.executable, str(_prep), "schedule", "--from", str(_m1),
             "--ceiling", "5", "--mode", "cloud"],
            capture_output=True, text=True).stdout)
        check("[unit] schedule --mode cloud recorded",
              json.loads(Path(_s2["job_file"]).read_text())["mode"], "cloud")
        check("[unit] schedule --mode cloud: previous_mode local", _s2.get("previous_mode"),
              "local")
        _s3 = json.loads(subprocess.run(
            [sys.executable, str(_prep), "schedule", "--from", str(_m1),
             "--ceiling", "5", "--mode", "local"],
            capture_output=True, text=True).stdout)
        check("[unit] schedule back to local: previous_mode cloud",
              _s3.get("previous_mode"), "cloud")
        _s4 = subprocess.run(
            [sys.executable, str(_prep), "schedule", "--from", str(_m1),
             "--ceiling", "5", "--mode", "everywhere"],
            capture_output=True, text=True)
        check("[unit] schedule --mode everywhere refuses (exit 2)", _s4.returncode, 2)
        check("[unit] schedule --mode everywhere left the job as it was",
              json.loads(Path(_s3["job_file"]).read_text())["mode"], "local")

    # ---- [unit] forecast subprocess-based tests ------------------------------
    # Uses the same temp skill layout approach: repeat.py copy is placed under
    # a "pain-and-messaging" directory so _own_skill_tool() returns "pain".
    # A stub pain_intel.py supports --forecast-only.
    print("\n-- [unit] forecast subprocess tests --")
    with tempfile.TemporaryDirectory() as td_fc:
        _fc_tmp = Path(td_fc)
        _fc_skill = _fc_tmp / "pain-and-messaging"
        _fc_scripts = _fc_skill / "scripts"
        _fc_scripts.mkdir(parents=True)
        _fc_repeat = _fc_scripts / "repeat.py"
        shutil.copy2(__file__, _fc_repeat)

        # Stub entry script supports --forecast-only
        _fc_stub = _fc_skill / "pain_intel.py"
        _fc_stub.write_text(
            "import argparse, json, sys, pathlib\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--config'); ap.add_argument('--workdir')\n"
            "ap.add_argument('--standing-approval')\n"
            "ap.add_argument('--forecast-only', action='store_true')\n"
            "a = ap.parse_args()\n"
            "if a.forecast_only:\n"
            "    print(json.dumps({'forecast_usd': 0.42}))\n"
            "    sys.exit(0)\n"
            "pathlib.Path(a.workdir).mkdir(parents=True, exist_ok=True)\n"
            "notif = {'status':'done','message':'stub done','report':''}\n"
            "(pathlib.Path(a.workdir)/'notification.json').write_text(\n"
            "    json.dumps(notif))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        # Source folder with a valid pain config and index
        _fc_prev = _fc_tmp / "pain_forecast-test_2026-01-01"
        _fc_prev.mkdir()
        (_fc_prev / "PAIN_CONFIG.json").write_text(
            json.dumps({"country_code": "us", "sources": ["maps"],
                        "competitor_names": ["ForecastTest"]}), encoding="utf-8")
        (_fc_prev / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "subject": "ForecastTest"}), encoding="utf-8")

        # ---- test 1: forecast on nonexistent dir exits non-zero ---------------
        _fc1 = subprocess.run(
            [sys.executable, str(_fc_repeat), "forecast",
             "--from", str(_fc_tmp / "pain_nonexistent")],
            capture_output=True, text=True,
        )
        check("[unit] forecast: nonexistent --from exits non-zero",
              _fc1.returncode != 0, True)

        # ---- test 2: happy path returns forecast_usd, tool, subject -----------
        _fc2 = subprocess.run(
            [sys.executable, str(_fc_repeat), "forecast",
             "--from", str(_fc_prev)],
            capture_output=True, text=True,
        )
        _fc2_json_start = _fc2.stdout.find("{")
        _fc2_out = json.loads(_fc2.stdout[_fc2_json_start:]) if _fc2_json_start >= 0 else {}
        check("[unit] forecast: happy path exits 0", _fc2.returncode, 0)
        check("[unit] forecast: output has forecast_usd",
              "forecast_usd" in _fc2_out, True)
        check("[unit] forecast: forecast_usd matches stub (0.42)",
              _fc2_out.get("forecast_usd"), 0.42)
        check("[unit] forecast: output tool = pain",
              _fc2_out.get("tool"), "pain")
        check("[unit] forecast: output subject non-empty",
              bool(_fc2_out.get("subject")), True)

        # ---- test 3: source folder unchanged after forecast -------------------
        _fc_prev_files_before = {
            f.name: f.stat().st_size
            for f in _fc_prev.iterdir() if f.is_file()
        }
        subprocess.run(
            [sys.executable, str(_fc_repeat), "forecast",
             "--from", str(_fc_prev)],
            capture_output=True, text=True,
        )
        _fc_prev_files_after = {
            f.name: f.stat().st_size
            for f in _fc_prev.iterdir() if f.is_file()
        }
        check("[unit] forecast: source folder unchanged",
              _fc_prev_files_before, _fc_prev_files_after)

        # ---- test 4: temp dir cleaned up after forecast -----------------------
        import glob as _glob
        _leftover = _glob.glob(str(_fc_tmp / "repeat_forecast_*"))
        check("[unit] forecast: temp dir cleaned up", _leftover, [])

    print(f"\n  {PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
