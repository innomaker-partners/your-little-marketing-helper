#!/usr/bin/env python3
"""
ad_intel.py — top-level entry point for the ad-intelligence tool.

Chains all pipeline steps for a single run:
  1. Config load
  2. Resolve → validate → confirm: every supplied competitor (name OR domain,
     any case) is resolved via SERP to a verified domain + Facebook Page + full
     LinkedIn name; a resolution report is shown; the run STOPS for confirmation
     before any paid stage (seed_terms instead → discovery, then stop to verify)
  3. Meta ad scrape (B4) — by advertiser Page (or explicit keyword sweep)
  4. Meta creative download (immediate — fbcdn.net URLs expire)
  5. Advertiser curation pass (surface unknown advertisers, non-blocking)
  6. Google ad scrape (B5) — by verified domain
  7. Google creative download
  8. OCR (credential-gated, graceful skip)
  9. LinkedIn ad scrape (LI) — by full company name, worldwide
 10. Summary

Usage:
    python3 ad_intel.py --config AD_INTEL_CONFIG.json --workdir <run> [--dry-run]
    # first run resolves and stops at the confirmation gate; re-run (or pass
    # --auto-confirm) to proceed to the paid scrapes with the reviewed targets.

Stdlib only. No pip install needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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
import config as C      # noqa: E402
import resolver         # noqa: E402
import resolve as R     # noqa: E402  (unified resolve→validate→confirm layer)
import creatives        # noqa: E402
import emit_index        # noqa: E402  (normalized frontend_index.json for the viewer)
import postprocess as _PP  # noqa: E402  (the one place the unfinished-step sentences are made)
import ocr as OCR       # noqa: E402
import selector         # noqa: E402
import e3_report        # noqa: E402
import stages as ST     # noqa: E402  (meta_search_words: the one reading of the words)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

# Keys excluded from the config fingerprint — must match repeat.py's constant
# (same file in all 3 skills; fingerprint must be reproducible by any of them).
_FINGERPRINT_EXCLUDE = frozenset(("subject", "repeat_of", "budget_caps",
                                  "resolution_confirmed"))


def _run_phase(phase, stage, cfg_path, workdir, dry_run, run_ceiling_usd=None,
               run_ledger_path=None):
    """
    Invoke run_phase.py for one stage. Streams output live.
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
_RECORD_NOUN = ("ad", "ads")
_SOURCE_NAMES = {"meta": "Meta", "google": "Google", "linkedin": "LinkedIn"}


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

    WHY it counts what the app shows: the viewer's run_size
    leaves out ads the user moved to the trash. A re-run of a folder the user has
    already looked at would otherwise state one number here and another there.
    Those ads are named with a plain reason and not counted, as run_size does.

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
    in_trash = 0
    for r in recs:
        if not isinstance(r, dict):
            continue
        if trashed.get(r.get("uid")):
            in_trash += 1
            continue
        k = r.get("platform") or ""
        by_source[k] = by_source.get(k, 0) + 1
    n = sum(by_source.values())
    if not (n or in_trash):
        return "", ""
    parts = ", ".join(f"{_SOURCE_NAMES.get(k, k.capitalize() or 'no source')} {v}"
                      for k, v in sorted(by_source.items()))
    out = (f" The results in this folder hold {n} "
           f"{_RECORD_NOUN[0] if n == 1 else _RECORD_NOUN[1]}" + (f": {parts}." if parts else "."))
    not_counted = ""
    if in_trash:
        not_counted = (f"{in_trash} {_RECORD_NOUN[0] if in_trash == 1 else _RECORD_NOUN[1]} "
                       f"you moved to the trash {'is' if in_trash == 1 else 'are'} not counted.")
    return out, not_counted


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
_CLOSING = {"gaps": None, "reached_end": False}


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
    "B1": "Google search for competitors",
    "B4": "Meta ad library",
    "B5": "Google ads",
    "LI": "LinkedIn ad library",
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


def _record_gap(gaps, name, reason):
    """Record that an ACTIVE source returned nothing this run, and why.

    Called at the point where the run decides to skip a source or to carry on
    with nothing from it, because that is where the reason is known. WHY not
    re-derived later from the run folder: the upstream step already knows what
    happened; re-detecting it from folder contents is how silent zeros came back
    before. One entry per source (the first, most specific reason wins).
    name: a plain source name (_STAGE_PLAIN); reason: plain words, no full stop.
    """
    if any(len(g) == 2 and g[0] == name for g in gaps):
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
    if any(len(g) == 3 and g[0] == name for g in gaps):
        return
    gaps.append((name, str(reason).replace("—", "-").replace("–", "-"), "partial"))


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
    do not open with "Run complete"."""
    whole = [(g[0], g[1]) for g in gaps if len(g) == 2]
    part = [(g[0], g[1]) for g in gaps if len(g) == 3]
    lines = []
    if whole:
        lines.append("Run complete, but these sources returned nothing this month:" if complete
                     else "These sources also returned nothing this month:")
        lines += [f"- {n}: {r}." for n, r in whole]
    if part:
        lines.append("Some of what you asked for is also missing this month:"
                     if whole or not complete
                     else "Run complete, but some of what you asked for is missing this month:")
        lines += [f"- {n}: {r}." for n, r in part]
    return lines


def _aside_message(cands, aside_note, tail):
    """Put the Google set-aside sentence into a notification message.
    cands are the messages without it (each ending in `tail`), most specific
    first. The message is chosen exactly as before (the first that fits in 200
    characters), so what it says about missing sources never gets less specific;
    then the full sentence is added when it still fits, else a short form that
    keeps the number ("20 Google ads were set aside."), else nothing (the report
    always carries the full sentence)."""
    pick = next((c for c in cands if len(c) <= 200), cands[-1])
    if not aside_note:
        return pick
    short = aside_note.split(" because ")[0].rstrip(".;") + "."
    body = pick[:-len(tail)] if tail and pick.endswith(tail) else pick
    end = tail if tail and pick.endswith(tail) else ""
    return next((m for m in (f"{body} {aside_note}{end}", f"{body} {short}{end}")
                 if len(m) <= 200), pick)


def _write_done_notification(workdir, ceiling, gaps, index_failed=False, summary_note=None,
                             aside_note="", key_warnings=()):
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
        report only; it is a note, not a source gap);
      - aside_note: Google ads were set aside as other advertisers'
      (resolve.google_set_aside_sentence). It is not a gap (the stage ran and
        nothing is missing), but the Google count the user reads is smaller than
        what was found, so the message and the report say so.
      - key_warnings: a "google_advertisers" key in competitors/resolution.json
        that the tool does not read (_key_warnings), in the report only.
    Never says "All stages ran successfully" unless all of these are empty,
    except aside_note and key_warnings, which leave that sentence true and are
    said after it.
    """
    cost_line = _total_billed_line(workdir)
    tail = f" Ceiling ${B.cap_text(ceiling)}."
    where = f"Results are in: {Path(workdir).name}"
    whole = [(g[0], g[1]) for g in gaps if len(g) == 2]
    part = [(g[0], g[1]) for g in gaps if len(g) == 3]
    if not whole and not part and not index_failed and not summary_note:
        _write_notification(workdir, "done",
                            _aside_message([f"Run complete. Ceiling ${B.cap_text(ceiling)}."],
                                           aside_note, tail),
                            report=(f"All steps ran successfully.\n"
                                    + (f"{aside_note}\n" if aside_note else "")
                                    + "".join(f"{w}\n" for w in key_warnings)
                                    + f"{cost_line}\n"
                                    f"{where}"))
        return

    # Message: the most specific form that fits in 200 characters.
    names = list(dict.fromkeys([n for n, _ in whole] + [n for n, _ in part]))
    if whole and not part:
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
    msg = _aside_message(cands, aside_note, tail)

    lines = _gap_lines(gaps)
    if not whole and not part:
        lines.append("Run complete. No source is missing this month.")
    if aside_note:
        lines.append(aside_note)
    lines += list(key_warnings)
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


# The file run_phase._stage_done tests. A step whose folder holds it
# is skipped by a run of the same command, whatever the latest attempt did.
_FINISHED_RESULT_FILE = "run_02_full.json"


