#!/usr/bin/env python3
"""
search_intel.py -- top-level pipeline runner for the search-intelligence tool.

Runs the full keyword-expansion pipeline in one command:
  A1 (autocomplete) -> A2 (volume/difficulty/CPC) -> B1 (SERP) -> synthesis -> S1 report.

Usage:
    python3 search_intel.py --config ../SEARCH_CONFIG.json --workdir ./run_01
    python3 search_intel.py --config ../SEARCH_CONFIG.json --workdir ./run_01 --dry-run
    python3 search_intel.py --config ../SEARCH_CONFIG.json --workdir ./run_01 --test-only

Options:
    --config      Path to SEARCH_CONFIG.json (default: the repo-level SEARCH_CONFIG.json).
    --workdir     Directory for stage output files. Created if it does not exist.
    --dry-run     Print plan + inputs for each stage, spend nothing ($0). Skips synthesis/report
                  (there is nothing to synthesise when no data was scraped).
    --test-only   Run only the small test pass for each stage (a few items, minimal spend).
                  Synthesis and report still run on the test artifacts -- useful smoke test.
    --force       Re-run Apify stages even if they already completed in this workdir.

Stdlib only -- no pip install needed.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_status as RS  # noqa: E402  (the run's status file)
import budget as B  # run ledger of an interactive run
import config as C
import extractor
import report
import emit_index  # normalized frontend_index.json for the viewer
import postprocess as _PP  # the one place the unfinished-step sentences are made
from seeds import resolve_seeds


# ---------------------------------------------------------------------------
# Subprocess helper (mirrors ad_intel.py._run_phase, but runs the WHOLE phase)
# ---------------------------------------------------------------------------

# Keys excluded from the config fingerprint — must match repeat.py's constant
# (same file in all 3 skills; fingerprint must be reproducible by any of them).
_FINGERPRINT_EXCLUDE = frozenset(("subject", "repeat_of", "budget_caps",
                                  "resolution_confirmed"))


def _run_phase(cfg_path, workdir, dry_run=False, test_only=False, force=False,
               run_ceiling_usd=None, run_ledger_path=None, second_request_extra_usd=None):
    """
    Invoke run_phase.py for phase 1 (all stages: A1 -> A2 -> B1).

    Does NOT pass --only so run_phase.py runs the full sequence with
    postprocessing between stages (which is exactly the scrape chain we want).
    Streams output live. Returns subprocess.CompletedProcess.

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
        str(HERE / "run_phase.py"),
        "1",
        "--config", str(cfg_path),
        "--workdir", str(workdir),
    ]
    if dry_run:
        cmd.append("--dry-run")
    if test_only:
        cmd.append("--test-only")
    if force:
        cmd.append("--force")
    if run_ceiling_usd is not None:
        cmd += ["--run-ceiling-usd", str(run_ceiling_usd)]
    if run_ledger_path is not None:
        cmd += ["--run-ledger-path", str(run_ledger_path)]
    if second_request_extra_usd is not None:   # Only the second request the user said yes to
        cmd += ["--second-request-extra-usd", str(second_request_extra_usd)]
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    sys.stdout.flush()
    # As subprocess.run(cmd, check=False), but the step holds the run folder's lock and
    # a stop of this run reaches it (run_status.run_child).
    _res = RS.run_child(cmd)
    if not dry_run:
        import stages as _st  # already on sys.path
        _name_not_run_by_setting(workdir, _st.PHASE_ORDER["1"])
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
    # The offer sentences this text carries, so they can be taken out again word for word.
    sent = [t for t in [_pending_offer_message(workdir)] + list(_CLOSING.get("empty_retry") or [])
            if t and ("waits for your yes" in t or "Asking again" in t) and t in notif["message"] + notif["report"]]
    if sent:
        notif["offer_text"] = sent
    p = Path(workdir) / "notification.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(notif, ensure_ascii=False, indent=2), encoding="utf-8")
    _refresh_dead_note(workdir)


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


