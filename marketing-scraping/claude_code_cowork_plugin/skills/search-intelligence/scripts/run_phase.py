#!/usr/bin/env python3
"""
Stage runner — runs a set of Apify stages SEQUENTIALLY, test-first, budget-gated,
auto-logging. Covers the three stages this tool uses (A1/A2/B1). The
higher-level search-intelligence flow (keyword expansion -> metrics -> SERP ->
synthesis -> S1 report) is driven on top of this in a separate entry point;
this module is the reusable "run one Apify stage safely" engine plus a thin CLI
for isolated single-stage runs.

    python3 run_phase.py 1 --config ../SEARCH_CONFIG.json --workdir <run>
    python3 run_phase.py 1 --only A2 --test-only --config ... --workdir ...
    python3 run_phase.py 1 --dry-run --config ... --workdir ...   # plan + inputs, spend nothing

For each stage: build TEST input (3-5 items) → check budget → run → recompute the
per-unit rate from ACTUAL test cost → estimate the full run → check budget → run
full → save output → append RUN_LOG.md + commands.sh. Each run is charged to the
budget the moment it ends, before its output is fetched.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE =Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
import apify_client as A
from budget import Budget, BudgetExceeded, cap_text, cents_up, run_ledger_spent
from runlog import append_run_log, append_command
from stages import STAGES, PHASE_ORDER
import stages as S
import postprocess as P


def _records_saved_text(stage, items, batches=None):
    """The "full: N records saved" console text of a step. A function of its own so that
    _run_one stays the same text in all three tools (test_client_drift compares it) while
    the ad tool's version can tell its Meta scraper's status records from ads."""
    return f"full: {len(items)} records saved" + (f" across {batches} batches" if batches else "")


def _counted_records(stage, items):
    """How many of a step's records the test pass and its per-record rate count. A function
    of its own so that _run_one stays the same text in all three tools; only the ad tool
    has records that are not part of the count (the Meta scraper's status records)."""
    return len(items)


def _save_dataset(stage_dir, tag, items):
    out = Path(stage_dir) / f"run_{tag}.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    if tag == "02_full":
        _STEP_SAVED.append(str(stage_dir))
    return out


def _stage_done(stage_dir, spec):
    """A stage is 'done' once its full output exists — lets a re-run resume
    without re-spending on completed stages."""
    return (stage_dir / "run_02_full.json").exists()


# --- run recovery -----------------------------------------------------------
# A run is CHARGED the instant it launches, but its output only lands on disk
# after a successful fetch. A transient client drop in between loses the run
# handle, _stage_done stays False, and the next call RE-LAUNCHES -- paying twice.
# We persist {runId, datasetId, input_hash, subkey} the instant a run launches
# (via run_actor's on_start), and on re-entry reattach to that live/finished run
# instead of launching again. Apify keeps runs and datasets indefinitely, so the
# result is still there to fetch. The handle is cleared once its output is safely
# consumed. `subkey` ('test' / 'full' / 'batchN') separates runs within a stage;
# only the current in-flight run is tracked (runs are sequential).

def _pending_path(stage_dir):
    return Path(stage_dir) / ".pending_run.json"


def _input_hash(actor_input):
    blob = json.dumps(actor_input, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _write_pending(stage_dir, data):
    _pending_path(stage_dir).write_text(json.dumps(data), encoding="utf-8")


def _read_pending(stage_dir):
    p = _pending_path(stage_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clear_pending(stage_dir):
    try:
        _pending_path(stage_dir).unlink()
    except OSError:
        pass


# The subkeys of the scraper runs the current step launched or picked up,
# in order. main() empties it before each step; _note_budget_stop reads it.
_STEP_RUNS = []

# Set when this call saved the current step's full result
# (_save_dataset). main() empties it before each step and passes it to
# _note_step_stopped, so a step that stops AFTER saving is recorded as having
# written its result in this call, not left to be guessed from the file.
_STEP_SAVED = []


def _launch_recover(spec, actor_input, stage_dir, subkey, **run_kw):
    """Launch an actor run, but first try to recover a run already launched for
    this exact input (a transient drop must never re-charge). Records the run
    handle at launch (on_start) so a mid-run or mid-fetch drop resumes by id."""
    _STEP_RUNS.append(subkey)
    ih = _input_hash(actor_input)
    pend = _read_pending(stage_dir)
    if pend and pend.get("subkey") == subkey and pend.get("input_hash") == ih:
        rec = A.reattach(pend.get("runId"), on_status=run_kw.get("on_status"))
        if rec is not None:
            print(f"   ↻ recovered run {pend.get('runId')} for {subkey} (no re-charge)")
            _arm_counted(spec.get("name", subkey), pend)
            return rec
        _clear_pending(stage_dir)  # unrecoverable -> fall through and re-launch
    actor = spec["actor"]

    def _on_start(rid, dsid, _sd=stage_dir, _ih=ih, _sk=subkey, _a=actor):
        _write_pending(_sd, {"runId": rid, "datasetId": dsid, "input_hash": _ih,
                             "subkey": _sk, "actor": _a})
    return A.run_actor(actor, actor_input, on_start=_on_start, **run_kw)


# --- a run that says it did not work ---------------------------------------
# A run can end SUCCEEDED with 0 items because the actor itself did no work. Once,
# scrapesage/linkedin-ad-library-scraper was put in maintenance mode by
# its developer; the run ended SUCCEEDED, exit 0, 0 items, and said so only in its
# statusMessage. The tool then called it "a genuine zero" for HubSpot, which was
# false. The run object already fetched carries that message, so reading it needs
# no new call. The notice is written next to the stage's output, and the entry
# script turns it into a gap for the stage (it knows the source and competitor).
# The word list is adjustable; it only matters for a run that returned 0 items.
_NOTICE_WORDS = ("maintenance", "disabled", "blocked", "deprecated", "suspended",
                 "not returning data", "temporarily unavailable")


def _run_notice(run, n_items):
    """The run's own statusMessage when the run returned 0 items and the message
    says the actor did not work (maintenance, disabled, blocked ...); else None."""
    if n_items:
        return None
    msg = str((run or {}).get("statusMessage") or "").strip()
    low = msg.lower()
    return msg if msg and any(w in low for w in _NOTICE_WORDS) else None


def _write_notice(stage_dir, notices, charged_usd):
    """Write <stage>/.run_notice.json when a run of this stage said it did not
    work; remove a stale one otherwise (a --force re-run that worked)."""
    p = Path(stage_dir) / ".run_notice.json"
    if not notices:
        try:
            p.unlink()
        except OSError:
            pass
        return
    p.write_text(json.dumps({"status_message": notices[0],
                             "charged_usd": round(float(charged_usd or 0.0), 6)},
                            ensure_ascii=False), encoding="utf-8")
    print(f"   \u26a0 the scraper reported it did not work: {notices[0]}")


# --- a run that did not finish its work ---------------------------
# <stage>/.run_incomplete.json records every kept run whose RUN INCOMPLETE block
# was printed (run["_incomplete"], apify_client); the entry script builds a
# scheduled run's notification from it. Full reason: the pain-and-messaging copy.
_INCOMPLETE_NAME = ".run_incomplete.json"


# The record of a step its settings asked for 0 results (see _run_one).
# It is not a record of a step that did not finish: the entry script reads it to
# name the step in the closing lines, never as an empty result.
_NOT_RUN_BY_SETTING_NAME = ".not_run_by_setting.json"


def _note_not_run_by_setting(stage_dir, stage):
    """Record that this step was not run because its settings ask for 0 results."""
    try:
        (Path(stage_dir) / _NOT_RUN_BY_SETTING_NAME).write_text(
            json.dumps({"stage": stage, "reason": "zero_count"}), encoding="utf-8")
    except OSError:
        pass


def _clear_not_run_by_setting(stage_dir):
    """Remove that record when the step does run (its settings changed)."""
    try:
        (Path(stage_dir) / _NOT_RUN_BY_SETTING_NAME).unlink()
    except OSError:
        pass


def _clear_incomplete(stage_dir):
    """Remove a stage's record from an earlier attempt (a --force re-run)."""
    try:
        (Path(stage_dir) / _INCOMPLETE_NAME).unlink()
    except OSError:
        pass


def _record_incomplete(stage_dir, stage, entry):
    """Add one entry to <stage>/.run_incomplete.json (created on first use)."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc["stage"] = stage
    doc.setdefault("entries", []).append(entry)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _note_run_incomplete(stage_dir, stage, what, run, n_items):
    """Record a run whose RUN INCOMPLETE block was printed (run["_incomplete"]).
    what: which run of the stage it was, in plain words ("full run", ...)."""
    info = (run or {}).get("_incomplete")
    if not info:
        return
    _record_incomplete(stage_dir, stage, {"kind": "run_ended", "what": what,
                                          "records": int(n_items or 0), **info})


def _note_step_stopped(stage_dir, stage, kind, cap_usd=None, spent_usd=None,
                       part_way=False, rerun_reuses_run=False,
                       result_saved=False, after_step=None):
    """Record that this step stopped before it finished.
    kind "step_failed": it raised an error; "budget_stop": the run reached the
    budget cap at this step. cap_usd: the cap it was stopped at, so a
    reader can name the figure without working it out again. spent_usd and
    part_way (see _note_budget_stop): the step had already run part of
    its work and been charged for it when the cap stopped it; the record then says
    "part_way", with what budget.charge() counted for the step. rerun_reuses_run:
    a re-run of the folder picks up the scraper run this step left
    instead of launching it again. Without them the record is exactly what it was before.

    WHY: a step that raised printed one warning line, the run went on, and its
    closing lines then read the empty result as "it ran, but nothing came back",
    so the user believed the step had run. The entry script names every recorded
    step where the user reads how the run went (the same record the RUN INCOMPLETE line uses,
    read by _incomplete_lines). A record of the same kind from an
    earlier attempt is replaced, so a step that fails twice is named once; a
    re-run that gets as far as the step's test run clears it (_clear_incomplete). A newer stop
    also replaces the record _note_kept_after_failed_attempt made."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        doc = {}
    entries = [e for e in doc.get("entries", [])
               if not (isinstance(e, dict)
                       and e.get("kind") in (kind, "kept_after_failed_attempt"))]
    entry = {"kind": kind}
    if cap_usd is not None:
        entry["cap_usd"] = round(float(cap_usd), 4)
    if float(spent_usd or 0.0) > 0 or part_way:
        entry["part_way"] = True
        entry["spent_usd"] = round(float(spent_usd or 0.0), 6)
        if rerun_reuses_run:
            entry["rerun_reuses_run"] = True
    if result_saved:
        # This call saved the step's result before it stopped, so
        # the result in the folder is this call's, not an earlier run's.
        entry["result_saved"] = True
    if after_step:
        # A later step of the same call that was never priced on its own: the
        # step before it was stopped, so this one never started. The readers
        # name that step instead of blaming this step's size or the cap.
        entry["after_step"] = after_step
    doc["stage"] = stage
    doc["entries"] = entries + [entry]
    try:
        Path(stage_dir).mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _note_kept_after_failed_attempt(stage_dir):
    """A run that SKIPS a finished step re-labels the step's stop record.

    WHY: <stage>/.run_incomplete.json is written by the run that failed or was stopped,
    and every later run of the folder read it as its own: a plain re-run that skipped
    the step (so made no attempt at it) was told "this run did not finish" and, after
    a crash that came after the save, that "this run" saved the result. The skipping
    run knows what happened, so it says so in the record, once: kind
    "kept_after_failed_attempt", "was" holding the old kind and every other field kept
    (result_saved, part_way, spent_usd, cap_usd). Readers word that kind as a finished
    result kept from an earlier fetch whose latest attempt to fetch again failed. The
    fact is kept, not cleared. A run that fails the step again replaces it
    (_note_step_stopped)."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        stops = [e for e in doc["entries"]
                 if isinstance(e, dict) and e.get("kind") in ("step_failed", "budget_stop")]
        for e in stops:
            e["was"], e["kind"] = e["kind"], "kept_after_failed_attempt"
        if stops:
            p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _note_budget_stop(workdir, order, stage, cap_usd, step_spent_usd, step_runs=None,
                      force=False):
    """Record a budget stop for `stage` and every later step of `order`.

    WHY: budget.check() and budget.enforce() can raise INSIDE _run_one after this
    step had already run part of its work and been charged for it: after its test
    pass, when the full estimate recalibrated from the test's actual rate goes over
    the cap; between the batches of a batched step; and (pain) before an entity
    refresh, after the full run had already run. The record said only
    "budget_stop", so the closing lines told the user the step "did not run".
    step_spent_usd: what budget.charge() counted for this step in this call
    (budget.spent now, minus budget.spent before the step started). Every run is counted the
    moment it ends, so this is every run the step was
    billed for, the full run before a refresh included.
    step_runs: the subkeys of the runs the step launched or picked up in
    this call (_STEP_RUNS). Any run makes the record part-way, a $0 one too. When
    the step's .pending_run.json still names a run (its output was not saved: the
    refresh case), a re-run of the folder picks it up without a new charge only if
    it is the first run the step launches again (_launch_recover matches subkey
    and input); otherwise an earlier run of the re-run (its test pass) replaces the
    handle and the run is launched and billed again. The later steps never
    started: they keep the plain record plus after_step, the step that was stopped
    before them, so the closing lines can say that instead of blaming their own size."""
    step_runs = list(step_runs or [])
    for left in order[order.index(stage):]:
        stage_dir = Path(workdir) / STAGES[left]["folder"]
        if _stage_done(stage_dir, STAGES[left]) and not force:
            continue  # Finished and not run again, so not stopped
        if left != stage:
            _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd,
                               after_step=stage)
            continue
        pending_now = _read_pending(stage_dir) or {}
        _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd,
                           spent_usd=max(0.0, float(step_spent_usd or 0.0)),
                           part_way=bool(step_runs),
                           rerun_reuses_run=bool(step_runs)
                           and pending_now.get("subkey") == step_runs[0])


def _annotate_budget_stop(workdir, stage, exc):
    """Add the figures of a refused forecast to the stop record that
    _note_budget_stop wrote for `stage`: forecast_usd (the cost forecast that did not
    fit), run_spent_usd (what the run had already spent when it was refused) and, when
    the exception carries it, first_pass (what a small first search found and cost).
    The cap is already in the record (cap_usd).

    WHY: the record said only "budget_stop", so the closing lines told the buyer "the
    run reached the budget cap" and offered one remedy, to raise the cap. In a real
    buyer's run $0.0362 of a $3.00 cap had been spent and a $6.00 forecast was
    refused: the cap was never reached, the forecast was over it, and making the step
    smaller was a remedy the lines never named. With the figures in the record the
    lines say what happened. A stop that is not a refusal of a forecast (Budget.
    enforce(): spend already over the cap) carries no forecast and leaves the record
    as it was, and so do records written before this existed; the readers fall back
    to words that are true of both. Never raises: bookkeeping must not change what a
    run does."""
    estimate = getattr(exc, "estimate_usd", None)
    if estimate is None:
        return
    p = Path(workdir) / STAGES[stage]["folder"] / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        entry = [e for e in doc["entries"]
                 if isinstance(e, dict) and e.get("kind") == "budget_stop"][-1]
        entry["forecast_usd"] = round(float(estimate), 6)
        entry["run_spent_usd"] = round(float(getattr(exc, "spent_usd", 0.0) or 0.0), 6)
        first_pass = getattr(exc, "first_pass", None)
        if first_pass:
            entry["first_pass"] = first_pass
        min_usd = getattr(exc, "min_usd", None)
        if min_usd:
            entry["min_usd"] = round(float(min_usd), 6)
        ceiling_usd = getattr(exc, "ceiling_usd", None)
        if ceiling_usd:
            entry["ceiling_usd"] = round(float(ceiling_usd), 6)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass


def _run_check_incremental(stage, spec, cfg, workdir):
    """No-op stub: search-intelligence stages are not in D2_INCREMENTAL.

    Pain skill only has the real implementation.  This stub keeps _run_one
    AST-identical across all three skill copies.
    """
    return None, None


# --- one more request for an item that came back empty ---------
# "retry for that single keyword if the projected cost is below 0.25." This
# project's founding money incident was an automatic re-purchase of finished work, so the
# second request is bounded, priced before it runs, inside the run's cap, recorded before
# it launches, and said out loud. Each rule below is FROZEN; its reason is beside it.
_EMPTY_RETRY_FILE = ".empty_retry.json"
_EMPTY_RETRY_MAX_USD = 0.25      # The bound, per keyword
_EMPTY_RETRY_INPUT_KEY = {"A2": "keywords", "B1": "queries"}   # the input field the items came from
_EMPTY_RETRY_WORDS = {"A2": ("keyword", "keywords", "came back with no data from Semrush"),
                      "B1": ("Google search page", "Google search pages", "came back as an error")}


_EMPTY_RETRY_UNREADABLE = "the note of what was already asked for could not be read"
_EMPTY_RETRY_LOGGED = ("the note of what was already asked for is missing, but the run log shows "
                       "it was asked once before")
# When "a bit more money" would let the second request fit the cap,
# the user is asked; "a bit" is at most this much over what is left of the cap. FROZEN: it is the
# user's line to set, not ours.
_EMPTY_RETRY_EXTRA_USD = 0.25


def _call_ceiling(stage, n):
    """The most one second request for n keys can be charged: the estimate times
    CHARGE_CEILING_FACTOR, the same ceiling every paid call is given as its spending limit.
    0.0 for none. The call is launched only when this whole amount fits."""
    return C.estimate(stage, n, runs=1)[0] * C.CHARGE_CEILING_FACTOR if n else 0.0


def _plan_second_request(stage, ask, left):
    """What to do with the keys `ask` when `left` dollars of the cap remain:
      ("all", ask, [], None)           their whole ceiling fits: launch for all of them;
      ("offer", [], ask, offer)        it passes `left` by at most $0.25 (A2 only): spend nothing and
                                       offer the extra to the user, who is asked before anything is spent;
      ("part", ask[:k], ask[k:], None) it passes by more (or the offer is not made): ask only for the
                                       first k keys that fit, in the order of the step's result. k may be 0.
    The call's limit is never squeezed below its ceiling: a launch needs the full ceiling inside `left`."""
    need = _call_ceiling(stage, len(ask))
    if need <= left + 1e-9:   # a billionth of a dollar is float noise (0.0846 - 0.0576), not a shortfall
        return "all", ask, [], None
    short = max(round(need - left, 6), 0.000001)   # a real shortfall is never offered as "$0.00 more"
    if stage == "A2" and short <= _EMPTY_RETRY_EXTRA_USD:
        return "offer", [], ask, {"keys": list(ask), "need_usd": round(need, 6), "left_usd": round(left, 6),
                                  "short_usd": short, "extra_usd": cents_up(short)}
    k = max(n for n in range(len(ask) + 1) if _call_ceiling(stage, n) <= left + 1e-9)
    return "part", ask[:k], ask[k:], None


def _empty_retry_record(stage_dir):
    """<stage>/.empty_retry.json as a dict. "asked" is the once-ever list of every key a second
    request was launched for in this folder.

    This fails CLOSED. A missing file is {} (a first run looks like that; see
    _second_request_logged for the one other sign). A file that exists but cannot be read, or
    does not have the shape this code writes, is {"closed": <plain reason>}: it reads as "may
    already have been asked", never as "never asked", because a crash in the middle of a write
    leaves exactly such a file and the next forced run would otherwise buy the keys again."""
    try:
        text = (Path(stage_dir) / _EMPTY_RETRY_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"closed": _EMPTY_RETRY_UNREADABLE}
    try:
        doc = json.loads(text)
    except ValueError:
        return {"closed": _EMPTY_RETRY_UNREADABLE}
    lists_ok = isinstance(doc, dict) and all(
        isinstance(doc.get(k, []), list) and all(isinstance(x, str) for x in doc.get(k, []))
        for k in ("asked", "in_flight"))
    shape_ok = lists_ok and isinstance(doc.get("last", {}), dict) \
        and isinstance(doc.get("closed", ""), str)
    return doc if shape_ok else {"closed": _EMPTY_RETRY_UNREADABLE}


def _drop_offer(stage_dir):
    """Nothing on offer still lacks data: the offer and its closing note go."""
    rec = _empty_retry_record(stage_dir)
    if isinstance(rec.get("offer"), dict) and not rec.get("closed"):
        _empty_retry_save(stage_dir, {k: v for k, v in rec.items() if k not in ("offer", "last")})


def _write_whole(path, items):
    """Write a JSON file whole or not at all: a temp file in the same folder, then os.replace,
    so a crash at any moment leaves the old file, never a missing or half-written one (for the
    step's result, _stage_done means "that file exists")."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _empty_retry_save(stage_dir, doc):
    """Write the once-ever record whole or not at all."""
    _write_whole(Path(stage_dir) / _EMPTY_RETRY_FILE, doc)


def _empty_retry_settle(stage_dir, last, **extra):
    """The record once the request has ended in this process: nothing in flight, `last` as given.
    stale_files=True: the saved answers may be ahead of the files built from them."""
    keep = {k: v for k, v in _empty_retry_record(stage_dir).items()
            if k not in ("in_flight", "in_flight_usd", "stale_files")}
    _empty_retry_save(stage_dir, {**keep, "in_flight": [], "last": last, **extra})


def _reason(exc):
    """A short plain reason for a failed write or call, with no file name in it."""
    return getattr(exc, "strerror", None) or type(exc).__name__


def _note_second_request_started(stage_dir, stage, run_id, dataset_id, limit_usd):
    """Name the second request's run in the run log when it starts, so a request
    that dies can be found on the Apify console. The run id is INFORMATION ONLY: nothing reads it
    back (no resume). The heading counts as "asked once before" when the record is gone
    (_second_request_logged)."""
    try:
        with open(Path(stage_dir) / "RUN_LOG.md", "a", encoding="utf-8") as f:
            f.write(f"\n### {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ·  {stage} "
                    f"(second request launched)\n- **Run ID:** `{run_id}`  ·  **Dataset:** `{dataset_id}`\n"
                    f"- **Note:** may have been charged, at most ${limit_usd:.4f}. Written when the request "
                    f"starts; its own entry follows only if it ends.\n")
    except OSError:
        pass


def _second_request_logged(stage_dir):
    """True when the step's run log holds a "(second request)" entry, or a "(second request
    launched)" one: the second sign, beside the record, that this folder asked once before.
    The first is written after a second request finished, the second when one starts
    (so also for one that died after it may have been charged). A record is
    written before every request, so the log can show one while the record is missing only if the
    record was deleted. The launch line's run id is never used to pick the run up (no resume)."""
    try:
        text = (Path(stage_dir) / "RUN_LOG.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return "(second request)" in text or "(second request launched)" in text


def _retry_input(stage, base_input, keys):
    """The step's own input for these keys only (search-results queries are one text)."""
    k = _EMPTY_RETRY_INPUT_KEY[stage]
    return {**base_input, k: "\n".join(keys) if stage == "B1" else list(keys)}


def _retry_empty_items(stage, spec, items, base_input, stage_dir, budget, args, cfg=None,
                       workdir=None, approved=None):
    """Ask once more for the items of a finished step that came back empty, and save the step's
    result again with the answers that carry data in place of the empty ones.

    Called by _run_after_save_hook AFTER the step's result is saved and built,
    so a crash here loses only this request: the next run finds the step finished and built,
    and buys nothing. (earlier designs ran it before the save or kept batch handles; both
    broke.)
    New answers replace the result whole and the files are built from it again; "stale_files" in
    the record is set before the replace and cleared after, so a kill in between is built again
    by the next plain run (postprocess). Every write after the billing is guarded and says in one
    plain sentence what became of the paid answer.
    A step with no detector (postprocess.empty_item_keys) does nothing; a dry run and a
    test-only run never reach the hook.
    (a) PRICE: only if config.estimate for ONE key is below $0.25 (the function that prices
        the forecast; a spend cap is floored by Apify at the actor's minimum, which would
        wrongly exclude cheap retries).
    (b) ONCE, EVER: the keys are written to <stage>/.empty_retry.json BEFORE the call and
        never removed (a --force run keeps them), so a crash, a resume or a forced re-run
        never buys a key twice. No record written, no call. A record that cannot be read, or
        a missing one the run log contradicts, closes the door. The record also
        marks the keys "in_flight" until this request has ended in this process; a mark still
        there in a later run means the request died, and the closing says so.
        NO RESUME: the request's run is not kept in any handle. A request that died is not
        picked up again, because fetching it later is a purchase decision the user did not make.
    (c) INSIDE THE CAP, with the FULL ceiling (replacing the earlier squeezed limit): a call is
        launched only when its whole ceiling (estimate x CHARGE_CEILING_FACTOR, _call_ceiling) fits
        in what the budget has left, and that ceiling is its spending limit. A limit squeezed below it
        could stop Semrush before it answered and still use up the keyword's one chance. When the
        ceiling passes what is left (_plan_second_request): by at most $0.25 (A2), nothing is
        spent and the offer is written into the record ("offer") for the user to answer; by more,
        only the first keys that fit are asked for (the step's result order) and the rest are named
        ("capped") and not recorded as asked. budget.charge(actual) after the call. An actor that
        ignores its limit overshoots, as every paid call does (e4cce21).
    (f) `approved`: the user said yes to the record's offer and `approved` dollars more than
        its cap. Only the offer's keys, once ever, and only when the ceiling now still fits in the
        offer's cap plus `approved` (at most $0.25 and at most the extra the user was shown);
        if the price grew past it nothing is spent and the offer is made again with the new figure.
    (e) adjustable, disclosed: ONE call carries the keys, at most the step's batch size (the
        actor's own per-call limit). Semrush has no start fee, so one call
        per key saves no money. Keys beyond one batch are left, named, and not recorded as
        asked: a later run may ask for them, once.
    No ratio guard: three real runs cannot calibrate one; the bound is (a) plus (c). A
    failure of the second request never costs the finished result."""
    key = _EMPTY_RETRY_INPUT_KEY.get(stage)
    flagged = P.empty_item_keys(stage, items) if key and not args.test_only else []
    if not flagged and approved is None:
        if key and not args.test_only:   # A run that finds nothing flagged ends any offer
            _drop_offer(stage_dir)
        return
    one, many, what = _EMPTY_RETRY_WORDS[stage]
    norm = lambda k: str(k).strip().lower()  # noqa: E731
    rec = _empty_retry_record(stage_dir)
    closed = rec.get("closed") or (_EMPTY_RETRY_LOGGED if not rec and _second_request_logged(stage_dir)
                                   else None)
    done = {norm(k) for k in rec.get("asked") or []}
    ask = [k for k in flagged if norm(k) not in done]
    offer0 = rec.get("offer") if isinstance(rec.get("offer"), dict) else {}
    left_out = []
    if approved is not None:   # Only the keys of the offer the user said yes to
        want = {norm(k) for k in offer0.get("keys") or []}
        left_out = [k for k in ask if norm(k) not in want]   # Still eligible, named in the closing
        ask = [k for k in ask if norm(k) in want]
        if not ask:   # no offer, or it was bought already, or all of it has data now: nothing to buy
            _drop_offer(stage_dir)
            print("   There is nothing left to ask for in this folder: no second request is on offer, "
                  "every keyword on offer has data now, or it was already asked for. Nothing was spent.")
            return
    limit = (spec.get("batch") or {}).get("size")
    if limit and len(ask) > limit:   # the actor's own per-call limit
        ask, left_out = ask[:limit], ask[limit:]
    unit = C.estimate(stage, 1)[0]
    est = C.estimate(stage, len(ask), runs=1)[0] if ask else 0.0
    last = {"flagged": flagged, "asked": [], "with_data": [], "still": flagged,
            "left_out": left_out, "limit": limit}
    print(f"   {len(flagged)} of {len(items)} {many if len(items) != 1 else one} {what}.")
    why, offer, held, grew = None, None, [], False
    if closed:
        why = closed
    elif not ask:   # "did not finish" is said only of the keys that did not
        hung = [k for k in flagged if norm(k) in {norm(x) for x in rec.get("in_flight") or []}]
        why = ("this folder already asked once before" if not hung else
               "an earlier second request for them did not finish" if len(hung) == len(flagged) else
               "an earlier second request for some of them did not finish, and the others were "
               "already asked once before")
    elif unit >= _EMPTY_RETRY_MAX_USD:
        why = (f"asking again usually costs about ${unit:.2f} each, which is not below "
               f"${_EMPTY_RETRY_MAX_USD:.2f}")
    elif approved is not None:   # The user said yes to up to `approved` more than the offer's cap
        shown = offer0.get("extra_usd")
        left0 = offer0.get("left_usd")
        need = _call_ceiling(stage, len(ask))
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (shown, left0)):
            why = "the offer in this folder could not be read"
        # never more than they were shown (not for keywords that now fit the cap, a yes of 0 buys them)
        elif need > left0 + 1e-9 and not 0 < approved <= min(_EMPTY_RETRY_EXTRA_USD, shown):
            offer = offer0   # the offer stands: they can still answer it with an amount within what they were shown
            why = (f"you approved ${approved:.2f}, which is not within the ${shown:.2f} you were shown "
                   f"and the ${_EMPTY_RETRY_EXTRA_USD:.2f} that can be offered")
        elif round(need - left0, 6) > approved:   # not enough for the price: spend nothing, ask again
            short = round(need - left0, 6)
            grew = short > float(offer0.get("short_usd", short)) + 1e-9   # Else only less than it needs
            if short <= _EMPTY_RETRY_EXTRA_USD:
                offer = {**offer0, "keys": list(ask), "need_usd": round(need, 6), "short_usd": short,
                         "extra_usd": cents_up(short)}
            why = (f"asking again can now be charged at most ${cents_up(need):.2f}, which is ${cents_up(short):.2f} "
                   f"more than what was left of your budget cap, and you approved ${approved:.2f}"
                   + ("" if grew else ", which is less than that"))
        else:
            budget.cap, budget.spent = left0 + approved, 0.0   # the cap as offered, plus the extra they said yes to
            try:
                limit_usd = _cap_for(args, budget, need)
            except BudgetExceeded:   # the scraper cannot start for what is left
                limit_usd = 0.0
            if limit_usd < need:
                why = "the spending limit left for this run is below what the request needs"
    else:
        need_all = _call_ceiling(stage, len(ask))
        mode, ask, held, offer = _plan_second_request(stage, ask, budget.remaining())
        est = C.estimate(stage, len(ask), runs=1)[0] if ask else 0.0
        if mode == "offer":   # short by at most $0.25: nothing is spent, the user is asked
            why = (f"asking again can be charged at most ${cents_up(offer['need_usd']):.2f}, which is "
                   f"${offer['extra_usd']:.2f} more than what is left of your budget cap, so it waits "
                   f"for your yes")
            offer.update(unattended=args.run_ceiling_usd is not None, more=len(left_out), limit=limit)
        elif not ask:   # short by more, and not even one key fits
            why = (f"asking again can be charged at most ${cents_up(need_all):.2f}, which would take the run past "
                   f"your budget cap")
        else:
            try:
                limit_usd = _cap_for(args, budget, _call_ceiling(stage, len(ask)))
            except BudgetExceeded:   # the scraper cannot start for what is left
                limit_usd = 0.0
            if limit_usd < _call_ceiling(stage, len(ask)):   # never launched with a squeezed limit
                why = "the spending limit left for this run is below what the request needs"
    if why:
        print(f"   Not asked again: {why}.")
        _empty_retry_save(stage_dir, {**{k: v for k, v in rec.items() if k != "offer"},
                                      **({"offer": offer} if offer else {}),
                                      **({"closed": closed} if closed else {}),
                                      "last": {**last, "left_out": [], "skipped": why,
                                               **({"offer": offer} if offer else {}),
                                               **({"approved_usd": approved, "price_grew": grew}
                                                  if approved is not None else {})}})
        return
    retry_in = _retry_input(stage, base_input, ask)
    last.update(asked=ask, estimate_usd=round(est, 6), capped=held,
                **({"approved_usd": approved} if approved is not None else {}))
    try:   # (b): the record comes first (with the most it may be charged); no record, no call
        _empty_retry_save(stage_dir, {"asked": list(rec.get("asked") or []) + ask,
                                      "in_flight": ask, "in_flight_usd": round(limit_usd, 6),
                                      "last": last})
    except OSError as exc:
        print(f"   Could not write the record that keeps a second request from being made "
              f"twice ({exc}), so nothing was asked for again.")
        return
    try:   # The total's "does not include" sentence is on disk BEFORE the call can charge
        import search_intel as _SI
        _SI._refresh_dead_note(workdir or Path(stage_dir).parent)
    except Exception:  # noqa: BLE001  (a note never stops the request that was approved)
        print("   Could not note in this folder's total billed that a second request is under way.")
    print(f"   Asking again, once (usually about ${est:.2f} in all, and it can be charged at most ${cents_up(limit_usd):.2f}).")
    if held:
        print(f"   {len(held)} more would take the run past your budget cap, so "
              f"{'they are' if len(held) != 1 else 'it is'} not asked for in this run.")
    if left_out:
        print(f"   {len(left_out)} more {'is' if len(left_out) == 1 else 'are'} not asked for again "
              f"in this run: one request asks for at most {limit}.")
    cost = None
    try:
        run = A.run_actor(spec["actor"], retry_in, memory_mb=_mem_for(spec, retry_in),
                          on_status=lambda s: print(f"     [second request: {s}]"),
                          on_start=lambda rid, dsid: _note_second_request_started(
                              stage_dir, stage, rid, dsid, limit_usd),
                          max_charge_usd=limit_usd)
        cost = A.actual_charge_usd(run) or 0.0   # counted before the fetch, as every run is
        budget.charge(cost)
        got = A.fetch_dataset(run.get("defaultDatasetId"))
    except Exception as exc:  # noqa: BLE001 - the finished result is not lost over an extra call
        print(f"   The second request did not complete ({str(exc)[:150]}); the result they "
              f"had is kept, and they are not asked for again."
              + (f" It was billed ${cost:.4f}." if cost else ""))
        _empty_retry_settle(stage_dir, {**last, "error": str(exc)[:150],
                                        **({"charged_usd": round(cost, 6)} if cost else {})})
        return
    try:   # The paid answer is not lost over a line in a log
        _note_run_incomplete(stage_dir, stage, "second request for empty items", run, len(got))
        append_run_log(stage_dir, f"{stage} (second request)", run, len(got), est_usd=est,
                       note="second request for items that came back empty")
        append_command(stage_dir, spec["actor"], retry_in, run, memory_mb=_mem_for(spec, retry_in))
    except Exception as exc:  # noqa: BLE001
        print(f"   The run log could not be completed for the second request ({_reason(exc)}).")
    fresh = {}
    for it in got:
        if not P.is_empty_item(stage, it):
            fresh.setdefault(P.item_key(stage, it), it)
    out = [fresh.get(P.item_key(stage, it), it) if P.is_empty_item(stage, it) else it for it in items]
    still = P.empty_item_keys(stage, out)
    last.update(charged_usd=round(cost, 6), with_data=[k for k in flagged if norm(k) in fresh],
                still=still)
    where = "answer"
    try:
        if out != items:   # the answers: the step's result whole or not at all, then what is built from it
            _empty_retry_save(stage_dir, {**_empty_retry_record(stage_dir), "stale_files": True})
            _write_whole(Path(stage_dir) / "run_02_full.json", out)
            where = "files"
            P.postprocess(stage, stage_dir, workdir or Path(stage_dir).parent, cfg)
        where = "note"
        _empty_retry_settle(stage_dir, last)
    except Exception as exc:  # noqa: BLE001 - the step is paid for and saved; say what became of the answer
        if where == "answer":   # the saved result was not replaced: the keys show as they did
            last.update(with_data=[], still=flagged, unsaved=True)
            print(f"   The second request was billed ${cost:.4f}, but its answer could not be saved "
                  f"({_reason(exc)}), so the {many} it asked for "
                  + ('still show as "no data from Semrush".' if stage == "A2" else "are still left out of the results."))
        elif where == "files":   # the answers are saved; the next plain run builds the files again
            print(f"   The second request's answers are saved, but updating what is built from them "
                  f"did not finish ({_reason(exc)}); the next run builds it again, at no charge.")
        else:
            print(f"   The second request's answers are saved and built, but the note of what was "
                  f"asked could not be updated ({_reason(exc)}).")
        try:
            _empty_retry_settle(stage_dir, last, **({"stale_files": True} if where == "files" else {}))
        except Exception:  # noqa: BLE001 - the record cannot be written either; the line above is all
            pass
        return
    print(f"   Asked again: {len(last['with_data'])} of {len(ask)} now "
          f"{'has' if len(ask) == 1 else 'have'} data, {len(still)} still "
          f"{'has' if len(still) == 1 else 'have'} none. Actually billed ${cost:.4f}.")


def _run_after_save_hook(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir):
    """Called by _run_one after the result is saved AND built (before the build, a kill
    here left a paid step with nothing built). Search uses it for the second request
    (_retry_empty_items). It never fails the step: what goes wrong is said in one sentence."""
    spent_before = budget.spent
    try:
        _retry_empty_items(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir)
    except Exception as exc:  # noqa: BLE001 - the step is paid for, saved and built
        paid = budget.spent - spent_before
        print(f"   The second request for empty items could not be finished ({_reason(exc)}). "
              + (f"It was billed ${paid:.4f}. " if paid > 0 else "Nothing was billed for it. ")
              + "The step's result and what is built from it are as they were.")


def _run_approved_second_request(args, cfg, workdir):
    """The user's yes to a second request that was offered (search_intel --second-request):
    makes ONLY that request, for the keywords in the folder's record, in the folder's finished
    volumes step. Every rule of the second request holds (_retry_empty_items, `approved`): priced,
    once ever, recorded before it launches, never on the test pass, at most one batch, no resume,
    traced in the run log, guarded so it never fails or empties the paid step, charged to the run's
    bill. Nothing else runs: no step, no test pass, no other source."""
    stage, spec = "A2", STAGES["A2"]
    stage_dir = Path(workdir) / spec["folder"]
    extra = args.second_request_extra_usd
    print(f"\n▶ Second request for keywords with no data (you approved up to ${extra:.2f} more "
          f"than the budget cap)")
    try:
        items = json.loads((stage_dir / "run_02_full.json").read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise ValueError("it is not a list")
        base_input, _ = spec["build"](cfg, str(workdir), test=False)
    except (OSError, ValueError) as exc:
        print(f"   Nothing was asked for: this folder has no finished keyword volumes to ask about "
              f"({_reason(exc)}). Nothing was spent.")
        return
    budget = Budget(args.phase, 0.0)
    # main() names the scraper of each step it runs before _cap_for is asked for its limit, so
    # _cap_for knows the smallest limit Apify accepts for it. This call comes before main()'s
    # loop and named none: the request was then limited to what 0.01 allows, and Apify raises a
    # smaller limit to the scraper's own minimum, so a request could be billed past the cap.
    real_actor, _CAP_ACTOR[0] = _CAP_ACTOR[0], spec.get("actor")
    try:
        _retry_empty_items(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir,
                           approved=extra)
    except Exception as exc:  # noqa: BLE001 - as the hook: what went wrong is said, nothing is hidden
        print(f"   The second request could not be finished ({_reason(exc)}). "
              + (f"It was billed ${budget.spent:.4f}. " if budget.spent > 0 else "Nothing was billed for it. ")
              + "The step's result and what is built from it are as they were.")
    finally:
        _CAP_ACTOR[0] = real_actor


def _run_after_fetch_hook(stage, spec, items, _inc, _repeat_of,
                          base_input, stage_dir, budget, args):
    """No-op stub: search-intelligence has no incremental merge logic.

    Pain skill only has the real implementation.  This stub keeps _run_one
    AST-identical across all three skill copies.
    """
    return items, None


def _run_one(stage, spec, cfg, workdir, budget, args):
    stage_dir = Path(workdir) / spec["folder"]
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Resume-safety: skip a stage that already completed (unless --force).
    if not args.dry_run and _stage_done(stage_dir, spec) and not args.force:
        print(f"\n▶ {spec.get('name', stage)} (step {stage}): already complete, "
              f"skipping (use --force to re-run).")
        # A step whose latest attempt failed keeps its result, and
        # the files built from it may have been removed; build them again, free.
        P.rebuild_dropped_outputs(stage, stage_dir, workdir, cfg)
        _note_kept_after_failed_attempt(stage_dir)
        return

    # --- incremental check (FROZEN) ----------------------------------
    # Compute early so --dry-run can show the injected filter field.
    _repeat_of, _inc = _run_check_incremental(stage, spec, cfg, workdir)

    _est_input, est_count = spec["build"](cfg, str(workdir), test=False)
    # One start charge per actor run the full input launches (config.actor_runs): a batched stage
    # such as Google ads (B5) pays one per batch.
    est_usd, unit, rate, unverified = C.estimate(stage, est_count,
                                                 runs=C.actor_runs(spec, _est_input))
    # The step's plain name, and its code for --only.
    print(f"\n▶ {spec.get('name', stage)} (step {stage})")
    # "query" is "queries" (a real run printed "~6 querys").
    print(f"   full-run forecast: ~{est_count} "
          f"{unit[:-1] + 'ies' if unit.endswith('y') else unit + 's'} → ~${est_usd:.2f}"
          + ("  [rate UNVERIFIED]" if unverified else ""))
    # A step whose full run counts 0 records is not
    # run. config.estimate prices a count of 0 at $0 ("a stage with nothing to do
    # launches no run"), and a $0 forecast gave the call no spending limit: with
    # google_max_items 0 the Google step sent maxItems 0 with no limit and billed
    # $0.10 under a $0.10 approval that Meta had already used half of. A 0 in a
    # count setting (accepted) means "none": nothing is launched,
    # nothing is spent, and the entry script names the step in its closing lines.
    # A 0 can also empty the full input itself. Search
    # builds its full input by slicing (queries[:serp_top_n], terms[:keyword_cap]),
    # so with a 0 the full input has no entries while the test input (the first 5)
    # still has some. The step then paid for its test run and the full call met
    # run_actor's "no spending limit" refusal: a false error, every month. So the
    # test input is asked too, before any call. Both are empty only when there is
    # nothing upstream to work on, which stays the "no inputs yet" skip below.
    _test_built = None
    if est_count <= 0 and not _has_inputs(stage, _est_input):
        _test_built = spec["build"](cfg, str(workdir), test=True)
    if est_count <= 0 and (_has_inputs(stage, _est_input)
                           or _has_inputs(stage, _test_built[0])):
        print("   your settings ask this step for 0 results, so it is not run and "
              "nothing is spent.")
        if not args.dry_run:
            # A record from an earlier attempt (the
            # step was stopped at the budget cap, or failed) is not true of this
            # run: the step was not run because of the setting, and only that is
            # said. Before, the closing lines gave both reasons.
            _clear_incomplete(stage_dir)
            _note_not_run_by_setting(stage_dir, stage)
            # The files an earlier run of this step left are not this
            # run's; this step is not run, so none were written for it now.
            P.drop_stage_outputs(stage, stage_dir, workdir)
        return
    if not args.dry_run:
        _clear_not_run_by_setting(stage_dir)

    # Dry-run clarity: when the actor's own minimum cap exceeds our forecast x
    # factor, the spend CAP is raised to that minimum so Apify accepts the run.
    # That cap is a ceiling, not the expected cost -- show it so the user does not
    # read the floor as the price.
    _fc_cap = est_usd * C.CHARGE_CEILING_FACTOR
    _fc_floor = A.actor_min_charge(spec["actor"])
    if _fc_floor > _fc_cap:
        print(f"   spend cap: ${max(_fc_floor, 0.01):.2f}, {spec['actor']}'s "
              f"minimum allowed cap (a ceiling, not the expected cost; the run "
              f"still bills ~the forecast)")

    if args.dry_run:
        di, _ = spec["build"](cfg, str(workdir), test=False)
        if _inc and _inc["mode"] == "incremental":
            di = {**di, _inc["filter_field"]: _inc["filter_value"]}
        blob = json.dumps(di, ensure_ascii=False)
        print(f"   input: {blob if len(blob) <= 600 else blob[:600] + ' …'}")
        return

    # --- TEST run ---
    _clear_incomplete(stage_dir)
    test_input, test_count = (_test_built if _test_built is not None
                              else spec["build"](cfg, str(workdir), test=True))
    if not _has_inputs(stage, test_input):
        print(f"   ⤫ no inputs for {spec.get('name', stage)} yet (missing upstream "
              f"output?), skipping.")
        # As above; the step did not run, so its earlier files are not
        # this run's.
        P.drop_stage_outputs(stage, stage_dir, workdir)
        return
    full_preview, _ = spec["build"](cfg, str(workdir), test=False)
    # When the full input is already no larger than a test pass, the test and the
    # full run send the SAME items — running both bills the actor twice for one
    # output, and for per-actor-start pricing (A1's actor charges a start fee per
    # run) that doubles the bill. A test run with 4 seeds paid the earlier A1
    # actor ~$0.46 this way for a single expansion. Skip the test in the normal path; the full-
    # estimate budget gate below still guards the cap, and the run is test-sized
    # so there is nothing to recalibrate. --test-only keeps running its one small
    # pass.
    _full_sz = _input_size(spec, full_preview)
    _test_sz = _input_size(spec, test_input)
    skip_test = (not args.test_only) and _full_sz is not None \
        and _test_sz is not None and _full_sz <= _test_sz

    if not skip_test:
        budget.check(est_usd)  # gate on the FULL estimate before spending a cent on the test
        # Spend cap even on the TEST run. A test builds only 3-5 items, but an actor
        # that ignores its own input cap (the live Capterra 3,058-vs-30 case) can
        # burn real money on a "test". Ceiling = the test's own forecast x factor.
        test_ceiling = C.estimate(stage, test_count)[0] * C.CHARGE_CEILING_FACTOR
        print("   running TEST …")
        run = _launch_recover(spec, test_input, stage_dir, "test",
                              memory_mb=_mem_for(spec, test_input),
                              on_status=lambda s: print(f"     [{s}]"),
                              max_charge_usd=_cap_for(args, budget, test_ceiling))
        # Every run is counted the moment its cost is known (the run
        # object that ended), before anything that can stop the step: fetching
        # its dataset can fail, and a later budget check can raise. Counted after
        # them, a billed run was left out of the budget and the run's bill.
        test_cost = A.actual_charge_usd(run)
        budget.charge(test_cost)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        _clear_pending(stage_dir)  # test output consumed -> nothing to recover
        append_run_log(stage_dir, f"{stage} (test)", run, len(items), est_usd=est_usd, note="test run")
        append_command(stage_dir, spec["actor"], test_input, run,
                       memory_mb=_mem_for(spec, test_input))
        n_test = _counted_records(stage, items)
        _notices = [m for m in [_run_notice(run, n_test)] if m]
        _notice_usd = test_cost
        print(f"   test: {n_test} records, reported ${test_cost:.3f} "
              f"(budget spent ${budget.spent:.2f}/{cap_text(budget.cap)})")

        if args.test_only:
            _save_dataset(stage_dir, "01_test", items) if items else None
            return

        # Recompute the full estimate from the ACTUAL test rate.
        if n_test and test_cost:
            actual_rate = test_cost / n_test
            est_usd = est_count * actual_rate * C.SAFETY_MARGIN
            print(f"   recalibrated full estimate from test: ~${est_usd:.2f} "
                  f"(${actual_rate:.5f}/{unit})")
    else:
        print(f"   full input is test-sized ({_full_sz} ≤ test {_test_sz}) "
              f"so the redundant test pass is skipped (it would bill the same run twice).")
    budget.check(est_usd)

    # --- FULL run ---
    if skip_test:
        _notices, _notice_usd = [], 0.0
    # Save base_input (no date filter) so after-fetch hook can use it without
    # the incremental filter that was injected for the date-windowed fetch.
    base_input, _ = spec["build"](cfg, str(workdir), test=False)
    full_input = base_input
    if _inc and _inc["mode"] == "incremental":
        full_input = {**base_input, _inc["filter_field"]: _inc["filter_value"]}
        print(f"   incremental: fetching only reviews since {_inc['filter_value']} "
              f"(forecast is the cap maximum)")
    print("   running FULL …")
    batch = spec.get("batch")
    arr = full_input.get(batch["key"]) if batch else None
    if batch and isinstance(arr, list) and len(arr) > batch["size"]:
        # This actor caps the input array per run. Split into chunks, run each,
        # concatenate the datasets into one run_02_full.json.
        size = batch["size"]
        chunks = [arr[i:i + size] for i in range(0, len(arr), size)]
        print(f"   batching {len(arr)} {batch['key']} → {len(chunks)} runs of ≤{size} …")
        items, full_cost = [], 0.0
        for bi, chunk in enumerate(chunks, 1):
            binput = {**full_input, batch["key"]: chunk}
            # this batch's slice of the full spend ceiling, proportional to its
            # share of the work array, so the cap tracks the batch it guards.
            # A stage whose batch spec says "per_run" counts its ceiling per actor
            # run (Google ads: maxItems is a total per run, so the expected count
            # is maxItems x runs). Every run may return a full maxItems, a short
            # last batch included, so each batch gets an equal share: one run's
            # worth. A proportional share gave a 2-URL last batch too little and
            # Apify would stop it over budget before it reached maxItems.
            bshare = (1 / len(chunks)) if batch.get("per_run") else (len(chunk) / len(arr))
            bceiling = est_usd * bshare * C.CHARGE_CEILING_FACTOR
            run = _launch_recover(spec, binput, stage_dir, f"batch{bi}",
                                  memory_mb=_mem_for(spec, binput),
                                  on_status=lambda s, bi=bi: print(f"     [batch {bi}/{len(chunks)}: {s}]"),
                                  max_charge_usd=_cap_for(args, budget, bceiling))
            bcost = A.actual_charge_usd(run)  # Counted before the fetch
            budget.charge(bcost)
            full_cost += bcost
            _notice_usd += bcost
            bitems = A.fetch_dataset(run.get("defaultDatasetId"))
            _note_run_incomplete(stage_dir, stage, f"batch {bi} of {len(chunks)}",
                                 run, len(bitems))
            items.extend(bitems)
            _notices += [m for m in [_run_notice(run, len(bitems))] if m]
            _clear_pending(stage_dir)  # this batch's output consumed
            # Logged before the cap is enforced, so the run log and
            # commands.sh list the batch that crossed the cap too (it was billed).
            append_run_log(stage_dir, f"{stage} (full batch {bi}/{len(chunks)})", run, len(bitems), est_usd=est_usd)
            append_command(stage_dir, spec["actor"], binput, run,
                           memory_mb=_mem_for(spec, binput))
            # Enforce the cap after each batch's charge so an over-budget
            # run aborts before launching the NEXT batch. Safe boundary: the next
            # batch has not yet started; no half-processed data on disk.
            budget.enforce()
        _only_err = P.only_error_items(items)  # As for a single run, below
        if _only_err:
            raise RuntimeError(_only_err)
        # After-fetch hook (FROZEN: merge raw BEFORE save, BEFORE postprocess)
        items, new_only = _run_after_fetch_hook(
            stage, spec, items, _inc, _repeat_of, base_input, stage_dir, budget, args)
        if new_only is not None:
            _save_dataset(stage_dir, "02_new", new_only)
            print(f"   incremental: {len(new_only)} new, {len(items)} total after merge+dedup+truncation")
        _save_dataset(stage_dir, "02_full", items)
        _write_notice(stage_dir, _notices, _notice_usd)
        print("   " + _records_saved_text(stage, items, len(chunks)))
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{cap_text(budget.cap)}")
    else:
        run = _launch_recover(spec, full_input, stage_dir, "full",
                              memory_mb=_mem_for(spec, full_input),
                              on_status=lambda s: print(f"     [{s}]"),
                              max_charge_usd=_cap_for(args, budget, est_usd * C.CHARGE_CEILING_FACTOR))
        # Counted before the fetch and the after-fetch hook. The hook's
        # entity refresh checks the budget and can stop the step; counted after
        # it, the billed full run was missing from the budget that check compared
        # against and from the run's bill.
        full_cost = A.actual_charge_usd(run)
        budget.charge(full_cost)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        _only_err = P.only_error_items(items)
        if _only_err:
            # The run was billed and is listed, but it brought no
            # result: nothing is saved over an earlier result, and the step fails.
            _clear_pending(stage_dir)
            append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd,
                           note="only an error came back")
            append_command(stage_dir, spec["actor"], full_input, run,
                           memory_mb=_mem_for(spec, full_input))
            raise RuntimeError(_only_err)
        _note_run_incomplete(stage_dir, stage, "full run", run, len(items))
        _notices += [m for m in [_run_notice(run, len(items))] if m]
        # After-fetch hook (FROZEN: merge raw BEFORE save, BEFORE postprocess)
        try:
            items, new_only = _run_after_fetch_hook(
                stage, spec, items, _inc, _repeat_of, base_input, stage_dir, budget, args)
        except Exception:
            # The step stops here with the full run billed and counted
            # but its output not saved; the run log and commands.sh still list it.
            append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd)
            append_command(stage_dir, spec["actor"], full_input, run,
                           memory_mb=_mem_for(spec, full_input))
            raise
        if new_only is not None:
            _save_dataset(stage_dir, "02_new", new_only)
            print(f"   incremental: {len(new_only)} new, {len(items)} total after merge+dedup+truncation")
        _save_dataset(stage_dir, "02_full", items)
        _write_notice(stage_dir, _notices, _notice_usd + full_cost)
        _clear_pending(stage_dir)  # full output saved -> nothing to recover
        append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd)
        append_command(stage_dir, spec["actor"], full_input, run,
                       memory_mb=_mem_for(spec, full_input))
        print("   " + _records_saved_text(stage, items))
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{cap_text(budget.cap)}")

    # Derived artifacts for downstream stages (B1 -> competitor domains).
    P.postprocess(stage, stage_dir, workdir, cfg)
    # After the result is saved AND built, so a kill in the hook leaves a built step.
    _run_after_save_hook(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir)


