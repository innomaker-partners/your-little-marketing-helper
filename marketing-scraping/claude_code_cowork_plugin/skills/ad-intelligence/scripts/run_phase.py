#!/usr/bin/env python3
"""
Stage runner — runs a set of Apify stages SEQUENTIALLY, test-first, budget-gated,
auto-logging. The higher-level ad-intelligence flow (competitor resolution,
creative download, OCR, report assembly) is driven from a separate entry point
(ad_intel.py); this module is the reusable "run one Apify stage safely" engine
plus a thin CLI for isolated single-stage runs.

    python3 run_phase.py 1 --config ../AD_INTEL_CONFIG.json --workdir <run>
    python3 run_phase.py 1 --only B4 --test-only --config ... --workdir ...
    python3 run_phase.py 1 --dry-run --config ... --workdir ...   # plan + inputs, spend nothing

For each stage: build TEST input (3-5 items) → check budget → run → recompute the
per-unit rate from ACTUAL test cost → estimate the full run → check budget → run
full → save output → append RUN_LOG.md + commands.sh. Each run is charged to the
budget the moment it ends, before its output is fetched.
"""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
import apify_client as A
from budget import Budget, BudgetExceeded, cap_text, run_ledger_spent
from runlog import append_run_log, append_command
from stages import STAGES, PHASE_ORDER
import stages as S
import postprocess as P
import adrecords as AR


def _records_saved_text(stage, items, batches=None):
    """The "full: N records saved" console text of a step.

    WHY a function of its own, here and not in _run_one: _run_one is the same text in
    all three tools (test_client_drift compares it), and the Meta scraper's status record
    ("Ads not found" for a page with no ads) is an ad-tool matter. In a real run it was
    printed as "1 records saved" and the buyer read it as one ad. In this tool a Meta step
    says how many of its records are ads (adrecords); the file keeps every record."""
    return AR.records_saved_line(stage, items, batches=batches)


def _counted_records(stage, items):
    """How many of a step's records the test pass and its per-record rate count: ads only.

    WHY: the Meta scraper answers a page with no ads by ONE status record (not an ad,
    not billed per ad). Counted, it printed "test: 3 records" for 2 ads and, as the
    divisor of the recalibrated per-ad rate, made every ad look cheaper than it is, so
    the whole run's forecast came out too low. A function of its own so that _run_one
    stays the same text in all three tools (test_client_drift compares it)."""
    if stage == "B4":
        return len(AR.split_meta_items(items)[0])
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
    """No-op stub: ad-intelligence stages are not in D2_INCREMENTAL.

    Pain skill only has the real implementation.  This stub keeps _run_one
    AST-identical across all three skill copies.
    """
    return None, None


def _run_after_fetch_hook(stage, spec, items, _inc, _repeat_of,
                          base_input, stage_dir, budget, args):
    """No-op stub: ad-intelligence has no incremental merge logic.

    Pain skill only has the real implementation.  This stub keeps _run_one
    AST-identical across all three skill copies.
    """
    return items, None


def _run_after_save_hook(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir):
    """No-op stub: runs after the step's full result is saved and built. Only the
    search tool uses it (a second request for items that came back empty); this stub keeps
    _run_one AST-identical across all three skill copies."""
    return None


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
    # output, and for per-actor-start pricing that doubles the bill (observed:
    # ~$0.46 for a small expansion). Skip the test in the normal path; the
    # full-estimate budget gate below still guards
    # the cap, and the run is test-sized so there is nothing to recalibrate.
    # --test-only keeps running its one small pass. _input_size returns None on
    # an unrecognised shape, so we never skip the test when we cannot size it.
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
    """Cheap guard: don't launch a stage whose input arrays are empty. Every
    known primary work-array is listed here — a stage that builds `{"companyIds":
    []}` (LinkedIn with no resolved id) MUST be caught as empty, or run_phase
    launches the actor with no target and bills for a wrong/empty scrape.

    Uses _real_entries() so that lists of blank/None values are treated as absent
    rather than present (ADV-014). `queries` (google-ads-transparency-scraper) is
    a newline-separated string, not a list — it needs its own branch; iterating a
    string yields characters which would make any non-blank string look non-empty."""
    for key in ("queries", "startUrls", "urls", "companyIds", "companyUrls"):
        v = actor_input.get(key)
        if v is not None:
            if isinstance(v, str):
                return bool([q for q in v.splitlines() if q.strip()])
            return bool(_real_entries(v))
    return True