def _earlier_result_reason(e):
    """The record entry of a step that failed or was stopped at the budget cap, for a
    step whose folder STILL HOLDS a finished result (the same function as in
    search_intel.py and pain_intel.py).

    WHY: _incomplete_reason tells the user to run the same command on the folder and says
    the steps that finished are skipped. That is the remedy for a step with no finished
    result. For a step that holds one, run_phase skips it (_stage_done) and keeps the
    result, so the advice promised a fix the command cannot make. This entry script has
    no way to run one step again (its --force only re-resolves the competitors), so a new
    run folder is the way. The record says whether the attempt saved the result before it
    stopped (result_saved), so the line says whose result it is. "Its latest
    attempt" is true whichever command reads the record."""
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
    budget cap (run_phase._note_step_stopped). Its empty result then says
    nothing about the competitors, so no reason for a zero is recorded for it: the
    closing lines name the step as not finished instead (_incomplete_lines).
    A step its settings asked for 0 results was not run either; the
    closing lines name it with that reason (_name_not_run_by_setting)."""
    if _not_run_by_setting(workdir, stage):
        return True
    p = Path(workdir) / ST.STAGES[stage]["folder"] / _RUN_INCOMPLETE_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and any(
        isinstance(e, dict) and e.get("kind") in ("step_failed", "budget_stop",
                                                  "kept_after_failed_attempt")
        for e in (doc.get("entries") or []))


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


def _key_warnings(workdir):
    """The plain warnings for a "google_advertisers" key in this run's
    competitors/resolution.json that the tool does not read ([] when none).

    WHY: the index builder prints these on stderr, which a
    run started by hand shows. A scheduled month runs unattended: repeat.py
    run-scheduled inherits stderr and never reads it, and passes on only
    notification.json. resolution.json is carried forward into every month, so a
    misplaced key stayed silent every month. The same pure function as the index
    builder (resolve.google_advertisers_misplaced), on the same file, so the
    report warns exactly when a rebuild by hand warns. It only reads a local file:
    it runs after every paid step and changes no call and no set-aside ad. It
    never stops the run: a file it cannot read gives no warning here, as before.

    The report is read by the user, not by Claude, so it
    says in plain words which competitor and what to ask for, one sentence per
    competitor. The exact place stays in the index builder's warning, which
    viewer_ctl.py reindex hands to Claude as "warnings"."""
    try:
        entries = _read_json(Path(workdir) / "competitors" / "resolution.json", []) or []
        if not isinstance(entries, list):
            return [_KEY_WARNING_FILE] if R.google_advertisers_misplaced(entries) else []
        out = []
        for e in entries:
            # One entry at a time: google_advertisers_misplaced names the entry's
            # competitor the same way (its "input", else its website).
            if isinstance(e, dict) and R.google_advertisers_misplaced([e]):
                dom = e.get("domain") if isinstance(e.get("domain"), dict) else {}
                line = _KEY_WARNING.format(
                    who=str(e.get("input") or dom.get("use") or "a competitor"))
                if line not in out:
                    out.append(line)
        return out
    except Exception:  # noqa: BLE001 - a warning must never cost the month its notification
        # WHY: this runs after every paid step. On Python 3.14 a resolution.json nested
        # about 2000 deep loads, and google_advertisers_misplaced then raises
        # RecursionError; without this, the month would end with no notification after
        # the user paid for it.
        return []


# The plain sentences of _key_warnings. "the app view" and
# "that competitor's Google advertisers" are the words resolve.GOOGLE_CONFIRM_HOW uses.
_KEY_WARNING = (
    "The Google advertiser names added for {who} are written in a place or a form the "
    "tool does not read, so they were not used. Ask Claude to put them in that "
    "competitor's Google advertisers and rebuild the app view for this run (free, "
    "nothing is scraped).")
_KEY_WARNING_FILE = (
    "The competitor list of this run is not written in the form the tool reads, so the "
    "Google advertiser names in it were not used. Ask Claude to correct the competitor "
    "list and rebuild the app view for this run (free, nothing is scraped).")


def _write_incomplete_run_notification(workdir, ceiling, inc, gaps, index_failed=False,
                                       summary_note=None, aside_note="", key_warnings=()):
    """The notification of an unattended run that reached its end but did not
    finish everything: status "incomplete", never "done", and never
    the words "Run complete". aside_note: the Google set-aside
    sentence, in the report (the message names what did not finish).
    key_warnings: as in _write_done_notification, in the report."""
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
    if aside_note:
        lines.append(aside_note)
    lines += list(key_warnings)
    if index_failed:
        lines.append(_index_failed_report(workdir))
    if summary_note:
        lines.append(summary_note)
    report = "\n".join(lines) + f"\n{cost_line}\n{where}"
    _write_notification(workdir, "incomplete", msg, report=report)


def _quoted(items):
    """'a', 'b' -- the user's own setting values, quoted, for a gap reason."""
    return ", ".join(f"'{str(x).strip()}'" for x in items)


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