def _real_entries(lst):
    """Filter a list to entries that are 'real' inputs.

    A bare bool(["", "  ", None]) is True, so a simple bool(v) guard passes
    lists of blank/None values and launches a paid actor run on garbage input.
    This helper keeps only entries that are substantive:
      - strings: non-empty after strip()
      - non-strings (dicts like {"url": "..."}, numbers, etc.): truthy value
    None and whitespace-only strings are discarded.
    """
    result = []
    for item in lst:
        if isinstance(item, str):
            if item.strip():
                result.append(item)
        elif item:   # truthy non-string: dict, int, etc.
            result.append(item)
    return result


def _has_inputs(stage, actor_input):
    """Cheap guard: don't launch a stage whose input arrays are empty.

    `keywords` is included because A1 (expansion) and A2 (metrics) key off it:
    an empty keyword universe must SKIP the stage, not launch the actor with
    an empty input. A1's actor bills a start fee per run, so an empty run is
    a real (small) charge for zero output — the exact silent-spend this tool's
    rework exists to kill. With seed resolution persisted upstream this array is
    normally non-empty; the guard protects the direct `run_phase.py --only`
    entry point too, where no caller has resolved seeds first.

    Uses _real_entries() so that lists of blank/None values are treated as absent
    rather than present (ADV-014). `queries` (google-search-scraper) is a
    newline-separated string, not a list — it needs its own branch; iterating a
    string yields characters which would make any non-blank string look non-empty."""
    for key in ("queries", "keywords", "startUrls", "urls"):
        v = actor_input.get(key)
        if v is not None:
            if isinstance(v, str):
                return bool([q for q in v.splitlines() if q.strip()])
            return bool(_real_entries(v))
    return True