def _input_size(spec, inp):
    """Count of items in the actor's primary work array — the unit a run is
    billed on. Used to detect when a 'full' run is already no larger than a test
    pass, so the test can be skipped instead of billing the same run twice.

    Returns None (meaning "do not skip the test") whenever the primary array is
    ambiguous: an unrecognised shape, or several known target arrays present at
    once (e.g. Reddit's searchTerms / subredditUrls / startUrls, where the
    billed unit is not one single array). Skipping on an input we cannot size
    would drop the test (and its rate recalibration) from large runs too. A
    spec-declared batch key is authoritative and always wins. The key list is
    covers common actor input shapes so the engine stays correct across tools."""
    if not isinstance(inp, dict):
        return None
    batch = spec.get("batch")
    if batch and isinstance(inp.get(batch["key"]), list):
        return len(inp[batch["key"]])
    keys = ("queries", "keywords", "startUrls", "urls", "companyIds", "companyUrls",
            "appIds", "subredditUrls", "searchTerms", "searchStringsArray", "profileUrls")
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


# --- a first small pass for a step whose full forecast does not fit the cap ----------
# The Google ads step (B5) is forecast at its largest size: google_max_items (2000 by
# default) times the price of an ad, about 6.00 dollars whatever the advertiser's size. A
# small advertiser has a few dozen ads and costs 2 to 4 cents, so in a real buyer's run a
# 3.00 dollar cap refused the step in every case, with nothing learned about the
# advertiser. The cap is the buyer's hard limit and stays one, and the forecast shown
# before any spending stays the honest worst case. What changes is only this: when the
# step's full forecast does not fit what is left of the cap, a small first pass is run
# instead of refusing, if the pass's own worst case fits.
#
# THE PASS IS THE FULL SEARCH WITH A SMALL maxItems (FROZEN): every start address of the
# run the full search would make, only maxItems lowered. The actor's maxItems is a total
# for the whole run, not per address (stages.build_B5: a paid test sent 2 domains with
# maxItems 25 and got exactly 25 ads; the actor's own schema says "Maximum number of items
# to scrape"). WHY every address: the inference below, "fewer ads than allowed means the
# actor ran out of ads", is only true when the pass looked at every website the full
# search would. The test input is a subset (the first 2 websites), so a pass built from it
# would call 3 websites "all of them" after seeing 2, and the buyer would pay for a pass
# that is thrown away. Where one pass cannot cover the whole search (the full search is
# more than one batch, over B5_BATCH_SIZE start addresses), no pass is taken at all and the
# original refusal stands.
#   * Fewer ads than the pass was allowed, on a run that SUCCEEDED and was not cut short:
#     the actor ran out of ads. The pass IS the full result; it is saved as that and no
#     second run is made.
#   * A pass that did not end SUCCEEDED and complete is that step's failure, not a result
#     and never a budget stop: the scraper failed, it did not find nothing. Its charge is
#     counted, logged and billed like any run's.
#   * Otherwise the pass tells the price of an ad, and the full search is priced from it
#     as the test pass does (_run_one) and checked against the cap; if it still does not
#     fit, the step stops, and the record says what the pass found and cost.
# THE CAP STAYS HARD (FROZEN): the pass and the continuation are each given Apify's max
# charge, and it is never more than what is left of the cap (_cap_for reads
# args.run_ceiling_usd, so the calls here use a copy of args whose ceiling is the cap). The
# pass's max charge is also never below its worst case at the table rate (its estimate,
# which includes the safety margin): the check that the pass fits the cap is made on that
# same estimate, so min(estimate x factor, what is left) >= estimate >= maxItems x rate.
# A pass cut short by its own charge limit while still reporting SUCCEEDED would otherwise
# read as "ran out of ads".
# WHY OUTSIDE _run_one: that function is the shared money core (the same text in all three
# tools, pinned by test_client_drift), and on 2026-10-13 a money change made inside it
# opened worse breaks (kept batch handles). Nothing in it changes here: main() calls this
# only where the step would be refused, and a step whose full forecast fits runs exactly
# as before. Only a step whose spec says "first_pass_when_over_cap" (B5) takes this path:
# for Meta and LinkedIn nothing says that fewer ads than the limit means no more ads exist.

FIRST_PASS_MAX_ITEMS = 25  # the size of a test pass (stages.build_B5)

