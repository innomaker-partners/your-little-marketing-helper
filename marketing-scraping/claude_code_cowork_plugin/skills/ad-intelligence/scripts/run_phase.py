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
                       result_saved=False):
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
    started: they keep the plain record, exactly as before."""
    step_runs = list(step_runs or [])
    for left in order[order.index(stage):]:
        stage_dir = Path(workdir) / STAGES[left]["folder"]
        if _stage_done(stage_dir, STAGES[left]) and not force:
            continue  # Finished and not run again, so not stopped
        if left != stage:
            _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd)
            continue
        pending_now = _read_pending(stage_dir) or {}
        _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd,
                           spent_usd=max(0.0, float(step_spent_usd or 0.0)),
                           part_way=bool(step_runs),
                           rerun_reuses_run=bool(step_runs)
                           and pending_now.get("subkey") == step_runs[0])


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
    print(f"   full-run forecast: ~{est_count} {unit}s → ~${est_usd:.2f}"
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
        n_test = len(items)
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
        print(f"   full: {len(items)} records saved across {len(chunks)} batches")
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
        print(f"   full: {len(items)} records saved")
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


def _cap_for(args, budget, est_cap):
    """Per-call maxTotalChargeUsd: est_cap, further bounded by the run ceiling.

    WHY: under --standing-approval the user approved a total-run ceiling; that
    ceiling must be enforced even if a single actor's estimated cap (est_usd *
    CHARGE_CEILING_FACTOR) would exceed what remains of the budget.  Without
    --run-ceiling-usd this function is transparent: returns est_cap unchanged.
    """
    ceiling = getattr(args, "run_ceiling_usd", None)
    if ceiling is None:
        return est_cap
    remaining = max(0.01, ceiling - budget.spent)
    return min(est_cap, remaining)


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
    budget = Budget(args.phase, cap)

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
        _STEP_SAVED.clear()
        try:
            # Enforce the cap before starting a new stage. Safe boundary:
            # nothing from this stage has been written yet.
            budget.enforce()
            _run_one(stage, spec, cfg, workdir, budget, args)
        except BudgetExceeded as e:
            print(f"\n⛔ {e}\n   Stopping. Spent ${budget.spent:.2f}.")
            # Every later step in this call did not run. This
            # step may have run part of its work first (_note_budget_stop).
            if not args.dry_run:
                _note_budget_stop(workdir, order, stage, cap,
                                  budget.spent - _spent_before, _STEP_RUNS,
                                  force=args.force)
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