def _input_size(spec, inp):
    """Count of items in the actor's primary work array — the unit a run is
    billed on. Used to detect when a 'full' run is already no larger than a test
    pass, so the test can be skipped instead of billing the same run twice. B1's
    queries arrive as a newline-joined string, so count non-blank lines there.

    Returns None (meaning "do not skip the test") whenever the primary array is
    ambiguous: an unrecognised shape, or several known target arrays present at
    once (e.g. Reddit's searchTerms / subredditUrls / startUrls, where the
    billed unit is not one single array). Skipping on an input we cannot size
    would drop the test (and its rate recalibration) from large runs too. A
    spec-declared batch key is authoritative and always wins. The key list covers
    the known input shapes across all supported actors so the engine stays correct
    everywhere."""
    if not isinstance(inp, dict):
        return None
    batch = spec.get("batch")
    if batch and isinstance(inp.get(batch["key"]), list):
        return len(inp[batch["key"]])
    keys = ("queries", "keywords", "startUrls", "urls", "companyUrls", "appIds",
            "subredditUrls", "searchTerms", "searchStringsArray", "profileUrls")
    present = [k for k in keys if inp.get(k)]
    if len(present) != 1:
        return None
    v = inp[present[0]]
    if isinstance(v, list):
        return len(v)
    if isinstance(v, str):
        return len([ln for ln in v.splitlines() if ln.strip()])
    return None