# A PAID RUN AN EARLIER COMMAND LEFT IS SETTLED BEFORE ANYTHING NEW IS LAUNCHED (FROZEN).
# .pending_run.json holds the one run of this step that was launched and not yet consumed
# (a dropped connection, a stopped command). A first pass writes the same file, so launching
# one over a handle that names another run (the full search a dropped command had started,
# a batch, a different test pass) would destroy the only way back to a run that was paid for:
# it could never be picked up and its charge would never be counted. So before a first pass
# the handle, whatever run it names, is reattached ONCE in this code (never through
# _launch_recover, whose launch-on-failure branch starts a new paid run with no budget check),
# and:
#   * the first pass itself, finished: it is the pass; its charge is counted and nothing is
#     launched, even when this command's cap no longer leaves room for a new pass;
#   * the full search, finished and not collected: it is THE result. It is collected through
#     the ordinary step code with no new charge for the run, with no pass and no
#     continuation (_collect_pending_full);
#   * any other finished run (a batch, a test pass of another input): nothing is launched this
#     time, and the step is refused as it was before a first pass existed. The run is not left
#     in the handle for a later run to overwrite unseen: its charge is counted now and, unless
#     a later run of the step picks it up with no new charge (_recoverable_runs), it is set
#     aside (_count_left_run). A cap that fits the full search does NOT collect such a run;
#     only a finished full search of the same input is collected;
#   * a run that Apify says ended FAILED, ABORTED or TIMED-OUT: it cannot be picked up, but it
#     may have been billed up to its charge limit and nobody else will ever read that. Its
#     final charge is read by run id and counted (bill, ledger, RUN_LOG), the handle is
#     cleared, and only then is a new pass decided, with the normal fit check on what is
#     left of the cap;
#   * unreadable now: the run cannot be reached (A.reattach answers None for that as well as
#     for a run that ended without a result, so the status is asked first, here), or its
#     final charge cannot be read, or is not a finite number of at least 0. Nothing is
#     launched and the handle is kept: a healthy run that a network blip made look abandoned
#     would be paid for twice, and an abandoned one whose charge is unknown would be left
#     uncounted forever.
# Not settled here, and inherited: a pending run of the shared core is not settled when the
# step is not refused (the core itself recovers it, or replaces it with its own test pass).
# A pending run that was counted by the command that launched it (its handle kept because
# saving failed) is counted again if it is abandoned, which overstates the spend and never
# understates it.
#
# A DATASET ITEM IS AN AD ONLY IF IT LOOKS LIKE ONE (FROZEN). "Fewer ads than allowed" proves
# the actor ran out of ads only if every item is an ad: an error, a status or a blocked
# message among them means it did not report on everything it was asked about. Errors come
# in many shapes (the platform's own {"#error": true}, "error", "errorMessage", "errors",
# "state": "FAILED", a bare "message"), so errors are not listed; ads are recognised by what
# a real record carries. Evidence: the lexis-solutions/google-ads-scraper dataset view
# declares id, advertiserName, advertiserId, creativeId, format, url ... and every Google ad
# recorded in the development fixtures (Roto-Rooter, Mr. Rooter, HubSpot, Basecamp, Revolut,
# Peloton) carries a non-empty advertiserId ("AR" + digits) and creativeId ("CR" + digits), as
# do the records creatives.py reads.
# The check is Google-specific and local on purpose.


def _is_google_ad_record(item):
    """True for an item that carries what a real Google Ads Transparency record carries: a
    non-empty advertiserId and creativeId. An item that also carries a truthy error marker
    (error, errors, errorMessage) or a state or status of FAILED, ABORTED or TIMED-OUT is not
    counted: an ad with "error": false still is."""
    if not isinstance(item, dict):
        return False
    if (item.get("#error") is True or item.get("error") or item.get("errors")
            or item.get("errorMessage")):
        return False
    for k in ("state", "status"):
        if str(item.get(k) or "").strip().upper() in _ENDED_BADLY:
            return False
    return all(isinstance(item.get(k), str) and item.get(k).strip()
               for k in ("advertiserId", "creativeId"))


def _non_ad_items(items):
    """The items of a Google ads pass that are not ads (see _is_google_ad_record)."""
    return [it for it in items or [] if not _is_google_ad_record(it)]


def _why_non_ad(item):
    """A few words from one non-ad item, for the failure message ('' when it says none)."""
    if not isinstance(item, dict):
        return ""
    dbg = item.get("#debug")
    why = (dbg.get("errorMessage") if isinstance(dbg, dict) else None) or item.get("error") \
        or item.get("errorMessage") or item.get("message") or item.get("statusMessage") \
        or item.get("state") or item.get("status")
    if not why and isinstance(item.get("errors"), list) and item["errors"]:
        why = item["errors"][0]
    why = str(why or "").strip()
    return f" ({why[:150]})" if why else ""


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