def _test_pass_usd(stg, build_cfg, workdir, full_inp, full_count):
    """What the small test run that run_phase makes BEFORE a step's full run can
    bill (its start fee included), or 0.0 when that run will not be made.

    WHY (the same fix as in the search tool): the full
    run re-sends the test run's own first inputs, so the two are billed
    separately, and the forecast priced only the full run. Measured on the fake
    at the tool's own prices, with every actor returning its full cap: five
    domains with a Google ads limit of 25 forecast $0.0752 and billed $0.1254;
    four LinkedIn companies with a limit of 25 forecast $1.68 and billed $2.45.
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


def _ad_forecast_usd(cfg, workdir, skip=()):
    """Zero-spend forecast: sum of B4, B5, and LI stage estimates, each with the
    small test run it makes first whenever that run will be made (_test_pass_usd).

    skip: the steps this run will skip because they are finished
    (_finished_paid_steps). Those are left out. Every run path passes it through
    _this_run_forecast_usd; with nothing finished it is empty and every line
    below is exactly as it was.

    Calls each stage's build(cfg, workdir, test=False) to get the item count
    (reads config + local files only; no network).  resolution.json must be
    present (copied by prepare) for accurate B4/B5/LI counts.
    """
    import stages as ST  # already on sys.path via SCRIPTS
    total = 0.0
    # Only the ad searches the run makes are priced: the config's `stages`
    # allow-list (stages.selected_stages; None = all) decides, as in the run.
    # The run used to ignore it, so a Meta-only run still paid for
    # Google and LinkedIn and the forecast priced all three.
    sel = ST.selected_stages(cfg)
    for stg in ("B4", "B5", "LI"):
        if sel is not None and stg not in sel:
            continue
        # The run skips LinkedIn when
        # include_linkedin is false ("Skipped: include_linkedin is false."), so it
        # is not priced either. Priced, it raised the figure a monthly ceiling is
        # approved or refused on with a step that never runs ($0.594 with
        # LinkedIn on or off).
        if stg == "LI" and not cfg.get("include_linkedin"):
            continue
        if stg in skip:
            continue
        inp, count = ST.STAGES[stg]["build"](cfg, str(workdir), test=False)
        # One start charge per actor run, as run_phase prices it.
        total += (C.estimate(stg, count, runs=C.actor_runs(ST.STAGES[stg], inp))[0]
                  + _test_pass_usd(stg, cfg, workdir, inp, count))
    return total


def _finished_paid_steps(cfg, workdir):
    """The paid ad searches (B4, B5, LI) a real run in `workdir` skips, in run order.

    A real run skips a step when run_phase's own test says it is finished
    (run_phase._stage_done; ad_intel never gives run_phase --force, so --force
    here only re-resolves the competitors and does not run a finished step
    again). This CALLS that test rather than repeating it: a second test could
    disagree with the real one, and then the forecast would be wrong in the
    direction that costs money. Only a step the forecast prices is listed: one
    the config's `stages` list leaves out, or LinkedIn with include_linkedin
    off, is not run at all and is not priced either."""
    import run_phase as RP  # already on sys.path via SCRIPTS
    import stages as ST
    sel = ST.selected_stages(cfg)
    return [stg for stg in ("B4", "B5", "LI")
            if (sel is None or stg in sel)
            and not (stg == "LI" and not cfg.get("include_linkedin"))
            and RP._stage_done(Path(workdir) / ST.STAGES[stg]["folder"], ST.STAGES[stg])]


def _this_run_forecast_usd(cfg, workdir):
    """What THIS run can still spend: (usd, the finished paid steps it skips).

    The one computation the dry run, --forecast-only and the standing-approval
    gate all use (as pain and search already do), so the
    three can never disagree about a folder. With no step finished the figure is
    the planned maximum, exactly as before."""
    done = _finished_paid_steps(cfg, workdir)
    return _ad_forecast_usd(cfg, workdir, skip=done), done


def _print_whole_forecast(cfg, workdir, resolution_reused):
    """Print the whole-run cost figure a dry-run shows for approval.

    WHY: the per-stage lines a dry-run prints are not one figure the user can
    approve, and they can leave stages out. When the confirmed competitor
    lookup is reused, this prints _ad_forecast_usd, the one function
    --forecast-only, the standing-approval gate and `repeat.py forecast`
    already use, so the figure approved interactively is the same number a
    monthly ceiling is set from. When the competitors still have to be looked
    up, the ad searches cannot be priced yet (they depend on what the lookup
    finds), and the real run stops after the lookup for confirmation, so it
    says that instead of printing a figure that would be too low.
    Printing only: nothing here changes what a run spends.
    """
    print("\n" + "=" * 60)
    if not resolution_reused:
        print("WHOLE-RUN COST FORECAST: not available yet. The competitors are looked")
        print("  up first (that lookup's cost is shown above), and the run stops after")
        print("  the lookup so you can confirm them. A dry-run after that confirmation")
        print("  shows the whole-run figure for the ad searches.")
    else:
        done = []
        try:
            usd, done = _this_run_forecast_usd(cfg, workdir)
        except Exception as exc:  # noqa: BLE001 - a dry-run must still finish
            usd = None
            print(f"WHOLE-RUN COST FORECAST: could not be computed ({exc}). "
                  "Do not approve the run on the per-step figures alone.")
        if usd is not None and done:
            # Only a folder with finished paid steps reaches this; a new folder's
            # lines below stay exactly as they were (as pain and search).
            print("Already collected in this folder, so this run skips them and they cost $0:")
            for name in [_STAGE_PLAIN.get(s, s) for s in done]:
                print(f"  - {name}")
            print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for what this run can still spend.")
            print("  The steps listed above are skipped and are not in this figure (the")
            print("  per-step lines above can still price them). The rest is priced at the")
            print("  largest size the settings allow, so a real run usually costs less.")
            print("  Writing the report runs on this computer and costs nothing on Apify.")
        elif usd is not None:
            print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for every step together.")
            print("  This is the worst case: every step priced at the largest size the")
            print("  settings allow, so a real run usually costs less. It is the figure to")
            print("  show before approval; the per-step lines above can leave steps out.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_domain(d):
    """Strip scheme, www, and path — return just the host."""
    if not d:
        return ""
    return (d.replace("https://", "").replace("http://", "")
             .replace("www.", "").rstrip("/").split("/")[0])


# ---------------------------------------------------------------------------
# Advertiser curation pass (surface-and-instruct, never blocks)
# ---------------------------------------------------------------------------

def _count_creatives(tally, manifest):
    """Add one platform's manifest to [slots, failed]."""
    for v in (manifest or {}).values():
        for s in v:
            tally[0] += 1
            if str(s).startswith("FAILED"):
                tally[1] += 1


def _read_checklist(md_path):
    """(text, readable) of an existing DISCOVERED_advertisers.md; ("", True) when
    there is none.

    WHY: the user reviews this file in their own editor. Windows Notepad can save
    it as UTF-16, and some editors write a UTF-8 byte-order mark; both are read in
    full. Anything else (Latin-1, say) is read with the bytes it cannot decode
    replaced, so the ticks, which are matched by page id (digits), still carry
    over, and readable is False: the caller then keeps the user's file as it was
    before rewriting the checklist. Before, such a file stopped the whole month."""
    try:
        raw = Path(md_path).read_bytes()
    except FileNotFoundError:
        return "", True
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), True
        except UnicodeDecodeError:
            pass
    else:
        try:
            return raw.decode("utf-8-sig"), True
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), False


def _keep_user_copy(md_path):
    """Copy the user's checklist, byte for byte, to a name beside it that is not
    taken yet, and return that path. Never overwrites an earlier copy."""
    md_path = Path(md_path)
    n = 1
    while True:
        tail = "" if n == 1 else f"-{n}"
        copy = md_path.with_name(f"{md_path.stem}.your-earlier-copy{tail}{md_path.suffix}")
        if not copy.exists():
            shutil.copy2(md_path, copy)
            return copy
        n += 1


def _ticked_rows(text):
    """{page_id: page name} for every row the user ticked ([x]) in the text of an
    existing DISCOVERED_advertisers.md, {} when there is none. WHY: the checklist is the user's
    own review work; a re-run of
    the same folder rewrote it with every row unticked."""
    ticked = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        if cells[-1].lower() == "[x]" and cells[1]:
            ticked[cells[1]] = " | ".join(cells[2:-1])
    return ticked