def _mem_for(spec, actor_input):
    """Memory (MB) for one actor run.

    Some actors require AT LEAST one input URL per 512 MB of RAM — the Meta ads
    scraper (B4) rejects a single-URL run at the default 1024 MB with
    "You must provide atleast 1 input URL per 512MB of RAM memory." A single
    Meta term in a single country is one URL, a completely normal request, so
    memory must scale to the URL count. Specs that declare `mem_per_url` get
    memory capped at mem_per_url × (input URL count); others use memory_mb.
    """
    base = spec.get("memory_mb", 4096)
    per = spec.get("mem_per_url")
    if not per:
        return base
    n = 0
    for key in ("urls", "startUrls"):
        v = actor_input.get(key)
        if isinstance(v, list):
            n = max(n, len(v))
    return min(base, per * max(1, n))


# The scraper of the step now running, so _cap_for knows the smallest limit Apify accepts for
# it. main() sets it before each step; None (a caller that did not) means only 0.01 applies.
_CAP_ACTOR = [None]


def _smallest_limit(actor):
    """The smallest max charge Apify accepts for `actor`: its own minimum when that is known and
    above 0.01, else 0.01. An unreadable or missing minimum is never a reason to refuse."""
    smallest = 0.01
    if actor:
        try:
            m = float(A.actor_min_charge(actor))
            if m == m and m != float("inf") and m > smallest:
                smallest = m
        except Exception:  # noqa: BLE001
            pass
    return smallest