def _settle_pending(stage, stage_dir, plan, budget):
    """(kind, run) for the run an earlier command left in this step's handle (see the block
    comment above). kind: "none" (no handle), "pass" (the first pass itself, finished: run),
    "collect" (the full search, finished: run), "hold" (a run that must be left alone, or
    cannot be read now: launch nothing), "settled" (an abandoned run: its charge is counted
    and the handle cleared, a new pass may be decided). Never launches; reattaches at most
    once."""
    pend = _read_pending(stage_dir)
    if not pend:
        return "none", None
    _guard_budget(budget)
    rid, sub, ih = pend.get("runId"), pend.get("subkey"), pend.get("input_hash")

    def hold(why):
        print(f"   the run an earlier command left in this step (run {rid}, {sub}) {why}, so no "
              f"first pass is started; the run is kept and the step is checked again next time. "
              f"Its charge is NOT in this bill: you can check it on your Apify console (run {rid})")
        _note_left_run(plan["step"], rid, sub, "unread", kept=True)
        return "hold", None

    try:
        info = A._run_info_retry(rid)
    except Exception as exc:  # noqa: BLE001
        return hold(f"cannot be reached now ({str(exc)[:100]})")
    run = None
    if info.get("status") not in _ENDED_BADLY:
        try:
            run = A.reattach(rid, on_status=lambda s: print(f"     [{s}]"))
        except Exception as exc:  # noqa: BLE001
            return hold(f"cannot be read now ({str(exc)[:100]})")
        if run is None:
            # None is "unreachable" as well as "ended without a result": ask which.
            try:
                info = A._run_info_retry(rid)
            except Exception as exc:  # noqa: BLE001
                return hold(f"cannot be reached now ({str(exc)[:100]})")
            if info.get("status") not in _ENDED_BADLY:
                return hold("could not be read just now")
    if run is not None:
        is_pass = sub == "test" and ih == _input_hash(plan["pass_input"])
        is_full = sub == "full" and ih == _input_hash(plan["full_input"])
        if is_pass or is_full:
            # The run is about to be used and its charge counted, so the charge must be a
            # number first: a negative one lowered the bill, a NaN crashed the pass's own
            # arithmetic and was printed as the amount spent. Held, as an unreadable charge is.
            try:
                cost = A.actual_charge_usd(run)
            except Exception as exc:  # noqa: BLE001
                return hold(f"finished but its charge cannot be read now ({str(exc)[:100]})")
            if not _finite_charge(cost):
                return hold(f"finished but its charge is not a valid figure ({cost!r})")
        if is_pass:
            print(f"   \u21bb recovered run {rid} for the first pass (no re-charge)")
            _arm_counted(plan["step"], pend)
            return "pass", run
        if is_full:
            return "collect", run
        # A finished run that is neither the pass nor the full search of this input (a batch,
        # a test pass of another input): nothing here can use it, and the next launch of the
        # step would overwrite its handle unseen. So it is counted now and, unless a later run
        # can pick it up, set aside; no first pass is started this time.
        _count_left_run(stage, plan["step"], stage_dir, budget, pend, plan["recoverable"],
                        run=run)
        return "hold", None
    try:
        done = A._settle_charge(rid, 3, 30)
        cost = A.actual_charge_usd(done)
    except Exception as exc:  # noqa: BLE001
        return hold(f"ended {info.get('status')} but its charge cannot be read now ({str(exc)[:100]})")
    if not _finite_charge(cost):
        return hold(f"ended {info.get('status')} but its charge is not a valid figure ({cost!r})")
    _STEP_RUNS.append(str(sub or "test"))
    budget.charge(cost)
    _note_left_run(plan["step"], rid, sub, "counted", usd=round(cost, 6))
    print(f"   the run an earlier command left in this step (run {rid}) ended "
          f"{done.get('status')} and was charged ${cost:.4f}; that is counted")
    append_run_log(stage_dir, f"{stage} ({sub}, abandoned)", done, 0,
                   note=(f"a run an earlier command left; it ended {done.get('status')} and "
                         f"could not be picked up. It was charged ${cost:.4f}, counted here."))
    _clear_pending(stage_dir)
    return "settled", None


def _first_pass_shape(stage, spec, cfg, workdir):
    """What a first pass for this step would be, or None when it takes none: the full input,
    its count and forecast, the pass input (the full input with a small maxItems), its count
    and forecast. Builds only; spends, reads and writes nothing, and says nothing about
    the cap."""
    if not spec.get("first_pass_when_over_cap"):
        return None
    full_input, est_count = spec["build"](cfg, str(workdir), test=False)
    if est_count <= 0 or not _has_inputs(stage, full_input):
        return None
    if C.actor_runs(spec, full_input) != 1:
        return None  # one pass cannot cover a search of several batches
    limit = FIRST_PASS_MAX_ITEMS
    if not 0 < limit < int(full_input.get("maxItems") or 0):
        return None
    pass_input = dict(full_input, maxItems=limit)
    est_usd = C.estimate(stage, est_count, runs=1)[0]
    pass_est = C.estimate(stage, limit, runs=1)[0]
    if not pass_est < est_usd:
        return None  # no cheaper than the step: no first look
    return {"full_input": full_input, "est_count": est_count, "est_usd": est_usd,
            "pass_input": pass_input, "limit": limit, "pass_est": pass_est,
            "recovered_run": None, "collect_run": None, "step": spec.get("name", stage),
            "recoverable": _recoverable_runs(spec, cfg, workdir)}


def _pass_fits(spec, budget, pass_est):
    """True when a new pass can be launched under the cap: what is left must cover its
    worst case AND the amount Apify is really given for it (the smallest limit it accepts
    for this actor, or 0.01), or the run could be billed past the cap."""
    floor = max(0.01, A.actor_min_charge(spec["actor"]))
    return budget.remaining() >= max(pass_est, floor) - 1e-9


