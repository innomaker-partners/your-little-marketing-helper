#!/usr/bin/env python3
"""
pain_intel.py — top-level coordinator for the pain & messaging tool.

ACQUISITION HALF:
  1. Load config
  2. Resolve subjects (subjects.py) — three input modes, all converging on
     B2a_maps_places/review_targets.json
  3. Run B2a (Maps places) — unless mode 1 already wrote review_targets.json
  4. Bridge B2a → review_targets.json (mode-specific)
  5. Guard: loud error if review_targets.json is empty
  6. Run B2b (Maps reviews)

ANALYSIS HALF — WIRED:
  extractor.extract(workdir, cfg)   # -> synthesis_facts.json (deterministic, no LLM)
  report.generate(workdir, cfg)     # -> E1_pain_points.md + E2_trust_signals.md (LLM subagent)
  The extractor counts + sources every phrase BEFORE any LLM runs;
  the report LLM only clusters/labels/writes from those facts + raw quotes.

Usage:
    python3 pain_intel.py --config PAIN_CONFIG.json --workdir my_run/
    python3 pain_intel.py --config PAIN_CONFIG.json --workdir my_run/ --dry-run

--dry-run:  Resolve subjects and print the full B2a/B2b plan.  $0 Apify spend.
            Subprocess run_phase calls receive --dry-run and print their inputs.
            No review_targets.json is written (no B2a output to select from).

Stdlib only.  No pip install needed.  Only credential: your Apify token.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

# ---------------------------------------------------------------------------
# Resolve the scripts directory and import local modules
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_status as RS  # noqa: E402  (the run's status file)
import budget as B  # noqa: E402  (run ledger of an interactive run)
import config as C         # noqa: E402
import subjects as SBJ     # noqa: E402
import extractor as EXT    # noqa: E402  (deterministic facts extractor)
import report as RPT       # noqa: E402  (LLM clusters facts -> E1/E2)
import emit_index as IDX   # noqa: E402  (normalized frontend_index.json for the viewer)
import postprocess as _PP  # noqa: E402  (the one place the unfinished-step sentences are made)


# ---------------------------------------------------------------------------
# Subprocess helpers (mirrors ad_intel.py's _run_phase for package consistency)
# ---------------------------------------------------------------------------

# Keys excluded from the config fingerprint — must match repeat.py's constant
# (same file in all 3 skills; fingerprint must be reproducible by any of them).
_FINGERPRINT_EXCLUDE = frozenset(("subject", "repeat_of", "budget_caps",
                                  "resolution_confirmed"))


def _run_phase(phase, stage, cfg_path, workdir, dry_run, run_ceiling_usd=None,
               run_ledger_path=None):
    """Invoke run_phase.py for one stage.  Streams output live.
    Returns subprocess.CompletedProcess (check returncode in caller).

    run_ceiling_usd: when set (standing-approval mode), passed as
    --run-ceiling-usd to run_phase so it can cap per-call maxTotalChargeUsd
    and exit 4 (INCOMPLETE) when the budget ceiling is hit mid-phase.

    run_ledger_path: path to the run-level ledger JSON.  When set,
    run_phase reads prior stages' cumulative spend from this file before
    starting and writes the updated total back on exit.  This threads the
    run-level spend accumulator across subprocess boundaries.
    """
    cmd = [
        sys.executable,
        str(SCRIPTS / "run_phase.py"),
        str(phase),
        "--only", stage,
        "--config", str(cfg_path),
        "--workdir", str(workdir),
    ]
    if dry_run:
        cmd.append("--dry-run")
    if run_ceiling_usd is not None:
        cmd += ["--run-ceiling-usd", str(run_ceiling_usd)]
    if run_ledger_path is not None:
        cmd += ["--run-ledger-path", str(run_ledger_path)]
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    sys.stdout.flush()
    # As subprocess.run(cmd, check=False), but the step holds the run folder's lock and
    # a stop of this run reaches it (run_status.run_child).
    _res = RS.run_child(cmd)
    if not dry_run:
        _name_not_run_by_setting(workdir, [stage])
    return _res


def _write_notification(workdir, status, message, report=""):
    """Write notification.json for the viewer / agent to read after a run.

    status:  "done" | "refused" | "incomplete"
    message: one-line plain text, max 200 chars, no em-dashes (copy rule)
    report:  optional multi-line detail
    """
    notif = {
        "status":  status,
        "message": str(message)[:200],
        "report":  str(report),
    }
    p = Path(workdir) / "notification.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(notif, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_incomplete_marker(workdir,
                             text="Budget ceiling hit mid-run under standing-approval.\n"):
    """Create the INCOMPLETE sentinel file for the viewer to display."""
    (Path(workdir) / "INCOMPLETE").write_text(text, encoding="utf-8")


def _read_run_ledger(workdir, name="_run_ledger.json"):
    """Read cumulative billed spend from the run-level ledger written by run_phase.
    Returns a float or None when the ledger is absent or unreadable.

    Replaces _read_spend_from_inputs_log (which read .harness/inputs.jsonl,
    a file that only exists in test-harness mode, causing the done-notification
    spend line to always show 'see run log' in production runs).
    """
    p = Path(workdir) / name
    if not p.exists():
        return None
    try:
        spent = json.loads(p.read_text(encoding="utf-8")).get("spent", 0)
        # NaN, Infinity, a negative figure or true/false
        # is not an amount spent. float() accepts them, and the closing line read
        # "Total billed: $nan." Unreadable gives "see run log", the same as a
        # ledger that cannot be parsed, as run_phase's readers treat it.
        if isinstance(spent, bool):
            return None
        spent = float(spent)
        if spent != spent or spent in (float("inf"), float("-inf")) or spent < 0:
            return None
        return spent
    except Exception:  # noqa: BLE001
        return None

def _total_billed_line(workdir, ledger_name="_run_ledger.json", label="Total billed"):
    """The whole run's billed total, read from a run ledger at 4 decimals.

    ONE function for both callers: the "done" notification of
    an unattended run (<workdir>/_run_ledger.json), and the last line of an
    interactive run (its own ledger, _INTERACTIVE_LEDGER_NAME). WHY one: two
    computations of the same number drift apart, and the user is told this one."""
    _spent = _read_run_ledger(workdir, ledger_name)
    return (f"{label}: ${_spent:.4f}." if _spent is not None
            else f"{label}: see run log.") + _charge_caveat(workdir)


# Where apify_client notes a paid step whose charges were still changing when it
# stopped waiting for them (apify_client._settle_charge).
# WHY: Apify records a finished run's charges a few seconds late; the client now
# waits until they stop changing, but its wait is bounded so a run never hangs.
# A step that hit the bound was counted at what Apify had recorded so far, which
# can be low, and the bill must say so instead of reading as final.
_CHARGE_NOTES_NAME = ".charge_notes.jsonl"


def _start_charge_notes(workdir, dry_run):
    """Point apify_client at this run's notes file, emptied (a real run only)."""
    import apify_client as A
    if dry_run:
        os.environ.pop(A.CHARGE_NOTES_ENV, None)
        return
    p = Path(workdir) / _CHARGE_NOTES_NAME
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    os.environ[A.CHARGE_NOTES_ENV] = str(p.resolve())