def _left_down(left, need):
    """What is left, as a refusal prints it: rounded DOWN, never up, to cents, or to more
    decimals when the shorter figure would read the same as the smallest limit. Rounded to
    nearest, 0.496 left of a 0.50 minimum read "less than $0.50, and only $0.50 is left"."""
    left = max(0.0, float(left))
    shown = cap_text(need)
    for digits in (2, 4, 6, 8):
        scale = 10 ** digits
        text = f"{int(left * scale + 1e-6) / scale:.{digits}f}"
        if text != shown:
            return text
    return text


def _cap_for(args, budget, est_cap):
    """Per-call maxTotalChargeUsd: est_cap, never more than what is left of the cap.

    WHY: est_cap is the forecast times CHARGE_CEILING_FACTOR, so a step whose forecast
    fits the cap by a hair was still handed 1.5 times that, and a run priced above its
    forecast could bill past a cap the user set as a hard limit. Every launch (test pass,
    batch, full run, first pass) takes its limit from here, so bounding it here bounds all
    of them in all three tools without touching the launch code. A run stopped at this
    limit ends ABORTED and is reported as cut short, never as complete. The same bound
    applies to the approved total-run ceiling when one is set.

    Apify refuses a limit below the scraper's own minimum and the client raises a smaller
    one to it (and to 0.01), so a run launched with less than that left would bill past the
    cap. Then this raises the budget's own BudgetExceeded, before anything is launched, and
    the step is refused like any step the cap does not allow; the record carries min_usd so
    the closing lines say it is the scraper's smallest limit, not the step's size.
    """
    left = budget.remaining()
    ceiling = getattr(args, "run_ceiling_usd", None)
    # The approved ceiling is the limit in force when it leaves no more than the cap does (a
    # scheduled run's cap is clamped to it): then it is what ran out, and raising budget_caps
    # would change nothing.
    ceiling_binds = ceiling is not None and ceiling - budget.spent <= left + 1e-9
    if ceiling is not None:
        left = min(left, ceiling - budget.spent)
    need = _smallest_limit(_CAP_ACTOR[0])
    if left + 1e-9 < need:
        exc = BudgetExceeded(
            f"Phase {budget.phase}: the scraper for this step cannot start for less than "
            f"${cap_text(need)}, and only ${_left_down(left, need)} of "
            + (f"your approved ceiling ${cap_text(ceiling)} is left. Raise the approved ceiling, "
               f"or stop." if ceiling_binds else
               f"the cap ${cap_text(budget.cap)} is left. Raise budget_caps in the config, "
               f"or stop."))
        exc.estimate_usd = need
        exc.spent_usd = budget.spent
        exc.cap_usd = budget.cap
        exc.min_usd = need
        if ceiling_binds:
            exc.ceiling_usd = ceiling
        raise exc
    return min(est_cap, max(0.01, left))