def _first_pass_plan(stage, spec, cfg, workdir, budget):
    """The first pass this step would take under the cap, or None when it takes none. The
    ONE fit rule for the real run and the dry run's line, so the line says only what a
    run will do: the pass's own worst case must fit what is left of the cap."""
    plan = _first_pass_shape(stage, spec, cfg, workdir)
    if not plan or not _pass_fits(spec, budget, plan["pass_est"]):
        return None
    return plan


def _first_pass_applies(stage, spec, cfg, workdir, exc, budget, spent_before, args):
    """The plan (a truthy dict) when a BudgetExceeded raised by _run_one is a refusal of
    this step's full forecast with nothing of the step launched or charged, for a step
    that takes a first pass and has a pass to take; else None. A refusal after the step's
    test pass, or a stop on spend already made (Budget.enforce, no forecast), is not this
    and stays a stop.

    A run an earlier command left is settled here first (_settle_pending): the first pass,
    or the full search, that finished is used and needs no room under the cap; any other
    finished run stops the pass; one that was abandoned has its charge counted and then the
    normal fit rule decides on a new pass."""
    if (args.dry_run or getattr(exc, "estimate_usd", None) is None
            or _STEP_RUNS or budget.spent != spent_before):
        return None
    plan = None if args.test_only else _first_pass_shape(stage, spec, cfg, workdir)
    if not plan:
        # No first pass for this step (or a test-only call): the refusal stands, but a run an
        # earlier command left in its handle is still read and counted (_account_left_run).
        try:
            _account_left_run(stage, spec, cfg, workdir, budget, args, exc, spent_before)
        except Exception as left_exc:  # noqa: BLE001
            print(f"   could not check the run an earlier command left in this step "
                  f"({str(left_exc)[:100]}); its charge is not in this bill")
        return None
    kind, run = _settle_pending(stage, Path(workdir) / spec["folder"], plan, budget)
    if kind == "hold":
        return None
    if kind == "pass":
        plan["recovered_run"] = run
        return plan
    if kind == "collect":
        plan["collect_run"] = run
        return plan
    if not _pass_fits(spec, budget, plan["pass_est"]):
        return None  # the pass's own worst case must fit what is left of the cap
    return plan


def _the_pass_run(plan, spec, stage_dir, ceiling):
    """The run of the first pass: the one an earlier command left and _settle_pending
    picked up, else a new launch. The launch is the only paid call of the pass, and it is
    reached only after the fit rule, with no handle left to reattach."""
    if plan.get("recovered_run") is not None:
        _STEP_RUNS.append("test")
        return plan["recovered_run"]
    return _launch_recover(spec, plan["pass_input"], stage_dir, "test",
                           memory_mb=_mem_for(spec, plan["pass_input"]),
                           on_status=lambda s: print(f"     [{s}]"),
                           max_charge_usd=ceiling)


def _start_addresses(full_input):
    """'its start address' for one, 'all 3 of its start addresses' for more, so the line
    never says '1 start addresses'."""
    n = len(full_input.get("startUrls") or [])
    return "its start address" if n == 1 else f"all {n} of its start addresses"


def _say_first_pass_plan(stage, spec, cfg, workdir, budget):
    """Dry run: one plain line under the step when a real run would take a first pass.
    Prints only; the whole-run forecast figure stays the worst case. It applies the same
    predicate as the real run (_first_pass_plan) and the same trigger: the full forecast
    does not fit. A dry run reads and settles nothing (that would be a call to Apify), so
    when an earlier paid run of the step is still tracked it does not state a decision: a
    real run checks that run first and the plan may change by what it was charged."""
    shape = _first_pass_shape(stage, spec, cfg, workdir)
    if not shape or budget.spent + shape["est_usd"] <= budget.cap:
        return
    pend = _read_pending(Path(workdir) / spec["folder"])
    if pend:
        print(f"   an earlier paid run of this step (run {pend.get('runId')}) is still tracked. A "
              f"real run checks it first, and whether it then takes a small first search, or "
              f"collects that run instead, depends on what that run turns out to have been "
              f"charged and whether it finished. Nothing is decided here.")
        return
    plan = _first_pass_plan(stage, spec, cfg, workdir, budget)
    if not plan:
        return
    unit = C.estimate(stage, 1)[1]
    print(f"   this forecast is more than your budget cap allows (${cap_text(budget.cap)}), so "
          f"a real run first runs a small pass over "
          f"{_start_addresses(plan['full_input'])}, of "
          f"at most {plan['limit']} {unit}s (at most ${plan['pass_est']:.4f}), and goes on to "
          f"the full search only if it then fits the cap; a pass that finds fewer ads than "
          f"that is the whole result.")