def _charge_caveat(workdir):
    """" This may be incomplete: ..." when a step's charge was noted as possibly
    low this run, else ""."""
    try:
        notes = [x for x in (Path(workdir) / _CHARGE_NOTES_NAME).read_text(
            encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return ""
    if not notes:
        return ""
    n = len(notes)
    return (f" This may be incomplete: Apify was still recording the charges of {n} "
            f"paid step{'' if n == 1 else 's'} when the tool stopped waiting, so the "
            f"real bill can be higher. The exact amount is on the Apify console.")


# How the closing line names this tool's records and sources (plain words).
_RECORD_NOUN = ("review", "reviews")
_SOURCE_NAMES = {"maps": "Google Maps", "trustpilot": "Trustpilot", "reddit": "Reddit", "appstore": "App Store",
                   "googleplay": "Google Play", "g2": "G2", "capterra": "Capterra"}


def _tombstone(r):
    """Return True for a removed Reddit post: the app does not show it, and it is not a review.

    A copy of the viewer's rule (marketing-viewer compare._tombstone), kept here
    because each skill ships standalone and must not import another skill.
    A development test asserts the two copies are identical and agree on the
    same records, so they cannot drift apart.
    - platform must be "reddit"
    - tags.removed is True (strict boolean, not merely truthy), OR
    - text is exactly "[removed]" or "[deleted]" (case-insensitive, stripped)
    """
    return (r.get("platform") == "reddit"
            and ((r.get("tags") or {}).get("removed") is True
                 or (r.get("text") or "").strip().lower() in ("[removed]", "[deleted]")))


def _trashed_uids(workdir):
    """The records the user moved to the trash in the app (.viewer/annotations.json)."""
    try:
        ann = json.loads((Path(workdir) / ".viewer" / "annotations.json").read_text(
            encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    t = ann.get("trashed") if isinstance(ann, dict) else None
    return t if isinstance(t, dict) else {}


def _folder_size_parts(workdir):
    """One sentence with the size of the folder's results, by source, that adds up.

    WHY: after a run real sessions described its size from the
    per-source log lines ("[reddit] 15 records" counts the posts Reddit returned)
    mixed with other outputs, and the numbers did not add up. This counts the
    records of frontend_index.json, the file the app reads, by source, so the
    total is the sum of the parts. Empty when there is no readable index.

    WHY it counts what the app shows: it used to count every
    record, so a removed Reddit post was called a review, and the sentence said
    "Reddit 90" while the viewer's run_size said "Reddit 89" for the same folder.
    The records the app leaves out (removed Reddit posts, records moved to the
    trash) are named with a plain reason and not counted, exactly as run_size
    does, so the two sentences state the same numbers.

    WHY the records it leaves out are named on their own line: they used to end the bill line,
    which then held three statements (the bill, the
    size, what is not counted). In real sessions Claude broke that one long line when
    quoting it: it dropped a sentence, or moved the not-counted sentence ahead of the
    bill. The bill line now has the same shape in every tool (bill sentences plus the size
    sentence), and the not-counted sentence is its own closing line, word for word as it
    was. Returns (size sentence, not-counted line); either may be "".
    """
    try:
        recs = json.loads((Path(workdir) / "frontend_index.json").read_text(
            encoding="utf-8")).get("records") or []
    except Exception:  # noqa: BLE001
        return "", ""
    trashed = _trashed_uids(workdir)
    by_source = {}
    removed = in_trash = 0
    for r in recs:
        if not isinstance(r, dict):
            continue
        # Same order as the viewer's run_size: a removed post that is also in the
        # trash is counted as a removed post.
        if _tombstone(r):
            removed += 1
            continue
        if trashed.get(r.get("uid")):
            in_trash += 1
            continue
        k = r.get("platform") or ""
        by_source[k] = by_source.get(k, 0) + 1
    n = sum(by_source.values())
    if not (n or removed or in_trash):
        return "", ""
    parts = ", ".join(f"{_SOURCE_NAMES.get(k, k.capitalize() or 'no source')} {v}"
                      for k, v in sorted(by_source.items()))
    out = (f" The results in this folder hold {n} "
           f"{_RECORD_NOUN[0] if n == 1 else _RECORD_NOUN[1]}" + (f": {parts}." if parts else "."))
    reasons = []
    if removed:
        reasons.append(f"{removed} removed Reddit {'post' if removed == 1 else 'posts'} "
                       f"(deleted by the author or a moderator) "
                       f"{'is' if removed == 1 else 'are'} not counted")
    if in_trash:
        reasons.append(f"{in_trash} {_RECORD_NOUN[0] if in_trash == 1 else _RECORD_NOUN[1]} "
                       f"you moved to the trash {'is' if in_trash == 1 else 'are'} not counted")
    return out, ("; ".join(reasons) + "." if reasons else "")


def _folder_size_sentence(workdir):
    """The size sentence that ends the closing bill line (see _folder_size_parts)."""
    return _folder_size_parts(workdir)[0]


def _not_counted_line(workdir):
    """The closing line after the bill line naming the records the size leaves out, with a
    plain reason ("" when none are left out; see _folder_size_parts)."""
    return _folder_size_parts(workdir)[1]


# The workdir of an interactive run whose ledger this process started, or None.
_INTERACTIVE_LEDGER_WORKDIR = None
# An interactive run's ledger lives in its own hidden file, removed when the run
# ends. WHY not _run_ledger.json: that file belongs to unattended runs (it also
# records why a run stopped), and a manual re-run of such a folder must not
# overwrite it; and without a standing approval a run must leave its folder with
# exactly the files it always had (test_p3_round6 pins the file list).
_INTERACTIVE_LEDGER_NAME = ".interactive_bill.json"
# Whether the run folder already held finished paid steps when this run started,
# and whether --force asked to run them again.
_INTERACTIVE_EARLIER_STEPS = False
_INTERACTIVE_FORCE = False


def _start_interactive_ledger(workdir, ceiling, dry_run, force=False):
    """Give an interactive run the same run ledger an unattended run has.

    WHY: an interactive run printed only per-stage lines
    rounded to cents, and real sessions added them up and told the user $0.27
    for a $0.281 run. The ledger starts at 0 and every charge is added to it
    (budget.add_to_run_ledger, reached through the environment variable, so
    run_phase.py and its command line stay exactly as committed). Under a
    standing approval run_phase keeps the ledger itself, so the variable is
    removed there; a dry run spends nothing and prints its forecast instead."""
    global _INTERACTIVE_LEDGER_WORKDIR, _INTERACTIVE_EARLIER_STEPS, _INTERACTIVE_FORCE
    if ceiling is not None or dry_run:
        os.environ.pop(B.RUN_LEDGER_ENV, None)
        return
    # A paid step counts as finished once its full output exists; that is
    # run_phase's own test (_stage_done: <stage folder>/run_02_full.json), and a
    # finished step is skipped, not billed again, unless --force is given.
    _INTERACTIVE_EARLIER_STEPS = any(Path(workdir).glob("*/run_02_full.json"))
    _INTERACTIVE_FORCE = bool(force)
    ledger = Path(workdir) / _INTERACTIVE_LEDGER_NAME
    ledger.write_text(json.dumps({"spent": 0.0}), encoding="utf-8")
    os.environ[B.RUN_LEDGER_ENV] = str(ledger.resolve())
    _INTERACTIVE_LEDGER_WORKDIR = Path(workdir)


# What the closing lines need from _main: the run's gap list (the
# same list object _main records into) and whether the run reached its last step.
# "reports_missing" is [(report name, plain reason)] for each of the two
# written reports this run did not produce ([] when both were). It is recorded where
# the report step gives up and read only by the closing lines. It is a note, never a
# gap or an outcome: the collected data is complete, and the monthly repeat must not
# read this run as unfinished.
# "steps_pending" is whether some paid step the config's sources
# ask for has no full result on disk when the reports are written (True), or has all of
# them (False). None means it was not worked out, and is read as True: the closing never
# says writing the reports later costs nothing unless this is False.
_CLOSING = {"gaps": None, "reached_end": False, "reports_missing": [], "steps_pending": None}


def _reports_missing_line(missing, unfinished=False):
    """The closing line for written reports that were not produced, with the reason.

    missing: [(report name, plain reason)], at least one entry.

    WHY (seen in a live search run, the same step here): the report step
    failed (the claude command was not logged in, exit 1, twice) and the run still
    ended with only its bill, so the user was told nothing about the missing report
    or why. The remedy is the one the skill gives under "Write (or finish) the report
    of a run". The wording is cause-agnostic because the cause is not always the
    command.

    unfinished: True when a step did not finish (the
    lines above name it). Then "costs nothing on Apify, because the finished steps
    are skipped" would be false: writing the reports later runs that step again, and
    it can cost money. The line says so, and that a dry run shows the cost first, as
    the skill does for this path. It is passed in, from the same step records that
    produced the lines above, not re-derived here.

    Also True when some paid step the config's sources ask
    for has no full result on disk although no line names it: a later run would pay
    for it. The clause "costs nothing" is said only when every such step is finished."""
    names = " and ".join(n for n, _ in missing)
    one = len(missing) == 1
    reasons = list(dict.fromkeys(str(r).rstrip(". ") for _, r in missing))
    reason = (reasons[0] if len(reasons) == 1 else
              "; ".join(f"{str(r).rstrip('. ')} ({n})" for n, r in missing))
    head = (f"The written {names} {'report was' if one else 'reports were'} not produced: "
            f"{reason}. The collected data is saved in this folder. Once that is fixed, ask "
            f"Claude to write the {'report' if one else 'reports'} of this research")
    if unfinished:
        return (head + ". Running it again may pay for the steps that did not finish, "
                       "so a dry run shows the cost first.")
    return head + "; it costs nothing on Apify, because the steps that already finished are skipped."


def _end_interactive_ledger():
    """Print the interactive run's whole bill at its end, however it ended.

    Returns the closing text it printed ("" when it printed none), which the run's
    status file carries word for word (run_status.finish). A run that reached its
    last step first names its gaps, in the notification's words (_gap_lines); then
    comes the bill line, and last, when the app leaves records out of the size, the
    line that names them (_not_counted_line)."""
    global _INTERACTIVE_LEDGER_WORKDIR
    wd = _INTERACTIVE_LEDGER_WORKDIR
    if wd is None:
        return ""
    _INTERACTIVE_LEDGER_WORKDIR = None
    os.environ.pop(B.RUN_LEDGER_ENV, None)
    # WHY two wordings: re-running a finished folder (for
    # example to write missing reports) skips every finished paid step, so its
    # bill can be $0.0000 while the month cost $0.2810. That bill must never read
    # as the month's cost. A fresh run's bill is the whole run's bill.
    if _INTERACTIVE_EARLIER_STEPS:
        line = _total_billed_line(wd, _INTERACTIVE_LEDGER_NAME, label="Billed by this run")
        line += (" This folder already held results from an earlier run, so this is only "
                 "what this run billed, not what the month cost.")
        if not _INTERACTIVE_FORCE:
            line += (" The steps that earlier run had finished were skipped and not billed "
                     "again.")
    else:
        line = _total_billed_line(wd, _INTERACTIVE_LEDGER_NAME)
        if line.startswith("Total billed: $") and not _charge_caveat(wd):
            line += (" This is the whole run's bill: give it as written, not rounded; "
                     "the step lines above are rounded.")
    line += _folder_size_sentence(wd)
    not_counted = _not_counted_line(wd)
    for leftover in (_INTERACTIVE_LEDGER_NAME, _INTERACTIVE_LEDGER_NAME + ".tmp",
                     _CHARGE_NOTES_NAME):
        try:
            (wd / leftover).unlink(missing_ok=True)
        except OSError:
            pass
    # A step that did not finish is named first, from the same stage
    # records a scheduled run's notification reads.
    inc_lines = (_incomplete_report_lines(_incomplete_lines(wd), scheduled=False)
                 if _CLOSING["reached_end"] else [])
    kept_lines = [s for _, s in _kept_lines(wd)] if _CLOSING["reached_end"] else []
    gap_lines = inc_lines + kept_lines + (_gap_lines(_CLOSING["gaps"], complete=not inc_lines)
                                          if _CLOSING["reached_end"] and _CLOSING["gaps"] else [])
    if _CLOSING["reached_end"] and _CLOSING["reports_missing"]:
        gap_lines.append(_reports_missing_line(
            _CLOSING["reports_missing"],
            unfinished=bool(inc_lines) or _CLOSING["steps_pending"] is not False))
    if gap_lines:
        print("\n" + "\n".join(gap_lines), flush=True)
    print("\n" + line + ("\n" + not_counted if not_counted else ""), flush=True)
    return "\n".join(gap_lines + [line] + ([not_counted] if not_counted else []))


def _read_stopped_info(workdir):
    """Read the stop reason written by run_phase into the run-level ledger.
    Returns a dict with 'stage', 'reason', 'detail' or None when absent.

    run_phase records the stop reason (budget / error / ledger) so the
    entry script can give an accurate notification instead of always saying
    'budget ceiling reached' even when the stop was an error.
    """
    p = Path(workdir) / "_run_ledger.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("stopped")
    except Exception:  # noqa: BLE001
        return None


# Plain names for the stage codes, used only in notification.json text.
_STAGE_PLAIN = {
    "B2a": "Google Maps places",
    "B2b": "Google Maps reviews",
    "TP": "Trustpilot reviews",
    "AS": "App Store reviews",
    "GP": "Google Play reviews",
    "RD": "Reddit",
    "G2": "G2 reviews",
    "CP": "Capterra reviews",
    "NONE": "the next step",
}


# What an INCOMPLETE report says when its spend figure cannot be trusted.
# WHY (reader side only; run_phase.py stays as committed and audited):
#   - reason "ledger": run_phase rewrites an unreadable ledger as spent 0.0, so
#     the number it holds is not what the run spent. Earlier stages may have
#     billed real money; printing "$0.0000" would be a false figure.
#   - reason "error" and an unexpected exit: a call that fails after its actor
#     started never adds its charge to the ledger, so the figure can be low.
_SPEND_UNKNOWN = ("Spent so far: unknown, because the record of spending could not "
                  "be read. Check your Apify console for what this run was charged.")
_MAY_BE_CHARGED = ("The step that failed may still have been charged by Apify; "
                   "check your Apify console for the exact amount.")


def _check_incomplete(rc, stage, ceiling, workdir):
    """After _run_phase: if any non-zero exit under standing-approval, write
    markers and exit 4 immediately.  Without a ceiling (no standing-approval),
    this is a no-op -- preserving byte-for-byte behaviour for non-approval runs.

    Extended from rc == 4 to any non-zero rc under standing-approval.
    Why: a run_phase crash (interpreter error, kill signal) would otherwise let
    the entry script continue to the next stage with a stale or absent ledger.

    Reads the stop reason from the ledger to give an accurate message.
    Mechanism: run_phase writes {"stopped": {"stage", "reason", "detail"}} to
    the ledger on a stop; we read it here to distinguish budget / error / ledger.
    """
    if rc.returncode == 0 or ceiling is None:
        return  # non-approval path or success: unchanged behaviour
    _write_incomplete_marker(workdir)
    _spent = _read_run_ledger(workdir)
    cost_line = (f"Spent so far: ${_spent:.4f}." if _spent is not None
                 else "Spent so far: see run log.") + _charge_caveat(workdir)
    # Read the stop reason from the ledger to give an accurate message.
    _stop = _read_stopped_info(workdir)
    _reason = _stop.get("reason") if _stop else None
    _detail = _stop.get("detail", "") if _stop else ""
    _failed_stage = _stop.get("stage", stage) if _stop else stage
    # Plain names in what the user reads; stage codes stay in the run log.
    _sn = _STAGE_PLAIN.get(stage, stage)
    _fn = _STAGE_PLAIN.get(_failed_stage, _failed_stage)

    if rc.returncode != 4:
        # Unexpected exit code (crash, interpreter error, kill signal)
        msg = f"{_sn} stopped unexpectedly."[:200]
        report = (
            f"{_sn} stopped unexpectedly (exit code {rc.returncode}).\n"
            f"{cost_line}\n"
            f"{_MAY_BE_CHARGED}\n"
            f"Check run logs and re-run manually before re-scheduling."
        )
    elif _reason == "ledger":
        msg = f"Run stopped before {_sn}: the record of what this run has spent could not be read."[:200]
        report = (
            f"The record of what this run has spent could not be read before {_sn} started, so nothing more was spent.\n"
            f"Something went wrong between steps.\n"
            f"{_SPEND_UNKNOWN}\n"
            f"Run the tool once by hand, then re-schedule."
        )
    elif _reason == "error":
        short_detail = _detail[:300] if _detail else "see the run log"
        msg = f"{_fn} failed, so the run stopped. Nothing more was spent after it."[:200]
        report = (
            f"{_fn} failed with an error.\n"
            f"Error (for troubleshooting): {short_detail}\n"
            f"{cost_line}\n"
            f"{_MAY_BE_CHARGED}\n"
            f"Run the tool once manually to fix the error, then re-schedule."
        )
    else:
        # budget stop (reason == "budget" or unknown)
        msg = (f"{_sn} stopped: its next step could have taken the run"
               f" past your approved ${B.cap_text(ceiling)}.")[:200]
        # The step the cap stopped may have run part of its work first,
        # and the run's spending may already be past the ceiling (an actor that
        # billed past its own spend cap); "approaching" and "next step" were
        # untrue then.
        _pw = _partway_entry(workdir, _failed_stage)
        _pw_line = (f"{_fn} had started when it was stopped. {_partway_charge(_pw)}, and "
                    f"nothing it collected was kept.\n") if _pw else ""
        if _pw:
            msg = (f"{_fn} stopped part-way because of your approved ${B.cap_text(ceiling)} "
                   f"ceiling; nothing it collected was kept.")[:200]
        _why = ("the run's spending passed" if _spent is not None and _spent > ceiling
                else "the run was approaching")
        report = (
            f"{_sn} stopped before completing because {_why} "
            f"the approved ceiling of ${B.cap_text(ceiling)}.\n"
            f"{_pw_line}"
            f"{cost_line}\n"
            f"Run the tool once manually for a full run, then re-schedule "
            f"with a higher ceiling if needed."
        )
    _write_notification(workdir, "incomplete", msg, report=report)
    sys.exit(4)


# Source key -> stage code, for the plain names in _STAGE_PLAIN.
_SOURCE_STAGE = {"maps": "B2b", "trustpilot": "TP", "appstore": "AS",
                 "googleplay": "GP", "reddit": "RD", "g2": "G2", "capterra": "CP"}


# Stage folder of each review source (stages.STAGES[...]["folder"]), where
# run_phase leaves .run_notice.json when a scraper said it did not work.
_NOTICE_FOLDER = {"B2b": "B2b_reviews", "TP": "trustpilot_reviews",
                  "AS": "appstore_reviews", "GP": "googleplay_reviews",
                  "RD": "reddit_reviews", "G2": "g2_reviews", "CP": "capterra_reviews"}


def _quoted(items):
    """'a', 'b' -- the user's own setting values, quoted, for a gap reason."""
    return ", ".join(f"'{str(x).strip()}'" for x in items)


def _record_gap(gaps, name, reason):
    """Record that an ACTIVE source returned nothing this run, and why.

    Called at the point where the run decides to skip a source or to carry on
    with nothing from it, because that is where the reason is known. WHY not
    re-derived later from the run folder: the upstream step already knows what
    happened; re-detecting it from folder contents is how silent zeros came back
    before. One entry per source (the first, most specific reason wins).
    name: a plain source name (_STAGE_PLAIN); reason: plain words, no full stop.
    """
    # A source named as not collected because its free check could not
    # be done keeps that reason; its empty result later is not a second one.
    if any(g[0] == name and (len(g) == 2 or g[2] == "unchecked") for g in gaps):
        return
    gaps.append((name, str(reason).replace("—", "-").replace("–", "-")))


def _maintenance_gap(gaps, workdir, folder, name, lead, source, claim, where):
    """When a stage came back empty AND its scraper said it did not work, record
    the stage as a gap and say so in plain words; return True. Else False.

    Reads <folder>/.run_notice.json, which run_phase writes from the run object
    it already fetched (no new call). WHY: once the LinkedIn scraper was in maintenance mode,
    returned 0 items with status SUCCEEDED, and the run
    called that "a genuine zero" for HubSpot. A scraper that fetched nothing is
    not evidence about the market. Recorded before any generic zero reason, so
    this, the most specific reason, is the one the closing lines give.
    lead: "LinkedIn returned no ads for 'HubSpot'"; source: "LinkedIn";
    claim: "'HubSpot' runs no LinkedIn ads"; where: "the LinkedIn Ad Library".
    """
    try:
        n = json.loads((Path(workdir) / folder / ".run_notice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(n, dict) or not str(n.get("status_message") or "").strip():
        return False
    said = ("reported it is under maintenance"
            if "maintenance" in str(n.get("status_message")).lower()
            else "reported it is not working right now")
    try:
        usd = float(n.get("charged_usd") or 0.0)
    except (TypeError, ValueError):
        usd = 0.0
    paid = ("Nothing was charged." if usd <= 0
            else f"Apify reported a charge of ${usd:.2f} for it.")
    print(f"  \u26a0 {lead}, but the {source} scraper {said} and fetched nothing, so this "
          f"is not evidence that {claim}. {paid} Check {where} by hand, or run again "
          f"when the scraper is back.")
    _record_gap(gaps, name, f"the {source} scraper {said} and fetched nothing, so this is "
                            f"not evidence that {claim}; check {where} by hand, or run "
                            f"again when the scraper is back")
    return True


def _record_partial(gaps, name, reason):
    """Record that SOME targets of an active source were lost this run.

    The source still ran for its other targets, so this is not "returned
    nothing": it is kept apart from _record_gap's entries (a 3-tuple tagged
    "partial") and reported under its own heading. Called only where the run
    already decided a target cannot be used (a name that matched nothing, a
    setting that is not a usable address); never from a comparison with last
    month, and never for a target that simply had nothing NEW (normal).
    One entry per source; the reason names every lost target.
    """
    if any(len(g) == 3 and g[2] == "partial" and g[0] == name for g in gaps):
        return
    gaps.append((name, str(reason).replace("—", "-").replace("–", "-"), "partial"))


def _record_tagged(gaps, name, reason, tag):
    """Two more kinds of entry, one per source each, tagged like "partial":
      "unchecked" -- the source was NOT collected because the free check before
                     its paid step could not be done (offline, a timeout, a host
                     the network does not allow) and no earlier answer could stand
                     in. Not "returned nothing": nothing was asked of it.
      "note"      -- the check could not be done, but the source was still
                     collected: from the earlier confirmed answer, or as written.
    WHY tagged and kept apart: a check that could not be done is not evidence
    about the market or about the user's settings."""
    if any(len(g) == 3 and g[2] == tag and g[0] == name for g in gaps):
        return
    gaps.append((name, str(reason).replace("—", "-").replace("–", "-"), tag))


def _lookup_outcome(gaps, name, entries, target_key, no_match, store):
    """Record what a source's free lookup left out, by kind.

    entries: the resolver's entries; a usable one has `target_key` set.
    no_match(inputs) -> the reason for inputs the store answered have no match
    (a real answer; the wording the run always used). store: "App Store" etc.
    Returns True when nothing usable is left (the step will scrape nothing)."""
    usable = [e for e in entries if e.get(target_key)]
    unchecked = [e.get("input", "") for e in entries
                 if not e.get(target_key) and e.get("unchecked")]
    missing = [e.get("input", "") for e in entries
               if not e.get(target_key) and not e.get("unchecked")
               and not e.get("not_rechecked")]
    # An earlier month's real "does not exist" answer that
    # stands because this month's check could not be done. Named apart, so the user
    # reads which month found it and that it was not checked again.
    absent_again = [e for e in entries
                    if not e.get(target_key) and e.get("not_rechecked")]
    stood_in = [e for e in entries if e.get(target_key)
                and (e.get("not_rechecked") or e.get("unchecked"))]
    pieces = []
    if missing:
        pieces.append(no_match(missing))
    for e in absent_again:
        where = (f"the check in {e['reused_from']}" if e.get("reused_from")
                 else "this folder's earlier check")
        pieces.append(f"{_quoted([e.get('input', '')])} was left out and nothing was spent "
                      f"on it: {where} found it does not exist, and this month it could not "
                      f"be checked again, because {e['not_rechecked']}")
    if unchecked:
        one = len(unchecked) == 1
        why = next(e.get("unchecked") for e in entries
                   if not e.get(target_key) and e.get("unchecked"))
        pieces.append(f"the free {store} lookup could not be done ({why}), so "
                      f"{_quoted(unchecked)} {'was' if one else 'were'} not looked up "
                      f"and nothing was spent on {'it' if one else 'them'}; the next "
                      f"run tries again")
    if pieces and not usable:
        if unchecked:
            _record_tagged(gaps, name, "; ".join(pieces), "unchecked")
        else:
            _record_gap(gaps, name, "; ".join(pieces))
    elif pieces:
        _record_partial(gaps, name, "; ".join(pieces) + " (the others were still searched)")
    if stood_in:
        said = []
        for e in stood_in:
            why = e.get("not_rechecked") or e.get("unchecked")
            if e.get("reused_from"):
                said.append(f"{_quoted([e.get('input', '')])} could not be checked this time "
                            f"({why}), so the result confirmed in {e['reused_from']} was used")
            elif e.get("not_rechecked"):
                said.append(f"{_quoted([e.get('input', '')])} could not be checked again "
                            f"({why}), so this folder's earlier check was used")
            else:
                said.append(f"{_quoted([e.get('input', '')])} could not be checked first "
                            f"({why}), so it was used as written")
        _record_tagged(gaps, name, "; ".join(said), "note")
    return not usable


# What the notification says when the app index could not be written.
# WHY this matters beyond the app: _run_completed in repeat.py counts a month
# only when its frontend_index.json exists, so next month's repeat silently
# builds on the month BEFORE this one. The user must hear both facts.
_INDEX_FAILED_MSG = ("the results could not be shown in the app, so next month's "
                     "repeat will build on the previous month")


def _index_failed_report(workdir):
    return (f"The results were collected but could not be shown in the app, and next "
            f"month's repeat will build on the previous month instead of this one. "
            f"To fix this, ask Claude to rebuild the app view for the folder "
            f"{Path(workdir).name} (it is free and scrapes nothing again).")


def _gap_lines(gaps, complete=True):
    """The plain lines that name a run's gaps: sources that returned nothing and
    sources that lost some of their targets, each with its reason.

    ONE function for both places the user reads them: the "done"
    notification of an unattended run, and the closing lines of a run started by
    hand. WHY: a run's closing lines are what the model reads and quotes; when they
    only said "Meta ads collected: 0", a competitor that was never searched could be
    reported as one that runs no ads. Empty when the run has no gaps.

    complete=False: the run did not finish everything, so the headings
    do not open with "Run complete".

    Sources not collected because a free check could not be done come
    first, under a heading that never says "Run complete"; sources still
    collected although their check could not be done come last, as a note."""
    whole = [(g[0], g[1]) for g in gaps if len(g) == 2]
    part = [(g[0], g[1]) for g in gaps if len(g) == 3 and g[2] == "partial"]
    unchecked = [(g[0], g[1]) for g in gaps if len(g) == 3 and g[2] == "unchecked"]
    notes = [(g[0], g[1]) for g in gaps if len(g) == 3 and g[2] == "note"]
    lines = []
    opened = not complete
    if unchecked:
        lines.append("These sources were also not collected this month, because a free check "
                     "before them could not be done:" if opened
                     else "Run finished, but these sources were not collected this month, "
                          "because a free check before them could not be done:")
        lines += [f"- {n}: {r}." for n, r in unchecked]
        opened = True
    if whole:
        lines.append("These sources also returned nothing this month:" if opened
                     else "Run complete, but these sources returned nothing this month:")
        lines += [f"- {n}: {r}." for n, r in whole]
        opened = True
    if part:
        lines.append("Some of what you asked for is also missing this month:" if opened
                     else "Run complete, but some of what you asked for is missing this month:")
        lines += [f"- {n}: {r}." for n, r in part]
    if notes:
        lines.append("Some free checks before the paid steps could not be done this month:")
        lines += [f"- {n}: {r}." for n, r in notes]
    return lines


def _write_done_notification(workdir, ceiling, gaps, index_failed=False, summary_note=None):
    """The "done" notification of an unattended run.

    With nothing to report, the text is exactly what it always was. Otherwise
    the status stays "done" (by design: the month stays usable, and
    "nothing more was spent" stays true), but the text never implies more than
    the run delivered:
      - gaps: sources that returned nothing (2-tuples) and sources that lost
        some of their targets (3-tuples, "partial"), each with its reason;
      - index_failed: the month cannot be shown in the app and next month will
        build on the previous one (message and report);
      - summary_note: the written summary is missing (a plain sentence for the
        report only; it is a note, not a source gap).
    Never says "All stages ran successfully" unless all of these are empty.
    """
    cost_line = _total_billed_line(workdir)
    tail = f" Ceiling ${B.cap_text(ceiling)}."
    where = f"Results are in: {Path(workdir).name}"
    whole = [(g[0], g[1]) for g in gaps if len(g) == 2]
    part = [(g[0], g[1]) for g in gaps if len(g) == 3 and g[2] == "partial"]
    unchecked = [(g[0], g[1]) for g in gaps if len(g) == 3 and g[2] == "unchecked"]
    notes = [g for g in gaps if len(g) == 3 and g[2] == "note"]
    if not whole and not part and not unchecked and not notes and not index_failed \
            and not summary_note:
        _write_notification(workdir, "done", f"Run complete. Ceiling ${B.cap_text(ceiling)}.",
                            report=(f"All steps ran successfully.\n"
                                    f"{cost_line}\n"
                                    f"{where}"))
        return

    # Message: the most specific form that fits in 200 characters.
    names = list(dict.fromkeys([n for n, _ in unchecked] + [n for n, _ in whole]
                               + [n for n, _ in part]))
    if unchecked:
        # Never "Run complete" while a source was not collected.
        if not whole and not part and len(unchecked) == 1:
            # "nothing from App Store reviews was
            # collected" read as "App Store reviews was". The names that reach here
            # (Trustpilot, App Store and Google Play reviews) are plural.
            heads = [f"Run finished, but no {unchecked[0][0]} were collected this "
                     f"month, because the free check before them could not be done.",
                     f"Run finished, but no {unchecked[0][0]} were collected this "
                     f"month; see the report."]
        elif not whole and not part:
            heads = [f"Run finished, but {len(unchecked)} sources were not collected this "
                     f"month, because a free check before them could not be done: "
                     f"{', '.join(names)}.",
                     f"Run finished, but {len(unchecked)} sources were not collected this "
                     f"month; see the report."]
        else:
            heads = [f"Run finished, but some results are missing this month: "
                     f"{', '.join(names)}.",
                     "Run finished, but some results are missing this month; see the report."]
    elif whole and not part:
        if len(whole) == 1:
            heads = [f"Run complete, but {whole[0][0]} returned nothing this month "
                     f"({whole[0][1]}).",
                     f"Run complete, but {whole[0][0]} returned nothing this month; "
                     f"see the report."]
        else:
            heads = [f"Run complete, but {len(whole)} sources returned nothing this month: "
                     f"{', '.join(names)}.",
                     f"Run complete, but {len(whole)} sources returned nothing this month; "
                     f"see the report."]
    elif part and not whole and len(part) == 1:
        heads = [f"Run complete, but part of {part[0][0]} is missing this month: "
                 f"{part[0][1]}.",
                 f"Run complete, but part of {part[0][0]} is missing this month; "
                 f"see the report."]
    elif part:
        heads = [f"Run complete, but some results are missing this month: "
                 f"{', '.join(names)}.",
                 "Run complete, but some results are missing this month; see the report."]
    else:
        heads = []
    if index_failed:
        if heads:
            cands = [h + f" Also, {_INDEX_FAILED_MSG}." + tail for h in heads]
        else:
            cands = [f"Run complete, but {_INDEX_FAILED_MSG}. "
                     f"Ask Claude to rebuild the app view." + tail]
        cands.append(f"Run complete, but {_INDEX_FAILED_MSG}; see the report." + tail)
    elif heads:
        cands = [h + tail for h in heads]
    else:
        cands = [f"Run complete. Ceiling ${B.cap_text(ceiling)}."]
    msg = next((c for c in cands if len(c) <= 200), cands[-1])

    lines = _gap_lines(gaps)
    if not whole and not part and not unchecked:
        lines.insert(0, "Run complete. No source is missing this month.")
    if index_failed:
        lines.append(_index_failed_report(workdir))
    if summary_note:
        lines.append(summary_note)
    report = "\n".join(lines) + f"\n{cost_line}\n{where}"
    _write_notification(workdir, "done", msg, report=report)


# --- a run that did not finish its work ---------------------------
# Once, a scheduled repeat's Trustpilot refresh ended ABORTED, the
# client printed its RUN INCOMPLETE block, and the notification still said
# "Run complete" with status done. run_phase now records, in each stage folder,
# every kept run whose block was printed and every refresh it could not make
# (<stage>/.run_incomplete.json, from the same judgment the block printed).
# The notification reads those records; it never judges "complete" a second
# way. A source that simply returned nothing is not this: it stays "done, but
# name the gap".
_RUN_INCOMPLETE_FILE = ".run_incomplete.json"
_ENDED_WORDS = {"ABORTED": "was stopped", "FAILED": "failed",
                "TIMED-OUT": "ran out of time", "TIMED_OUT": "ran out of time"}
_LAST_MONTHS_FIGURES = "so the rating and review count shown are last month's"


def _incomplete_reason(e):
    """One record entry (run_phase._record_incomplete) in plain words."""
    kind = e.get("kind")
    names = _quoted(e.get("names") or e.get("targets") or [])
    if kind == "run_ended":
        how = _ENDED_WORDS.get(str(e.get("status")), "did not finish")
        cap = " at its spending cap" if e.get("cap_stop") else ""
        if e.get("what") == "entity refresh":
            return (f"the check of the current rating and review count {how}{cap} "
                    f"before it finished, {_LAST_MONTHS_FIGURES}")
        return (f"its {e.get('what') or 'scraper run'} {how}{cap} before it finished, "
                f"so what it collected this month is partial")
    if kind == "refresh_unmatched":
        them = "them" if len(e.get("names") or e.get("targets") or []) > 1 else "it"
        return (f"the current rating and review count of {names} could not be fetched, "
                f"because the scraper named {them} differently from your settings and "
                f"{them} could not be matched, {_LAST_MONTHS_FIGURES}")
    if kind == "refresh_missing":
        return f"no current rating and review count came back for {names}, {_LAST_MONTHS_FIGURES}"
    # (run_phase._note_step_stopped): a step that never finished is named
    # as such, never read as an empty result.
    if kind == "step_failed":
        return ("it stopped with an error before it finished, so its results are missing "
                "from this run (the error is in the run log). To run only this step "
                "again, run the same command on this run folder: the steps that "
                "finished are skipped and not charged again")
    if kind == "budget_stop" and e.get("part_way"):
        # The cap stopped this step after part of it had run and been
        # charged (run_phase._note_budget_stop), so "it did not run" was untrue.
        return ("it started, but the run reached the budget cap in your settings "
                f"(budget_caps) part-way through it. {_partway_charge(e)}, and nothing "
                "it collected was kept. To finish it, raise the cap and run the same "
                "command on this run folder: this step starts again from the beginning "
                f"and the part that already ran {_partway_rerun(e)}, while the steps "
                "that finished are skipped and not charged again")
    if kind == "budget_stop":
        return ("it did not run, because the run reached the budget cap in your settings "
                "(budget_caps). To run it, raise the cap and run the same command on this "
                "run folder: the steps that finished are skipped and not charged again")
    return "a step did not finish"


# The file run_phase._stage_done tests. A step whose folder holds
# it is skipped by a run of the same command, whatever this run's attempt did.
_FINISHED_RESULT_FILE = "run_02_full.json"


def _earlier_result_reason(e):
    """The record entry of a step that failed or was stopped at the budget cap
    (run_phase._note_step_stopped), for a step whose folder STILL HOLDS a finished
    result.

    WHY: _incomplete_reason tells the user to run the same command on the folder
    and says the steps that finished are skipped. That is the remedy for a step
    that has no finished result. Verified on the fake for the search tool (a run,
    then a --force run whose search-results step failed, then a plain run): raw
    results are never removed, so the earlier run_02_full.json was still there, the
    plain run printed "already complete, skipping" for the step, kept the earlier
    results and left the error record in place. The advice promised a fix the
    command cannot make. Here the step is named as it is: the folder holds a
    finished result, the same command keeps it, and fetching the step again takes a
    new run folder (this entry script has no --force). The record says
    whether the attempt saved that result before it stopped (result_saved), so the line
    says whose result it is. "Its latest attempt" is true whichever command reads the
    record: a plain run of the folder makes no attempt at a finished step. Nothing is
    built again here: a review step's files stay with its result (postprocess.
    drop_stage_outputs)."""
    kind = e.get("kind")
    again = ("To fetch this step again, start a new run folder; that charges for "
             "every step again, and a dry run shows the cost first")
    keeps = ("This run folder still holds a finished result of this step, and running "
             "the same command on it skips the step and keeps that result. ")
    if kind == "step_failed":
        if e.get("result_saved"):
            return ("it saved its results and then stopped with an error (the error is "
                    "in the run log), so what is shown for it comes from this run but "
                    "may be incomplete. " + keeps + again)
        return ("its latest attempt stopped with an error (the error is in the run log). "
                + keeps + again)
    if kind == "budget_stop" and e.get("part_way"):
        return ("it started, but the run reached the budget cap in your settings "
                f"(budget_caps) part-way through it. {_partway_charge(e)}, and nothing "
                "it collected was kept. " + keeps + "Raise the cap first. " + again)
    return ("the run reached the budget cap in your settings (budget_caps) before "
            "this step. " + keeps + "Raise the cap first. " + again)


def _partway_charge(e):
    """What a step stopped part-way by the budget cap was charged, in plain words,
    from its stop record (run_phase._note_budget_stop): spent_usd is what
    the run's budget counted for the step. Every scraper run is
    counted the moment it ends, so this is every run of the step that was billed (in
    pain, the full run before an entity refresh too), the same figure as the bill.
    Nothing of the step is kept: its test pass and its batches are held in memory
    and written to the folder only when the whole step finishes."""
    try:
        spent = float(e.get("spent_usd") or 0.0)
    except (TypeError, ValueError):
        spent = 0.0
    return f"The part that ran was charged ${spent:.4f}"


def _partway_rerun(e):
    """What running the folder again does with what already ran.
    A re-run finds no finished output for the step, so it runs the step from its
    test pass on, with new scraper runs that are billed again (verified on the fake:
    the places test pass, both keyword volume batches, and a Trustpilot test pass
    and full run stopped before their entity refresh). The exception
    (rerun_reuses_run): when the run the step left behind was the first
    run it launched, the re-run picks that run up from Apify instead of launching
    it (run_phase._launch_recover; verified on one fake server: no new run and no
    new charge), though the re-run's own bill counts it again."""
    if e.get("rerun_reuses_run"):
        return ("is picked up from Apify instead of being run again, so Apify does not "
                "charge for it a second time (the bill shown at the end of that run "
                "still counts it)")
    return "is charged again"


def _partway_entry(workdir, stage):
    """The part-way budget stop record of this step, or None. Found by
    the step code each record names (as _incomplete_lines reads them), so this needs
    no import of stages.py."""
    for p in sorted(Path(workdir).glob("*/" + _RUN_INCOMPLETE_FILE)):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("stage") != stage:
            continue
        for e in doc.get("entries") or []:
            if isinstance(e, dict) and e.get("kind") == "budget_stop" and e.get("part_way"):
                return e
    return None


def _stopped_entry(workdir, stage):
    """The record entry of this step that says it failed or was stopped at the
    budget cap (run_phase._note_step_stopped: {"kind": ..., "cap_usd"?}), or None.
    The empty-places guard reads it to tell the true reason."""
    import stages as _st  # already on sys.path
    p = Path(workdir) / _st.STAGES[stage]["folder"] / _RUN_INCOMPLETE_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    if not isinstance(doc, dict):
        return None
    e = next((e for e in (doc.get("entries") or [])
              if isinstance(e, dict) and e.get("kind") in
              ("step_failed", "budget_stop", "kept_after_failed_attempt")), None)
    if e and e.get("kind") == "kept_after_failed_attempt":
        e = {**e, "kind": e.get("was") or "step_failed"}  # Read as it always was
    return e


# run_phase writes this record in a step's folder
# when the step's settings ask it for 0 results (google_max_items 0, say) and it
# was not run (run_phase._note_not_run_by_setting). The closing lines and the
# notification name the step with this reason, never as an empty result.
_NOT_RUN_BY_SETTING_FILE = ".not_run_by_setting.json"
_NOT_RUN_BY_SETTING_REASON = ("it was not run, because your settings ask it for 0 "
                              "results, so nothing was collected and nothing was spent")


def _not_run_by_setting(workdir, stage):
    """True when run_phase recorded that this step's settings ask for 0 results."""
    import stages as _st  # already on sys.path
    try:
        return (Path(workdir) / _st.STAGES[stage]["folder"]
                / _NOT_RUN_BY_SETTING_FILE).exists()
    except (KeyError, TypeError):
        return False


def _name_not_run_by_setting(workdir, stages):
    """Name every step of `stages` that was not run because its settings ask for
    0 results, in the run's gap list, right after the step was called (the first
    reason recorded for a step is the one the closing lines give)."""
    gaps = _CLOSING.get("gaps")
    if gaps is None:
        return
    for stage in stages:
        if _not_run_by_setting(workdir, stage):
            _record_gap(gaps, _STAGE_PLAIN.get(stage, stage), _NOT_RUN_BY_SETTING_REASON)


def _did_not_run(workdir, stage):
    """True when run_phase recorded that this step failed or was stopped at the
    budget cap (run_phase._note_step_stopped): no reason for a zero is
    recorded for it; the closing lines name it as not finished instead.
    A step its settings asked for 0 results was not run either; the
    closing lines name it with that reason (_name_not_run_by_setting).
    A scraper run that ended without finishing (stopped, failed,
    timed out) and kept no record is the same case. Its own line already says it
    "was stopped before it finished", so the line "it ran, but nothing usable came
    back" next to it said two opposite things about one step. A run that ended
    unfinished but DID keep records is not this: a zero is then a real count. The
    check of the current rating and review count (an entity refresh) is left out: it
    comes after the step's main run, and an empty refresh says nothing about whether
    the main run found reviews."""
    return (_stopped_entry(workdir, stage) is not None or _not_run_by_setting(workdir, stage)
            or _ended_empty(workdir, stage))


def _ended_empty(workdir, stage):
    """True when a scraper run of this step ended without finishing and kept no
    record (run_phase._note_run_incomplete writes {"kind": "run_ended", "records": n})."""
    import stages as _st  # already on sys.path
    p = Path(workdir) / _st.STAGES[stage]["folder"] / _RUN_INCOMPLETE_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return False
    return isinstance(doc, dict) and any(
        isinstance(e, dict) and e.get("kind") == "run_ended" and e.get("records") == 0
        and e.get("what") != "entity refresh"
        for e in (doc.get("entries") or []))


def _zero_targets_lines(cfg, n_places):
    """The stop lines for a category-mode run whose Google Maps places step found
    places but queued none of them for reviews. Only review_target_count 0
    queues none, so the lines name that. WHY the remedy is a new run folder and not the
    same one: the places step of this folder is finished, so running it again skips that
    step and never rebuilds the list of places to collect reviews for (verified on the
    fake); a new folder searches Google Maps again, and that search is charged
    again, about what the places step charged here. No figure is given because the
    forecast of the new run depends on the settings it is started with."""
    try:
        now = int(cfg.get("review_target_count", 60))
    except (TypeError, ValueError):
        now = 60
    places = f"{n_places} place{'' if n_places == 1 else 's'}"
    if now == 0:
        head = (f"! STOPPED: Google Maps found {places}, but your settings ask for 0 "
                f"reviews (review_target_count is 0), so no reviews were collected.")
        ask = ("! To collect reviews, set review_target_count to the number of places "
               "you want reviews for, and")
    else:
        head = (f"! STOPPED: this run folder holds {places} that Google Maps found, but "
                f"none was queued for reviews, because review_target_count was 0 when "
                f"they were found, so no reviews were collected. Your settings now say "
                f"{now}, but that does not change this folder.")
        ask = "! To collect reviews,"
    return ["\n" + "!" * 60, head, ask + " start a new run folder.",
            "! Running this same folder again does not collect them: its Google Maps "
            "places step is finished, so it is skipped and the list of places is not "
            "rebuilt.",
            "! A new folder searches Google Maps again, and that search is charged "
            "again, about what the places step charged here.",
            "!" * 60]


def _places_stopped_lines(entry):
    """What the empty-places guard says when the Google Maps places step itself did
    not finish. WHY: the guard listed location and address causes, which
    sent the user to fix a setting that was never tried. The figure is the cap the
    step was stopped at, as run_phase recorded it; without it, no figure is given."""
    if entry.get("kind") == "budget_stop":
        try:
            cap = f" of ${B.cap_text(entry['cap_usd'])}"
        except (KeyError, TypeError, ValueError):
            cap = ""
        if entry.get("part_way"):
            # The cap stopped the places step after part of its search had
            # run with the user's settings and been charged (its test pass: the
            # places step has no batches and no entity refresh in stages.py and
            # run_phase.py). "Never tried" and "nothing to fix" were untrue then.
            return [f"The Google Maps places step started, but the run reached the budget "
                    f"cap{cap} in your settings (budget_caps) part-way through it. "
                    f"{_partway_charge(entry)}, and nothing it found was kept, so there "
                    f"are no places to collect reviews for.",
                    "The part that ran used your location and competitor settings, and "
                    "it was the cap that stopped the search, not those settings, so "
                    "nothing here says they need fixing.",
                    "To continue, raise the cap and run the same command on this run "
                    "folder: this step starts again from the beginning and the part that "
                    f"already ran {_partway_rerun(entry)}, while the steps that finished "
                    "are skipped and not charged again."]
        return [f"The Google Maps places step did not run, because the run reached the "
                f"budget cap{cap} in your settings (budget_caps), so no place was "
                f"searched and there are no places to collect reviews for.",
                "Your location and competitor settings were never tried, so there is "
                "nothing to fix in them.",
                "To continue, raise the cap and run the same command on this run folder: "
                "the steps that finished are skipped and not charged again."]
    return ["The Google Maps places step stopped with an error before it finished (the "
            "error is in the run log), so there are no places to collect reviews for.",
            "The search did not finish, so this says nothing yet about your location or "
            "competitor settings.",
            "To try again, run the same command on this run folder: the steps that "
            "finished are skipped and not charged again."]


def _incomplete_lines(workdir):
    """[(plain stage name, reason)] for every stage record of a step that did
    not finish (run_phase writes <stage>/.run_incomplete.json); [] for none.
    A record that cannot be read still counts: something did not finish."""
    out = []
    for p in sorted(Path(workdir).glob("*/" + _RUN_INCOMPLETE_FILE)):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict) or not doc.get("entries"):
            out.append(("One step", "it recorded that it did not finish, but the record "
                                    "could not be read; check the run log"))
            continue
        name = _STAGE_PLAIN.get(doc.get("stage"), doc.get("stage") or "One step")
        entries = [e if isinstance(e, dict) else {} for e in doc["entries"]
                   if not (isinstance(e, dict)
                           and e.get("kind") == "kept_after_failed_attempt")]
        if not entries:
            continue  # A result kept after a failed attempt, named by _kept_lines
        if any(e.get("kind") == "budget_stop" and e.get("part_way") for e in entries):
            # Nothing of a step the cap stopped part-way was kept, so a
            # line saying one of its runs collected a partial result is not true.
            entries = [e for e in entries if e.get("kind") != "run_ended"]
        holds = (p.parent / _FINISHED_RESULT_FILE).exists()
        for e in entries:
            if holds and e.get("kind") in ("step_failed", "budget_stop"):
                out.append((name, _earlier_result_reason(e)))
            else:
                out.append((name, _incomplete_reason(e)))
    return out


def _kept_lines(workdir):
    """[(plain step name, sentence)] for every step whose finished result a later run
    kept after the latest attempt to fetch it again failed (run_phase
    _note_kept_after_failed_attempt writes the record). The sentences are the ones
    postprocess.unfinished_steps makes, so the app, the report and the closing lines
    say one thing. These steps did not fail in THIS run, so they are not
    _incomplete_lines and the closing does not open with "did not finish"."""
    return [(u["kept_step"], u["sentence"]) for u in _PP.unfinished_steps(workdir)
            if u.get("kept_step")]


def _incomplete_report_lines(inc, scheduled=True):
    """The report lines that name what did not finish ([] for nothing).
    scheduled: the run ends "incomplete", so repeat._run_completed skips this
    month and the next repeat builds on the last month that finished."""
    if not inc:
        return []
    return (["This run did not finish everything it was asked to do:"]
            + [f"- {n}: {r}." for n, r in inc]
            + (["Next month's repeat will build on the last month that finished. "
                "To check this month, run the tool once by hand."] if scheduled else []))


def _write_incomplete_run_notification(workdir, ceiling, inc, gaps, index_failed=False,
                                       summary_note=None):
    """The notification of an unattended run that reached its end but did not
    finish everything: status "incomplete", never "done", and never
    the words "Run complete"."""
    cost_line = _total_billed_line(workdir)
    tail = f" Ceiling ${B.cap_text(ceiling)}."
    where = f"Results are in: {Path(workdir).name}"
    names = list(dict.fromkeys(n for n, _ in inc))
    cands = []
    if len(inc) == 1:
        cands.append(f"Run incomplete: {inc[0][0]}: {inc[0][1]}.")
    cands += [f"Run incomplete: part of {', '.join(names)} did not finish this month; "
              f"see the report.",
              "Run incomplete: part of this month's run did not finish; see the report."]
    msg = next((c + tail for c in cands if len(c + tail) <= 200), cands[-1] + tail)
    lines = _incomplete_report_lines(inc) + _gap_lines(gaps, complete=False)
    if index_failed:
        lines.append(_index_failed_report(workdir))
    if summary_note:
        lines.append(summary_note)
    report = "\n".join(lines) + f"\n{cost_line}\n{where}"
    _write_notification(workdir, "incomplete", msg, report=report)


def _write_run_ledger(workdir, data):
    """Replace <workdir>/_run_ledger.json with data, atomically (temp file, then
    rename), so a reader never sees a half-written ledger. Used only by the G2
    name-lookup spend guard below; run_phase writes the ledger itself."""
    p = Path(workdir) / "_run_ledger.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# What one G2 name lookup (factden 'products' mode) bills per product row.
# The same figure _pain_forecast_usd prices it at and subjects.resolve_g2's
# dry-run note shows.
_G2_DISCOVER_RATE_USD = 0.004


def _g2_names_not_looked_up(cfg):
    """The g2_names the run does not look up because g2_discover_max_products is
    0; [] otherwise.

    WHY (a 0 in a count setting is a skip at $0,
    before any paid call): each name lookup is a paid factden run, and with 0 it
    was still started, asking for 0 products, and billed its start charge for
    each name. Whether the actor reads 0 as "none" or as "no limit" is not
    documented. The names are not looked up, nothing is spent on them, and the
    closing lines say why. The forecast leaves them out the same way."""
    if int(cfg.get("g2_discover_max_products", 5)) > 0:
        return []
    return [str(n).strip() for n in (cfg.get("g2_names") or []) if str(n).strip()]


def _g2_step_finished(workdir):
    """True when this folder's G2 review step is finished, by run_phase's own
    test (run_phase._stage_done, the one _finished_paid_steps calls)."""
    import run_phase as RP  # already on sys.path via SCRIPTS
    import stages as ST
    return RP._stage_done(Path(workdir) / ST.STAGES["G2"]["folder"], ST.STAGES["G2"])


class _G2Stop(Exception):
    """Raised by _G2DiscoverGuard to stop an unattended run at a G2 name lookup.
    reason is one of run_phase's stop reasons: "budget" or "ledger"."""

    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _g2_lookup_estimate_usd(max_products):
    """The forecast of one G2 name lookup: its product rows at the lookup rate,
    padded like every forecast, plus the lookup run's own start charge."""
    return (int(max_products) * _G2_DISCOVER_RATE_USD * C.SAFETY_MARGIN
            + C.start_fee_usd("G2"))


class _G2LookupLimit:
    """The spending limit of a G2 name lookup in an interactive run.

    WHY: subjects.discover_g2_products passes its max_charge_usd to Apify as the
    run's hard limit, and an interactive run passed none, so the lookup was the
    one paid call that could bill without a limit. resolve_g2 asks this object
    for each lookup's limit (cap_for_discover: the lookup's forecast x
    CHARGE_CEILING_FACTOR, as run_phase caps every other call) and hands it each
    lookup's bill (record), which goes to the run's ledger exactly as the
    on_charge path did. It never stops the run: a person approved this run's
    cost at the gate, and an unattended run uses _G2DiscoverGuard instead."""

    def cap_for_discover(self, max_products):
        return _g2_lookup_estimate_usd(max_products) * C.CHARGE_CEILING_FACTOR

    def record(self, cost_usd):
        B.add_to_run_ledger(cost_usd)


class _G2DiscoverGuard:
    """The standing approval's spending promise for G2 name lookups.

    WHY: every other paid call runs inside run_phase, which caps it at what is
    left under the approved ceiling and adds its bill to the run ledger. G2 name
    lookups run from here (subjects.discover_g2_products), so without this guard
    a scheduled run's lookup had no Apify hard cap, was missing from the ledger
    (later caps and the "done" total left it out), and a failed lookup let the
    run finish "done" with no G2 data. It applies the same rule run_phase does:
      before a call: read the ledger (unreadable: stop, reason "ledger"); stop
        with reason "budget" if the estimate does not fit under ceiling - spent;
        otherwise cap the call at min(estimate x CHARGE_CEILING_FACTOR,
        ceiling - spent);
      after a call: add what it billed to the ledger.
    Built only under --standing-approval; interactive runs never create one.
    """

    def __init__(self, workdir, ceiling):
        self.workdir = Path(workdir)
        self.ceiling = float(ceiling)
        self.spent = None      # run spend as last read or written by this guard
        self.last_cap = None   # the cap given to the most recent lookup
        self.last_cost = None  # what the most recent lookup billed

    def cap_for_discover(self, max_products):
        spent = _read_run_ledger(self.workdir)
        if spent is None:
            raise _G2Stop("ledger", "run ledger unreadable before a G2 name lookup")
        self.spent = spent
        # + the lookup run's own start charge (factden's per-GB apify-actor-start).
        est = _g2_lookup_estimate_usd(max_products)
        remaining = self.ceiling - spent
        if est > remaining:
            raise _G2Stop("budget",
                          f"G2 name lookup estimate ${est:.4f} does not fit in the "
                          f"${remaining:.4f} left under the ${B.cap_text(self.ceiling)} ceiling")
        self.last_cap = min(est * C.CHARGE_CEILING_FACTOR, remaining)
        self.last_cost = None
        return self.last_cap

    def record(self, cost_usd):
        cost = float(cost_usd or 0.0)
        self.last_cost = cost
        spent = _read_run_ledger(self.workdir)
        if spent is None:
            self.spent = (self.spent or 0.0) + cost
            raise _G2Stop("ledger", "run ledger unreadable after a G2 name lookup")
        self.spent = spent + cost
        _write_run_ledger(self.workdir, {"spent": round(self.spent, 8)})

    def failure_reason(self):
        """Stop reason for a lookup that raised: "budget" when it stopped at
        its cap (billed at least 90% of it, the rule apify_client._warn_incomplete
        uses to call a stop a cap stop), otherwise "error"."""
        if (self.last_cap and self.last_cost is not None
                and self.last_cost >= 0.9 * self.last_cap):
            return "budget"
        return "error"


def _stop_at_g2_lookup(workdir, ceiling, reason, detail, spent):
    """Record why the run stopped at a G2 name lookup, then end it INCOMPLETE
    through _check_incomplete (marker, "incomplete" notification, exit 4), the
    same exit every run_phase stop takes, so nothing after it can spend."""
    _write_run_ledger(workdir, {
        "spent": round(spent if spent is not None else 0.0, 8),
        "stopped": {"stage": "G2", "reason": reason,
                    "detail": str(detail).replace("\u2014", "-")[:150]},
    })
    _check_incomplete(subprocess.CompletedProcess(args=[], returncode=4), "G2",
                      ceiling, workdir)


def _test_pass_usd(stg, build_cfg, workdir, full_inp, full_count):
    """What the small test run that run_phase makes BEFORE a step's full run can
    bill (its start fee included), or 0.0 when that run will not be made.

    WHY (the same fix as in the search tool): the full
    run re-sends the test run's own first inputs, so the two are billed
    separately, and the forecast priced only the full run. Measured on the fake
    at the tool's own prices, with every actor returning its full cap: two
    Trustpilot companies at 5 reviews each forecast $0.0119 and billed $0.0186;
    three named competitors forecast $0.3136 for Maps places and billed $0.3816.
    A forecast is a spending gate and must never be under the real charge.

    The test is run_phase._run_one's own, in the same words: no run when the full
    input counts 0 (the step is not run), when the test input has nothing to work
    on (_has_inputs), or when the full input is no larger than the test input
    (_input_size; the test would bill the same run twice, so _run_one skips it).
    An input _input_size cannot size always gets its test run, as in _run_one.
    The test run is priced as _run_one prices its spend limit: estimate(stage,
    test_count), one run. build_cfg / workdir: what the full input was built from.
    The test input is built silently: the full input's build already printed any
    warning about an entry it skipped."""
    import contextlib
    import io
    import run_phase as RP  # already on sys.path via SCRIPTS
    import stages as ST
    if full_count <= 0:
        return 0.0
    spec = ST.STAGES[stg]
    with contextlib.redirect_stdout(io.StringIO()):
        t_inp, t_count = spec["build"](build_cfg, str(workdir), test=True)
    if not RP._has_inputs(stg, t_inp):
        return 0.0
    f_sz, t_sz = RP._input_size(spec, full_inp), RP._input_size(spec, t_inp)
    if f_sz is not None and t_sz is not None and f_sz <= t_sz:
        return 0.0
    return C.estimate(stg, t_count)[0]


def _planned_b2b_test_pass_usd(cfg, n_places, b2b_count):
    """The Maps reviews step's test run when the places are not known yet (no
    review_targets.json): the step is priced from n_places placeholder places, as
    its full run is, so the test run is asked of the stage builder on a folder of
    its own that holds a list of that many. Nothing is written in the run folder."""
    import tempfile
    import stages as ST
    with tempfile.TemporaryDirectory(prefix="pain_forecast_") as td:
        d = Path(td) / ST.STAGES["B2a"]["folder"]
        d.mkdir()
        (d / "review_targets.json").write_text(
            json.dumps([{"url": f"planned-place-{i}"} for i in range(int(n_places))]),
            encoding="utf-8")
        full_inp, _ = ST.build_B2b(cfg, td, test=False)
        return _test_pass_usd("B2b", cfg, td, full_inp, b2b_count)


# The config key that lists a named source's targets, and a placeholder valid in
# that key's own format (the stage builders drop an entry they cannot read).
_PLANNED_TARGET_KEY = {"appstore": ("appstore_app_ids", lambda i: str(900000000 + i)),
                       "googleplay": ("googleplay_app_ids", lambda i: f"com.planned.app{i}"),
                       "g2": ("g2_urls", lambda i: f"planned-product-{i}")}


def _with_planned_targets(cfg, src, n_names):
    """cfg with one placeholder target added to the source's own target list per
    name that is still to be resolved. WHY: such names become targets only after
    the confirmation step, so the stage builders cannot see them yet, and the
    test run is made when the targets the names will resolve to, together with
    those already supplied, are more than the test run's one. Each name resolves
    to at most one target, so this never plans fewer than the real run has."""
    key, make = _PLANNED_TARGET_KEY[src]
    planned = dict(cfg)
    planned[key] = list(cfg.get(key) or []) + [make(i) for i in range(n_names)]
    return planned


def _pain_forecast_usd(cfg, workdir, skip=None):
    """Zero-spend forecast: sum of all active-source stage estimates.

    skip: the list of paid steps this run will skip because
    they are finished (_finished_paid_steps). Those are left out. Every run
    path passes it through _this_run_forecast_usd; None is left only for
    direct calls, and then every line below is exactly as it was.
    Two things a rerun of a used folder still pays are then always priced,
    because an earlier run or dry run may have left a resolution.json behind:
    the per-name targets of a step that is not finished, and every G2 name
    lookup (subjects.resolve_g2 looks each name up again on every real run).
    In a new folder both are priced anyway, so the figure is the same.

    Calls each stage's build(cfg, workdir, test=False) to get the item count
    (reads config + local files only; no network).  For B2b specifically:
    when review_targets.json is absent (fresh repeat run), falls back to
    n_places * max_reviews_per_place so the forecast is not zero.
    WHY not zero: a zero B2b forecast would make any ceiling look sufficient,
    defeating the preflight check for the biggest single cost source.
    The same rule covers mode-2 B2a (priced on the per-name config main()
    hands it) and App Store / Google Play / G2 names, which are resolved to
    targets only after this gate runs. This number is a spending gate, so it
    errs high: too high costs a slightly higher ceiling, too low makes a
    scheduled run stop partway every month.

    Every step is priced with the small test run it makes first whenever that
    run will be made (_test_pass_usd). The test run and the full run
    are billed separately.
    """
    import stages as ST  # already on sys.path via SCRIPTS
    sources = cfg.get("sources", ["maps"]) or ["maps"]
    mode = SBJ.detect_mode(cfg)
    total = 0.0
    rerun = skip is not None
    skip = set(skip or ())

    if "maps" in sources:
        # Mode 1 skips B2a (SBJ.runs_b2a, the test main() branches on), so
        # B2a adds nothing; B2b below is priced from the supplied URLs.
        b2a_count = 0
        if SBJ.runs_b2a(mode):
            # Mode 2: main() runs B2a on SBJ.build_mode2_cfg (one search per NAME),
            # not on cfg. Price the config B2a actually receives: priced from cfg,
            # three names forecast 25 places while B2a ran 75.
            b2a_cfg = (SBJ.build_mode2_cfg(cfg, cfg.get("competitor_names", []))
                       if mode == "2" else cfg)
            b2a_inp, b2a_count = ST.build_B2a(b2a_cfg, str(workdir), test=False)
            if "B2a" not in skip:
                total += (C.estimate("B2a", b2a_count)[0]
                          + _test_pass_usd("B2a", b2a_cfg, str(workdir), b2a_inp, b2a_count))

        rt_path = Path(workdir) / "B2a_maps_places" / "review_targets.json"
        b2b_test_usd = None  # None: ask the stage builder on the real folder
        if rt_path.exists():
            b2b_inp, b2b_count = ST.build_B2b(cfg, str(workdir), test=False)
        else:
            # Fallback: n_places * max_reviews_per_place
            if mode == "1":
                n_places = len(cfg.get("competitor_place_urls") or [])
            elif mode == "2":
                n_places = len(cfg.get("competitor_names") or [])
            else:  # mode 3: category discovery
                n_places = b2a_count  # upper-bound estimate
            b2b_count = n_places * int(cfg.get("max_reviews_per_place", 20))
            b2b_test_usd = _planned_b2b_test_pass_usd(cfg, n_places, b2b_count)
        if "B2b" not in skip:
            if b2b_test_usd is None:
                b2b_test_usd = _test_pass_usd("B2b", cfg, str(workdir), b2b_inp, b2b_count)
            total += C.estimate("B2b", b2b_count)[0] + b2b_test_usd

    # Sources whose NAMES main() resolves to targets after this gate runs (the
    # stage builders only see ids/urls until <folder>/resolution.json exists).
    # source -> (names key, the builder's per-target cap key in its input).
    _named = {"appstore":   ("appstore_names",   "maxItems"),
              "googleplay": ("googleplay_names", "maxItems"),
              "g2":         ("g2_names",         "maxReviewsPerProduct")}

    for src, stg in (("trustpilot", "TP"), ("appstore", "AS"), ("googleplay", "GP"),
                     ("reddit", "RD"), ("g2", "G2"), ("capterra", "CP")):
        if src in sources:
            build_fn = ST.STAGES[stg]["build"]
            inp, count = build_fn(cfg, str(workdir), test=False)
            planned_cfg = cfg  # what the test run's size and cost are asked of
            planned_test_usd = None  # set below when the names are planned as targets
            if src in _named:
                names_key, cap_key = _named[src]
                names = [n for n in (cfg.get(names_key) or []) if str(n).strip()]
                if src == "g2" and _g2_names_not_looked_up(cfg):
                    names = []  # Not looked up, so not priced
                rp = Path(workdir) / ST.STAGES[stg]["folder"] / "resolution.json"
                if names and (rerun or not rp.exists()):
                    # Each name resolves to at most one target, scraped up to the
                    # builder's own per-target cap. Same rule as B2b's fallback
                    # above: price the planned maximum, never zero.
                    count += len(names) * int(inp.get(cap_key) or 0)
                    planned_cfg = _with_planned_targets(cfg, src, len(names))
                    # The builders prefer a saved resolution.json in the folder over
                    # the settings, and a folder left by an earlier dry run holds one
                    # with the names still pending (no targets): built there, the
                    # planned targets would be ignored and the test run priced as 0
                    # (found by test_w2h2_report_request U-P2). Plan them in an
                    # empty folder instead, where only the settings count.
                    import tempfile
                    with tempfile.TemporaryDirectory() as _planned_dir:
                        inp, _ = build_fn(planned_cfg, _planned_dir, test=False)
                        planned_test_usd = _test_pass_usd(stg, planned_cfg, _planned_dir,
                                                          inp, count)
                    if src == "g2":
                        # Each G2 name first costs a small PAID discover run
                        # (subjects.discover_g2_products: $0.004 per product row,
                        # up to g2_discover_max_products rows; the same figure
                        # resolve_g2's dry-run note shows), plus each lookup
                        # run's own start charge (config.START_EVENTS["G2"]).
                        total += (len(names) * int(cfg.get("g2_discover_max_products", 5))
                                  * 0.004 * C.SAFETY_MARGIN)
                        total += len(names) * C.start_fee_usd("G2")
            # Reddit with no search, community or
            # link in the settings is not run (run_phase._has_inputs, the test the
            # run itself makes: "the Reddit step will be skipped"), so it is not
            # priced. Its builder counts at least one target, which priced a step
            # that never runs into the figure a ceiling is approved on.
            if stg == "RD":
                import run_phase as RP  # already on sys.path via SCRIPTS
                if not RP._has_inputs("RD", inp):
                    continue
            if stg not in skip:
                if planned_test_usd is None:
                    planned_test_usd = _test_pass_usd(stg, planned_cfg, str(workdir), inp, count)
                total += C.estimate(stg, count)[0] + planned_test_usd

    return total


# The paid step of each source, in the order a run takes them.
_PAID_STEPS = (("maps", "B2a"), ("maps", "B2b"), ("trustpilot", "TP"), ("appstore", "AS"),
               ("googleplay", "GP"), ("reddit", "RD"), ("g2", "G2"), ("capterra", "CP"))


def _finished_paid_steps(cfg, workdir):
    """The paid steps of the config's sources that a real run in `workdir` skips.

    A real run skips a step when run_phase's own test says it is finished
    (run_phase._stage_done; this entry script has no --force). This CALLS that
    test rather than repeating it: a second test could disagree with the real
    one, and then the forecast would be wrong in the direction that costs money.
    A G2 name lookup is not a step here: it runs on every real run."""
    import run_phase as RP  # already on sys.path via SCRIPTS
    import stages as ST
    sources = cfg.get("sources", ["maps"]) or ["maps"]
    return [stg for src, stg in _PAID_STEPS
            if src in sources
            and RP._stage_done(Path(workdir) / ST.STAGES[stg]["folder"], ST.STAGES[stg])]


def _paid_steps_pending(cfg, workdir):
    """True when some paid step of the config's sources has no full result on disk.

    WHY: "writing the reports later costs nothing on Apify,
    because the steps that already finished are skipped" is true only when every paid
    step the config asks for is finished. This uses the run's own skip test
    (_finished_paid_steps, which CALLS run_phase._stage_done), so the claim and the
    real skip cannot disagree."""
    sources = cfg.get("sources", ["maps"]) or ["maps"]
    asked = [stg for src, stg in _PAID_STEPS if src in sources]
    done = set(_finished_paid_steps(cfg, workdir))
    return any(stg not in done for stg in asked)


def _forecast_since_approval(job, forecast, ceiling):
    """One sentence for a month refused because its forecast is above the
    ceiling: how the forecast compares with the one `repeat.py schedule`
    recorded when the repeat was approved.

    WHY: "the forecast is above your ceiling" alone reads as if the
    ceiling had been set wrong, when usually the forecast rose since approval.
    Both figures are printed as the forecast is (4 decimals) and compared as
    printed, so the sentence never says "rose" between two equal-looking
    figures.  A job approved before the forecast was recorded with it (or
    when it could not be computed) has none, and then no earlier figure is
    ever stated.  Same words in the three entry scripts."""
    then = job.get("approved_forecast_usd") if isinstance(job, dict) else None
    if isinstance(then, bool) or not isinstance(then, (int, float)) \
            or not (0 <= then < float("inf")):
        return ("No forecast was recorded when this repeat was approved, so whether it has "
                "risen since then is not known.")
    now_t, then_t = f"{forecast:.4f}", f"{then:.4f}"
    if float(now_t) > float(then_t):
        return (f"When you approved this repeat the forecast was ${then_t}; it is "
                f"${now_t} now, so it has risen since then.")
    if then > ceiling:
        return (f"When you approved this repeat the forecast was ${then_t}, already above "
                f"this ceiling.")
    return f"When you approved this repeat the forecast was ${then_t}."



def _this_run_forecast_usd(cfg, workdir):
    """What THIS run can still spend: (usd, the finished paid steps it skips).

    The one computation the dry run, --forecast-only and the standing-approval
    gate all use. WHY one: the dry run was moved to it
    first, and the other two kept the old call, which does not price the G2
    name lookups once an earlier dry run has left a resolution.json behind. In
    that folder the gate forecast $0.0228 for a run that then billed $0.0757.
    A gate that under-states lets an unattended run start that the ceiling
    cannot cover. In a new folder the figure is the same as the old call's."""
    done = _finished_paid_steps(cfg, workdir)
    return _pain_forecast_usd(cfg, workdir, skip=done), done


def _dry_run_whole_forecast(cfg, workdir):
    """The whole-run cost figure a dry-run shows for approval, or the reason
    it could not be computed.

    WHY: the per-stage lines a dry-run prints leave stages out (Trustpilot and
    Reddit print none, and Maps reviews print ~$0.00 before the places are
    known), so a user once approved a run whose largest charge was never shown.
    This calls _pain_forecast_usd, the one function --forecast-only, the
    standing-approval gate and `repeat.py forecast` already use, so the figure
    approved interactively is the same number a monthly ceiling is set from.
    Printing only: nothing here changes what a run spends.

    A folder whose paid steps are already finished (for example
    one that only lacks its written reports) is priced for what THIS run can
    still spend, with the finished steps taken from the real run's own skip
    test (_finished_paid_steps). In a new folder the figure is the one above.
    Returns (usd or None, error text or None, plain names of the skipped steps).
    """
    try:
        usd, done = _this_run_forecast_usd(cfg, workdir)
        return usd, None, [_STAGE_PLAIN.get(s, s) for s in done]
    except Exception as exc:  # noqa: BLE001 - a dry-run must still finish
        return None, str(exc), []


def _print_whole_forecast(usd, err, skipped=()):
    print("\n" + "=" * 60)
    if usd is None:
        print("WHOLE-RUN COST FORECAST: could not be computed "
              f"({err}). Do not approve the run on the per-step figures alone.")
    elif skipped:
        # Only a folder with finished paid steps reaches this; a new folder's
        # lines below stay exactly as they were.
        print("Already collected in this folder, so this run skips them and they cost $0:")
        for name in skipped:
            print(f"  - {name}")
        print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for what this run can still spend.")
        print("  The steps listed above are skipped and are not in this figure (the")
        print("  per-step lines above can still price them). The rest is priced at the")
        print("  largest size the settings allow, so a real run usually costs less.")
        print("  Writing the reports runs on this computer and costs nothing on Apify.")
    else:
        print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for every step together.")
        print("  This is the worst case: every step priced at the largest size the")
        print("  settings allow, so a real run usually costs less. It is the figure to")
        print("  show before approval; the per-step lines above can leave steps out.")
    print("=" * 60)


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] could not parse {p}: {exc}")
        return default


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(
        description=(
            "Pain and messaging pipeline runner.  "
            "Chains subject resolution -> Google Maps places -> "
            "bridge -> Google Maps reviews.  Analysis half (extractor + "
            "pain-points and trust-signals reports) wired in."
        )
    )
    ap.add_argument("--config", default=None,
                    help="Path to PAIN_CONFIG.json (default: auto-detect beside this script)")
    ap.add_argument("--workdir", required=True,
                    help="Working folder for this run; all outputs land here")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan + inputs, spend nothing "
                         "(passes --dry-run to run_phase subprocesses; "
                         "Python steps operate on existing fixtures or skip gracefully)")
    ap.add_argument("--standing-approval", default=None, metavar="JOB_FILE",
                    help="Path to a job file written by `repeat.py schedule`. "
                         "Before spending anything: verifies the config fingerprint "
                         "and the forecast are within the approved ceiling; refuses "
                         "with exit code 3 on any mismatch; writes notification.json.")
    ap.add_argument("--forecast-only", action="store_true",
                    help="Print the cost forecast as JSON and exit 0. "
                         "Calls the same _pain_forecast_usd function the "
                         "standing-approval gate calls. Writes nothing, makes no "
                         "actor calls, needs no job file.")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else C.repo_default_config_path()
    workdir = Path(args.workdir)

    if args.forecast_only:
        _fo_cfg = C.load_config(cfg_path)
        _fo_usd, _ = _this_run_forecast_usd(_fo_cfg, workdir)
        print(json.dumps({"forecast_usd": _fo_usd}))
        sys.exit(0)

    workdir.mkdir(parents=True, exist_ok=True)
    RS.start(workdir, "pain", dry_run=args.dry_run,
             scheduled=bool(args.standing_approval))

    print("=" * 60)
    print("pain_intel: Pain & Messaging Pipeline")
    print("=" * 60)
    if args.dry_run:
        print("[dry-run] No Apify runs, no API calls, $0 spend.")
    sys.stdout.flush()

    # ------------------------------------------------------------------
    # 1. Load config
    # ------------------------------------------------------------------
    cfg = C.load_config(cfg_path)
    mode    = SBJ.detect_mode(cfg)
    sources = cfg.get("sources", ["maps"]) or ["maps"]
    # Computed now, before the dry-run below writes anything into workdir, so
    # it sees the folder exactly as --forecast-only and `repeat.py forecast` do.
    _whole = _dry_run_whole_forecast(cfg, workdir) if args.dry_run else None

    print(f"\nConfig  : {cfg_path}")
    print(f"  vertical  : {cfg.get('vertical', '(none)')}")
    print(f"  city      : {cfg.get('city', '(none)')}")
    print(f"  country   : {cfg.get('country', '(none)')}")
    print(f"  mode      : {mode}  "
          f"({'place URLs supplied' if mode == '1' else 'names supplied' if mode == '2' else 'category discovery'})")
    print(f"  sources   : {', '.join(sources)}")
    print(f"  budget cap: ${B.cap_text(cfg['budget_caps'].get('phase1', 999))}")

    # ------------------------------------------------------------------
    # 1b. Standing-approval preflight (FROZEN).
    #
    # WHY this gate runs before ANY spending: an unattended agent must not
    # be the thing deciding whether it may spend money. The fingerprint
    # comparison and forecast check are the user's approval, executed in
    # code, spending nothing.  On any mismatch: exit 3 (refused), zero
    # actor runs, notification.json written.
    # ------------------------------------------------------------------
    _ceiling = None       # None = no standing-approval; set below when present
    _ledger_path = None  # Run-level spend ledger; initialised below when ceiling is set
    _gaps = []           # (plain source name, plain reason) per source that returned nothing
    _CLOSING["gaps"] = _gaps  # the closing lines name them
    _index_failed = False  # The app index could not be written
    _summary_note = None   # The written summary is missing (a note, not a gap)

    if args.standing_approval:
        _job_path = Path(args.standing_approval)
        if not _job_path.exists():
            print(f"[ERROR] --standing-approval file not found: {_job_path}")
            _write_notification(workdir, "refused",
                                f"Job file not found: {_job_path.name}")
            sys.exit(3)
        try:
            _job = json.loads(_job_path.read_text(encoding="utf-8"))
        except Exception as _e:
            print(f"[ERROR] Could not read job file: {_e}")
            _write_notification(workdir, "refused", "Could not read job file.")
            sys.exit(3)

        if _job.get("tool") != "pain":
            print(f"[ERROR] Job is for tool '{_job.get('tool')}', not 'pain'.")
            _write_notification(workdir, "refused",
                                f"Job tool mismatch: expected 'pain', got '{_job.get('tool')}'.")
            sys.exit(3)

        # Recompute fingerprint (same logic as repeat.py cmd_fingerprint).
        # WHY raw JSON not cfg: C.load_config() normalises the dict (adds default
        # fields such as "vertical"), which would shift the hash vs what repeat.py
        # wrote at schedule time.  Read the raw bytes so both sides hash the same
        # representation.
        _raw_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        _filtered = {k: v for k, v in _raw_cfg.items()
                     if k not in _FINGERPRINT_EXCLUDE and not k.startswith("_")}
        _canonical = json.dumps(_filtered, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False)
        _fp = hashlib.sha256(_canonical.encode("utf-8")).hexdigest()
        if _fp != _job.get("fingerprint"):
            print(f"[REFUSED] Config fingerprint mismatch: settings changed since approval.")
            print(f"  approved: {_job.get('fingerprint')}")
            print(f"  current:  {_fp}")
            _write_notification(workdir, "refused",
                                "Config changed since standing approval was recorded.",
                                report=("Config settings changed after standing approval was set.\n"
                                        "Run the tool once manually for a full run, then "
                                        "re-schedule with the new settings."))
            sys.exit(3)

        # A stored ceiling that is not a finite
        # amount of dollars above zero is refused before anything is spent. NaN
        # and Infinity made every "forecast > ceiling" test below False, so the
        # refusal could never fire, and the user read "Ceiling $nan"; text, true
        # or a missing ceiling crashed or read as $0.
        _ceiling = B.usable_usd(_job.get("ceiling"))
        if _ceiling is None:
            # The value as the job file holds it (NaN, Infinity, "abc", true),
            # never Python's spelling of it (nan, True, None).
            if "ceiling" in _job:
                print(f"[REFUSED] The approved ceiling in the job file is "
                      f"{json.dumps(_job.get('ceiling'))}, which is not an amount of "
                      f"dollars above zero. Nothing was spent.")
            else:
                print("[REFUSED] The job file holds no approved ceiling. Nothing was spent.")
            _write_notification(workdir, "refused",
                                "The approved ceiling for this repeat is not an amount of "
                                "dollars, so nothing was run and nothing was spent.",
                                report=("The approved ceiling stored for this repeat is not "
                                        "an amount of dollars above zero, so this month's "
                                        "forecast could not be checked against it. Nothing "
                                        "was spent.\nRe-schedule this repeat with a ceiling "
                                        "in dollars above zero."))
            sys.exit(3)
        _forecast, _ = _this_run_forecast_usd(cfg, workdir)
        print(f"[standing-approval] forecast: ${_forecast:.4f} | ceiling: ${B.cap_text(_ceiling)}")
        if _forecast > _ceiling:
            print(f"[REFUSED] Forecast ${_forecast:.4f} exceeds approved ceiling ${B.cap_text(_ceiling)}.")
            _since = _forecast_since_approval(_job, _forecast, _ceiling)
            _write_notification(workdir, "refused",
                                f"Forecast ${_forecast:.4f} exceeds approved ceiling "
                                f"${B.cap_text(_ceiling)}. {_since}",
                                report=(f"Cost forecast of ${_forecast:.4f} exceeds the approved "
                                        f"ceiling of ${B.cap_text(_ceiling)}. Nothing was "
                                        f"spent. {_since}\n"
                                        f"Run the tool once manually for a full run, then "
                                        f"re-schedule with a higher ceiling."))
            sys.exit(3)

        # Initialise the run-level ledger at 0.  run_phase subprocesses read
        # this file to pick up prior stages' accumulated spend and write back the
        # updated total.  WHY we own the create-and-zero step: the entry script
        # is the only process that knows a new run is starting; run_phase must not
        # assume a missing ledger means "zero spent" (could be a stale file from
        # a previous run in the same workdir).
        # Removed cfg["budget_caps"]["phase1"] = _ceiling dead code — run_phase
        # reads the config from disk, so an in-memory dict override has no effect.
        _ledger_path = workdir / "_run_ledger.json"
        _ledger_path.write_text(json.dumps({"spent": 0.0}), encoding="utf-8")
        print(f"[standing-approval] fingerprint OK | proceeding unattended")

    # An interactive run keeps a run ledger too, and ends by
    # printing its whole bill from it (main -> _end_interactive_ledger).
    _start_charge_notes(workdir, args.dry_run)
    _start_interactive_ledger(workdir, _ceiling, args.dry_run)

    # ------------------------------------------------------------------
    # 2-4. Maps acquisition (B2a -> bridge -> B2b).
    #      Runs only when "maps" is in cfg["sources"].
    # ------------------------------------------------------------------
    stage_dir    = workdir / "B2a_maps_places"
    rt_path      = stage_dir / "review_targets.json"
    review_items = []

    if "maps" in sources:
        # ---- 2. Competitor resolution (three modes -> review_targets.json) ---
        print("\n--- Maps Acquisition: Subject Resolution ---")
        stage_dir.mkdir(parents=True, exist_ok=True)

        # Location sanity note (mirrors stages.build_B2a's locationQuery semantics:
        # None => "city, country"; "" => omit; a string => use verbatim). B2a hands
        # locationQuery to the Maps actor, which geocodes it via Nominatim and FAILS
        # THE WHOLE RUN if it is not a single findable place. Surfacing the effective
        # location HERE, at the free gate, lets a region ("the Bay Area") or a list
        # get caught before any paid B2a run instead of after. Mode 1 (place URLs)
        # does not use locationQuery, so this does not apply there.
        if mode != "1":
            _loc = cfg.get("maps_location_query")
            if _loc is None:
                _loc = f'{cfg.get("city","")}, {cfg.get("country","")}'.strip().strip(",").strip()
            if _loc:
                print(f"[maps] Pinning results to: {_loc!r}")
                print("       This must be ONE place Google Maps can find (e.g. "
                      "'Chicago, USA'). A region ('the Bay Area', 'Greater London')")
                print("       or a list ('X or Y') makes the whole Maps run return "
                      "nothing. To let the search terms carry the location instead,")
                print("       set maps_location_query to \"\" (empty).")

        if not SBJ.runs_b2a(mode):
            # Mode 1: Maps place URLs provided directly; B2a is skipped
            urls = cfg.get("competitor_place_urls", [])
            print(f"Mode 1 (place URLs supplied): {len(urls)} URL(s)")
            for u in urls:
                print(f"  {u}")
            if args.dry_run:
                print("[dry-run] would write B2a_maps_places/review_targets.json with these URLs")
                print("[dry-run] the Google Maps places search would be SKIPPED")
            else:
                SBJ.resolve_mode1(cfg, workdir)

        elif mode == "2":
            # Mode 2: business names -> B2a -> best-match selection
            names = cfg.get("competitor_names", [])
            print(f"Mode 2 (names -> Maps): {len(names)} name(s)")
            for n in names:
                print(f"  {n}")

            mode2_cfg      = SBJ.build_mode2_cfg(cfg, names)
            mode2_cfg_path = workdir / "mode2_b2a_config.json"
            _write_json(mode2_cfg_path, mode2_cfg)
            print(f"\nMode-2 Google Maps places config written: {mode2_cfg_path}")
            print(f"  search strings: {mode2_cfg['maps_search_queries']}")

            _rc = _run_phase(1, "B2a", mode2_cfg_path, workdir, dry_run=args.dry_run,
                             run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
            _check_incomplete(_rc, "B2a", _ceiling, workdir)

            if args.dry_run:
                print("\n[dry-run] would call subjects.select_mode2_targets() -> review_targets.json")
                print("          best-match selector picks ONE Maps place per name, "
                      "skips aggregators, keeps ALL names (no review-count cap)")
            elif _did_not_run(workdir, "B2a"):
                # Nothing was searched, so "could not be resolved, verify
                # the name" would be wrong advice; the guard below says what happened.
                print("\n--- Mode-2 best-match selection skipped: the Google Maps places "
                      "step did not finish ---")
            else:
                print("\n--- Mode-2 best-match selection ---")
                results = SBJ.select_mode2_targets(names, stage_dir, workdir)
                print("\nResolution results:")
                for r in results:
                    flag = "  " if r.get("url") else "! "
                    print(f"  {flag}{r['name']!r:32s} -> {r.get('url') or '(unresolved)'!r}")
                    print(f"       {r.get('note', '')}")
                unresolved = [r for r in results if not r.get("url")]
                if unresolved:
                    print(f"\n[warn] {len(unresolved)} name(s) could not be resolved to a Maps place:")
                    for r in unresolved:
                        print(f"   {r['name']!r}: {r.get('note', '')}")
                    print("  Verify the name is findable on Google Maps, or supply "
                          "competitor_place_urls directly (mode 1).")
                    # The other names go on to the review scrape; these
                    # do not. (All names unresolved stops at the guard below.)
                    _no_match = [r["name"] for r in unresolved
                                 if str(r.get("note", "")).startswith(SBJ.NO_PLACE_NOTE)]
                    _no_url = [r["name"] for r in unresolved if r["name"] not in _no_match]
                    _record_partial(_gaps, _STAGE_PLAIN["B2b"], "; ".join(
                        ([f"no place matched {_quoted(_no_match)}"] if _no_match else [])
                        + ([f"no usable place was found for {_quoted(_no_url)}"] if _no_url else []))
                        + " (the others were still searched)")

        else:
            # Mode 3: category + location -> B2a discovery
            queries = cfg.get("maps_search_queries") or \
                      ([f'{cfg.get("vertical","")} {cfg.get("city","")}'.strip()]
                       if cfg.get("vertical") or cfg.get("city") else [])
            print(f"Mode 3 (category discovery): {len(queries)} search query/queries")
            for q in queries:
                print(f"  {q}")
            if not queries:
                print("[warn] No search queries, so the Google Maps places search will find nothing. "
                      "Set maps_search_queries, or vertical+city, in your config.")

            _rc = _run_phase(1, "B2a", cfg_path, workdir, dry_run=args.dry_run,
                             run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
            _check_incomplete(_rc, "B2a", _ceiling, workdir)

            if args.dry_run:
                print("[dry-run] postprocess._pp_b2a would sort places by review count "
                      f"and cap at {cfg.get('review_target_count', 60)} -> review_targets.json")

        # ---- 3. empty corpus guard -------------------------------------------
        if not args.dry_run:
            targets = _read_json(rt_path, []) or []
            _b2a_stop = _stopped_entry(workdir, "B2a") if not targets else None
            if _b2a_stop:
                # The places step itself did not finish, so the causes
                # below (settings) do not apply; say what happened. The message is
                # also the exit text, so the run's status file carries it.
                _why = _places_stopped_lines(_b2a_stop)
                print("\n" + "!" * 60)
                print("! STOPPED: the Google Maps places step did not finish, so no reviews "
                      "were collected.")
                for _l in _why:
                    print("! " + _l)
                print("!" * 60, flush=True)
                sys.exit(" ".join(_why))
            # Category mode only.
            # The bridge for it (postprocess._pp_b2a) lists every place it found in
            # businesses_ranked.json and queues the first review_target_count of them,
            # so places found with nothing queued means the count was 0 when the places
            # step ran. "No Google Maps place was found" was false then. Other modes
            # never write that file (a leftover one is another mode's, and says nothing
            # about this run), so they keep the generic text below. The exit stays what
            # it was: exit 1, the places step already paid for.
            _found = (_read_json(stage_dir / "businesses_ranked.json", [])
                      if not targets and mode == "3" else [])
            if not targets and isinstance(_found, list) and _found:
                for _l in _zero_targets_lines(cfg, len(_found)):
                    print(_l)
                sys.exit(1)
            if not targets:
                print("\n" + "!" * 60)
                print("! STOPPED: no Google Maps place was found, so no reviews were collected.")
                print("! Google Maps reviews would scrape ZERO reviews: the corpus would be empty.")
                print("! Possible causes:")
                # Location is the first thing to suspect when it was set: a region
                # or list geocodes to nothing and empties the whole B2a run.
                _loc = cfg.get("maps_location_query")
                if _loc is None:
                    _loc = f'{cfg.get("city","")}, {cfg.get("country","")}'.strip().strip(",").strip()
                if _loc and mode != "1":
                    print(f"!   location: {_loc!r} may not be a single place Google Maps")
                    print("!             can find; a region or list fails the whole run.")
                    print("!             Try one city (e.g. 'Chicago, USA'), or set")
                    print("!             maps_location_query to \"\" to omit it.")
                print("!   mode 1: check competitor_place_urls are valid Maps URLs")
                print("!   mode 2: check names are findable on Maps; inspect B2a_maps_places/")
                print("!   mode 3: check the Maps places search ran, places were found, postprocess ran")
                print("!" * 60)
                sys.exit(1)
            else:
                print(f"\nreview_targets.json: {len(targets)} place(s) queued for Google Maps reviews")
                for t in targets:
                    print(f"  {t.get('url', '(no url)')}")

        # ---- 4. Maps reviews (B2b) -------------------------------------------
        print("\n--- Google Maps Reviews ---")
        _rc = _run_phase(1, "B2b", cfg_path, workdir, dry_run=args.dry_run,
                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
        _check_incomplete(_rc, "B2b", _ceiling, workdir)

        reviews_path = workdir / "B2b_reviews" / "run_02_full.json"
        if args.dry_run:
            print("[dry-run] would read B2b_reviews/run_02_full.json")
        else:
            review_items = _read_json(reviews_path, []) or []
            print(f"  Loaded {len(review_items)} review record(s)")
            if not review_items and _maintenance_gap(
                    _gaps, workdir, "B2b_reviews", _STAGE_PLAIN["B2b"],
                    "Google Maps returned no reviews for the places you asked for",
                    "Google Maps reviews", "those places have no reviews", "Google Maps"):
                pass
            elif not review_items and not _did_not_run(workdir, "B2b"):
                # A reviews step that failed or hit the budget is named as
                # not finished (_incomplete_lines), never as "no reviews came back".
                print("  [warn] 0 reviews: check that the Google Maps reviews step ran and "
                      "review_targets.json contained valid Maps URLs.")
                _record_gap(_gaps, _STAGE_PLAIN["B2b"],
                            "no reviews came back for the places found")

    # ------------------------------------------------------------------
    # Trustpilot acquisition.
    # Runs only when "trustpilot" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "trustpilot" in sources:
        print("\n--- Trustpilot Acquisition: Domain Validation ---")
        # Validate-only: no name→domain search. Each supplied
        # domain is format-checked + a free reachability/redirect check surfaces a
        # dead domain or a domain redirect before spend.
        # Free, so it runs in dry-run too.
        if not cfg.get("trustpilot_domains"):
            print("  [warn] trustpilot_domains is empty, so the Trustpilot step has no websites to scrape; "
                  "add domains to config before running.")
            _record_gap(_gaps, _STAGE_PLAIN["TP"], "no Trustpilot website is set in the settings")
        else:
            tp_entries = SBJ.resolve_trustpilot(cfg, workdir)
            print(SBJ.format_trustpilot_report(tp_entries))
            # A domain whose check could not be done is never dropped
            # (subjects.resolve_trustpilot uses the earlier answer or the domain
            # as written), so an unusable entry here is a real answer; the ones
            # used without a check this time are named as a note.
            if _lookup_outcome(
                    _gaps, _STAGE_PLAIN["TP"], tp_entries, "url",
                    lambda ins: ("no Trustpilot website in the settings could be used ("
                                 + _quoted(ins) + ")")
                    if not [e for e in tp_entries if e.get("url")]
                    else _quoted(ins) + " could not be used as a Trustpilot website",
                    "Trustpilot"):
                print("  [warn] no valid Trustpilot domain, so the Trustpilot step would scrape nothing.")
            if args.dry_run:
                print("[dry-run] would run the Trustpilot step "
                      "(reusing trustpilot_reviews/resolution.json)")
            else:
                _rc = _run_phase(1, "TP", cfg_path, workdir, dry_run=False,
                                 run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                _check_incomplete(_rc, "TP", _ceiling, workdir)

    # ------------------------------------------------------------------
    # App Store acquisition.
    # Runs only when "appstore" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "appstore" in sources:
        print("\n--- App Store Acquisition: Subject Resolution ---")
        # Resolve names→ids and VALIDATE supplied ids via the free iTunes APIs,
        # BEFORE any spend. Runs in dry-run too (it costs nothing) so the human
        # confirms the exact apps from the evidence, then the real run reuses the
        # persisted resolution. Kills the "wrong/typo'd id scrapes nothing" lie.
        if not (cfg.get("appstore_names") or cfg.get("appstore_app_ids")):
            print("  [warn] neither appstore_names nor appstore_app_ids is set, so "
                  "the App Store step has no apps; add app names or ids to config.")
            _record_gap(_gaps, _STAGE_PLAIN["AS"], "no App Store app is set in the settings")
        else:
            as_entries = SBJ.resolve_appstore(cfg, workdir)
            print(SBJ.format_appstore_report(as_entries, SBJ.store_country(cfg, "appstore")))
            # id non-empty == usable by the stage (see subjects: failures set id="").
            # A lookup that could not be done is named as such, never as
            # "no app matched" (_lookup_outcome).
            _as_any = bool([e for e in as_entries if e.get("id")])
            if _lookup_outcome(
                    _gaps, _STAGE_PLAIN["AS"], as_entries, "id",
                    lambda ins: (f"no App Store app matched {_quoted(ins)}" if _as_any
                                 else f"no App Store app matched the settings ({_quoted(ins)})"),
                    "App Store"):
                print("  [warn] no App Store app resolved, so the App Store step would scrape nothing; "
                      "fix the names/ids above before the real run.")
            if args.dry_run:
                print("[dry-run] would run the App Store step "
                      "(reusing appstore_reviews/resolution.json)")
            else:
                _rc = _run_phase(1, "AS", cfg_path, workdir, dry_run=False,
                                 run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                _check_incomplete(_rc, "AS", _ceiling, workdir)

    # ------------------------------------------------------------------
    # Google Play acquisition.
    # Runs only when "googleplay" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "googleplay" in sources:
        print("\n--- Google Play Acquisition: Subject Resolution ---")
        # Resolve names→packages and VALIDATE supplied packages via the public
        # Play store pages (free), BEFORE any spend. Runs in dry-run too.
        if not (cfg.get("googleplay_names") or cfg.get("googleplay_app_ids")):
            print("  [warn] neither googleplay_names nor googleplay_app_ids is set, so "
                  "the Google Play step has no apps; add app names or packages to config.")
            _record_gap(_gaps, _STAGE_PLAIN["GP"], "no Google Play app is set in the settings")
        else:
            gp_entries = SBJ.resolve_googleplay(cfg, workdir)
            print(SBJ.format_googleplay_report(gp_entries, SBJ.store_country(cfg, "googleplay")))
            # id non-empty == usable by the stage (failures set id=""; the network
            # pass-through keeps its id on purpose).
            _gp_any = bool([e for e in gp_entries if e.get("id")])
            if _lookup_outcome(
                    _gaps, _STAGE_PLAIN["GP"], gp_entries, "id",
                    lambda ins: (f"no Google Play app matched {_quoted(ins)}" if _gp_any
                                 else f"no Google Play app matched the settings ({_quoted(ins)})"),
                    "Google Play"):
                print("  [warn] no Google Play app resolved, so the Google Play step would scrape nothing; "
                      "fix the names/packages above before the real run.")
            if args.dry_run:
                print("[dry-run] would run the Google Play step "
                      "(reusing googleplay_reviews/resolution.json)")
            else:
                _rc = _run_phase(1, "GP", cfg_path, workdir, dry_run=False,
                                 run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                _check_incomplete(_rc, "GP", _ceiling, workdir)

    # ------------------------------------------------------------------
    # 4e. Reddit Acquisition (RD) — the first NO-STAR source.
    #     Runs only when "reddit" is in cfg["sources"]. The scrape collects a
    #     post+comment corpus; postprocess._pp_reddit + banding.py then
    #     synthesize the low/high signal (Component B) and set neutral items
    #     aside — so by the time the analysis half reads the corpus, it looks
    #     like any other star-rated source (frozen spine unchanged).
    # ------------------------------------------------------------------
    if "reddit" in sources:
        print("\n--- Reddit Acquisition: no-star source, banded after scrape ---")
        rd_targets = (cfg.get("reddit_queries") or []) + \
                     (cfg.get("reddit_subreddits") or []) + \
                     (cfg.get("reddit_urls") or [])
        if rd_targets:
            for t in rd_targets:
                print(f"  {t}")
            sub = cfg.get("reddit_subreddit")
            if sub:
                print(f"  (searches limited to r/{str(sub).lstrip('r/')})")
        else:
            print("  [warn] no reddit targets: set reddit_queries, reddit_subreddits, "
                  "or reddit_urls in config before running; the Reddit step will be skipped.")
            _record_gap(_gaps, _STAGE_PLAIN["RD"],
                        "no Reddit search or community is set in the settings")
        if args.dry_run:
            print("[dry-run] would run the Reddit step, then band the corpus")
        else:
            _rc = _run_phase(1, "RD", cfg_path, workdir, dry_run=False,
                             run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
            _check_incomplete(_rc, "RD", _ceiling, workdir)

    # ------------------------------------------------------------------
    # G2 acquisition — first B2B-SaaS source, star-rated.
    # Runs only when "g2" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "g2" in sources:
        print("\n--- G2 Acquisition (G2): Subject Resolution ---")
        # Supplied urls/slugs are format-validated (free). g2_names are resolved
        # to a canonical slug by a small PAID factden discover run — so name
        # discovery happens on the REAL run only (dry-run lists names as pending
        # with their forecast cost, spending nothing). Money-path: discover_g2
        # raises on actor failure and we STOP rather than proceed.
        if not (cfg.get("g2_names") or cfg.get("g2_urls")):
            print("  [warn] neither g2_names nor g2_urls is set, so the G2 step will have "
                  "no products; add product names or G2 URLs/slugs to config.")
            _record_gap(_gaps, _STAGE_PLAIN["G2"], "no G2 product is set in the settings")
        else:
            # With g2_discover_max_products at 0 in a folder
            # whose G2 step already finished, the 0 has nothing to skip. Before,
            # the names were dropped anyway: resolution.json was rewritten to []
            # and the closing lines said G2 "was not run ... nothing was
            # collected" above counts that held G2's reviews. Now the stored
            # products are kept, nothing is looked up, and run_phase skips the
            # finished step as it skips every finished step.
            if _g2_names_not_looked_up(cfg) and _g2_step_finished(workdir):
                print("  the G2 step already finished in this folder, so its stored "
                      "products are kept and nothing is looked up again.")
                if not args.dry_run:
                    _rc = _run_phase(1, "G2", cfg_path, workdir, dry_run=False,
                                     run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                    _check_incomplete(_rc, "G2", _ceiling, workdir)
            else:
                # Under a standing approval each paid name lookup is capped, billed
                # to the run ledger, and a failed or over-budget lookup ends the run
                # INCOMPLETE (see _G2DiscoverGuard). Interactive runs take the
                # original path unchanged: a person is at the gate.
                _g2_guard = (_G2DiscoverGuard(workdir, _ceiling)
                             if _ceiling is not None and not args.dry_run else None)
                _g2_unlooked = _g2_names_not_looked_up(cfg)
                _g2_cfg = cfg
                if _g2_unlooked:
                    print("  your settings ask each G2 name lookup for 0 products "
                          "(g2_discover_max_products), so the names are not looked up and "
                          "nothing is spent on them.")
                    _g2_cfg = {**cfg, "g2_names": []}
                _g2_one = len(_g2_unlooked) == 1
                _g2_unlooked_text = (f"{_quoted(_g2_unlooked)} {'was' if _g2_one else 'were'} not "
                                     f"looked up, because your settings ask each G2 name lookup "
                                     f"for 0 products, so nothing was spent on "
                                     f"{'it' if _g2_one else 'them'}")
                try:
                    if _g2_guard is None:
                        # An interactive lookup is a paid
                        # run too, and it was started with no spending limit. It gets
                        # its own forecast x the ceiling factor as its Apify limit
                        # (_G2LookupLimit); its bill still reaches the run's ledger.
                        g2_entries, g2_cost = SBJ.resolve_g2(_g2_cfg, workdir, discover=not args.dry_run,
                                                             spend_guard=_G2LookupLimit())
                    else:
                        g2_entries, g2_cost = SBJ.resolve_g2(_g2_cfg, workdir, discover=True,
                                                             spend_guard=_g2_guard)
                except _G2Stop as e:
                    print(f"  [STOP] G2 name lookup: {e.detail}")
                    _stop_at_g2_lookup(workdir, _ceiling, e.reason, e.detail, _g2_guard.spent)
                except RuntimeError as e:
                    print(f"  [STOP] G2 name discovery failed: {e}")
                    if _g2_guard is not None:
                        _stop_at_g2_lookup(workdir, _ceiling, _g2_guard.failure_reason(),
                                           e, _g2_guard.spent)
                    print("  Not proceeding to the G2 review scrape. Re-run after checking the "
                          "actor/token; do not retry blindly (money-path).")
                    g2_entries = None
                    # The G2 step never ran, so the closing lines must name it
                    # as stopped with an error, not read its empty result as "it ran, but
                    # nothing usable came back" under "Run complete". Same record, same
                    # writer as a step that fails inside run_phase.
                    if not args.dry_run:
                        import run_phase as _RP  # already on sys.path via SCRIPTS
                        import stages as _ST
                        _RP._note_step_stopped(workdir / _ST.STAGES["G2"]["folder"], "G2",
                                               "step_failed")
                if g2_entries is not None:
                    print(SBJ.format_g2_report(g2_entries))
                    if not args.dry_run and g2_cost:
                        print(f"  (G2 discovery spent ${g2_cost:.3f})")
                    resolved = [e for e in g2_entries if e.get("slug") and not e.get("note")]
                    if not resolved and _g2_unlooked and not g2_entries:
                        # Only names were set, and the setting asks for 0.
                        _record_gap(_gaps, _STAGE_PLAIN["G2"], _NOT_RUN_BY_SETTING_REASON)
                    elif not resolved:
                        print("  [warn] no G2 product resolved, so the G2 review scrape would have no "
                              "products; fix the names/URLs above before proceeding.")
                        _g2_names = [e.get("input", "") for e in g2_entries if e.get("kind") == "name"]
                        _g2_urls = [e.get("input", "") for e in g2_entries if e.get("kind") != "name"]
                        _record_gap(_gaps, _STAGE_PLAIN["G2"], "; ".join(
                            ([f"no product matched {_quoted(_g2_names)}"] if _g2_names else [])
                            + ([f"{_quoted(_g2_urls)} is not a G2 product address"] if _g2_urls else [])
                            + ([_g2_unlooked_text] if _g2_unlooked else [])))
                    elif args.dry_run:
                        print("[dry-run] would run G2 review scrape via run_phase --only G2 "
                              "(reusing g2_reviews/resolution.json)")
                    else:
                        # The gate's ⚠ entries are left out of the review scrape.
                        _g2_lost = [e for e in g2_entries if e not in resolved]
                        if _g2_lost or _g2_unlooked:
                            _g2_names = [e.get("input", "") for e in _g2_lost if e.get("kind") == "name"]
                            _g2_urls = [e.get("input", "") for e in _g2_lost if e.get("kind") != "name"]
                            _record_partial(_gaps, _STAGE_PLAIN["G2"], "; ".join(
                                ([f"no product matched {_quoted(_g2_names)}"] if _g2_names else [])
                                + ([f"{_quoted(_g2_urls)} is not a G2 product address"] if _g2_urls else [])
                                # The names the setting left out are named too.
                                + ([_g2_unlooked_text] if _g2_unlooked else []))
                                + " (the others were still searched)")
                        _rc = _run_phase(1, "G2", cfg_path, workdir, dry_run=False,
                                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                        _check_incomplete(_rc, "G2", _ceiling, workdir)

    # ------------------------------------------------------------------
    # Capterra acquisition — second B2B-SaaS source, star-rated.
    # Runs only when "capterra" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "capterra" in sources:
        print("\n--- Capterra Acquisition: URL Validation ---")
        # Validate-only: Capterra bot-walls a free GET and its
        # actor has no search field, so supplied product URLs are structurally
        # validated and surfaced at the gate — a malformed URL is flagged, not
        # silently dropped at stage-build. Free, runs in dry-run too.
        if not cfg.get("capterra_urls"):
            print("  [warn] capterra_urls is empty, so the Capterra step has no product pages to scrape; "
                  "add Capterra product URLs (/p/<id>/<slug>) to config before running.")
            _record_gap(_gaps, _STAGE_PLAIN["CP"], "no Capterra product page is set in the settings")
        else:
            cp_entries = SBJ.resolve_capterra(cfg, workdir)
            print(SBJ.format_capterra_report(cp_entries))
            if not [e for e in cp_entries if e.get("url")]:
                print("  [warn] no valid Capterra URL, so the Capterra step would scrape nothing.")
                _record_gap(_gaps, _STAGE_PLAIN["CP"],
                            "no Capterra product page in the settings could be used ("
                            + _quoted(e.get("input", "") for e in cp_entries) + ")")
            elif [e for e in cp_entries if not e.get("url")]:
                # Flagged at the gate (⚠) and left out of the scrape.
                _record_partial(_gaps, _STAGE_PLAIN["CP"],
                                _quoted(e.get("input", "") for e in cp_entries if not e.get("url"))
                                + " is not a Capterra product page"
                                " (the others were still searched)")
            if args.dry_run:
                print("[dry-run] would run the Capterra step "
                      "(reusing capterra_reviews/resolution.json)")
            else:
                _rc = _run_phase(1, "CP", cfg_path, workdir, dry_run=False,
                                 run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
                _check_incomplete(_rc, "CP", _ceiling, workdir)

    # Other sources: any source with no scraper wired yet. The v1.1 source set
    # is now built; this guard only fires if a NEW source name is added to
    # KNOWN_SOURCES before its stage exists.
    for src in sources:
        if src not in ("maps", "trustpilot", "appstore", "googleplay", "reddit",
                       "g2", "capterra"):
            print(f"\n[{src}] scraper not built yet; skipping acquisition")

    # ------------------------------------------------------------------
    # 5. Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("ACQUISITION COMPLETE")
    print("=" * 60)
    if "maps" in sources:
        print(f"  Mode              : {mode}")
        if mode == "2":
            names = cfg.get("competitor_names", [])
            print(f"  Competitors named : {len(names)}")
        targets_count = len(_read_json(rt_path, []) or []) if not args.dry_run else "?"
        print(f"  Places queued     : {targets_count}")
        print(f"  Reviews collected : {len(review_items) if not args.dry_run else '?'}")
    print(f"  Sources active    : {', '.join(sources)}")
    print(f"  Outputs in        : {workdir.resolve()}")
    if args.dry_run:
        print("\n  [dry-run] No real data was fetched or charged.")

    # ------------------------------------------------------------------
    # 6. ANALYSIS HALF: load each source corpus, merge in code, extract
    #    deterministic facts, then write the E1/E2 reports. The extractor
    #    counts + sources every pain and trust phrase BEFORE any LLM runs;
    #    the report LLM only clusters, labels, and writes from those facts
    #    + the raw quotes -- it never counts.
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("ANALYSIS HALF (extract -> report)")
    print("=" * 60)
    if args.dry_run:
        print("[dry-run] would load each source corpus and merge before extraction:")
        for src in sources:
            print(f"[dry-run]   [{src}] EXT.load_source_corpus(workdir, {src!r})")
        print("[dry-run] would run EXT.extract_records(merged, ...)  -> synthesis_facts.json")
        print("[dry-run] would run RPT.generate()                    -> E1_pain_points.md")
        print("[dry-run]                                                  E2_trust_signals.md")
        print("[dry-run] (skipped: no real corpus, no LLM call, $0)")
        print("[dry-run] would run IDX.build_index(workdir, cfg)     -> frontend_index.json")
    else:
        # Load each source corpus explicitly (merge in code, not in the LLM).
        # Print a loud warning when an enabled source returns 0 records so the
        # user knows it will appear as N=0 in the report (not silently absent).
        merged = []
        for src in sources:
            records = EXT.load_source_corpus(workdir, src)
            if records:
                print(f"  [{src}] {len(records)} records")
            else:
                print(f"\n{'!'*60}")
                print(f"! WARNING: [{src}] returned 0 records.")
                print(f"!   This source is enabled in cfg but loaded no data.")
                print(f"!   It will appear as N=0 in the report (source_distribution).")
                print(f"!   Check that the scraper ran and its output file exists.")
                print(f"{'!'*60}\n")
                # A gap only when nothing is in this run's results: a repeat that
                # found zero NEW items still holds the carried ones, so a
                # quiet month never lands here.
                _stg = _SOURCE_STAGE.get(src)
                _sname = _STAGE_PLAIN.get(_stg, src)
                if _stg and _did_not_run(workdir, _stg):
                    merged.extend(records)
                    continue  # Named as not finished, not as an empty result
                if _stg in _NOTICE_FOLDER and not any(len(g) == 2 and g[0] == _sname
                                                      for g in _gaps):
                    _maintenance_gap(_gaps, workdir, _NOTICE_FOLDER[_stg], _sname,
                                     f"{_sname} returned nothing for what you asked for",
                                     _sname, f"there are no {_sname}", _sname)
                _record_gap(_gaps, _STAGE_PLAIN.get(_SOURCE_STAGE.get(src), src),
                            "it ran, but nothing usable came back")
            merged.extend(records)
        if not merged:
            print(f"\n{'!'*60}")
            print("! WARNING: ALL enabled sources returned 0 records.")
            print("!   The merged corpus is empty, so no pain or trust phrases can be extracted.")
            print("!   Verify that at least one source ran successfully.")
            print(f"{'!'*60}\n")
        facts_path = EXT.extract_records(merged, workdir, cfg)
        print(f"  facts : {facts_path}")
        try:
            _CLOSING["steps_pending"] = _paid_steps_pending(cfg, workdir)
        except Exception:  # noqa: BLE001  (unknown stays None, which reads as pending)
            _CLOSING["steps_pending"] = None
        reports = RPT.generate(workdir, cfg)
        e1, e2  = reports.get("E1"), reports.get("E2")
        # Why each report is missing, from the reason the report step
        # recorded when it gave up (passed forward, not re-derived here); the
        # fallback is for a report that is absent without a recorded reason.
        _missing = [(_n, RPT.failure_reason(_lbl) or "the report step produced nothing")
                    for _n, _lbl, _p in (("pain-points", "the pain-points report", e1),
                                         ("trust-signals", "the trust-signals report", e2))
                    if not _p]
        _CLOSING["reports_missing"] = _missing
        _why = dict(_missing)
        print(f"  Pain points   : {e1 or '(not generated)'}"
              + (f"  ({_why['pain-points']})" if not e1 else ""))
        print(f"  Trust signals : {e2 or '(not generated)'}"
              + (f"  ({_why['trust-signals']})" if not e2 else ""))
        if not e1 and not e2:
            # "only repeats the free analysis step" is true
            # only when every paid step the config asks for is finished, and none is
            # recorded as stopped part-way (the closing lines name those the same way).
            print("  [note] No reports were written. If the `claude` CLI is not "
                  "installed, install it (or set report_model to a model your CLI "
                  "has) and re-run. The collected reviews and the analysis made from "
                  "them are saved in this folder regardless"
                  + (", so re-running only repeats the free analysis step."
                     if _CLOSING["steps_pending"] is False
                     and not _incomplete_lines(workdir) else
                     ". Re-running may pay for the steps that did not finish, so a dry "
                     "run shows the cost first."))
        # Say so in an unattended run's notification (a note, not a gap:
        # the collected data is intact). E3 is optional by design, so only the
        # two main reports count.
        if not e1 and not e2:
            _summary_note = ("The written summary could not be produced this month; "
                             "the collected reviews are in the results folder.")
        elif not e1 or not e2:
            _summary_note = ("Part of the written summary could not be produced this month; "
                             "the collected reviews are in the results folder.")
        # Normalized index for the Local Viewer. Free, no network; reads only
        # what is already on disk, so it runs even if the report step above
        # produced nothing. Never fail the whole run over the index.
        try:
            IDX.build_index(workdir, cfg)
        except Exception as e:  # noqa: BLE001 - index is a convenience, not the deliverable
            print(f"  [note] frontend_index.json not written ({e}); "
                  "the run's data and reports are unaffected.")
            # Without the index the month is invisible in the app and the
            # next repeat builds on the month before (repeat._run_completed).
            _index_failed = True
    print("=" * 60)
    if args.dry_run:
        _print_whole_forecast(*_whole)

    # ------------------------------------------------------------------
    # Standing-approval: write "done" notification after a complete run.
    # (INCOMPLETE and refused paths already wrote their notifications and
    # exited before reaching this point.)
    # ------------------------------------------------------------------
    if _ceiling is not None:
        # A step that did not finish (its RUN INCOMPLETE block, or a
        # refresh it could not make) ends the run "incomplete", never "done".
        _inc = [] if args.dry_run else _incomplete_lines(workdir)
        if _inc:
            _CLOSING["reached_end"] = True
            _write_incomplete_run_notification(workdir, _ceiling, _inc, _gaps,
                                               index_failed=_index_failed,
                                               summary_note=_summary_note)
            _write_incomplete_marker(workdir, "A step of this scheduled run did not "
                                              "finish; see notification.json.\n")
            sys.exit(4)
        # A dry run fetched nothing, so it has no gaps to report.
        _write_done_notification(workdir, _ceiling, [] if args.dry_run else _gaps,
                                 index_failed=_index_failed, summary_note=_summary_note)
    # The run reached its last step: its closing lines name its gaps.
    _CLOSING["reached_end"] = True


# An attended run stopped at a spending cap leaves the same status file
# a scheduled run leaves (notification.json, status "incomplete").
# WHY: the app, compare.py and repeat.py read that file to know a month holds only part
# of its records. An attended run stopped at the cap wrote none, so nothing could tell,
# and the next full month was compared with it ("329 new"). This is bookkeeping after
# the run has stopped on its own: it changes no paid call, no cap and no moment at
# which the run stops.
_ATTENDED_STOP_LEAD = "Stopped at the spending cap:"


def _cap_stopped(workdir):
    """True when a stage record says a step was stopped at a spending cap: run_phase's
    "budget_stop" (the run reached the cap in budget_caps) or a scraper run that ended
    "at its spending cap" (the client's cap_stop). Other unfinished steps are not
    this; they are named by the closing lines, as before."""
    for p in sorted(Path(workdir).glob("*/" + _RUN_INCOMPLETE_FILE)):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        for e in doc.get("entries") or []:
            if isinstance(e, dict) and (e.get("kind") == "budget_stop"
                                        or (e.get("kind") == "run_ended" and e.get("cap_stop"))):
                return True
    return False


def _attended_status_file(workdir):
    """(path, kind, doc) of the folder's notification.json. kind: None when there is
    no file; "ours" when it is the one an attended cap stop wrote (status
    "incomplete", message opening with _ATTENDED_STOP_LEAD); "other" for anything
    else: a scheduled run's file, or one that cannot be read (doc is None then)."""
    p = Path(workdir) / "notification.json"
    if not p.exists():
        return p, None, None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return p, "other", None
    if (isinstance(doc, dict) and doc.get("status") == "incomplete"
            and str(doc.get("message") or "").startswith(_ATTENDED_STOP_LEAD)):
        return p, "ours", doc
    return p, "other", doc


def _index_written_after(workdir, status_path):
    """True when the folder's frontend_index.json is newer than the status file. WHY: the
    index is the last thing a finished run writes. A scheduled run that stopped because
    the index could not be written has no stage record, and a run by hand that failed to
    write it too would leave the scheduled run's older index in place; the month must
    not be called complete then."""
    try:
        return ((workdir / "frontend_index.json").stat().st_mtime_ns
                > status_path.stat().st_mtime_ns)
    except OSError:
        return False


def _record_attended_stop(workdir):
    """At the end of an attended (interactive) run, keep the folder's status file true.
    workdir None: not an attended run (a dry run, a standing approval, or one that
    ended before it started): nothing to do. Never fails the run.

    (the file is only ever written, replaced or removed here, by these rules):
      - The run was stopped at a cap: write the file, unless the folder already holds
        a file this function did not write (a scheduled run's). That one is left as it
        is and the run says so. WHY: it is that run's own record, and the month is
        partial either way.
      - The run did not stop at a cap and did NOT reach the end of the work with no
        step left unfinished (_CLOSING["reached_end"] false, or any stage record left):
        the file is kept. WHY: a step that failed, or a Ctrl-C, ends the attempt with
        the month still partial; the cap stop it recorded is still the truth.
      - The run reached its end with no step left: the month is complete. Our own file
        is removed. A scheduled run's "incomplete" file would keep this month out of
        every baseline for good, so, once the month's index exists, it is replaced by a
        "done" file that keeps the scheduled run's message and report as history, and
        the scheduled run's INCOMPLETE marker (read only by repeat.py) is removed."""
    if workdir is None:
        return
    try:
        p, kind, doc = _attended_status_file(workdir)
        if _cap_stopped(workdir):
            if kind == "other":
                print("[note] This folder already holds a status file written by a "
                      "scheduled run, so it was left as it was.", flush=True)
                return
            inc = _incomplete_lines(workdir)
            names = ", ".join(dict.fromkeys(n for n, _ in inc)) or "a step"
            _write_notification(
                workdir, "incomplete",
                f"{_ATTENDED_STOP_LEAD} part of this run did not finish ({names}).",
                report="\n".join(_incomplete_report_lines(inc, scheduled=False)))
            return
        if (not _CLOSING["reached_end"] or _incomplete_lines(workdir)
                or _kept_lines(workdir)):
            return  # A plain re-run changes no step, so it leaves the status file
        if kind == "ours":
            p.unlink()
        elif (kind == "other" and isinstance(doc, dict)
                and doc.get("status") == "incomplete"
                and _index_written_after(Path(workdir), p)):
            was = str(doc.get("message") or "").strip()
            history = "\n".join(x for x in (was, str(doc.get("report") or "").strip()) if x)
            _write_notification(
                workdir, "done",
                "Run complete. A run by hand finished what the scheduled run left undone.",
                report=("A run by hand finished the work this month's scheduled run left "
                        "undone, so every step of this month has now finished.\n"
                        "What the scheduled run said when it stopped:\n" + history))
            # The scheduled run's other stop marker; repeat.py skips a month holding it.
            (Path(workdir) / "INCOMPLETE").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - bookkeeping, never the run's outcome
        print(f"[note] could not update notification.json after the run ({e}).", flush=True)


def main():
    """Run the pipeline. An interactive run ends with its whole bill.

    However the run ends (finished, refused, stopped, an error),
    its status file records how, with the closing lines, so a Claude waiting on a
    run it started in the background learns that it ended (run_status.py wait)."""
    code, error = 0, None
    try:
        _main()
    except SystemExit as e:
        code, error = RS.exit_of(e.code)  # a message exits 1; its text is the error
        raise
    except BaseException as e:
        code, error = 1, f"{type(e).__name__}: {e}"
        raise
    finally:
        _record_attended_stop(_INTERACTIVE_LEDGER_WORKDIR)
        closing = _end_interactive_ledger()
        RS.finish(code, reached_end=_CLOSING["reached_end"], closing=closing, error=error)


if __name__ == "__main__":
    main()