def _say_how_search_results_were_chosen(stage, spec, cfg, workdir, args):
    """Print the one line that says the Google search-results step is using the
    first keywords in list order instead of the highest-volume ones, when that is
    what it will do (the keyword volumes are not available). Nothing is printed
    when the step picks by volume, when it will not run (it finished earlier and
    --force was not given), or when it has no keywords to pull.

    WHY here and not in the step builder: the builder also runs for the forecast
    and the test pass, so a line printed there would appear several times and for
    a forecast that has no folder at all. This is search-specific by design (main
    and the stage list already are); the shared _run_one is left as it is."""
    if stage != "B1":
        return
    if not args.dry_run and not args.force and \
            _stage_done(Path(workdir) / spec["folder"], spec):
        return
    note = S.serp_selection_note(cfg, str(workdir), dry_run=args.dry_run)
    if note:
        print(f"\n{note}")


_ENDED_BADLY = ("FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT")


def _finite_charge(x):
    """True for a charge that is a finite number of at least 0 (anything else is unreadable)."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool) and x == x
            and x not in (float("inf"), float("-inf")) and x >= 0)


# A RUN AN EARLIER COMMAND LEFT IS COUNTED WHEN THIS COMMAND READS IT (FROZEN).
# .pending_run.json names the one run of a step that was launched and not consumed. A step
# refused at the forecast gate never reaches _launch_recover, and a finished run that this
# command will not use used to be left in the handle or overwritten by the next launch: its
# charge was on the Apify bill and in nobody's count (a test pass of 0.045 billed, 0.0000
# shown). Such a run is read here, outside _run_one, and:
#   * finished (any end): its final charge is counted once (bill, ledger, RUN_LOG) and the bill
#     line says so (a note the entry script's closing reads);
#   * a run that SUCCEEDED and that a later run of this step picks up (_recoverable_runs: its test
#     pass, or its full run when the test pass is skipped) is kept, because that later run uses it
#     with no new Apify charge; every other run is cleared, because nothing could pick it up and
#     the next launch would overwrite it unseen;
#   * unreadable now (cannot be reached, still running after the wait, or a charge that is not a
#     finite number of at least 0): the handle is KEPT, nothing is guessed, and the closing says
#     the charge is not in the bill and where to check it. Clearing it would lose the only way
#     to the charge; guessing 0 would show a bill lower than Apify's.
# A run is never counted zero times; when in doubt it is counted twice, never none. A kept run is
# counted by the command that read it and carries that mark (counted_usd, see "ONE COMMAND
# COUNTS A RUN'S CHARGE" below), so a later command that reads or picks it up does not count it
# again; a handle without the mark is counted in full.

_FINAL_STATES = ("SUCCEEDED",) + _ENDED_BADLY

# What this step counted for runs an earlier command left (main() empties it per step). The
# step's stop record is given the spend MINUS this, so a step that launched nothing is not
# told as one that "started and ran part of its work" because an old run was counted.
_LEFT_RUN_USD = [0.0]

# ONE COMMAND COUNTS A RUN'S CHARGE, AND ONLY ONE (FROZEN). A run that a command counted and
# KEPT (the test pass an earlier command read at its forecast gate, above) is picked up by a
# later command through the ordinary recovery, and that counts a recovered run's charge like
# any run's: the same run was in two commands' bills, so together they were above what Apify
# billed. The command that counts a kept run therefore writes the amount on its handle
# (counted_usd in .pending_run.json), and a command that picks the run up again finds it there
# and is charged only what the run cost beyond it (usually nothing). This is done on the Budget
# the command counts into (_guard_budget), not inside _run_one: _run_one counts every run it
# is handed, the same way, and cannot know whose count it is. A handle without the mark, or
# one whose mark cannot be read, is counted in full: the safe side is a run counted twice,
# never a run counted by nobody.
_COUNTED_EARLIER = [None]   # (run id, usd): the next charge was already counted that far


def _counted_earlier_usd(pend):
    """What an earlier command counted of the run this handle names (counted_usd), as a float,
    or None when the handle carries no usable figure."""
    x = (pend or {}).get("counted_usd")
    return float(x) if _finite_charge(x) else None


def _guard_budget(budget):
    """Make the next charge on `budget` count only what exceeds what an earlier command
    already counted for that run (_COUNTED_EARLIER, armed by _arm_counted); every other
    charge passes unchanged. Idempotent, and it changes this one Budget object only."""
    if getattr(budget, "_counted_guard", False):
        return budget
    real_charge = budget.charge

    def charge(actual_usd):
        owed, _COUNTED_EARLIER[0] = _COUNTED_EARLIER[0], None
        _note_charged_run(actual_usd)
        if owed is not None:
            try:
                x = float(actual_usd)
            except (TypeError, ValueError):
                x = None
            if x is not None and x == x:   # a NaN goes through as it is: it is not a charge
                # A mark that reads higher than the run's own charge (a damaged handle) can
                # waive that run's charge and no more.
                return real_charge(max(0.0, x - min(owed[1], x)))
        return real_charge(actual_usd)

    budget.charge = charge
    budget._counted_guard = True
    return budget


def _mark_counted(stage_dir, pend, usd):
    """Write on the handle that `usd` of the run it names is counted (see above)."""
    try:
        _write_pending(stage_dir, dict(pend, counted_usd=round(float(usd), 8)))
    except OSError:
        pass   # then the next command counts the run again: the safe side


def _arm_counted(step, pend):
    """A run is picked up again from `pend`: when the handle says an earlier command counted
    it, the next charge is reduced by that amount and the closing says whose count it is."""
    owed = _counted_earlier_usd(pend)
    if owed is None:
        _COUNTED_EARLIER[0] = None
        return
    rid = pend.get("runId")
    _COUNTED_EARLIER[0] = (rid, owed)
    if owed > 0:
        print(f"   this run (run {rid}) was already counted in an earlier command's bill "
              f"(${owed:.4f}), so only what it cost beyond that is counted here")
    _note_left_run(step, rid, pend.get("subkey"), "earlier", usd=round(owed, 6), kept=True)


# A STEP THAT FAILED AFTER A RUN WAS COUNTED MARKS THAT RUN (FROZEN). Every run is counted the
# moment it ends, before its dataset is fetched, so a fetch that fails (or any failure before the
# handle is cleared) leaves the handle of a run this command already counted. Without the mark
# the next command picks it up "with no re-charge" and counts it again: two bills for one run.
# The guard on the Budget notes each charge with the run the step's handle names at that moment
# (_CHARGED_RUNS), and main() marks the handle from that when a step fails
# (_mark_charged_pending). Only a run charged in this command is marked: a handle whose run was
# launched and not yet charged (a dropped launch) stays unmarked, and so is counted by whoever
# reads it.
_STEP_DIR = [None]    # the folder of the step now running (main() sets it before each step)
_CHARGED_RUNS = {}    # run id -> the run's whole charge, for runs counted in this step


def _note_charged_run(usd):
    """Remember that the run named by the step's handle has been counted, for how much."""
    folder = _STEP_DIR[0]
    if folder is None or not _finite_charge(usd):
        return
    pend = _read_pending(folder)
    if pend and pend.get("runId"):
        _CHARGED_RUNS[pend["runId"]] = float(usd)