def _collect_pending_full(stage, spec, cfg, workdir, budget, args, plan):
    """The full search an earlier command launched, and which finished, is the step's result.
    It is collected by the ordinary step code (_run_one) with no new charge for the run, its
    items saved and built and the handle cleared. _run_one is asked for a forecast of one item
    only because the run is already paid for (the gate has nothing to protect), and it sees a
    test-sized full run so it makes no test pass, as for the continuation of a first pass.

    The run's charge is counted FIRST, before the dataset is fetched and before anything that
    can refuse or fail, and the handle is marked as counted (see _guard_budget). Counted after
    them, a fetch that failed left a run billed by Apify and in nobody's count. _run_one then
    counts the same run once more, and the guard makes that second count nothing.

    Collecting launches nothing, so no limit on a launch may refuse it. _run_one gives every
    launch a limit from _cap_for, which refuses a run when less than the scraper's smallest
    limit (or 0.01) is left of the cap, and checks a forecast against the cap; with the run
    paid for and the cap used up by it, both would throw away a finished result and report a
    refusal that is not true. Both are replaced for this one call, and restored after.

    The run was reattached ONCE, in _settle_pending. _run_one gets it through a stand-in for
    _launch_recover that returns exactly that run for exactly that input and refuses anything
    else: the real one reattaches again, and an answer of None the second time (a network
    blip) would launch a new paid full search and leave this run's charge uncounted forever.
    A dataset with items but not one real ad (only error or status records) is a failed
    step: its charge is counted and logged, nothing is saved."""
    global _launch_recover, append_run_log, _cap_for
    stage_dir = Path(workdir) / spec["folder"]
    run, ih = plan["collect_run"], _input_hash(plan["full_input"])
    pend = _read_pending(stage_dir) or {}
    rid = pend.get("runId")
    cost = A.actual_charge_usd(run)   # _settle_pending held every run whose charge is no number
    _guard_budget(budget)
    counted_here = _counted_earlier_usd(pend) is None
    if counted_here:
        budget.charge(cost)
        _mark_counted(stage_dir, pend, cost)
    try:
        items = A.fetch_dataset(run.get("defaultDatasetId"))
    except Exception:
        # Billed and counted above, and the handle stays: the next command collects the run
        # again. The log says so, so the run is not missing from it.
        append_run_log(stage_dir, f"{stage} (full)", run, "not read",
                       note=f"the full search an earlier command started (run {rid}) finished "
                            f"and was charged ${cost:.4f}, counted; its results could not be "
                            f"fetched, so nothing of it was kept and the run is left to be "
                            f"collected again")
        raise
    if items and not any(_is_google_ad_record(it) for it in items):
        _STEP_RUNS.append("full")
        append_run_log(stage_dir, f"{stage} (full)", run, len(items),
                       note="only error or status records came back")
        append_command(stage_dir, spec["actor"], plan["full_input"], run,
                       memory_mb=_mem_for(spec, plan["full_input"]))
        _clear_pending(stage_dir)
        raise RuntimeError(
            f"the full search an earlier command started finished but returned no ads, only "
            f"{len(items)} error or status record{'s' if len(items) != 1 else ''}"
            f"{_why_non_ad(items[0])}; it was charged ${cost:.4f}, and nothing of it was kept")
    print(f"   the full search an earlier command started (run {rid}) finished and was not "
          f"collected: it is collected now with no new charge for the run, in place of a "
          f"first pass. (The forecast line below is for collecting it, not for a run: the "
          f"run is already paid for.)")

    def _build_collected(cfg_, workdir_, test, _build=spec["build"]):
        inp, _ = _build(cfg_, workdir_, test=False)
        return inp, 1

    def _only_that_run(spec_, actor_input, stage_dir_, subkey, **run_kw):
        if subkey != "full" or _input_hash(actor_input) != ih:
            raise RuntimeError("collecting the earlier full search would have launched a new "
                               "paid run; nothing was launched")
        _STEP_RUNS.append(subkey)
        print(f"   \u21bb recovered run {rid} for {subkey} (no re-charge)")
        return run

    def _log_collected(stage_dir_, label, run_, n_items, note="", est_usd=None):
        # The forecast _run_one prints and logs for this call is the one-item stand-in, so the
        # log entry would read "Pre-run estimate: ~$0.00". The run was paid for by an earlier
        # command: no estimate is logged, and the entry says where the run came from.
        real_log(stage_dir_, label, run_, n_items,
                 note=((note + " ") if note else "")
                 + f"collected from the run an earlier command started (run {rid}), which was "
                   f"already paid for: no new run was made, so there is no pre-run estimate.",
                 est_usd=None)

    def _no_limit(args_, budget_, est_cap):
        return est_cap   # nothing is launched, so there is no limit to give or to refuse

    real_launch, real_cap, real_log = _launch_recover, _cap_for, append_run_log
    own_check = budget.__dict__.get("check")
    _launch_recover, _cap_for, append_run_log = _only_that_run, _no_limit, _log_collected
    budget.check = lambda estimate_usd: None   # the forecast of a run already made gates nothing
    if counted_here:
        _COUNTED_EARLIER[0] = (rid, cost)   # counted above: _run_one's count adds nothing
    else:
        _arm_counted(plan["step"], pend)
    try:
        _run_one(stage, dict(spec, build=_build_collected), cfg, workdir, budget, args)
    finally:
        _launch_recover, _cap_for, append_run_log = real_launch, real_cap, real_log
        if own_check is None:
            budget.__dict__.pop("check", None)
        else:
            budget.check = own_check
        _COUNTED_EARLIER[0] = None