def _curation_pass(meta_items, supplied_competitor_set, workdir):
    """
    Collect distinct advertisers from Meta records. Diff against the supplied
    competitor set (rough name-overlap heuristic). Write DISCOVERED_advertisers.md
    as a human-review checklist.

    Field assumptions [ASSUMED from curious_coder/facebook-ads-library-scraper]:
      - record["page_id"]                 — top-level advertiser page ID
      - record["snapshot"]["page_name"]   — human-readable advertiser name
    Both observed in test run output. Mark [VERIFY] on first live run.
    """
    seen_ids: set[str] = set()
    discovered: list[dict] = []

    # Build a token set from supplied domains for the heuristic overlap check.
    supplied_tokens = {_bare_domain(d).lower().replace("-", "").replace(".", "")
                       for d in supplied_competitor_set if d}

    for rec in meta_items:
        if not isinstance(rec, dict):
            continue
        page_id = str(rec.get("page_id") or "")          # [ASSUMED]
        snap = rec.get("snapshot") or {}
        page_name = (snap.get("page_name") or "").strip() # [ASSUMED]

        if not page_id or page_id in seen_ids:
            continue
        seen_ids.add(page_id)

        # Heuristic: skip if page_name tokens overlap a supplied domain token.
        # Non-blocking — user confirms via the checklist regardless.
        name_tok = page_name.lower().replace(" ", "").replace("-", "").replace(".", "")
        known = any(tok and tok in name_tok for tok in supplied_tokens)

        if not known:
            discovered.append({"page_id": page_id, "page_name": page_name})

    # Write the review checklist
    md_path = Path(workdir) / "competitors" / "DISCOVERED_advertisers.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    # The user's ticks survive a re-run, matched by page id. A ticked
    # page this run did not find at all is kept as a ticked row; one this run
    # found but now counts as already in the list is not repeated.
    text, readable = _read_checklist(md_path)
    ticked = _ticked_rows(text)
    carried = [(pid, name) for pid, name in ticked.items() if pid not in seen_ids]
    # A checklist this run cannot read in full is kept, unchanged,
    # beside the new one before anything is rewritten.
    user_copy = _keep_user_copy(md_path) if not readable else None

    lines = [
        "# Discovered Meta Advertisers: Review Checklist",
        "",
        "Advertisers found in the Meta Ad Library scrape that are **NOT** in your",
        "competitor list. Review each row: tick the real competitors, add their domains",
        "to `competitor_domains` in your config, then re-run to pick up their full ad history.",
        "",
        "| # | Page ID | Page Name | Add to competitor list? |",
        "|---|---------|-----------|------------------------|",
    ]
    for i, d in enumerate(discovered, 1):
        name = d["page_name"] or "(no name)"
        box = "[x]" if d["page_id"] in ticked else "[ ]"
        lines.append(f"| {i} | {d['page_id']} | {name} | {box} |")
    for i, (pid, name) in enumerate(carried, len(discovered) + 1):
        lines.append(f"| {i} | {pid} | {name or '(no name)'} | [x] |")

    lines += [
        "",
        f"Total distinct advertisers in scrape: {len(seen_ids)}",
        f"Already in your list (approx, heuristic): {len(seen_ids) - len(discovered)}",
        f"Surfaced for review: {len(discovered)}",
    ]
    if carried:
        lines.append(f"Ticked by you earlier but not found in this scrape: {len(carried)}")

    # Written to a temporary file first, so a failed write never leaves the
    # user's checklist cut short.
    tmp_path = md_path.with_name(md_path.name + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, md_path)

    print(f"\n[curation] {len(discovered)} new advertiser(s) surfaced for review -> {md_path}")
    if user_copy is not None:
        print(f"  Your earlier checklist was not saved as UTF-8 or UTF-16 text, so it could "
              f"not be read in full. It is kept unchanged as {user_copy.name}; "
              f"{len(ticked)} ticked row(s) were carried over by page id. Check that "
              f"copy for anything else you wrote in it.")
    if discovered:
        print("  Add confirmed competitors to competitor_domains in your config and re-run.")

    return discovered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(
        description="Ad-intelligence pipeline runner. "
                    "Chains competitor resolution -> Meta -> Google -> creatives -> OCR."
    )
    ap.add_argument("--config", default=None,
                    help="Path to AD_INTEL_CONFIG.json (default: auto-detect beside this script)")
    ap.add_argument("--workdir", required=True,
                    help="Working folder for this run; all outputs land here")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan + inputs, spend nothing "
                         "(passes --dry-run to all run_phase.py subprocesses; "
                         "Python steps operate on existing fixtures or skip gracefully)")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="Skip the post-resolution confirmation gate and proceed "
                         "straight to the paid ad scrapes. Use only when the "
                         "resolution report has ALREADY been reviewed (e.g. an "
                         "a validation run approved earlier). Default is to STOP after "
                         "resolution so a human can confirm the targets before spend.")
    ap.add_argument("--force", action="store_true",
                    help="Re-resolve even if competitors/resolution.json exists")
    ap.add_argument("--standing-approval", default=None, metavar="JOB_FILE",
                    help="Path to a job file written by `repeat.py schedule`. "
                         "Before spending anything: verifies the config fingerprint "
                         "and the forecast are within the approved ceiling; refuses "
                         "with exit code 3 on any mismatch; writes notification.json.")
    ap.add_argument("--forecast-only", action="store_true",
                    help="Print the cost forecast as JSON and exit 0. "
                         "Calls the same _ad_forecast_usd function the "
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
    RS.start(workdir, "ad", dry_run=args.dry_run,
             scheduled=bool(args.standing_approval))

    print("=" * 60)
    print("ad_intel: Ad Intelligence Pipeline")
    print("=" * 60)
    if args.dry_run:
        print("[dry-run] No Apify runs, no API calls, $0 spend.")
    sys.stdout.flush()

    # ------------------------------------------------------------------
    # 1. Load config
    # ------------------------------------------------------------------
    cfg = C.load_config(cfg_path)
    countries = cfg.get("country_codes") or [cfg["country_code"]]
    print(f"\nConfig : {cfg_path}")
    print(f"  vertical    : {cfg.get('vertical', '(none)')}")
    print(f"  countries   : {countries}")
    if cfg.get("_uk_read_as_gb"):
        # config._read_countries: said once, where the config is shown.
        print("  'uk' in your settings was read as the United Kingdom (gb), the code "
              "the ad libraries use.")
    if cfg.get("_country_listed_twice"):
        # config._read_countries: a repeat is searched, and paid for, once.
        print("  A country listed more than once in your settings is searched once.")
    print(f"  budget cap  :${B.cap_text(cfg['budget_caps'].get('phase1', 999))}")

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

    # The ad searches this run makes: the config's `stages` allow-list (empty =
    # all), the same stages.selected_stages _ad_forecast_usd prices. The list was ignored, so asking
    # for Meta only still ran (and paid for)
    # Google and LinkedIn. A search the settings leave out is not a gap. A name
    # in the list that is no step of this tool runs nothing, so it is named as
    # a gap here, where the config is read (the same words search uses).
    _sel = ST.selected_stages(cfg)

    def _wanted(code):
        return _sel is None or code in _sel

    for _code in (cfg.get("stages") or []):
        if _code not in ST.STAGES:
            _record_gap(_gaps, f"the step '{_code}' in the settings",
                        "this tool has no step with that name, so it never ran")

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

        if _job.get("tool") != "ad":
            print(f"[ERROR] Job is for tool '{_job.get('tool')}', not 'ad'.")
            _write_notification(workdir, "refused",
                                f"Job tool mismatch: expected 'ad', got '{_job.get('tool')}'.")
            sys.exit(3)

        # Recompute fingerprint (same logic as repeat.py cmd_fingerprint).
        # WHY raw JSON not cfg: C.load_config() normalises the dict (adds default
        # fields), which would shift the hash vs what repeat.py wrote at schedule
        # time.  Read the raw bytes so both sides hash the same representation.
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
        # Removed cfg["budget_caps"]["phase1"] = _ceiling dead code -- run_phase
        # reads the config from disk, so an in-memory dict override has no effect.
        _ledger_path = workdir / "_run_ledger.json"
        _ledger_path.write_text(json.dumps({"spent": 0.0}), encoding="utf-8")
        print(f"[standing-approval] fingerprint OK | proceeding unattended")

    # An interactive run keeps a run ledger too, and ends by
    # printing its whole bill from it (main -> _end_interactive_ledger).
    _start_charge_notes(workdir, args.dry_run)
    _start_interactive_ledger(workdir, _ceiling, args.dry_run, args.force)

    # ------------------------------------------------------------------
    # Creative reuse - resolve prev_creatives_dir from repeat_of
    # ------------------------------------------------------------------
    # repeat_of is the folder NAME of the previous run, a sibling of workdir.
    # We do NOT gate on the config fingerprint: a creative's identity is its
    # own id plus URL path, not the settings.  A changed competitor list still
    # shares the unchanged competitors' ads, and the URL path check in
    # creatives.py keeps it correct.
    prev_creatives_dir: str | None = None
    repeat_of = cfg.get("repeat_of")
    if repeat_of:
        candidate = workdir.parent / str(repeat_of)
        if candidate.is_dir():
            prev_creatives_dir = str(candidate / "creatives")
            print(f"  repeat_of   : {repeat_of} (reusing creatives where URL path matches)")
        else:
            print(f"  repeat_of   : {repeat_of} (previous run folder not found, downloading fresh)")

    # ------------------------------------------------------------------
    # 2. Competitor resolution — three modes, priority order
    # ------------------------------------------------------------------
    print("\n--- Competitor Resolution ---")
    comp_dir = workdir / "competitors"
    comp_dir.mkdir(parents=True, exist_ok=True)

    seed_terms = cfg.get("seed_terms", [])
    competitor_set: list[str] = []       # chosen `use` domains, for the curation diff
    entries: list[dict] = []             # resolution entries (identities path)
    resolution_path = comp_dir / "resolution.json"

    # One unified identity list — names AND domains, any case. There is no
    # separate "mode": every supplied competitor is resolved the same way
    # (name/domain/URL → verified domain + Facebook Page + full LinkedIn name),
    # validated, and shown for confirmation before a cent is spent.
    identities = R.build_identities(cfg)
    _resolution_reused = False  # set below; read by the dry-run's whole-run figure

    if identities:
        reused = resolution_path.exists() and not args.force
        # True when the stored resolution covers a DIFFERENT set of competitors
        # than the current config — triggers re-resolution and blocks the old
        # confirmation flags (they were written for the old targets, not these).
        resolution_mismatch = False

        if reused:
            stored = _read_json(resolution_path, []) or []
            match_info = R.check_resolution_match(identities, stored)
            if match_info["match"]:
                entries = stored
                print(f"Reusing resolution for {len(entries)} competitor(s) "
                      f"(competitors/resolution.json exists; pass --force to re-resolve).")
            else:
                resolution_mismatch = True
                reused = False
                print(match_info["notice"])
        _resolution_reused = reused

        # STANDING-APPROVAL CHECK: refuse before any B1 spend if resolution
        # not reused. The approved run had a specific competitor resolution;
        # if competitors re-resolve differently (or no resolution exists yet),
        # this is not the approved run -- exit 3, spend nothing.
        if _ceiling is not None and not reused:
            print("[REFUSED] Competitor list changed or was not pre-resolved. Re-approve.")
            _write_notification(workdir, "refused",
                                "Competitor list changed or was not pre-resolved. Re-approve.",
                                report=("Competitor list changed since standing approval was set, "
                                        "or resolution.json was not present.\n"
                                        "Run the tool once manually to resolve and confirm competitors, "
                                        "then re-schedule with the new settings."))
            sys.exit(3)

        if not reused:
            print(f"Resolving {len(identities)} competitor(s) via SERP: {identities}")
            for _name, _dom in R.supplied_domains(cfg).items():
                print(f"  {_name!r} and {_dom!r} are the same company: one competitor, "
                      f"searched by its name, with {_dom} checked against what the search finds.")
            # Each competitor gets a small query set (bare + site:facebook.com +
            # site:linkedin.com) so the verified domain, the advertiser Facebook
            # Page, and the full LinkedIn name are each surfaced reliably.
            R.write_queries(identities, workdir)
            _rc = _run_phase(1, "B1", cfg_path, workdir, dry_run=args.dry_run,
                             run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
            _check_incomplete(_rc, "B1", _ceiling, workdir)
            if args.dry_run:
                print("[dry-run] would resolve each competitor from SERP → verified "
                      "domain / Facebook Page / full LinkedIn name, then write "
                      "competitors/resolution.json + domains.json and show a "
                      "resolution report at this confirmation gate.")
            else:
                entries = R.resolve_all(identities, workdir, R.supplied_domains(cfg))
                # Fill each entry's LinkedIn NUMERIC company id (a free GET of the
                # public company page) BEFORE writing + reporting. The LI stage
                # scopes only by numeric id; resolving it here keeps the id inside
                # the confirmation gate, so the human sees "[id 68529]" (or the
                # ⚠ no-id warning) and approves the real target before any spend.
                R.enrich_linkedin_ids(entries)
                R.write_resolution(entries, workdir)

        if entries:
            print(R.format_resolution_report(entries, cfg.get("meta_search_terms")))
        competitor_set = [e["domain"]["use"] for e in entries
                          if (e.get("domain") or {}).get("use")]

        # ---- CONFIRMATION GATE — never spend on unconfirmed targets ----
        # When the competitor list changed (resolution_mismatch=True), the stored
        # resolution_confirmed flag and a stale CONFIRMED file cannot count:
        # they were approved for a DIFFERENT set of competitors.  Only an explicit
        # --auto-confirm on this invocation is accepted.
        if resolution_mismatch:
            confirmed = args.auto_confirm
        else:
            confirmed = (args.auto_confirm or reused
                         or bool(cfg.get("resolution_confirmed"))
                         or (comp_dir / "CONFIRMED").exists())
        if not args.dry_run and not confirmed:
            print("\n" + "#" * 64)
            print("# STOP: resolution complete. Confirm the targets before any spend.")
            print("#" * 64)
            print("Review the RESOLUTION REPORT above, especially any ⚠ lines.")
            print("If a target is wrong: edit its `use` value in")
            print("  competitors/resolution.json   (Facebook Page, LinkedIn name)")
            print("  competitors/domains.json      (the Google verified domain)")
            print("then re-run. Re-running REUSES this resolution and proceeds to the")
            print("paid Meta / Google / LinkedIn scrapes. A reviewed, non-interactive")
            print("caller may pass --auto-confirm instead.")
            print("#" * 64)
            return

    elif seed_terms:
        # ---- Seed-term discovery: find unknown competitors, then STOP ----
        # Discovery yields UNVERIFIED domains. Per resolve→validate→confirm they
        # must not drive paid scrapes directly: the user confirms the list, pastes
        # it into competitor_domains, and re-runs — where it becomes an identity
        # set and passes through full resolution + the confirmation gate.

        # Under standing-approval, seed-term discovery mode is not a valid
        # repeat run (it has no resolution to reuse and always stops for human
        # review). Refuse before spending anything.
        if _ceiling is not None:
            print("[REFUSED] Seed-term discovery mode cannot run unattended. "
                  "Re-approve with resolved competitor list.")
            _write_notification(workdir, "refused",
                                "Seed-term discovery mode cannot run unattended. "
                                "Use resolved competitors.",
                                report=("Seed-term discovery always stops for human review "
                                        "and cannot run unattended.\n"
                                        "Run the tool once manually to resolve and confirm competitors, "
                                        "then re-schedule with a resolved competitor list."))
            sys.exit(3)

        print(f"Mode: seed-term discovery ({len(seed_terms)} seed term(s))")
        q_dir = workdir / "B1_serp_competitors"
        q_dir.mkdir(parents=True, exist_ok=True)
        (q_dir / "queries.json").write_text(
            json.dumps({"queries": seed_terms}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _rc = _run_phase(1, "B1", cfg_path, workdir, dry_run=args.dry_run,
                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
        _check_incomplete(_rc, "B1", _ceiling, workdir)
        if args.dry_run:
            print("[dry-run] would surface discovered competitor domains for review, "
                  "then stop for confirmation (no paid scrape on unverified domains).")
            _print_whole_forecast(cfg, workdir, resolution_reused=False)
            return
        discovered = _read_json(
            workdir / "B1_serp_competitors" / "competitor_domains.json", []) or []
        top = discovered[:20]
        print("\n" + "!" * 60)
        print(f"! DISCOVERY: {len(top)} candidate competitor domain(s). VERIFY them before spend")
        print("!" * 60)
        for d in top:
            print(f"    {d}")
        print("\nThese are auto-discovered and may include false positives.")
        print("Add the confirmed ones to `competitor_domains` in your config and")
        print("re-run; they will be resolved, validated, and confirmed before any")
        print("paid Meta / Google / LinkedIn scrape.")
        print("!" * 60)
        return

    else:
        print("[warn] No competitor_names, competitor_domains, or seed_terms in the "
              "config, so there is nothing to resolve. Add competitors and re-run.")
        return

    # Which paid ad stages actually have a target (never spend on an empty one).
    def _has_meta_target():
        # Blank means absent: a page or a search word that is only
        # whitespace is no target, the same reading as stages.build_B4, so no
        # paid Meta search fires with nothing to search.
        has_page = any(((e.get("facebook") or {}).get("use") or "").strip() for e in entries)
        return has_page or bool(ST.meta_search_words(cfg))

    def _has_google_target():
        return bool(competitor_set)

    # ------------------------------------------------------------------
    # 3. Meta Ad Library (B4)
    # ------------------------------------------------------------------
    print("\n--- Meta Ad Library ---")
    # Item f: Meta searches by Facebook page; the search words are used only
    # when no competitor has a page (stages.build_B4). A competitor without a
    # page is therefore not searched on Meta whenever another one has a page.
    # Named here, where the run decides, so its empty Meta result is never
    # read as "runs no Meta ads" (the resolution report names it too).
    _meta_no_page = [e.get("input", "") for e in entries
                     if not ((e.get("facebook") or {}).get("use") or "").strip()]
    # When no competitor has a
    # page, Meta is searched by the search words, and no competitor's OWN Meta ads
    # are searched. Nothing the run printed said so, and Claude filled the silence
    # with a market claim ("a Meta blind spot") or a share it computed itself. So
    # that case is a gap too, and search words the run does not use are named in
    # every case. One Meta entry in _gaps, so all three places (closing lines,
    # finish signal, scheduled notification) say it in the same words.
    _meta_words = ST.meta_search_words(cfg)
    _meta_any_page = len(_meta_no_page) < len(entries)
    # What the report bundle is told about Meta (passed forward, not re-derived).
    _meta_cov = {"not_searched": [], "words_used": [], "any_page": _meta_any_page}
    if not _wanted("B4"):
        print("  Meta is left out by your settings (the `stages` list), so it is not "
              "searched this run.")
    elif args.dry_run or _has_meta_target():
        _meta_reasons = []
        if _meta_no_page and _meta_any_page:
            print(f"  ⤫ Not searched on Meta (no Facebook page): {_quoted(_meta_no_page)}. "
                  "Meta searches the other competitors by their Facebook pages.")
            _meta_reasons.append(f"{_quoted(_meta_no_page)} has no Facebook page, so it was not "
                                 f"searched (the others were still searched)")
            _meta_cov["not_searched"] = list(_meta_no_page)
        elif _meta_no_page and _meta_words:
            _many = len(_meta_no_page) > 1
            # The singular read "this competitor' own ads".
            _whose = "these competitors'" if _many else "this competitor's"
            print(f"  ⤫ Own Meta ads not searched (no Facebook page): {_quoted(_meta_no_page)}. "
                  f"Meta is searched by your search words instead ({_quoted(_meta_words)}), "
                  f"which finds any advertiser using those words, not "
                  f"{_whose} own ads.")
            _meta_reasons.append(
                f"{_quoted(_meta_no_page)} {'have' if _many else 'has'} no Facebook page, so "
                f"{'their' if _many else 'its'} own Meta ads were not searched; Meta was "
                f"searched by your search words instead ({_quoted(_meta_words)}), which finds "
                f"any advertiser using those words, not "
                f"{_whose} own ads")
            _meta_cov["not_searched"] = list(_meta_no_page)
            _meta_cov["words_used"] = list(_meta_words)
        if _meta_words and _meta_any_page:
            print(f"  ⤫ Your Meta search words are not used ({_quoted(_meta_words)}): Meta "
                  "searches by Facebook page whenever any competitor has one.")
            _meta_reasons.append(f"your Meta search words ({_quoted(_meta_words)}) were not "
                                 f"used, because Meta searches by Facebook page whenever any "
                                 f"competitor has one")
        if _meta_reasons:
            _record_partial(_gaps, _STAGE_PLAIN["B4"], "; ".join(_meta_reasons))
        _rc = _run_phase(1, "B4", cfg_path, workdir, dry_run=args.dry_run,
                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
        _check_incomplete(_rc, "B4", _ceiling, workdir)
    else:
        print(f"  ⤫ Skipping Meta: no competitor has a Facebook page and no Meta search "
              f"words are set, so Meta is not searched for {_quoted(_meta_no_page)}. To "
              f"include them, add their Facebook pages in competitors/resolution.json, or "
              f"set `meta_search_terms`. (The tool never searches the whole market by "
              f"keyword on its own.)")
        _record_gap(_gaps, _STAGE_PLAIN["B4"],
                    "no competitor has a Facebook page and no Meta search words are set, "
                    "so it was not searched")
        _meta_cov["not_searched"] = list(_meta_no_page)

    meta_path = workdir / "B4_meta_ad_library" / "run_02_full.json"
    if args.dry_run:
        meta_items: list[dict] = []
        print("[dry-run] would read B4_meta_ad_library/run_02_full.json")
    else:
        meta_items = _read_json(meta_path, []) or []
        print(f"  Loaded {len(meta_items)} Meta ad record(s)")
        if (not meta_items and _wanted("B4") and (args.dry_run or _has_meta_target())
                and not _did_not_run(workdir, "B4")):
            _q = _quoted(e.get("input", "") for e in entries)
            _maintenance_gap(_gaps, workdir, "B4_meta_ad_library", _STAGE_PLAIN["B4"],
                             f"Meta returned no ads for {_q}", "Meta",
                             f"{_q} runs no Meta ads", "the Meta Ad Library")

    # ------------------------------------------------------------------
    # 4. Meta creative download IMMEDIATELY (fbcdn.net URLs expire)
    # ------------------------------------------------------------------
    print("\n--- Meta Creative Download ---")
    meta_saved = 0
    # Creatives this run could not download,
    # across the three platforms, named in one gap line at the end.
    _creatives_tried = [0, 0]  # [slots, failed]
    if cfg.get("download_creatives"):
        if args.dry_run:
            print("[dry-run] would call creatives.download_meta_creatives() "
                  "(Meta's image and video links expire, so this runs right after the Meta step)")
        elif meta_items:
            manifest = creatives.download_meta_creatives(
                meta_items, str(workdir / "creatives"),
                prev_creatives_dir=prev_creatives_dir,
            )
            meta_saved = sum(
                1 for v in manifest.values() for s in v if not s.startswith("FAILED")
            )
            _count_creatives(_creatives_tried, manifest)
        else:
            print("  No Meta items, nothing to download.")
    else:
        print("  Skipped: download_creatives is false.")

    # ------------------------------------------------------------------
    # 5. Advertiser curation pass (surface, don't block)
    # ------------------------------------------------------------------
    print("\n--- Advertiser Curation Pass ---")
    if args.dry_run:
        print("[dry-run] would diff Meta advertisers against supplied set")
        print("[dry-run] would write competitors/DISCOVERED_advertisers.md")
        discovered_advertisers: list[dict] = []
    else:
        # The checklist is a review aid, so no
        # problem with it stops the month. Before, an unreadable checklist ended
        # every re-run of the folder here, before Google and LinkedIn.
        try:
            discovered_advertisers = _curation_pass(meta_items, competitor_set, workdir)
        except Exception as exc:  # noqa: BLE001 - surface, don't block
            discovered_advertisers = []
            print(f"  The review checklist (DISCOVERED_advertisers.md) could not be written "
                  f"this run ({type(exc).__name__}: {exc}). The rest of the run goes on, "
                  f"and the checklist from before, if there was one, is left as it was.")

    # ------------------------------------------------------------------
    # 6. Google Ads Transparency (B5)
    # ------------------------------------------------------------------
    print("\n--- Google Ads Transparency ---")
    if not _wanted("B5"):
        print("  Google ads are left out by your settings (the `stages` list), so they "
              "are not searched this run.")
    elif args.dry_run or _has_google_target():
        # Google ads searches only by confirmed website; the resolution
        # report flagged these competitors "NOT resolved".
        _google_lost = [e.get("input", "") for e in entries
                        if not (e.get("domain") or {}).get("use")]
        if _google_lost:
            _record_partial(_gaps, _STAGE_PLAIN["B5"],
                            f"{_quoted(_google_lost)} has no confirmed website, so it was not "
                            f"searched (the others were still searched)")
        _rc = _run_phase(1, "B5", cfg_path, workdir, dry_run=args.dry_run,
                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
        _check_incomplete(_rc, "B5", _ceiling, workdir)
    else:
        print("  ⤫ No verified competitor domain resolved: skipping Google Ads "
              "Transparency (it searches ONLY by verified domain). See the "
              "resolution report for which competitors did not resolve a domain.")
        _record_gap(_gaps, _STAGE_PLAIN["B5"],
                    "no competitor has a confirmed website, so it was not searched")

    google_path = workdir / "B5_google_ads" / "run_02_full.json"
    if args.dry_run:
        google_items: list[dict] = []
        google_own: list[dict] = []
        _g_aside_note = ""
        print("[dry-run] would read B5_google_ads/run_02_full.json")
    else:
        google_items = _read_json(google_path, []) or []
        print(f"  Loaded {len(google_items)} Google ad record(s)")
        # An ad found by a competitor's website may be another
        # advertiser's (resolve.google_ad_owners). Those are set aside, named
        # here and in the report, and the rest of the run (OCR, the summary)
        # uses the competitor's own ads only. The creatives of every record are
        # still downloaded below, so an advertiser the user later adds to
        # "google_advertisers" shows with its images on a re-index.
        _g_owner = R.google_ad_owners(google_items, entries)
        for _line in R.google_set_aside_lines(google_items, entries, _g_owner):
            print(f"  ⤫ {_line}")
        # How to count them, once after the lines (not once per line), and
        # only when an advertiser was named: an unnamed ad has no name to add.
        if "other" in _g_owner:
            print(f"  {R.GOOGLE_CONFIRM_HOW}")
        google_own = [g for g, v in zip(google_items, _g_owner) if v == "own"]
        # The one sentence the closing lines and the notification use.
        _g_aside_note = R.google_set_aside_sentence(_g_owner)
        _q =_quoted(e.get("input", "") for e in entries)
        if not _wanted("B5"):
            pass  # left out on purpose: an empty result is not a gap
        elif not google_items and _did_not_run(workdir, "B5"):
            pass  # The closing lines name the step as not finished
        elif not google_items and _has_google_target() and _maintenance_gap(
                _gaps, workdir, "B5_google_ads", _STAGE_PLAIN["B5"],
                f"Google returned no ads for {_q}", "Google ads",
                f"{_q} runs no Google ads", "the Google Ads Transparency Center"):
            pass
        elif not google_items:
            # B5 uses RESIDENTIAL proxies; without them Google's SearchService
            # RPC blocks datacenter IPs with HTTP 429, zeroing out the run.
            # A 0 result can still mean a block (Google throttles even
            # residential IPs occasionally) OR that these competitors genuinely
            # run no Google ads — we cannot tell them apart from the empty
            # result alone, so flag it without over-claiming, and suggest a retry.
            # The how-to, with the commands the skill already
            # documents. This folder records the Google step as done (run_phase.
            # _stage_done: its full output exists, though empty), so running the
            # command here again skips it: the search can only be repeated in a
            # new folder, and `stages` keeps Meta and LinkedIn unbilled.
            print("  ⚠ 0 Google ads came back. The tool cannot tell whether these competitors "
                  "run no Google ads or the search was blocked. If you expected ads, "
                  "search Google again in a new run folder, because this folder counts "
                  "the Google search as done and running the command here again skips "
                  "it: make the folder from this one with repeat.py prepare (it keeps the "
                  "settings and the confirmed competitors), set \"stages\": [\"B5\"] in its "
                  "settings so only the Google ads step runs and is billed, then dry-run "
                  "and run it as usual.")
            _record_gap(_gaps, _STAGE_PLAIN["B5"],
                        "the search came back empty, and the tool cannot tell whether these "
                        "competitors run no Google ads or the search was blocked")

    # ------------------------------------------------------------------
    # 7. Google creative download
    # ------------------------------------------------------------------
    print("\n--- Google Creative Download ---")
    google_saved = 0
    if cfg.get("download_creatives"):
        if args.dry_run:
            print("[dry-run] would call creatives.download_google_creatives()")
        elif google_items:
            manifest = creatives.download_google_creatives(
                google_items, str(workdir / "creatives"),
                prev_creatives_dir=prev_creatives_dir,
            )
            google_saved = sum(
                1 for v in manifest.values() for s in v if not s.startswith("FAILED")
            )
            _count_creatives(_creatives_tried, manifest)
        else:
            print("  No Google items, nothing to download.")
    else:
        print("  Skipped: download_creatives is false.")

    # ------------------------------------------------------------------
    # 8. OCR (subagent OCR; skipped gracefully if the claude CLI is absent)
    # ------------------------------------------------------------------
    print("\n--- OCR ---")
    ocr_count = 0
    if cfg.get("ocr_google_text"):
        if args.dry_run:
            print("[dry-run] would OCR saved Google ad images -> google_ocr.json")
        else:
            google_manifest_path = workdir / "creatives" / "google" / "manifest.json"
            google_manifest = _read_json(google_manifest_path, {}) or {}
            creatives_dir = workdir / "creatives" / "google"
            ocr_results: dict[str, str] = {}
            model = cfg.get("vision_ocr_model", "")

            # Limit OCR to the top fraction of ads by duration (the profitability
            # proxy — longer-running ads = more profitable copy worth reading).
            frac = cfg.get("ocr_top_fraction", 0.2)
            ocr_ids = selector.top_creative_ids_by_duration(google_own, frac)
            print(f"  OCR scope: top {int(frac * 100)}% by duration → {len(ocr_ids)} of {len(google_own)} ads")

            ocr_tried = 0  # images handed to OCR (the lines below say which case)
            ocr_tried_ads: set = set()  # chosen ads with a saved image OCR was run on
            # Chosen ads with an image whose call did not finish, and the
            # count of images read with no text back (a different case, named apart).
            ocr_failed_ads: set = set()
            ocr_empty = 0
            for creative_id, filenames in google_manifest.items():
                if creative_id not in ocr_ids:
                    continue
                for fname in filenames:
                    if not isinstance(fname, str) or fname.startswith("FAILED"):
                        continue
                    img_path = creatives_dir / fname
                    if not img_path.exists():
                        continue
                    ocr_tried += 1
                    ocr_tried_ads.add(creative_id)
                    try:
                        text, how = OCR.transcribe(str(img_path), model)
                        if text:
                            ocr_results[creative_id] = text
                            ocr_count += 1
                        elif how == "empty":
                            ocr_empty += 1
                        else:
                            ocr_failed_ads.add(creative_id)
                    except Exception as exc:
                        ocr_failed_ads.add(creative_id)
                        print(f"  [ocr] Skipped {fname}: {exc}; continuing")

            if ocr_results:
                ocr_out = workdir / "google_ocr.json"
                _write_json(ocr_out, ocr_results)
                print(f"  {ocr_count} image(s) transcribed -> {ocr_out}")
            elif not google_manifest and not cfg.get("download_creatives"):
                # The user's own setting, not a failure (no gap line).
                print("  OCR has no images to read, because downloading ad images is "
                      "turned off in your settings (download_creatives is false).")
            elif not google_manifest:
                print("  No Google creative manifest found, so OCR is skipped.")
            elif ocr_tried and not shutil.which("claude"):
                print(f"  OCR did not run for the {ocr_tried} image(s) chosen, because the "
                      "Claude Code command-line tool was not found.")
            elif ocr_tried and ocr_empty == ocr_tried:
                print(f"  OCR read the {ocr_tried} image(s) it tried, but no text came "
                      "back from any of them.")
            elif ocr_tried:
                print(f"  OCR could not read any of the {ocr_tried} image(s) it tried "
                      "(the lines above say why).")
            else:
                print("  No images to transcribe (all failed or empty manifest).")
            # OCR that ran for fewer ads than its
            # scope chose is a gap, named in the closing lines. It used to end with
            # "OCR transcriptions: 0" and nothing more, so a timed-out or missing
            # transcription read as a run that went fine.
            # Each cause is named with its own count: the tool not found here (the
            # Cowork and cloud case), a transcription that ran and failed, and a
            # chosen ad with no saved image to read.
            _ocr_m = len(ocr_ids)
            _ocr_missing = set(ocr_ids) - set(ocr_results)
            # With downloading turned off by the
            # user, a chosen ad with no saved image is their own setting, not a
            # missing result, so it is left out of the gap (the step line says why).
            if not cfg.get("download_creatives"):
                _ocr_missing &= ocr_tried_ads
            if _ocr_missing and not shutil.which("claude"):
                _record_partial(_gaps, "Google ad image text (OCR)",
                                f"it could not run here, because the Claude Code command-line "
                                f"tool was not found, so the ad copy of {len(_ocr_missing)} "
                                f"of {_ocr_m} ads is missing from the report")
            elif _ocr_missing:
                _failed = len(_ocr_missing & ocr_failed_ads)
                _read_empty = len(_ocr_missing & ocr_tried_ads) - _failed
                _no_img = len(_ocr_missing - ocr_tried_ads)
                _parts = ([f"it did not finish for {_failed} of {_ocr_m} ads"] if _failed
                          else [])
                _parts += ([f"the image was read but no text came back for {_read_empty} "
                            f"of {_ocr_m} ads"] if _read_empty else [])
                _parts += ([f"{_no_img} of {_ocr_m} ads had no saved image to read"]
                           if _no_img else [])
                _record_partial(_gaps, "Google ad image text (OCR)",
                                "; ".join(_parts)
                                + ", so their ad copy is missing from the report")
    else:
        print("  Skipped: ocr_google_text is false.")

    # ------------------------------------------------------------------
    # 9. LinkedIn Ad Library (LI) — opt-in via include_linkedin (default True)
    # ------------------------------------------------------------------
    li_items: list[dict] = []
    li_saved = 0

    # LinkedIn scopes ONLY by numeric company id (name/keyword search returns
    # other advertisers). A target exists only if resolution resolved an id.
    li_targets = [e for e in entries if str((e.get("linkedin") or {}).get("company_id") or "").strip()]
    li_named = [e for e in entries if (e.get("linkedin") or {}).get("use")]

    print("\n--- LinkedIn Ad Library ---")
    if not cfg.get("include_linkedin"):
        print("  Skipped: include_linkedin is false.")
    elif not _wanted("LI"):
        print("  LinkedIn is left out by your settings (the `stages` list), so it is not "
              "searched this run.")
    elif not args.dry_run and not li_targets:
        # No numeric id => no precise target. Do NOT spend on a fuzzy name search
        # that would scrape the wrong advertiser. Distinguish the two gap causes.
        if not li_named:
            print("  Skipping LinkedIn: NO competitor resolved to a LinkedIn company. "
                  "This is a RESOLUTION gap, not a finding. Add/confirm the LinkedIn "
                  "company in competitors/resolution.json, then re-run.")
            _record_gap(_gaps, _STAGE_PLAIN["LI"],
                        "no competitor has a LinkedIn company page, so it was not searched")
        else:
            names = ", ".join(e['linkedin']['use'] for e in li_named)
            print(f"  Skipping LinkedIn: resolved a company NAME ({names}) but NO numeric "
                  "company id. The Ad Library scopes only by id, so a scrape now would "
                  "return the wrong advertiser. The id lookup (public company page) failed "
                  "or was blocked; add `company_id` to the entry in "
                  "competitors/resolution.json and re-run.")
            _record_gap(_gaps, _STAGE_PLAIN["LI"],
                        "the LinkedIn page number of "
                        + _quoted(e['linkedin']['use'] for e in li_named)
                        + " could not be found, so it was not searched")
    else:
        # LinkedIn scopes only by numeric id; the resolution report
        # flagged these "NO numeric id", and they are left out while the
        # others are searched.
        _li_no_id = [e["linkedin"]["use"] for e in li_named if e not in li_targets]
        if li_targets and _li_no_id:
            _record_partial(_gaps, _STAGE_PLAIN["LI"],
                            f"the LinkedIn page number of {_quoted(_li_no_id)} could not be "
                            f"found, so it was not searched (the others were still searched)")
        _rc = _run_phase(1, "LI", cfg_path, workdir, dry_run=args.dry_run,
                         run_ceiling_usd=_ceiling, run_ledger_path=_ledger_path)
        _check_incomplete(_rc, "LI", _ceiling, workdir)

        li_path = workdir / "LI_linkedin_ads" / "run_02_full.json"
        if args.dry_run:
            print("[dry-run] would read LI_linkedin_ads/run_02_full.json")
        else:
            li_items = _read_json(li_path, []) or []
            print(f"  Loaded {len(li_items)} LinkedIn ad record(s)")
            _li_names = _quoted(e['linkedin']['use'] for e in li_targets)
            # The country filter actually sent (stages.build_LI), in plain words;
            # "" when every country was searched.
            _li_where = ST.linkedin_where(cfg)
            if not li_items and _did_not_run(workdir, "LI"):
                pass  # The closing lines name the step as not finished
            elif not li_items and _maintenance_gap(
                    _gaps, workdir, "LI_linkedin_ads", _STAGE_PLAIN["LI"],
                    f"LinkedIn returned no ads{_li_where} for {_li_names}", "LinkedIn",
                    f"{_li_names} runs no LinkedIn ads{_li_where}", "the LinkedIn Ad Library"):
                pass
            elif not li_items:
                # A numeric id resolved but the scrape returned nothing. The id is
                # EXACT (not a fuzzy name), so this is not a wrong target; but an
                # empty result cannot tell a real zero from a scraper that fetched
                # nothing (seen once with a maintenance-mode actor), so it is
                # stated as unconfirmed, never as a fact about the company.
                ids = ", ".join(f"{e['linkedin']['use']} (id {e['linkedin']['company_id']})"
                                for e in li_targets)
                # " in the UK"[3:] == " the UK"
                _only = (f" The search was limited to{_li_where[3:]}, so it says nothing "
                         "about ads shown in other countries." if _li_where else "")
                print(f"  LinkedIn returned 0 ads{_li_where} for {ids}. The company id is "
                      "exact, so these are not another advertiser's ads, but the tool "
                      "cannot tell a real zero from a scraper that fetched nothing. Treat "
                      f"it as unconfirmed.{_only}")

        print("\n--- LinkedIn Creative Download ---")
        if cfg.get("download_creatives"):
            if args.dry_run:
                print("[dry-run] would call creatives.download_linkedin_creatives() "
                      "(some LinkedIn image links expire, so they are downloaded right away)")
            elif li_items:
                li_manifest = creatives.download_linkedin_creatives(
                    li_items, str(workdir / "creatives"),
                    prev_creatives_dir=prev_creatives_dir,
                )
                li_saved = sum(
                    1 for v in li_manifest.values() for s in v
                    if not s.startswith("FAILED")
                )
                _count_creatives(_creatives_tried, li_manifest)
            else:
                print("  No LinkedIn items, nothing to download.")
        else:
            print("  Skipped: download_creatives is false.")

    # This line says only what the downloads
    # show. It used to add "the ads themselves are all there", which the run
    # never checked (a source can be empty or unsearched in the same month).
    if _creatives_tried[1]:
        _record_partial(_gaps, "Ad images and videos",
                        f"{_creatives_tried[1]} of {_creatives_tried[0]} could not be "
                        f"downloaded (the link failed or has expired), so those images and "
                        f"videos are missing from the app")

    # ------------------------------------------------------------------
    # 10. Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Competitors resolved  : {len(competitor_set)}")
    print(f"  Meta ads collected    : {len(meta_items)}")
    print(f"  Google ads collected  : {len(google_own)}"
          + (f"  ({_g_aside_note})" if _g_aside_note else ""))
    print(f"  LinkedIn ads collected: {len(li_items)}")
    print(f"  Creatives saved       : {meta_saved + google_saved + li_saved}"
          f"  (meta={meta_saved}, google={google_saved}, linkedin={li_saved})")
    print(f"  OCR transcriptions    : {ocr_count}")
    print(f"  Outputs in            : {workdir.resolve()}")
    if args.dry_run:
        print("\n  [dry-run] No real data was fetched or charged.")
        print("\n  [dry-run] would assemble report/ bundle")
        print("  [dry-run] would run emit_index.build_index() -> frontend_index.json")
        _print_whole_forecast(cfg, workdir, _resolution_reused)
    else:
        # The report bundle carries the run's own gap lines and what Meta searched,
        # so the written report cannot turn a search it did not make into a claim
        # about the market.
        # A step that did not finish is named there too, first.
        _inc_lines = _incomplete_report_lines(_incomplete_lines(workdir), scheduled=False)
        e3_report.assemble_report(workdir, cfg,
                                  gap_lines=_inc_lines + [s for _, s in _kept_lines(workdir)]
                                  + _gap_lines(_gaps, complete=not _inc_lines),
                                  meta_coverage=_meta_cov)
        # Normalized index for the Local Viewer. Free, no network; reads only what
        # is already on disk. Never fail the whole run over the index.
        try:
            emit_index.build_index(workdir, cfg)
        except Exception as e:  # noqa: BLE001 - index is a convenience, not the deliverable
            print(f"  [note] frontend_index.json not written ({e}); "
                  "the run's data and report are unaffected.")
            # Without the index the month is invisible in the app and the
            # next repeat builds on the month before (repeat._run_completed).
            _index_failed = True
    if _ceiling is not None:
        # A dry run fetched nothing, so it has no gaps to report. No written-
        # summary note here: this tool's report bundle is assembled by code
        # (e3_report, no model), so it cannot go missing the way pain's can.
        # A step that did not finish (its RUN INCOMPLETE block) ends
        # the run "incomplete", never "done".
        _inc = [] if args.dry_run else _incomplete_lines(workdir)
        if _inc:
            print("=" * 60)
            _CLOSING["reached_end"] = True
            _write_incomplete_run_notification(workdir, _ceiling, _inc, _gaps,
                                               index_failed=_index_failed,
                                               aside_note=_g_aside_note,
                                               key_warnings=_key_warnings(workdir))
            _write_incomplete_marker(workdir, "A step of this scheduled run did not "
                                              "finish; see notification.json.\n")
            sys.exit(4)
        _write_done_notification(workdir, _ceiling, [] if args.dry_run else _gaps,
                                 index_failed=_index_failed,
                                 aside_note="" if args.dry_run else _g_aside_note,
                                 key_warnings=() if args.dry_run else _key_warnings(workdir))
    print("=" * 60)
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