def _mark_charged_pending(stage_dir):
    """After a step failed: if its handle names a run this command counted, mark it (see
    above). Bookkeeping: never raises."""
    try:
        pend = _read_pending(stage_dir)
        usd = _CHARGED_RUNS.get((pend or {}).get("runId"))
        if usd is not None:
            _mark_counted(stage_dir, pend, max(usd, _counted_earlier_usd(pend) or 0.0))
    except Exception:  # noqa: BLE001
        pass


def _settle_before_launch(stage, spec, cfg, workdir, budget, args):
    """Count a dropped full or batch run before the step's test pass can overwrite its handle.

    _run_one launches the test pass first, and the launch writes the new run into
    .pending_run.json: a full or batch run an earlier command launched and dropped was then
    lost from the handle with its charge counted by nobody, and the step paid for it again. The
    handle is read here, before _run_one, only when _run_one will go on to launch and will not
    pick that run up itself (_recoverable_runs is its own skip-the-test rule): then the run is
    settled by _count_left_run (final charge counted once, logged, set aside), and a run that
    cannot be read stops the step, because starting it would erase the only record of a charge.
    A step the forecast gate would refuse launches nothing and is left to the refusal path."""
    if args.dry_run:
        return
    stage_dir = Path(workdir) / spec["folder"]
    pend = _read_pending(stage_dir)
    sub = str((pend or {}).get("subkey") or "")
    if not pend or not pend.get("runId") or not (sub == "full" or sub.startswith("batch")):
        return
    if _stage_done(stage_dir, spec) and not args.force:
        return
    try:
        recoverable = _recoverable_runs(spec, cfg, workdir)
        if args.test_only:   # a test-only call always launches its test pass
            recoverable = {k for k in recoverable if k[0] != "full"}
        full_input, est_count = spec["build"](cfg, str(workdir), test=False)
        if est_count <= 0:
            return
        est_usd = C.estimate(stage, est_count, runs=C.actor_runs(spec, full_input))[0]
        if sub in {k[0] for k in recoverable}:
            # The step skips its test pass, so _run_one picks a left full run up itself. Its
            # input may differ from a plain build (a repeat run adds a date filter), so the
            # kind of run decides here, not the hash.
            return
        budget.check(est_usd)
    except BudgetExceeded:
        return
    except Exception:  # noqa: BLE001
        return
    if _count_left_run(stage, spec.get("name", stage), stage_dir, budget, pend,
                       recoverable) == "unread":
        raise RuntimeError(
            f"an earlier command left a run in this step (run {pend.get('runId')}, {sub}) whose "
            f"charge cannot be read now, so the step was not started: starting it would "
            f"overwrite the only record of that run. It is kept; run the same command again "
            f"once Apify can be reached")


def _note_left_run(step, rid, sub, state, usd=None, kept=False):
    """Tell the closing bill about a run an earlier command left (the entry script's
    _charge_caveat reads these lines from the charge notes file). Never raises."""
    import os
    path = os.environ.get(A.CHARGE_NOTES_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"left_run": rid, "step": step, "sub": sub, "state": state,
                                "usd": usd, "kept": kept}) + "\n")
    except Exception:  # noqa: BLE001
        return


def _recoverable_runs(spec, cfg, workdir):
    """{(subkey, input hash)} of the run a later run of this step would pick up instead of
    launching again: its test pass, or its full run when the step skips the test pass because
    the full input is no larger (the same rule as _run_one's skip_test). Empty when the step
    cannot be built."""
    try:
        test_input = spec["build"](cfg, str(workdir), test=True)[0]
        full_input = spec["build"](cfg, str(workdir), test=False)[0]
        full_size, test_size = _input_size(spec, full_input), _input_size(spec, test_input)
        if full_size is not None and test_size is not None and full_size <= test_size:
            return {("full", _input_hash(full_input))}
        return {("test", _input_hash(test_input))}
    except Exception:  # noqa: BLE001
        return set()


def _count_left_run(stage, step, stage_dir, budget, pend, recoverable, run=None):
    """Count the final charge of the run `pend` names (see the block comment above): "counted"
    or "unread". `run` is the settled record when the caller already holds it, else the run is
    read here (a running one is waited out). Launches nothing."""
    rid, sub, ih = pend.get("runId"), pend.get("subkey"), pend.get("input_hash")

    def unread(why):
        print(f"   an earlier command left a run in this step (run {rid}, {sub}), but {why}, so "
              f"its charge is NOT in this bill. The run is kept and read again next time; you "
              f"can check what it was charged on your Apify console (run {rid})")
        _note_left_run(step, rid, sub, "unread", kept=True)
        return "unread"

    owed = _counted_earlier_usd(pend)
    if owed is not None:
        # Counted by the command that kept it (see the block above): this one does not count
        # it again, and the handle stays only while a later run can still pick it up.
        keep = (sub, ih) in recoverable
        print(f"   an earlier command left a run in this step (run {rid}, {sub}); it was "
              f"counted in that command's bill, so it is not counted again here"
              + (". It is kept for a later run to pick up." if keep else ", and it is set aside."))
        _note_left_run(step, rid, sub, "earlier", usd=round(owed, 6), kept=keep)
        if not keep:
            _clear_pending(stage_dir)
        return "counted"
    done = run
    if done is None:
        try:
            info = A._run_info_retry(rid)
            if info.get("status") not in _FINAL_STATES:
                A.reattach(rid, on_status=lambda s: print(f"     [{s}]"))
                info = A._run_info_retry(rid)
            if info.get("status") not in _FINAL_STATES:
                return unread("it has not finished")
            done = A._settle_charge(rid, 3, 30)
        except Exception as exc:  # noqa: BLE001
            return unread(f"it cannot be read now ({str(exc)[:100]})")
    try:
        cost = A.actual_charge_usd(done)
    except Exception as exc:  # noqa: BLE001
        return unread(f"its charge cannot be read now ({str(exc)[:100]})")
    if not _finite_charge(cost):
        return unread(f"its charge is not a valid figure ({cost!r})")
    keep = done.get("status") == "SUCCEEDED" and (sub, ih) in recoverable
    budget.charge(cost)
    _LEFT_RUN_USD[0] += cost
    if keep:
        _mark_counted(stage_dir, pend, cost)
    print(f"   an earlier command left a finished run in this step (run {rid}, {sub}); Apify "
          f"charged ${cost:.4f} for it, and that is counted in this run's bill. "
          + ("It is kept, so a later run can pick it up again instead of paying for a new one."
             if keep else
             f"This tool cannot use it for this step, so it is set aside: its results stay on "
             f"your Apify account (run {rid}) and a later run of the step starts fresh."))
    append_run_log(stage_dir, f"{stage} ({sub}, left by an earlier command)", done, "not read",
                   note=(f"a run an earlier command left; it ended {done.get('status')} and "
                         f"was charged ${cost:.4f}, counted here. "
                         + ("It was kept for a later run to pick up."
                            if keep else "It was set aside: nothing could pick it up.")))
    _note_left_run(step, rid, sub, "counted", usd=round(cost, 6), kept=keep)
    if not keep:
        _clear_pending(stage_dir)
    return "counted"


def _account_left_run(stage, spec, cfg, workdir, budget, args, exc, spent_before):
    """A step refused at its forecast gate with nothing of it launched or charged in this
    command: count the run an earlier command left in its handle, if any (see the block
    comment above). Never launches. A stop on spend already made (no forecast), a dry run and
    a step that did run something are not this and are left alone."""
    if (args.dry_run or getattr(exc, "estimate_usd", None) is None or _STEP_RUNS
            or budget.spent != spent_before):
        return None
    stage_dir = Path(workdir) / spec["folder"]
    pend = _read_pending(stage_dir)
    if not pend or not pend.get("runId"):
        return None
    return _count_left_run(stage, spec.get("name", stage), stage_dir, budget, pend,
                           _recoverable_runs(spec, cfg, workdir))


def _drop_unrun_outputs(steps, workdir, args):
    """Remove the files an earlier run left for each of `steps`, which this call did not
    finish: it failed, or the budget cap or a failed start stopped it.

    WHY: a step that does not get a result writes none of its own files, so the ones an
    earlier run of the same folder left stayed and the report, the app and the month
    comparison showed them as this run's ("37 unchanged" for a step that never ran). A
    step that this call would not have run again (it is done and --force was not given)
    keeps its files: they are this folder's result. A dry run changes nothing. The step's
    raw run files are its result as it stands and are never touched
    (postprocess.drop_stage_outputs)."""
    if args.dry_run:
        return
    for st in steps:
        stage_dir = Path(workdir) / STAGES[st]["folder"]
        if _stage_done(stage_dir, STAGES[st]) and not args.force:
            continue
        P.drop_stage_outputs(st, stage_dir, workdir)