def _first_pass_instead_of_refusal(stage, spec, cfg, workdir, budget, args, refused, plan):
    """Run the first pass for a step whose full forecast was refused (`refused` is the
    BudgetExceeded _run_one raised, `plan` what _first_pass_applies returned). Raises a
    new BudgetExceeded, carrying first_pass, when the full search still does not fit
    after the pass; raises RuntimeError when the pass did not finish (the step's failure,
    never a budget stop). Returns normally when the step finished: its result is saved
    and built."""
    stage_dir = Path(workdir) / spec["folder"]
    full_input, est_count, est_usd = plan["full_input"], plan["est_count"], plan["est_usd"]
    pass_input, limit, pass_est = plan["pass_input"], plan["limit"], plan["pass_est"]
    unit = C.estimate(stage, 1)[1]
    if plan.get("collect_run") is not None:
        return _collect_pending_full(stage, spec, cfg, workdir, budget, args, plan)
    if plan.get("recovered_run") is None:   # a recovered pass is not planned: it was run before
        print(f"   the full forecast (~${est_usd:.2f}) does not fit the budget cap "
              f"(${cap_text(budget.cap)}, spent ${budget.spent:.2f}), so a small first pass runs "
              f"first, over {_start_addresses(full_input)}: "
              f"at most {limit} {unit}s, at most ${pass_est:.4f}. The cap stays a hard limit: "
              f"the full search runs only if it then fits.")
    _clear_incomplete(stage_dir)
    if plan.get("recovered_run") is None:
        print("   running FIRST PASS …")
    # Subkey "test": the pass is handled exactly as the test pass is, so a dropped
    # connection after it launched picks the same run up again with no second charge.
    # _cap_for bounds every Apify max charge by what is left of the cap.
    if plan.get("recovered_run") is None:
        pass_ceiling = _cap_for(args, budget, pass_est * C.CHARGE_CEILING_FACTOR)
    else:
        # Nothing is launched, so there is no limit to give and no smallest limit to meet:
        # asking _cap_for would refuse a pass that is already paid for when its charge used up
        # the cap. What limit the earlier command gave it is not known, but a pass is launched
        # only when what is left covers its worst case, so that limit was at least pass_est.
        pass_ceiling = pass_est
    run = _the_pass_run(plan, spec, stage_dir, pass_ceiling)
    # Counted the moment the run ends, before the fetch (see _run_one's test pass).
    pass_cost = A.actual_charge_usd(run)
    budget.charge(pass_cost)
    try:
        items = A.fetch_dataset(run.get("defaultDatasetId"))
    except Exception:
        # Billed and counted above, and its handle stays for a later command. Without this
        # entry the run was in the bill and in neither the run log nor commands.sh.
        append_run_log(stage_dir, f"{stage} (first pass)", run, "not read", est_usd=pass_est,
                       note="first small pass; it was charged and counted, but its results "
                            "could not be fetched, so nothing of it was kept")
        raise
    _clear_pending(stage_dir)  # the pass's output is consumed
    bad = _non_ad_items(items)
    n = len(items) - len(bad)   # ads only: an error or status item is never counted as one
    finished = run.get("status") == "SUCCEEDED" and not run.get("_incomplete")
    # Whatever the pass ended as, it was billed, so it is logged and listed.
    exhausted = (finished and not bad and n < limit
                 and pass_cost < 0.9 * pass_ceiling)   # not cut short by its own charge limit
    append_run_log(stage_dir, f"{stage} (first pass)", run, len(items), est_usd=pass_est,
                   note=("first small pass, run because the full forecast did not fit the "
                         "cap; it found fewer ads than it was allowed, so it is the full "
                         "result" if exhausted else
                         "first small pass, run because the full forecast did not fit the "
                         "cap"))
    append_command(stage_dir, spec["actor"], pass_input, run,
                   memory_mb=_mem_for(spec, pass_input))
    print(f"   first pass: {len(items)} records"
          + (f" ({len(bad)} of them not ads)" if bad else "")
          + f", reported ${pass_cost:.3f} "
          f"(budget spent ${budget.spent:.2f}/{cap_text(budget.cap)})")
    if not finished:
        # The scraper failed, timed out or was stopped: that is no answer about how many
        # ads exist. The step fails like any step whose run did not finish; it is never a
        # budget stop and never "found no ads". Nothing of the pass is kept.
        _write_notice(stage_dir, [m for m in [_run_notice(run, n)] if m], pass_cost)
        raise RuntimeError(
            f"the first small pass ended {run.get('status') or 'without finishing'} (run "
            f"{run.get('id', '?')}) after it was charged ${pass_cost:.4f}, so nothing of it "
            f"was kept and the full search was not run; this says nothing about how many "
            f"ads there are")
    if bad:
        # An error or status item among the results: the actor did not report on every
        # website, so a count below the limit proves nothing about how many ads exist.
        # The step fails (its charge is already counted); it is never a budget stop and
        # never a result.
        _only = P.only_error_items(items)
        raise RuntimeError(
            _only or f"the first small pass returned {len(bad)} error or status "
            f"record{'s' if len(bad) != 1 else ''}{_why_non_ad(bad[0])} among its {n} "
            f"{unit}s, so it did not report on everything it was asked about; it was "
            f"charged ${pass_cost:.4f}, nothing of it was kept and the full search was "
            f"not run")
    if exhausted:
        print(f"   the first pass found {n}, fewer than the {limit} it was allowed, and it "
              f"searched what the full search would: the search ran out of ads, so this is "
              f"the full result and no second run is made.")
        _notices = [m for m in [_run_notice(run, n)] if m]
        items, _new = _run_after_fetch_hook(stage, spec, items, None, None, full_input,
                                            stage_dir, budget, args)
        _save_dataset(stage_dir, "02_full", items)
        _write_notice(stage_dir, _notices, pass_cost)
        print(f"   full: {len(items)} records saved")
        print(f"   reported ${pass_cost:.3f}  ·  spent ${budget.spent:.2f}/{cap_text(budget.cap)}")
        P.postprocess(stage, stage_dir, workdir, cfg)
        _run_after_save_hook(stage, spec, items, full_input, stage_dir, budget, args,
                             cfg, workdir)
        return
    # Not the whole result: price the full search from what the pass cost per ad, as
    # the test pass does (_run_one), and refuse if it is still over the cap.
    est_new, rate_ratio = est_usd, 1.0
    if n and pass_cost:
        actual_rate = pass_cost / n
        est_new = est_count * actual_rate * C.SAFETY_MARGIN
        rate_ratio = actual_rate / C.EMPIRICAL_RATES[stage][2]
        print(f"   recalibrated full estimate from the first pass: ~${est_new:.2f} "
              f"(${actual_rate:.5f}/{unit})")
    # The full search, priced at that rate, is what _run_one gates and caps. The count
    # is the one that prices to it (estimate = count x table rate x margin, plus the
    # start fee), rounded up so the price is never low.
    priced_count = int(math.ceil(est_count * rate_ratio))
    est_priced = C.estimate(stage, priced_count, runs=1)[0]
    try:
        budget.check(est_priced)
    except BudgetExceeded as exc:
        exc.first_pass = {"records": n, "limit": limit, "unit": unit + "s",
                          "succeeded": True, "charged_usd": round(float(pass_cost or 0.0), 6)}
        raise

    def _build_priced(cfg_, workdir_, test, _build=spec["build"], _count=priced_count):
        # The full input at the count that prices to the first pass's own rate. The test
        # input is the same input, so _run_one sees a test-sized full run and makes no
        # second test pass (the first pass was it).
        inp, _ = _build(cfg_, workdir_, test=False)
        return inp, _count

    print(f"   the full search fits the cap at the first pass's price. Running it; the "
          f"forecast line below shows ~{priced_count} {unit}s, the count that prices to "
          f"that rate, not your setting. Apify's own limit on it is what is left of the "
          f"cap.")
    # _run_one gives the full run Apify's max charge through _cap_for, which never passes
    # what is left of the cap, whatever the real price per ad turns out to be.
    _run_one(stage, dict(spec, build=_build_priced), cfg, workdir, budget, args)


def main():
    ap = argparse.ArgumentParser(description="Run ad-intelligence Apify steps.")
    ap.add_argument("phase", choices=list(PHASE_ORDER.keys()))
    ap.add_argument("--config", default=None)
    ap.add_argument("--workdir", required=True, help="this run's working folder")
    ap.add_argument("--only", default=None, help="run just one step, by its code: B1 (Google search for competitors), "
                         "B4 (Meta ads), B5 (Google ads) or LI (LinkedIn ads)")
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
    args = ap.parse_args()

    cfg = C.load_config(args.config)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

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
            try:
                _run_one(stage, spec, cfg, workdir, budget, args)
            except BudgetExceeded as _refused:
                # A step whose full forecast was refused takes a small first pass if
                # its spec says so and the pass fits (see _first_pass_instead_of_refusal).
                # Anything else is the stop it always was.
                _plan = _first_pass_applies(stage, spec, cfg, workdir, _refused, budget,
                                            _spent_before, args)
                if not _plan:
                    raise
                _first_pass_instead_of_refusal(stage, spec, cfg, workdir, budget, args,
                                               _refused, _plan)
            if args.dry_run:
                _say_first_pass_plan(stage, spec, cfg, workdir, budget)
        except BudgetExceeded as e:
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