def _charge_caveat(workdir, earlier=0):
    """" This may be incomplete: ..." when a step's charge was noted as possibly
    low this run, else "". earlier: steps the folder's total already carries."""
    try:
        notes = [x for x in (Path(workdir) / _CHARGE_NOTES_NAME).read_text(
            encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        notes = []
    n = len(notes) + earlier
    if not n:
        return ""
    return (f" This may be incomplete: Apify was still recording the charges of {n} "
            f"paid step{'' if n == 1 else 's'} when the tool stopped waiting, so the "
            f"real bill can be higher. The exact amount is on the Apify console.")


# How the closing line names this tool's records and sources (plain words).
_RECORD_NOUN = ("record", "records")
_SOURCE_NAMES = {"google_serp": "Google", "semrush": "Semrush"}


def _folder_size_sentence(workdir):
    """One sentence with the size of the folder's results, by source, that adds up.

    WHY: after a run real sessions described its size from the
    per-source log lines ("[reddit] 15 records" counts the posts Reddit returned)
    mixed with other outputs, and the numbers did not add up. This counts the
    records of frontend_index.json, the file the app reads, by source, so the
    total is the sum of the parts. Empty when there is no readable index."""
    try:
        recs = json.loads((Path(workdir) / "frontend_index.json").read_text(
            encoding="utf-8")).get("records") or []
    except Exception:  # noqa: BLE001
        return ""
    by_source = {}
    for r in recs:
        if isinstance(r, dict):
            k = r.get("platform") or ""
            by_source[k] = by_source.get(k, 0) + 1
    n = sum(by_source.values())
    if not n:
        return ""
    parts = ", ".join(f"{_SOURCE_NAMES.get(k, k.capitalize() or 'no source')} {v}"
                      for k, v in sorted(by_source.items()))
    return (f" The results in this folder hold {n} "
            f"{_RECORD_NOUN[0] if n == 1 else _RECORD_NOUN[1]}: {parts}.")


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
# "report_missing" is the plain reason the written report was not produced
# ("" when it was). It is recorded where the report step gives up and read only by
# the closing lines. It is a note, never a gap or an outcome: the collected data is
# complete, and the monthly repeat must not read this run as unfinished.
# "serp_note" is the one sentence saying the Google search-results step
# pulled the first keywords in list order because no keyword volumes were available
# ("" when it did not). run_phase prints it to the console only; a session that
# relays the closing lines would never mention it, and the user would believe the
# pulls were the top keywords.
# "steps_pending" is whether some paid step this run's config
# asks for has no full result on disk once the scrape chain is over (True), or has all
# of them (False). None means it was not worked out, and is read as True: the closing
# never says writing the report later costs nothing unless this is False.
_CLOSING = {"gaps": None, "reached_end": False, "report_missing": "", "serp_note": "",
            "steps_pending": None, "empty_retry": []}


# What a step did about items that came back empty (run_phase writes "last" in
# <step folder>/.empty_retry.json). Named only for a step whose record THIS call wrote or
# changed, so a resumed folder never says an earlier run's retry is in this run's bill. One
# exception: a record still marked "in_flight" is a request that died, and it is
# said whenever the record is read, because those keywords stay without data.
_EMPTY_RETRY_STEPS = (("A2", "A2_keyword_metrics"), ("B1", "B1_serp_landscape"))
_EMPTY_RETRY_NAME = ".empty_retry.json"


def _empty_retry_state(workdir):
    """{step folder: (mtime ns, size)} of each step's retry record."""
    out = {}
    for _stg, folder in _EMPTY_RETRY_STEPS:
        try:
            st = (Path(workdir) / folder / _EMPTY_RETRY_NAME).stat()
            out[folder] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return out


def _empty_retry_sentence_for_approval():
    """The one sentence the approval adds (wrapped like the lines around it); priced by the
    function that prices the retry."""
    import textwrap
    import run_phase as RP  # already on sys.path via HERE
    unit = C.estimate("A2", 1)[0]
    return textwrap.fill(
        f"A keyword that comes back with no data from Semrush is asked for once more, usually at "
        f"about ${unit:.2f} each, inside your budget cap and counted in the total billed "
        f"(it is not in the figure above). If your budget cap is a little short for that, by "
        f"at most ${RP._EMPTY_RETRY_EXTRA_USD:.2f}, you are asked first and nothing extra is "
        f"spent without your yes. If it is short by more, only the keywords that fit inside "
        f"the cap are asked for.", width=70, initial_indent="  ", subsequent_indent="  ")


def _usd(x):
    """An amount shown to the user, rounded UP to the cent (budget.cents_up): a stated
    figure is never below the real one."""
    return f"${B.cents_up(x):.2f}"


def _keyword_names(keys, extra=0):
    """'"a", "b", "c" and 4 more' for a list of keywords (the first three are named); `extra`
    unlisted ones are counted too."""
    return ", ".join(f'"{k}"' for k in keys[:3]) + (
        f" and {len(keys) + extra - 3} more" if len(keys) + extra > 3 else "")


def _offer_more(offer):
    """The keywords past one request's limit; no later request is promised."""
    m = int(offer.get("more") or 0)
    return "" if not m else (f"; one request asks for at most {offer.get('limit')}, so the offer is for "
                             f"{len(offer.get('keys') or [])} of them and the other {m} "
                             f"{'is' if m == 1 else 'are'} left out of it and not asked for")


def _offer_sentence(lead, offer, approved, left_text, grew=True):
    """The closing sentence for a second request that was NOT made because the budget cap is a
    little short: what it would cost, how much that is over what is left of the cap, and
    that nothing was spent. `lead` opens it (how many keywords had no data), `left_text` says how
    they show until then. approved: the extra the user said yes to when it was not enough;
    grew=False:
    the price is unchanged, the amount was less than it needed."""
    need, extra = _usd(offer["need_usd"]), _usd(offer["extra_usd"])
    if approved is not None:   # they said yes, but not enough for the price
        return (f"{lead}. Asking again can now be charged at most {need}, which is {extra} more than what was left "
                f"of your budget cap, and you approved up to {_usd(approved)}"
                f"{'' if grew else ', which is less than that'}, so nothing was asked and "
                f"nothing was spent. Until you agree to spend up to {extra} more on it, {left_text}.")
    if offer.get("unattended"):   # nobody was there to ask: it waits for the next talk about this research
        return (f"{lead}. Asking again can be charged at most {need}, which is {extra} more than what is left "
                f"of the spending limit you approved for this run, so nothing was asked and nothing was "
                f"spent on it. Nothing is spent beyond that limit without your yes: the next time you "
                f"talk to Claude about this research, it will ask whether you agree to spend up to "
                f"{extra} more on asking again. Until then, {left_text}.")
    return (f"{lead}. Asking again can be charged at most {need}, which is {extra} more than what is left of "
            f"your budget cap, so no second request was made and nothing was spent on it. Until you "
            f"agree to spend up to {extra} more on it, {left_text}.")


def _pending_offer(workdir):
    """(keys, offer) for a second request that waits for the user's yes in this folder, or (None,
    None). Read from the volumes step's record: its "offer" minus every key the record
    already counts as asked (and every key with data in the saved results now), and
    none when the record is closed (unreadable). Never raises."""
    try:
        rec = json.loads((Path(workdir) / "A2_keyword_metrics" / _EMPTY_RETRY_NAME).read_text(
            encoding="utf-8")) or {}
        offer = rec.get("offer")
        if not isinstance(offer, dict) or rec.get("closed"):
            return None, None
        asked = {str(k).strip().lower() for k in rec.get("asked") or []}
        items = json.loads((Path(workdir) / "A2_keyword_metrics" / "run_02_full.json").read_text(
            encoding="utf-8"))
        asked |= {str(k).strip().lower() for k in offer.get("keys") or []} \
            - {str(k).strip().lower() for k in _PP.empty_item_keys("A2", items)}
        keys = [str(k) for k in offer.get("keys") or [] if str(k).strip().lower() not in asked]
        float(offer["need_usd"]), float(offer["extra_usd"])
        if keys and len(keys) < len(offer.get("keys") or []):   # The price of the keys shown
            import run_phase as RP  # already on sys.path via HERE
            need = RP._call_ceiling("A2", len(keys))   # as the decision: _plan_second_request
            short = max(round(need - float(offer["left_usd"]), 6), 0.0)   # 0 when they now fit the cap
            offer = {**offer, "need_usd": round(need, 6), "short_usd": short, "extra_usd": B.cents_up(short),
                     "more": 0}
        return (keys, offer) if keys else (None, None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, None


def _pending_offer_lines(workdir):
    """[sentence] when a second request waits for the user's yes in this folder (shown by a dry run of
    the folder, which is how a research is looked at again). Nothing otherwise."""
    keys, offer = _pending_offer(workdir)
    if not keys:
        return []
    m = int(offer.get("more") or 0)
    n = len(keys) + m   # The same count as the closing
    return [f"A second request to Semrush is waiting for your yes: it had no data for {n} "
            f"keyword{'s' if n != 1 else ''} ({_keyword_names(keys, m)}){_offer_more(offer)}. Asking again can be charged at most "
            f"{_usd(offer['need_usd'])}, "
            + ("which now fits inside what is left of the budget cap, so no extra is needed"
               if not offer["extra_usd"] else
               f"which is {_usd(offer['extra_usd'])} more than what was left of the budget cap when that run ended")
            + ". Nothing has been spent on it."]


def _pending_offer_message(workdir):
    """One short sentence for a scheduled run's notification message when it left an offer waiting,
or "". The message is cut at 200 characters, so the caller adds it only if it fits."""
    keys, offer = _pending_offer(workdir)
    if not keys:
        return ""
    n = len(keys) + int(offer.get("more") or 0)   # The same count as the closing
    return (f"Semrush had no data for {n} keyword{'s' if n != 1 else ''}; " + (
        f"asking again, costing at most {_usd(offer['need_usd'])}, fits your limit, so it waits for your yes."
        if not offer["extra_usd"] else
        f"asking again needs up to {_usd(offer['extra_usd'])} more than your limit, so it waits for your yes."))


def _offer_of(workdir):
    """The waiting second request: its keywords and extra, None when none waits."""
    keys, offer = _pending_offer(workdir)
    return (keys, offer["extra_usd"]) if keys else None


def _count_yes_in_total(workdir):
    """A yes buys after the scheduled run wrote its "Total billed". Its own bill (this process's run
    ledger) is added once to the folder's record (_run_ledger.json) and the notification's total is rewritten to
    read the record, so a yes that bought nothing, or a second yes, changes nothing. Unreadable bill: "see run log"."""
    led, p = Path(workdir) / "_run_ledger.json", Path(workdir) / "notification.json"
    if not led.exists():   # a folder run by hand keeps no such record
        return
    yes = _read_run_ledger(workdir, _INTERACTIVE_LEDGER_NAME)
    try:
        doc = json.loads(led.read_text(encoding="utf-8"))
        if "spent" not in doc or _read_run_ledger(workdir) is None:   # a record with no usable total: damaged
            raise KeyError("spent")
        if yes:
            doc["spent"] = round(float(doc["spent"]) + yes, 8)
            tmp = led.with_name(led.name + ".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, led)
    except (OSError, ValueError, KeyError, TypeError):
        if yes == 0:
            return
        print("[note] could not add this request to the total billed in this folder's notification.", flush=True)
        yes = None   # The record cannot take a charge: the total says "see run log", never a figure without it
    try:
        note = json.loads(p.read_text(encoding="utf-8"))
        old = re.search(r"^Total billed: .*$", note["report"], re.M)
        if old:
            was = re.search(r"charges of (\d+) paid step", old.group(0))
            whole = _read_run_ledger(workdir)
            line = ((f"Total billed: ${whole:.4f}." if whole is not None and yes is not None
                     else "Total billed: see run log.") + _charge_caveat(workdir, int(was.group(1)) if was else 0))
            if line != old.group(0):
                note["report"] = note["report"][:old.start()] + line + note["report"][old.end():]
                p.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        print("[note] could not add this request to the total billed in this folder's notification.", flush=True)


def _refresh_dead_note(workdir):
    """While a record shows a request in flight (running, or died mid-call), the notification's total
    line says it may have been charged and is not in the total; once it does not, the sentence is gone. run_phase
    calls this before the request is launched, so it is on disk before anything can be charged. Only that sentence
    changes. Any in-flight figure that cannot be read (None) leaves the whole sentence without a figure."""
    lead, held = " This total does not include a second request", []
    for _stg, folder in _EMPTY_RETRY_STEPS:
        try:
            rec = json.loads((Path(workdir) / folder / _EMPTY_RETRY_NAME).read_text(encoding="utf-8")) or {}
            if rec.get("in_flight"):
                held.append(B.usable_usd(rec.get("in_flight_usd")))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        p = Path(workdir) / "notification.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        old = re.search(r"^Total billed: .*$", doc["report"], re.M)
        line = old.group(0).split(lead)[0] + (f"{lead} that was started and has not finished; it may have been charged"
                                              + (f", at most ${sum(held):.4f}" if None not in held else "") + "." if held else "")
        if line != old.group(0):
            doc["report"] = doc["report"][:old.start()] + line + doc["report"][old.end():]
            p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass


def _drop_stale_offer_text(workdir, before):
    """Once the offer `before` is gone or changed, its sentences leave notification.json
    (what `list-runs` shows); nothing else in it changes."""
    now = _offer_of(workdir)
    if now is not None and now == before:   # An offer gone for any reason (no results file) goes too
        return
    try:
        p = Path(workdir) / "notification.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        if "offer_text" not in doc:   # nothing of it to take out: the file stays as it was
            return
        for t in doc.pop("offer_text"):
            for f in ("message", "report"):
                doc[f] = doc[f].replace(" " + t, "").replace(t, "")
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, AttributeError):
        pass


def _empty_retry_lines(workdir, before):
    """[sentence] for each step whose retry record this call wrote or changed."""
    out = []
    for stg, folder in _EMPTY_RETRY_STEPS:
        try:
            p = Path(workdir) / folder / _EMPTY_RETRY_NAME
            st = p.stat()
            rec = json.loads(p.read_text(encoding="utf-8")) or {}
            last, held = rec.get("last") or {}, [str(k) for k in rec.get("in_flight") or []]
        except (OSError, ValueError, AttributeError):
            continue
        if held:   # A second request that started and never ended (the process died)
            m = len(held)
            one = m == 1
            out.append(f"A second request for {m} " + (
                f"keyword{'' if one else 's'} was started and did not finish. "
                + ('If Semrush had no data for it, it shows' if one else 'Any that Semrush had no data for show')
                + ' as "no data from Semrush", not as zero.' if stg == "A2" else
                f"Google search page{'' if one else 's'} was started and did not finish. "
                + ("If it came back as an error, it is" if one else "Any that came back as an error are")
                + " left out of the results.")
                + (f" That request may have been charged, at most ${float(rec['in_flight_usd']):.4f}"
                   f" (its run is named in the run log)." if rec.get("in_flight_usd") else ""))
            continue
        if before.get(folder) == (st.st_mtime_ns, st.st_size):
            continue
        flagged = [str(k) for k in last.get("flagged") or []]
        if not flagged:
            continue
        n, a2 = len(flagged), stg == "A2"
        names = _keyword_names(flagged)
        lead =(f"Semrush had no data for {n} keyword{'s' if n != 1 else ''} ({names})" if a2 else
                f"{n} Google search page{'s' if n != 1 else ''} came back as an error ({names})")

        def left(m):   # what the ones still empty look like in the results
            return ((f'{"they show" if m != 1 else "it shows"} as "no data from Semrush", not as zero')
                    if a2 else f'{"they are" if m != 1 else "it is"} left out of the results')
        if isinstance(last.get("offer"), dict):   # The cap is a little short; the user is asked
            s = _offer_sentence(lead + _offer_more(last["offer"]), last["offer"], last.get("approved_usd"),
                                left(n), last.get("price_grew", True))
        elif last.get("skipped"):
            s = f"{lead}. No second request was made: {last['skipped']}, so {left(n)}."
        elif last.get("error"):
            s = (f"{lead}. The second request did not complete (see the run log)"
                 + (f" and was billed ${float(last['charged_usd']):.4f}, which is in the bill below"
                    if last.get("charged_usd") else "") + f", so {left(n)}.")
        elif last.get("unsaved"):   # Billed, answered, and the answer could not be saved
            s = (f"{lead}. The second request was billed ${float(last.get('charged_usd') or 0):.4f}, "
                 f"which is in the bill below, but its answer could not be saved, so {left(n)}.")
        else:
            asked = len(last.get("asked") or [])
            k, m = len(last.get("with_data") or []), len(last.get("still") or [])
            who = "it was" if n == 1 else "they were" if asked == n else f"{asked} of them were"
            s = (f"{lead}, so {who} asked for once more, for ${float(last.get('charged_usd') or 0):.4f}, "
                 f"which is in the bill below. {k} of {asked} now {'has' if asked == 1 else 'have'} data.")
            if m:
                s += f" {m} still {'has' if m == 1 else 'have'} none, so {left(m)}."
            lo = len(last.get("left_out") or [])   # One request asks for at most `limit`
            if lo:
                s += (f" {lo} more {'was' if lo == 1 else 'were'} not asked for again, because one "
                      f"request asks for at most {last.get('limit')}.")
            cp = len(last.get("capped") or [])   # The rest would have passed the budget cap
            if cp:
                s += (f" {cp} more {'was' if cp == 1 else 'were'} not asked for, because asking for "
                      f"{'it' if cp == 1 else 'them'} would take the run past your budget cap. "
                      f"{'It was' if cp == 1 else 'They were'} not marked as asked, so a later request "
                      f"can still include {'it' if cp == 1 else 'them'}.")
        out.append(s)
    return out


# Files a step's folder holds that are records ABOUT the step, not traces of its runs
# (run_phase writes them for a step that was refused at the start or left out by a
# setting). They do not show that the step pulled anything.
_STEP_RECORD_FILES = (".run_incomplete.json", ".not_run_by_setting.json")


def _step_files_state(workdir, folder):
    """{file name: (modified time in ns, size)} of the files in a step's folder that
    are traces of its runs (the run log, the saved results, the recovery handle), not
    the step's own records. {} when the folder is missing. Taken before the scrape
    chain and again after it, so a step that wrote or changed any file in this run is
    told from one that was refused at the start, skipped or left alone."""
    out = {}
    try:
        for p in Path(workdir, folder).iterdir():
            if p.is_file() and p.name not in _STEP_RECORD_FILES:
                st = p.stat()
                out[p.name] = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    return out


def _paid_steps_pending(cfg, workdir):
    """True when some paid step this run's config asks for has no full result on disk.

    WHY: "writing the report later costs nothing on Apify,
    because the steps that already finished are skipped" is true only when every paid
    step the config asks for is finished. A test-only run leaves no stage record yet
    finished nothing, and a later run would pay for every full pass. This uses the
    run's own skip test (_finished_paid_steps, which CALLS run_phase._stage_done), so
    the claim and the real skip cannot disagree."""
    import stages as ST
    sel = ST.selected_stages(cfg)
    asked = [s for s in ST.PHASE_ORDER["1"] if sel is None or s in sel]
    done = set(_finished_paid_steps(cfg, workdir, False))
    return any(s not in done for s in asked)


def _serp_selection_note_for_closing(cfg, workdir, finished_before, files_before=None,
                                     test_only=False):
    """The sentence stages.serp_selection_note gives for a search-results step that
    this run started and that fell back to list order, else "".

    It is the same sentence the console shows (stages.serp_selection_note, called
    as it is, not re-worded here), asked after the run, when the keyword volumes
    are on disk or are not. It is asked only for a run in which the step really
    started: the step is in the config's stages, it was not already finished when
    this run started (finished_before: run_phase's own test, taken before the run),
    and this run wrote or changed a file in its folder (files_before: _step_files_state
    taken before the run; None means every file there counts as written by this run).
    A step that was skipped, refused at the start, left out or never started pulled
    nothing in this run, so a sentence about how its keywords were chosen would be
    untrue. A step that started DID get that list, however it ended: a full pass that
    raised, a stop at the budget between batches, or a test-only pass. It keeps the
    sentence, next to the line that says it did not finish (this used to ask for the full
    result on disk, which these runs never leave).

    test_only: the run was a test-only pass, which pulls only the keywords
    stages.serp_keywords(test=True) gives (5 at most), not the full pass's. The count
    in the sentence is then that number: the sentence is asked of the same stages
    function with the settings narrowed to those keywords, so it names what was
    pulled and not the full pass's count."""
    import stages as ST
    sel = ST.selected_stages(cfg)
    if sel is not None and "B1" not in sel:
        return ""
    if "B1" in finished_before:
        return ""
    spec = ST.STAGES["B1"]
    now = _step_files_state(workdir, spec["folder"])
    before = files_before or {}
    if not any(before.get(name) != sig for name, sig in now.items()):
        return ""
    if test_only:
        pulled, _by_volume = ST.serp_keywords(cfg, str(workdir), test=True)
        cfg = dict(cfg, serp_top_n=len(pulled), keyword_cap=max(len(pulled), 1))
    return ST.serp_selection_note(cfg, str(workdir)) or ""


def _report_missing_line(reason, unfinished=False):
    """The closing line for a report that was not written, with the reason.

    WHY (seen in a live run): the report step failed (the claude command
    was not logged in, exit 1, twice) and the run still ended with only its bill,
    so the user was told nothing about the missing report or why. The remedy is
    the one the skill gives under "Write (or finish) the report of a run". The
    wording is cause-agnostic because the cause is not always the command (the
    analysis step can fail too).

    unfinished: True when a step did not finish (the
    lines above name it). Then "costs nothing on Apify, because the finished steps
    are skipped" would be false: writing the report later runs that step again, and
    it can cost money. The line says so, and that a dry run shows the cost first,
    as the skill does for this path. It is passed in, from the same step records
    that produced the lines above, not re-derived here.

    Also True when some paid step the config asks for has
    no full result on disk although no line names it (a test-only run leaves only
    test results and no record): a later run would pay for those steps. The clause
    "costs nothing" is said only when every such step is finished."""
    head = (f"The written search landscape report was not produced: {str(reason).rstrip('. ')}. "
            f"The collected data is saved in this folder. Once that is fixed, ask Claude "
            f"to write the report of this research")
    if unfinished:
        return (head + ". Running it again may pay for the steps that did not finish, "
                       "so a dry run shows the cost first.")
    return head + "; it costs nothing on Apify, because the steps that already finished are skipped."


def _end_interactive_ledger():
    """Print the interactive run's whole bill as its last line, however it ended.

    Returns the closing text it printed ("" when it printed none), which the run's
    status file carries word for word (run_status.finish). A run that reached its
    last step first names its gaps, in the notification's words (_gap_lines); the
    bill line stays the last line."""
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
    if _CLOSING["serp_note"]:
        gap_lines.append(_CLOSING["serp_note"])
    gap_lines += _CLOSING.get("empty_retry") or []   # Before the bill line
    if _CLOSING["reached_end"] and _CLOSING["report_missing"]:
        gap_lines.append(_report_missing_line(
            _CLOSING["report_missing"],
            unfinished=bool(inc_lines) or _CLOSING["steps_pending"] is not False))
    if gap_lines:
        print("\n" + "\n".join(gap_lines), flush=True)
    print("\n" + line, flush=True)
    return "\n".join(gap_lines + [line])


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
    "A1": "Keyword ideas",
    "A2": "Keyword volumes",
    "B1": "Google search results",
    "search-phase1": "The search research",
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

    Under standing-approval, any non-zero rc (not just 4) triggers INCOMPLETE.
    Why: a run_phase crash (interpreter error, kill signal) would otherwise let
    the caller continue with a stale or absent ledger.

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


# What the keyword-ideas step leaves next to its output when the rows it got back
# were not what was asked (postprocess.A1_ROWS_NOTICE).
_A1_ROWS_NOTICE = ".a1_rows_notice.json"


def _keyword_rows_gaps(gaps, workdir):
    """Name, in the run's gap list, what postprocess._pp_a1 said about the rows the
    keyword-ideas step got back: rows that could not be read at all (a step that ran
    and returned nothing usable, the same kind of gap as a volumes or results step
    that ran and returned nothing) and rows for another country than the one asked
    for (some of what was asked is missing: a partial gap). The sentences are the
    ones _pp_a1 wrote, from its own counts: nothing is re-counted here.
    WHY the run goes on rather than stops: a step that ran and returned nothing
    usable is already carried on and named in the closing lines (A2 and B1, below),
    and the money for the step is spent either way; a stop would also be a new stop
    path in the entry script."""
    try:
        n = json.loads((Path(workdir) / "A1_keyword_expansion" / _A1_ROWS_NOTICE)
                       .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(n, dict):
        return
    if str(n.get("unreadable") or "").strip():
        _record_gap(gaps, _STAGE_PLAIN["A1"], n["unreadable"])
    if str(n.get("region") or "").strip():
        _record_partial(gaps, _STAGE_PLAIN["A1"], n["region"])


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
    part = [(g[0], g[1]) for g in gaps if len(g) == 3]
    if not whole and not part and not index_failed and not summary_note:
        _write_notification(workdir, "done", f"Run complete. Ceiling ${B.cap_text(ceiling)}.",
                            report=(f"All steps ran successfully.\n"
                                    f"{cost_line}\n"
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
    msg = next((c for c in cands if len(c) <= 200), cands[-1])
    # A second request left waiting for the user's yes is in the message when it fits (the
    # report, below, always carries it): the extra cost is what the user needs to decide.
    offer_msg = _pending_offer_message(workdir)
    if offer_msg and len(f"{msg} {offer_msg}") <= 200:
        msg = f"{msg} {offer_msg}"

    lines = _gap_lines(gaps)
    if not whole and not part:
        lines.append("Run complete. No source is missing this month.")
    if index_failed:
        lines.append(_index_failed_report(workdir))
    if summary_note:
        lines.append(summary_note)
    report = "\n".join(lines) + f"\n{cost_line}\n{where}"
    _write_notification(workdir, "done", msg, report=report)


def _quoted(items):
    """'a', 'b' -- the user's own setting values, quoted, for a gap reason."""
    return ", ".join(f"'{str(x).strip()}'" for x in items)


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


def _earlier_result_reason(e, rebuilds=False):
    """The record entry of a step that failed or was stopped at the budget cap
    (run_phase._note_step_stopped), for a step whose folder STILL HOLDS a finished
    result.

    WHY: _incomplete_reason tells the user to run the same command on the folder
    and says the steps that finished are skipped. That is the remedy for a step
    that has no finished result. Verified on the fake (a run, then a --force run
    whose search-results step failed, then a plain run): raw results are never
    removed, so the earlier run_02_full.json was still there, the plain run printed
    "already complete, skipping" for the step, kept the earlier results and left
    the error record in place. The advice promised a fix the command cannot make.
    Here the step is named as it is: the folder holds a finished result, the same
    command keeps it, and fetching the step again takes a new run folder or --force
    (which runs every step again; the entry script has no way to force one step).
    The record says whether the attempt saved that result before
    it stopped (result_saved), so the line says whose result it is, and the plain run now
    builds again the tables the failed attempt removed (run_phase skip, postprocess.
    rebuild_dropped_outputs), so "keeps that result" is true of what the user sees.
    "Its latest attempt" is true whichever command reads the record: a plain run of the
    folder makes no attempt at a finished step, and the record is the last one made.
    rebuilds is True only where postprocess.will_rebuild_dropped_outputs
    says the plain run will build the tables again (the same test the rebuild applies);
    the promise "at no charge" is made only then. Once a later run has
    skipped the step, the record is re-labelled and _kept_lines words it instead."""
    kind = e.get("kind")
    again = ("To fetch this step again, start a new run folder, or run the same "
             "command with --force, which runs every step again and is charged again "
             "for each of them; a dry run shows the cost first")
    keeps = ("This run folder still holds a finished result of this step, and running "
             "the same command on it skips the step and keeps that result"
             + (", and builds the tables from it again at no charge. " if rebuilds
                else ". "))
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
    budget cap (run_phase._note_step_stopped): no reason for a zero is
    recorded for it; the closing lines name it as not finished instead.
    A step its settings asked for 0 results was not run either; the
    closing lines name it with that reason (_name_not_run_by_setting).
    A scraper run that ended without finishing (stopped, failed,
    timed out) and kept no record is the same case. Its own line already says it
    "was stopped before it finished", so the line "it ran, but no X came back" next
    to it said two opposite things about one step. A run that ended unfinished but
    DID keep records is not this: a zero is then a real count."""
    if _not_run_by_setting(workdir, stage):
        return True
    import stages as _st  # already on sys.path
    p = Path(workdir) / _st.STAGES[stage]["folder"] / _RUN_INCOMPLETE_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return False
    return isinstance(doc, dict) and any(
        isinstance(e, dict) and (e.get("kind") in ("step_failed", "budget_stop",
                                                   "kept_after_failed_attempt")
                                 or (e.get("kind") == "run_ended" and e.get("records") == 0))
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
                out.append((name, _earlier_result_reason(
                    e, _PP.will_rebuild_dropped_outputs(doc.get("stage"), p.parent, workdir))))
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


def _test_pass_usd(stg, build_cfg, workdir, full_inp, full_count):
    """What the small test run that run_phase makes BEFORE a step's full run can
    bill (its start fee included), or 0.0 when that run will not be made.

    WHY: the full run re-sends the test run's own first inputs, so the
    two are billed separately, and the forecast priced only the full run: ten
    seeds forecast $0.185 for the keyword-ideas step while it billed $0.235
    (a $0.08 test run plus a $0.155 full run). A forecast is a spending gate and
    must never be under the real charge.

    The test is run_phase._run_one's own, in the same words: no run when the full
    input counts 0 (the step is not run), when the test input has nothing to work
    on (_has_inputs), or when the full input is no larger than the test input
    (_input_size; the test would bill the same run twice, so _run_one skips it).
    An input _input_size cannot size always gets its test run, as in _run_one.
    The test run is priced as _run_one prices its spend limit: estimate(stage,
    test_count), one run. build_cfg / workdir: what the full input was built from."""
    import run_phase as RP  # already on sys.path via HERE
    import stages as ST
    if full_count <= 0:
        return 0.0
    spec = ST.STAGES[stg]
    t_inp, t_count = spec["build"](build_cfg, workdir, test=True)
    if not RP._has_inputs(stg, t_inp):
        return 0.0
    f_sz, t_sz = RP._input_size(spec, full_inp), RP._input_size(spec, t_inp)
    if f_sz is not None and t_sz is not None and f_sz <= t_sz:
        return 0.0
    return C.estimate(stg, t_count)[0]


def _search_forecast_usd(cfg_with_seeds, skip=(), workdir=None):
    """Zero-spend forecast: sum of A1, A2, B1 stage estimates, each with the small
    test run it makes first whenever that run will be made (_test_pass_usd).

    skip / workdir (dry run of a folder that already holds finished steps only): the steps in
    `skip` are left out, because this run will not
    pay for them (see _finished_paid_steps). When A1 is skipped, A2 and B1 read
    the keyword list already on disk, so each is priced at the LARGER of the
    planned maximum below and that real list. Every run path goes through
    _this_run_forecast_usd; with nothing finished it passes neither, and then
    nothing here changes.

    cfg_with_seeds must already have seed_terms populated (post-resolution).

    A1 is priced from the seeds. A2 and B1 are NOT: in a real run they read
    A1's EXPANDED keyword list (keywords/keywords.json, which postprocess._pp_a1
    builds as the seeds first, then A1's unique suggestions, capped at
    keyword_cap). So they are priced from A1's PLANNED MAXIMUM output: the
    suggestion count A1's own estimate uses (stages.build_A1, ~30 per seed)
    plus the seeds, capped the way _pp_a1 caps it. build_A2 / build_B1 then
    apply their own caps (keyword_cap, serp_top_n) to that planned list.

    WHY the planned maximum and not the seeds or last month's actual count:
    this number is a spending gate. Priced from the seeds, the whole-run
    forecast came out smaller than A2 alone at run time, so a user who approved
    1.5x the forecast was stopped at A2 every month. Too high costs the user a
    slightly higher ceiling; too low makes every scheduled run fail.
    (Same rule as pain's B2b fallback: n_places x max_reviews_per_place.)

    Reads nothing from disk: the workdir handed to the builders cannot exist,
    so a keywords.json in the current directory can never leak into the number.
    """
    import os
    import stages as ST  # already on sys.path via HERE
    no_workdir = str(Path(os.devnull) / "no-workdir")  # never exists: builders use their cfg fallback

    # Only the steps the run runs are priced: run_phase runs the config's
    # `stages` allow-list (stages.selected_stages; None = all), so a step left
    # out of it adds nothing here either.
    sel = ST.selected_stages(cfg_with_seeds)

    # Each estimate prices one start charge per actor run, as run_phase does
    # (config.actor_runs).
    a1_inp, a1_count = ST.STAGES["A1"]["build"](cfg_with_seeds, no_workdir, test=False)
    total = (C.estimate("A1", a1_count, runs=C.actor_runs(ST.STAGES["A1"], a1_inp))[0]
             + _test_pass_usd("A1", cfg_with_seeds, no_workdir, a1_inp, a1_count)
             if (sel is None or "A1" in sel) and "A1" not in skip else 0.0)

    # A1's planned output as _pp_a1 would write it: seeds + a1_count
    # suggestions, capped at keyword_cap. Placeholder strings stand in for the
    # not-yet-known suggestions; only the list length reaches the estimate.
    seeds = list(cfg_with_seeds.get("seed_terms", []))
    planned_len = min(len(seeds) + a1_count, int(cfg_with_seeds.get("keyword_cap", 300)))
    planned_cfg = dict(cfg_with_seeds)
    planned_cfg["seed_terms"] = (seeds + [f"planned-keyword-{i}" for i in range(a1_count)])[:planned_len]
    for stg in ("A2", "B1"):
        if (sel is not None and stg not in sel) or stg in skip:
            continue
        inp, count = ST.STAGES[stg]["build"](planned_cfg, no_workdir, test=False)
        est = (C.estimate(stg, count, runs=C.actor_runs(ST.STAGES[stg], inp))[0]
               + _test_pass_usd(stg, planned_cfg, no_workdir, inp, count))
        if "A1" in skip and workdir is not None:
            # A1 will not run again: this step reads the list A1 already wrote,
            # exactly as the real run builds it. Never price below that list.
            d_inp, on_disk = ST.STAGES[stg]["build"](cfg_with_seeds, str(workdir), test=False)
            est = max(est, C.estimate(stg, on_disk,
                                      runs=C.actor_runs(ST.STAGES[stg], d_inp))[0]
                      + _test_pass_usd(stg, cfg_with_seeds, str(workdir), d_inp, on_disk))
        total += est
    return total


def _finished_paid_steps(cfg_with_seeds, workdir, force=False):
    """The paid steps a real run in `workdir` will skip, in run order.

    A real run skips a step when run_phase's own test says it is finished
    (run_phase._stage_done) and --force was not given. This CALLS that test
    rather than repeating it: a second test could disagree with the real one,
    and then the forecast would be wrong in the direction that costs money.
    A step the config's `stages` list leaves out is not run at all, so it is
    not listed either."""
    if force:
        return []
    import run_phase as RP  # already on sys.path via HERE
    import stages as ST
    sel = ST.selected_stages(cfg_with_seeds)
    return [s for s in ST.PHASE_ORDER["1"]
            if (sel is None or s in sel)
            and RP._stage_done(Path(workdir) / ST.STAGES[s]["folder"], ST.STAGES[s])]


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



def _this_run_forecast_usd(cfg_with_seeds, workdir, force=False):
    """What THIS run can still spend: (usd, the finished paid steps it skips).

    The one computation the dry run, --forecast-only and the standing-approval
    gate all use, so the three can never disagree about
    a folder. With no step finished the figure is the planned maximum, exactly
    as before."""
    done = _finished_paid_steps(cfg_with_seeds, workdir, force)
    usd = (_search_forecast_usd(cfg_with_seeds, skip=done, workdir=workdir) if done
           else _search_forecast_usd(cfg_with_seeds))
    return usd, done


def _dry_run_whole_forecast(cfg_with_seeds, workdir=None, force=False):
    """The whole-run cost figure a dry-run shows for approval, or the reason
    it could not be computed.

    WHY: the per-stage lines a dry-run prints price the volume and search
    result steps from the seed list alone, which leaves out most of what those
    steps cost once the keyword list is expanded. This calls
    _search_forecast_usd, the one function --forecast-only, the
    standing-approval gate and `repeat.py forecast` already use, so the figure
    approved interactively is the same number a monthly ceiling is set from.
    Printing only: nothing here changes what a run spends.

    A folder whose paid steps are already finished (for example
    one that only lacks its written report) is priced for what THIS run can
    still spend. A real session saw "at most $1.7702" for a rerun that would
    spend $0, and rightly stopped to ask. The finished steps come from the real
    run's own skip test (_finished_paid_steps). With none finished, the figure
    is exactly the one above.
    Returns (usd or None, error text or None, plain names of the skipped steps).
    """
    try:
        if workdir is None:
            usd, done = _search_forecast_usd(cfg_with_seeds), []
        else:
            usd, done = _this_run_forecast_usd(cfg_with_seeds, workdir, force)
        return usd, None, [_STAGE_PLAIN.get(s, s) for s in done]
    except Exception as exc:  # noqa: BLE001 - a dry-run must still finish
        return None, str(exc), []


def _print_whole_forecast(usd, err, skipped=()):
    print("\n" + "=" * 60)
    if usd is None:
        print("WHOLE-RUN COST FORECAST: could not be computed "
              f"({err}). Do not approve the run on the per-step figures alone.")
    elif skipped:
        # Only a folder with finished paid steps reaches this; a fresh run's
        # lines below stay exactly as they were.
        print("Already collected in this folder, so this run skips them and they cost $0:")
        for name in skipped:
            print(f"  - {name}")
        print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for what this run can still spend.")
        print("  The steps listed above are skipped and are not in this figure (the")
        print("  per-step lines above still price them). The rest is priced at the")
        print("  largest size the settings allow, so a real run usually costs less.")
        print("  Writing the report runs on this computer and costs nothing on Apify.")
        if "Keyword volumes" not in skipped:
            print(_empty_retry_sentence_for_approval())
    else:
        print(f"WHOLE-RUN COST FORECAST: at most ${usd:.4f} for every step together.")
        print("  This is the worst case: every step priced at the largest size the")
        print("  settings allow, so a real run usually costs less. It is the figure to")
        print("  show before approval; the per-step lines above can leave steps out.")
        print(_empty_retry_sentence_for_approval())
    print("=" * 60)


# ---------------------------------------------------------------------------
# Synthesis + report tail (unit-testable without the scrape)
# ---------------------------------------------------------------------------

def _synthesize_and_report(workdir, cfg, gaps=None, active=()):
    """
    Run extraction then the S1 report on the artifacts in workdir.

    gaps / active (unattended runs): when gaps is a list, a step in
    `active` whose results the extractor counted as zero is recorded there.
    The count is the extractor's own (synthesis_facts.json "coverage", written
    a moment earlier), passed forward rather than re-counted from the stage
    folders. A step the settings left out is not in `active` and never a gap.

    Defensive: if synthesis_facts.json ends up empty or missing after extraction,
    prints a clear message rather than crashing.

    Returns the Path to S1_search_landscape.md, or None when the report was not
    generated (e.g. the claude CLI is absent or returned no output).
    """
    workdir = Path(workdir)

    # What the keyword-ideas step said about the rows it got back is named
    # before anything that can end this function early (the extractor failing would
    # otherwise drop it from the closing lines).
    if gaps is not None and "A1" in active:
        _keyword_rows_gaps(gaps, workdir)

    print("\n--- Synthesis ---")
    try:
        facts_path = extractor.extract(workdir, cfg)
    except Exception as exc:
        print(f"[search-intel] extractor failed: {exc}, so the report is skipped")
        _CLOSING["report_missing"] = (f"the analysis step it is written from failed "
                                      f"({report._short_tail(exc)})")
        return None

    # Sanity check: make sure the file is non-empty before calling the report step.
    if not facts_path.exists() or facts_path.stat().st_size == 0:
        print("[search-intel] the analysis the report is written from is empty or "
              "missing, so the report is skipped")
        _CLOSING["report_missing"] = ("the analysis the report is written from "
                                      "is empty or missing")
        return None

    if gaps is not None:
        try:
            cov = json.loads(facts_path.read_text(encoding="utf-8")).get("coverage") or {}
        except (OSError, ValueError, AttributeError):
            cov = {}
        # Search is re-scraped in full every run, so zero here is never
        # "nothing new": it means the step produced nothing at all.
        # A step whose scraper said it did not work is named first (the most
        # specific reason wins in _record_gap).
        for _stg, _folder, _what in (("A1", "A1_keyword_expansion", "keyword ideas"),
                                     ("A2", "A2_keyword_metrics", "keyword volumes"),
                                     ("B1", "B1_serp_landscape", "search results")):
            if _stg in active:
                _maintenance_gap(gaps, workdir, _folder, _STAGE_PLAIN[_stg],
                                 f"{_STAGE_PLAIN[_stg]} returned nothing for your keywords",
                                 _STAGE_PLAIN[_stg].lower(),
                                 f"there are no {_what} for your keywords",
                                 "your keywords in another tool")
        if "A2" in active and cov.get("priced_keywords") == 0 and not _did_not_run(workdir, "A2"):
            _record_gap(gaps, _STAGE_PLAIN["A2"], "it ran, but no keyword volumes came back")
        if "B1" in active and cov.get("serp_keywords") == 0 and not _did_not_run(workdir, "B1"):
            _record_gap(gaps, _STAGE_PLAIN["B1"], "it ran, but no search results came back")

    print("\n--- Report (search landscape) ---")
    try:
        report_path = report.generate(workdir, cfg)
    except Exception as exc:
        print(f"[search-intel] report.generate failed: {exc}")
        _CLOSING["report_missing"] = (f"the report step stopped with an error "
                                      f"({report._short_tail(exc)})")
        return None

    if report_path is None:
        # The reason report.py recorded when it gave up (passed forward, not
        # re-derived here); the fallback is for a step that returned nothing
        # without recording one.
        _why = report.failure_reason() or "the report step produced nothing"
        _CLOSING["report_missing"] = _why
        print(f"[search-intel] search landscape report not generated: {_why}")
    else:
        _CLOSING["report_missing"] = ""
        print(f"\nSearch landscape report -> {report_path}")

    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _main() -> None:
    ap = argparse.ArgumentParser(
        description="search-intelligence pipeline: "
                    "keyword ideas -> keyword volumes -> Google search results -> "
                    "synthesis -> search landscape report."
    )
    ap.add_argument("--config", default=None, help="path to SEARCH_CONFIG.json")
    ap.add_argument("--workdir", required=True, help="run output directory (created if absent)")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="print plan + inputs, spend nothing ($0); skips synthesis/report",
    )
    ap.add_argument(
        "--test-only", action="store_true",
        help="run only the small test pass for each step; still runs synthesis/report on test data",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="re-run steps even if already complete in this workdir",
    )
    ap.add_argument("--standing-approval", default=None, metavar="JOB_FILE",
                    help="Path to a job file written by `repeat.py schedule`. "
                         "Before spending anything: verifies the config fingerprint "
                         "and the forecast are within the approved ceiling; refuses "
                         "with exit code 3 on any mismatch; writes notification.json.")
    ap.add_argument("--forecast-only", action="store_true",
                    help="Print the cost forecast as JSON and exit 0. "
                         "Calls the same _search_forecast_usd function the "
                         "standing-approval gate calls. Writes nothing, makes no "
                         "actor calls, needs no job file.")
    ap.add_argument("--second-request", action="store_true",
                    help="The user's yes to a second request this folder offered (the closing lines "
                         "or a dry run of the folder say so): asks Semrush once more, ONLY for the "
                         "offered keywords, then writes the report again. Needs --approved-extra-usd. "
                         "Never with --standing-approval, --dry-run, --test-only or --force.")
    ap.add_argument("--approved-extra-usd", type=float, default=None, metavar="USD",
                    help="With --second-request: the extra the user said yes to, in dollars: at most "
                         "0.25 and at most the extra the offer showed them.")
    args = ap.parse_args()

    # Resolve the config path once so we can pass it cleanly to the subprocess.
    cfg_path = Path(args.config).resolve() if args.config else C.repo_default_config_path()

    # Load and validate the config.
    cfg = C.load_config(cfg_path)

    # Create the workdir so downstream stages can write into it immediately.
    workdir = Path(args.workdir)

    if args.forecast_only:
        # resolve_seeds is pure (no I/O); seed_terms must be populated for
        # accurate A1/A2/B1 counts, same as the standing-approval gate.
        _fo_seeds, _ = resolve_seeds(cfg)
        _fo_cfg = dict(cfg)
        _fo_cfg["seed_terms"] = _fo_seeds
        _fo_usd, _ = _this_run_forecast_usd(_fo_cfg, workdir, args.force)
        print(json.dumps({"forecast_usd": _fo_usd}))
        sys.exit(0)

    workdir.mkdir(parents=True, exist_ok=True)
    RS.start(workdir, "search", dry_run=args.dry_run,
             scheduled=bool(args.standing_approval))

    # The user's yes to a second request that was offered. Refused, before anything is
    # spent, wherever nobody said yes: a scheduled run (its approval is the ceiling and no more),
    # a dry run, a test pass, a re-run of the paid steps; and without the amount the user approved.
    # The amount itself is checked against the offer by run_phase (at most $0.25, at most shown).
    if args.second_request or args.approved_extra_usd is not None:
        _x = args.approved_extra_usd
        _no = ("A scheduled run never spends beyond what was approved in advance, so it never makes "
               "this request." if args.standing_approval else
               "This request is made on its own, not with a dry run, a test pass or a re-run."
               if (args.dry_run or args.test_only or args.force) else
               "Say which extra amount the user approved with --approved-extra-usd, and use "
               "--second-request with it." if (not args.second_request or _x is None) else
               "The approved extra must be 0 or more and at most $0.25."
               if not (0 <= _x <= 0.25) else
               "A yes of 0 is only for keywords that now fit inside the budget cap, and none are waiting that way."
               if _x == 0 and (_offer_of(workdir) or (0, 1))[1] != 0 else None)
        if _no:
            print(f"[REFUSED] {_no} Nothing was spent.")
            sys.exit(3)

    # Resolve seeds -- determines which input mode the user has configured.
    # resolve_seeds raises SystemExit if NEITHER seed_terms nor vertical is set,
    # so past this point `seeds` is guaranteed non-empty (the FROZEN "non-empty
    # keyword universe OR stop loudly" requirement, satisfied at resolution time).
    seeds, mode = resolve_seeds(cfg)

    # ------------------------------------------------------------------
    # 1b. Standing-approval preflight (FROZEN).
    #
    # WHY fingerprint uses the ORIGINAL cfg (before seed mutation):
    # repeat.py schedule reads SEARCH_CONFIG.json from the run dir (the
    # original config, not resolved_config.json) and fingerprints that.
    # We must fingerprint the same object here -- before cfg["seed_terms"]
    # is set -- so both sides hash the same document.
    #
    # WHY forecast uses cfg_with_seeds (after resolving): the actual
    # scrape cost depends on the resolved seeds, not the vertical label.
    # ------------------------------------------------------------------
    _ceiling = None       # None = no standing-approval; set below when present
    _ledger_path = None  # Run-level spend ledger; initialised below when ceiling is set
    _gaps = []           # (plain name, plain reason) per step that returned nothing
    _CLOSING["gaps"] = _gaps  # the closing lines name them
    _index_failed = False  # The app index could not be written

    # The steps this run will actually run: the config's `stages` allow-list
    # (empty = all), decided by the same stages.selected_stages that run_phase
    # applies. A name in the list that is no step of this tool silently runs
    # nothing, so it is recorded as a gap here, where the config is read.
    import stages as _ST
    _sel = _ST.selected_stages(cfg)
    _active = [s for s in _ST.PHASE_ORDER["1"] if _sel is None or s in _sel]
    for _code in (cfg.get("stages") or []):
        if _code not in _ST.PHASE_ORDER["1"]:
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

        if _job.get("tool") != "search":
            print(f"[ERROR] Job is for tool '{_job.get('tool')}', not 'search'.")
            _write_notification(workdir, "refused",
                                f"Job tool mismatch: expected 'search', got '{_job.get('tool')}'.")
            sys.exit(3)

        # Fingerprint from the raw SEARCH_CONFIG.json bytes -- matches what
        # repeat.py schedule computed.  WHY raw not cfg: C.load_config()
        # normalises the dict (adds defaults); raw JSON gives the same
        # representation both sides hash.  Also: must be pre-seed-mutation
        # (see WHY comment above) so read the file, not the cfg dict.
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
        # Forecast from resolved seeds so A1/A2/B1 counts are accurate.
        _cfg_for_forecast = dict(cfg)
        _cfg_for_forecast["seed_terms"] = seeds
        _forecast, _ = _this_run_forecast_usd(_cfg_for_forecast, workdir, args.force)
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

    # persist the resolved seeds into the config the SUBPROCESS
    # reads. resolve_seeds is pure and returns seeds into THIS process's memory
    # only; run_phase.py shells out and re-reads the config from disk, where in
    # vertical mode `seed_terms` is empty -- so A1 was scraping nothing and the
    # whole run produced a silent empty landscape. Writing the resolved seeds
    # back into `seed_terms` makes both modes uniform: mode 1 keeps its keywords
    # (idempotent), mode 2 gets the vertical label(s) as the initial seeds that
    # A1 then expands via autocomplete. Mirrors pain_intel.py's mode-2 config
    # persistence -- resolve once in the caller, hand the subprocess a
    # resolved config, never re-derive downstream.
    cfg["seed_terms"] = seeds
    resolved_cfg_path = workdir / "resolved_config.json"
    resolved_cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("search-intelligence: pipeline start")
    print(f"  config     : {cfg_path}")
    print(f"  resolved   : {resolved_cfg_path}")
    print(f"  workdir    : {workdir.resolve()}")
    print(f"  input mode : {mode}")
    print(f"  seeds ({len(seeds)}) : {seeds}")
    if mode == "vertical":
        print("  (vertical mode: these seeds will be expanded by the keyword ideas step, Google autocomplete)")
    if args.dry_run:
        print("  [dry-run] No Apify runs, no API calls, $0 spend.")
    if args.test_only:
        print("  [test-only] Small test pass only; synthesis will run on test-sized data.")

    # ------------------------------------------------------------------
    # Scrape chain: A1 -> A2 -> B1 (run_phase.py handles sequencing +
    # postprocessing between stages). Pass the RESOLVED config so the
    # subprocess sees the seeds we just resolved, not the on-disk original.
    # ------------------------------------------------------------------
    # Which steps were already finished when this run started, taken
    # BEFORE the run (run_phase's own test), so the closing lines can tell a
    # search-results step that ran now from one that was skipped.
    _finished_before = [] if args.dry_run else _finished_paid_steps(cfg, workdir, args.force)
    _b1_files_before = {} if args.dry_run else _step_files_state(workdir, "B1_serp_landscape")
    _retry_before = {} if args.dry_run else _empty_retry_state(workdir)
    _a2_before = _step_files_state(workdir, "A2_keyword_metrics").get("run_02_full.json")
    _offer_before = None if args.dry_run else _offer_of(workdir)
    result = _run_phase(
        resolved_cfg_path, workdir,
        dry_run=args.dry_run,
        test_only=args.test_only,
        force=args.force,
        run_ceiling_usd=_ceiling,
        run_ledger_path=_ledger_path,
        second_request_extra_usd=args.approved_extra_usd if args.second_request else None,
    )
    if not args.dry_run:
        _drop_stale_offer_text(workdir, _offer_before)   # No notification still offers what is gone
        if args.second_request:
            _count_yes_in_total(workdir)
        try:   # What was asked again is said even when the run stopped later
            _CLOSING["empty_retry"] = _empty_retry_lines(workdir, _retry_before)
        except Exception:  # noqa: BLE001  (money is spent; never end the run over a note)
            _CLOSING["empty_retry"] = []
        if args.second_request and not _CLOSING["empty_retry"]:   # A yes that bought nothing
            _CLOSING["empty_retry"] = ["No second request was made and nothing was spent on it. This "
                                       "folder has none waiting for your yes, or it was already asked "
                                       "for, or it could not be made (the lines above say which)."]
        try:
            _CLOSING["serp_note"] = _serp_selection_note_for_closing(
                cfg, workdir, _finished_before, _b1_files_before, test_only=args.test_only)
        except Exception as exc:  # noqa: BLE001  (money is spent; never end the run over a note)
            _CLOSING["serp_note"] = ""
            print(f"[search-intel] could not check how the search-results keywords were "
                  f"chosen: {report._short_tail(exc)}", flush=True)
        try:
            _CLOSING["steps_pending"] = _paid_steps_pending(cfg, workdir)
        except Exception:  # noqa: BLE001  (unknown stays None, which reads as pending)
            _CLOSING["steps_pending"] = None

    _refresh_dead_note(workdir)
    # _check_incomplete handles all non-zero exits under standing-approval.
    # Under ceiling: any non-zero rc -> INCOMPLETE marker + notification + exit 4.
    # Without ceiling: _check_incomplete is a no-op (non-approval path unchanged).
    _check_incomplete(result, "search-phase1", _ceiling, workdir)

    if result.returncode != 0:
        # Non-approval path (ceiling is None): propagate the exit code as before.
        print(
            f"\n[search-intel] run_phase.py exited with code {result.returncode}, "
            f"so the pipeline stops. Fix the scrape error and re-run.",
            flush=True,
        )
        sys.exit(result.returncode)

    if args.second_request and \
            _step_files_state(workdir, "A2_keyword_metrics").get("run_02_full.json") == _a2_before:
        # The yes bought nothing that changed the saved volumes, so the folder's report and
        # app index are as they were and are not written again.
        _CLOSING["reached_end"] = True
        return

    # ------------------------------------------------------------------
    # Synthesis + report tail.
    # Skipped on --dry-run (nothing was scraped, nothing to synthesise).
    # Runs on --test-only (small artifacts make a useful smoke test).
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n[dry-run] Scrape chain plan printed. Synthesis/report skipped ($0 spent).")
        print("[dry-run] would run emit_index.build_index() -> frontend_index.json")
        # cfg["seed_terms"] holds the resolved seeds (set above), the same
        # config --forecast-only prices.
        _print_whole_forecast(*_dry_run_whole_forecast(cfg, workdir, args.force))
        for _line in _pending_offer_lines(workdir):   # A second request waiting for a yes
            print("\n" + _line)
        return

    if args.test_only:
        print("\n[test-only] Running synthesis/report on test-sized data (directional only).")

    _report_path = _synthesize_and_report(workdir, cfg, gaps=_gaps, active=_active)
    # None means the written summary was not produced (the claude CLI is
    # missing, the extractor or the report step failed). A note, not a gap:
    # the collected data is intact.
    _summary_note = (None if _report_path is not None else
                     "The written summary could not be produced this month; "
                     "the collected search data is in the results folder.")
    # A scheduled run's report also says what was asked for again (a note, not a gap).
    _retry_note = " ".join(_CLOSING.get("empty_retry") or [])
    if _retry_note:
        _summary_note = f"{_summary_note} {_retry_note}" if _summary_note else _retry_note

    # Normalized index for the Local Viewer. Free, no network; reads only what is
    # already on disk (metrics/serp files exist even if the report step failed).
    # Never fail the whole run over the index.
    try:
        emit_index.build_index(workdir, cfg)
    except Exception as exc:  # noqa: BLE001 - index is a convenience, not the deliverable
        print(f"[search-intel] frontend_index.json not written ({exc}); "
              "the run's data and report are unaffected.")
        # Without the index the month is invisible in the app and the
        # next repeat builds on the month before (repeat._run_completed).
        _index_failed = True

    if _ceiling is not None:
        # A step that did not finish (its RUN INCOMPLETE block) ends
        # the run "incomplete", never "done".
        _inc = _incomplete_lines(workdir)
        if _inc:
            _CLOSING["reached_end"] = True
            _write_incomplete_run_notification(workdir, _ceiling, _inc, _gaps,
                                               index_failed=_index_failed,
                                               summary_note=_summary_note)
            _write_incomplete_marker(workdir, "A step of this scheduled run did not "
                                              "finish; see notification.json.\n")
            sys.exit(4)
        _write_done_notification(workdir, _ceiling, _gaps,
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


def main() -> None:
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