def main():
    ap = argparse.ArgumentParser(description="Run search-intelligence Apify steps.")
    ap.add_argument("phase", choices=list(PHASE_ORDER.keys()))
    ap.add_argument("--config", default=None)
    ap.add_argument("--workdir", required=True, help="this run's working folder")
    ap.add_argument("--only", default=None, help="run just one step, by its code: A1 (keyword ideas), "
                         "A2 (keyword volumes) or B1 (Google search results)")
    ap.add_argument("--test-only", action="store_true", help="stop after the test run")
    ap.add_argument("--dry-run", action="store_true", help="print plan + inputs, spend nothing")
    ap.add_argument("--force", action="store_true", help="re-run steps even if already complete")
    ap.add_argument("--run-ceiling-usd", type=float, default=None,
                    help="Hard per-run ceiling (USD); caps the budget and per-call "
                         "maxTotalChargeUsd. Set by entry script under --standing-approval. "
                         "Without this flag, behaviour is identical to today.")
    ap.add_argument("--run-ledger-path", default=None,
                    help="Path to a run-level ledger JSON file. When set, "
                         "budget.spent is initialised from the file's 'spent' value "
                         "(prior steps' cumulative spend for this run) and updated "
                         "after this step completes. Only meaningful with "
                         "--run-ceiling-usd (it enforces the ceiling across the whole "
                         "run, not just within a single subprocess).")
    ap.add_argument("--second-request-extra-usd", type=float, default=None,
                    help="Make ONLY the second request the user said yes to (search_intel "
                         "--second-request): the folder's offered keywords, with this much more "
                         "than the cap the offer was made under. Nothing else runs.")
    args = ap.parse_args()

    cfg = C.load_config(args.config)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if args.second_request_extra_usd is not None:   # The user's yes; no step, no test pass
        if args.dry_run or args.test_only or args.force or args.run_ceiling_usd is not None \
                or args.only or not 0 <= args.second_request_extra_usd <= _EMPTY_RETRY_EXTRA_USD:
            print("⛔ A second request the user said yes to runs alone: no dry run, test pass, "
                  "re-run, step choice or standing approval with it, and the extra is 0 or more and "
                  f"at most ${_EMPTY_RETRY_EXTRA_USD:.2f}. Nothing was spent.")
            sys.exit(2)
        _run_approved_second_request(args, cfg, workdir)
        return

    cap = cfg["budget_caps"].get(f"phase{args.phase}", 999.0)
    # --run-ceiling-usd (standing-approval mode): clamp the budget cap to the
    # user-approved ceiling.  WHY min not replace: the config's own cap may be
    # even tighter and must not be silently overridden upward.
    if args.run_ceiling_usd is not None:
        cap = min(cap, args.run_ceiling_usd)
    budget = _guard_budget(Budget(args.phase, cap))

    # Run-level spend ledger.  Each stage subprocess is a fresh Budget(spent=0),
    # so without the ledger _cap_for() sees only the current stage's spend, not the
    # whole run.  The ledger file holds {"spent": <cumulative_float>}; we read it
    # before any stage runs to initialise budget.spent with prior stages' spend, and
    # write it back after this subprocess exits.  WHY inside workdir: the ledger is
    # part of the run record (same lifetime as the run folder) and must persist
    # across subprocess boundaries without a shared memory or daemon channel.
    _ledger_path = Path(args.run_ledger_path) if args.run_ledger_path else None
    if _ledger_path and _ledger_path.exists():
        try:
            _prior = json.loads(_ledger_path.read_text(encoding="utf-8"))
            _prior_spent = float(_prior.get("spent", 0.0))
            # NaN, Infinity or a negative figure is a corrupt ledger too.
            # float() accepts them, and then "ceiling - spent" was NaN and the step
            # ran under the smallest cap instead of stopping before any call.
            if _prior_spent != _prior_spent or _prior_spent in (float("inf"), float("-inf")) \
                    or _prior_spent < 0:
                raise ValueError(f"spent is {_prior_spent!r}, not an amount spent")
            budget.spent = _prior_spent
        except (OSError, ValueError, TypeError) as _ledger_exc:
            # A corrupt ledger under --run-ceiling-usd is NOT safe to ignore.
            # Starting at 0 is the LEAST conservative choice -- it lets this stage
            # spend the full ceiling on top of whatever prior stages already spent.
            # The entry script always creates the ledger before the first stage, so
            # a corrupt file means something went wrong.  Stop before any actor call.
            if args.run_ceiling_usd is not None:
                _detail = f"ledger corrupt: {_ledger_exc}"
                print(f"\n⛔ Run-level ledger unreadable: stopping before any actor call.")
                try:
                    _ledger_path.write_text(
                        json.dumps({"spent": 0.0,
                                    "stopped": {"stage": "NONE", "reason": "ledger",
                                                 "detail": _detail}},
                                   ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                sys.exit(4)
            # Without a ceiling (non-approval path): keep original behaviour.
    elif _ledger_path is not None and args.run_ceiling_usd is not None:
        # Ledger path was set by the entry script but the file is missing.
        # The entry script always creates it before the first stage, so missing
        # means something went wrong.  Stop before any actor call.
        _detail = "ledger file missing"
        print(f"\n⛔ Run-level ledger missing: stopping before any actor call.")
        try:
            _ledger_path.write_text(
                json.dumps({"spent": 0.0,
                            "stopped": {"stage": "NONE", "reason": "ledger",
                                         "detail": _detail}},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        sys.exit(4)

    order = [args.only] if args.only else list(PHASE_ORDER[args.phase])

    # Task selection: if the config has a `stages` allow-list, run only those.
    sel = S.selected_stages(cfg)
    if sel is not None and not args.only:
        order = [s for s in order if s in sel]

    if not order:
        print(f"=== Phase {args.phase}: no selected steps, nothing to run. ===")
        return

    # An interactive run's cap holds for the whole
    # run, not for each step. Every paid step is its own run_phase process, so its
    # budget started at $0: with a $0.10 cap, Meta spent $0.055, Google's check
    # then saw "$0 spent" and billed $0.10 more, and the run billed $0.155. The
    # entry script names this run's ledger (budget.RUN_LEDGER_ENV),
    # which already counts every charge once (budget.charge), and the
    # budget now starts from it. Under a standing approval --run-ledger-path does
    # the same and the variable is not set; a dry run spends nothing.
    # The ledger restarts at $0 for each command (ad_intel, pain_intel and
    # search_intel write it when they start), so the cap is per command run: a
    # by-hand re-run of a folder is a new command with its own cost question,
    # and the steps that finished are skipped and bill nothing.
    _so_far = None
    if _ledger_path is None and not args.dry_run:
        try:
            _so_far = run_ledger_spent()
        except ValueError as _exc:
            # Starting from $0 would hand this step the whole cap again: stop
            # before any paid call, and name every step of this call as stopped.
            print(f"\n⛔ {_exc}, so what this run has already spent is not known. "
                  f"Stopping before any paid call.")
            for _st in order:
                if _stage_done(Path(workdir) / STAGES[_st]["folder"], STAGES[_st]) \
                        and not args.force:
                    continue  # Finished and not run again
                _note_step_stopped(Path(workdir) / STAGES[_st]["folder"], _st, "step_failed")
            _drop_unrun_outputs(order, workdir, args)
            sys.exit(4)
    if _so_far:
        budget.spent = _so_far

    print(f"=== Phase {args.phase} · budget cap ${cap_text(cap)} ===")
    if _so_far:
        print(f"   this run already spent ${_so_far:.4f} in its earlier steps, "
              f"and that counts against this cap")
    _budget_hit = False
    _stopped_info = None  # Set when we stop early; written to ledger for entry script
    for stage in order:
        spec = STAGES[stage]
        # What this step was charged before it started, and
        # the runs it launches, so a budget stop inside it can tell "part-way"
        # from "did not run".
        _spent_before = budget.spent
        _STEP_RUNS.clear()
        _LEFT_RUN_USD[0] = 0.0
        _COUNTED_EARLIER[0] = None
        _CHARGED_RUNS.clear()
        _STEP_DIR[0] = Path(workdir) / spec["folder"]
        _CAP_ACTOR[0] = spec.get("actor")
        _STEP_SAVED.clear()
        try:
            # Enforce the cap before starting a new stage. Safe boundary:
            # nothing from this stage has been written yet.
            budget.enforce()
            _settle_before_launch(stage, spec, cfg, workdir, budget, args)
            _say_how_search_results_were_chosen(stage, spec, cfg, workdir, args)
            _run_one(stage, spec, cfg, workdir, budget, args)
        except BudgetExceeded as e:
            # A step refused before it launched anything still owes the bill a run an earlier
            # command left in its handle (see _count_left_run). Bookkeeping: never raises.
            try:
                _account_left_run(stage, spec, cfg, workdir, budget, args, e, _spent_before)
            except Exception as _left_exc:  # noqa: BLE001
                print(f"   could not check the run an earlier command left in this step "
                      f"({str(_left_exc)[:100]}); its charge is not in this bill")
            print(f"\n⛔ {e}\n   Stopping. Spent ${budget.spent:.2f}.")
            # Every later step in this call did not run. This
            # step may have run part of its work first (_note_budget_stop).
            if not args.dry_run:
                _note_budget_stop(workdir, order, stage, cap,
                                  round(budget.spent - _spent_before - _LEFT_RUN_USD[0], 8), _STEP_RUNS,
                                  force=args.force)
                _annotate_budget_stop(workdir, stage, e)
                _drop_unrun_outputs(order[order.index(stage):], workdir, args)
            _budget_hit = True
            _stopped_info = {"stage": stage, "reason": "budget",
                             "detail": f"forecast exceeded ${cap_text(cap)} ceiling"}
            break
        except Exception as e:  # noqa: BLE001
            # Under standing-approval a stage exception must propagate as
            # INCOMPLETE (exit 4), not be silently swallowed.  WHY: an unattended
            # scheduled run that encounters an error and continues would report
            # success while skipping data collection silently — violating the "refuses or stops before
            # spending" guarantee.
            if args.run_ceiling_usd is not None:
                print(f"\n⛔ {spec.get('name', stage)} (step {stage}) failed: {e}\n"
                      f"   Step error under standing approval: stopping.")
                if not args.dry_run:
                    _note_step_stopped(Path(workdir) / spec["folder"], stage, "step_failed",
                                       result_saved=bool(_STEP_SAVED))
                _drop_unrun_outputs(order[order.index(stage):], workdir, args)
                _budget_hit = True
                _stopped_info = {"stage": stage, "reason": "error",
                                 "detail": str(e)[:150]}
                break
            print(f"\n⚠ {spec.get('name', stage)} (step {stage}) failed: {e}\n"
                  f"   Continuing to the next step. The closing lines name this step.")
            # Recorded, so the closing lines name the failed step
            # instead of reading its empty result as "it ran, but nothing came back".
            if not args.dry_run:
                _note_step_stopped(Path(workdir) / spec["folder"], stage, "step_failed",
                                       result_saved=bool(_STEP_SAVED))
            _drop_unrun_outputs([stage], workdir, args)
        finally:
            # However the step ended (a budget stop, an error, a Ctrl-C), a run this command
            # counted and that is still in the handle is marked as counted (see above).
            _mark_charged_pending(Path(workdir) / spec["folder"])
    print(f"\n=== Phase {args.phase} done · spent ${budget.spent:.2f}/{cap_text(cap)} ===")
    # Write the run-level ledger BEFORE exiting, so the next subprocess in
    # this tool run reads the correct cumulative spend.  Written even on exit 4
    # so that any caller can audit total spend after an INCOMPLETE.
    # Also write stopped reason so the entry script can give an accurate message.
    if _ledger_path is not None:
        try:
            _ledger: dict = {"spent": round(budget.spent, 8)}
            if _stopped_info is not None:
                _ledger["stopped"] = _stopped_info
            _ledger_path.write_text(
                json.dumps(_ledger, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # Non-fatal: next subprocess will under-estimate remaining cap
    # Exit 4 (INCOMPLETE) when --run-ceiling-usd is set and the budget was hit
    # mid-run.  WHY only when ceiling is set: without --standing-approval the
    # existing behaviour (exit 0 after budget stop) is preserved byte-for-byte.
    if _budget_hit and args.run_ceiling_usd is not None:
        sys.exit(4)


if __name__ == "__main__":
    main()
