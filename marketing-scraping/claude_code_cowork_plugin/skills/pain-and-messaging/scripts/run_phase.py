#!/usr/bin/env python3
"""
Stage runner — runs a set of Apify stages SEQUENTIALLY, test-first, budget-gated,
auto-logging. Runs this tool's Apify stages (B2a places, B2b reviews, and review
sources). The higher-level pain/messaging flow
(subject-mode resolution, the corpus-derived extractor, the E1/E2 report) is
coordinated on top of this in a separate entry point; this module is the
reusable "run one Apify stage safely" engine plus a thin CLI for isolated
single-stage runs.

    python3 run_phase.py 1 --config ../PAIN_CONFIG.json --workdir <run>
    python3 run_phase.py 1 --only B2a --test-only --config ... --workdir ...
    python3 run_phase.py 1 --dry-run --config ... --workdir ...   # plan + inputs, spend nothing

For each stage: build TEST input (3-5 items) → check budget → run → recompute the
per-unit rate from ACTUAL test cost → estimate the full run → check budget → run
full → save output → append RUN_LOG.md + commands.sh → charge the budget.
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
from budget import Budget, BudgetExceeded
from runlog import append_run_log, append_command
from stages import STAGES, PHASE_ORDER
import stages as S
import postprocess as P


def _save_dataset(stage_dir, tag, items):
    out = Path(stage_dir) / f"run_{tag}.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _launch_recover(spec, actor_input, stage_dir, subkey, **run_kw):
    """Launch an actor run, but first try to recover a run already launched for
    this exact input (a transient drop must never re-charge). Records the run
    handle at launch (on_start) so a mid-run or mid-fetch drop resumes by id."""
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


def _run_one(stage, spec, cfg, workdir, budget, args):
    stage_dir = Path(workdir) / spec["folder"]
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Resume-safety: skip a stage that already completed (unless --force).
    if not args.dry_run and _stage_done(stage_dir, spec) and not args.force:
        print(f"\n▶ {stage}: already complete — skipping (use --force to re-run).")
        return

    est_count = spec["build"](cfg, str(workdir), test=False)[1]
    est_usd, unit, rate, unverified = C.estimate(stage, est_count)
    print(f"\n▶ {stage}  ({spec['actor']})")
    print(f"   full-run forecast: ~{est_count} {unit}s → ~${est_usd:.2f}"
          + ("  [rate UNVERIFIED]" if unverified else ""))
    # Dry-run clarity: when the actor's own minimum cap exceeds our forecast x
    # factor, the spend CAP is raised to that minimum so Apify accepts the run.
    # That cap is a ceiling, not the expected cost -- show it so the user does not
    # read the floor as the price.
    _fc_cap = est_usd * C.CHARGE_CEILING_FACTOR
    _fc_floor = A.actor_min_charge(spec["actor"])
    if _fc_floor > _fc_cap:
        print(f"   spend cap: ${max(_fc_floor, 0.01):.2f} — {spec['actor']}'s "
              f"minimum allowed cap (a ceiling, not the expected cost; the run "
              f"still bills ~the forecast)")

    if args.dry_run:
        di, _ = spec["build"](cfg, str(workdir), test=False)
        blob = json.dumps(di, ensure_ascii=False)
        print(f"   input: {blob if len(blob) <= 600 else blob[:600] + ' …'}")
        return

    # --- TEST run ---
    test_input, test_count = spec["build"](cfg, str(workdir), test=True)
    if not _has_inputs(stage, test_input):
        print(f"   ⤫ no inputs for {stage} yet (missing upstream output?) — skipping.")
        return
    full_preview, _ = spec["build"](cfg, str(workdir), test=False)
    # When the full input is already no larger than a test pass, the test and the
    # full run send the SAME items — running both bills the actor twice for one
    # output, and for per-actor-start pricing that doubles the bill. Skip the
    # test in the normal path; the full-estimate budget gate below still guards
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
                              max_charge_usd=test_ceiling)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        test_cost = A.actual_charge_usd(run)
        budget.charge(test_cost)
        _clear_pending(stage_dir)  # test output consumed -> nothing to recover
        append_run_log(stage_dir, f"{stage} (test)", run, len(items), est_usd=est_usd, note="test run")
        append_command(stage_dir, spec["actor"], test_input, run)
        n_test = len(items)
        print(f"   test: {n_test} records, reported ${test_cost:.3f} "
              f"(budget spent ${budget.spent:.2f}/{budget.cap:.2f})")

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
              f"— skipping the redundant test pass (it would bill the same run twice).")
    budget.check(est_usd)

    # --- FULL run ---
    full_input, _ = spec["build"](cfg, str(workdir), test=False)
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
            bceiling = est_usd * (len(chunk) / len(arr)) * C.CHARGE_CEILING_FACTOR
            run = _launch_recover(spec, binput, stage_dir, f"batch{bi}",
                                  memory_mb=_mem_for(spec, binput),
                                  on_status=lambda s, bi=bi: print(f"     [batch {bi}/{len(chunks)}: {s}]"),
                                  max_charge_usd=bceiling)
            bitems = A.fetch_dataset(run.get("defaultDatasetId"))
            items.extend(bitems)
            bcost = A.actual_charge_usd(run)
            full_cost += bcost
            budget.charge(bcost)
            _clear_pending(stage_dir)  # this batch's output consumed
            # Enforce the cap after each batch's charge so an over-budget
            # run aborts before launching the NEXT batch. Safe boundary: the next
            # batch has not yet started; no half-processed data on disk.
            budget.enforce()
            append_run_log(stage_dir, f"{stage} (full batch {bi}/{len(chunks)})", run, len(bitems), est_usd=est_usd)
            append_command(stage_dir, spec["actor"], binput, run)
        _save_dataset(stage_dir, "02_full", items)
        print(f"   full: {len(items)} records saved across {len(chunks)} batches")
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{budget.cap:.2f}")
    else:
        run = _launch_recover(spec, full_input, stage_dir, "full",
                              memory_mb=_mem_for(spec, full_input),
                              on_status=lambda s: print(f"     [{s}]"),
                              max_charge_usd=est_usd * C.CHARGE_CEILING_FACTOR)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        _save_dataset(stage_dir, "02_full", items)
        full_cost = A.actual_charge_usd(run)
        budget.charge(full_cost)
        _clear_pending(stage_dir)  # full output saved -> nothing to recover
        append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd)
        append_command(stage_dir, spec["actor"], full_input, run)
        print(f"   full: {len(items)} records saved")
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{budget.cap:.2f}")

    # Derived artifacts for downstream stages (B1 -> competitor domains).
    P.postprocess(stage, stage_dir, workdir, cfg)


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
    B2a keys its search on `searchStringsArray`; B2b on `startUrls`;
    TP (Trustpilot) on `companyUrls`; apps on `appIds`.

    RD (Reddit) is special: it has SEVERAL heterogeneous target arrays
    (searchTerms / subredditUrls / startUrls) and is runnable if ANY one is
    non-empty. The generic loop below returns on the first key it sees, which
    cannot express that "any of" semantics -- so Reddit gets its own branch.

    Uses _real_entries() so that lists of blank/None values are
    treated as absent rather than present — preventing garbage input from
    passing the gate and spending money on an empty actor run."""
    if stage == "RD":
        return any(bool(_real_entries(actor_input.get(k) or []))
                   for k in ("searchTerms", "subredditUrls", "startUrls"))
    for key in ("searchStringsArray", "startUrls", "queries", "urls",
                "companyUrls", "appIds", "profileUrls"):
        v = actor_input.get(key)
        if v is not None:
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
    the union across all supported stages so the shared engine stays correct
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


def main():
    ap = argparse.ArgumentParser(description="Run pain/messaging Apify stages.")
    ap.add_argument("phase", choices=list(PHASE_ORDER.keys()))
    ap.add_argument("--config", default=None)
    ap.add_argument("--workdir", required=True, help="this run's working folder")
    ap.add_argument("--only", default=None, help="run just this stage code (B2a/B2b)")
    ap.add_argument("--test-only", action="store_true", help="stop after the test run")
    ap.add_argument("--dry-run", action="store_true", help="print plan + inputs, spend nothing")
    ap.add_argument("--force", action="store_true", help="re-run stages even if already complete")
    args = ap.parse_args()

    cfg = C.load_config(args.config)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    cap = cfg["budget_caps"].get(f"phase{args.phase}", 999.0)
    budget = Budget(args.phase, cap)

    order = [args.only] if args.only else list(PHASE_ORDER[args.phase])

    # Task selection: if the config has a `stages` allow-list, run only those.
    sel = S.selected_stages(cfg)
    if sel is not None and not args.only:
        order = [s for s in order if s in sel]

    if not order:
        print(f"=== Phase {args.phase}: no selected tasks — nothing to run. ===")
        return

    print(f"=== Phase {args.phase} · budget cap ${cap:.2f} ===")
    for stage in order:
        spec = STAGES[stage]
        try:
            # Enforce the cap before starting a new stage. Safe boundary:
            # nothing from this stage has been written yet.
            budget.enforce()
            _run_one(stage, spec, cfg, workdir, budget, args)
        except BudgetExceeded as e:
            print(f"\n⛔ {e}\n   Stopping. Spent ${budget.spent:.2f}.")
            break
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠ {stage} failed: {e}\n   Continuing to next stage.")
    print(f"\n=== Phase {args.phase} done · spent ${budget.spent:.2f}/{cap:.2f} ===")


def _run_has_inputs_tests():
    """Offline unit tests for _has_inputs. No network, no Apify calls."""
    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        nonlocal PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}")
            print(f"        got:      {got!r}")
            print(f"        expected: {expected!r}")
            FAIL += 1

    print("\n=== _has_inputs tests blank-list bypass ===")

    # --- Cases that MUST return False (would bypass the gate before fix) ---
    check('["", ""] -> False (blank strings)',
          _has_inputs("TP", {"companyUrls": ["", ""]}), False)
    check('["  "] -> False (whitespace-only)',
          _has_inputs("TP", {"companyUrls": ["  "]}), False)
    check('[None] -> False (None entries)',
          _has_inputs("AS", {"appIds": [None]}), False)
    check('[None, None] -> False (all-None list)',
          _has_inputs("AS", {"appIds": [None, None]}), False)
    check('["   "] -> False (whitespace URL for B2b)',
          _has_inputs("B2b", {"startUrls": ["   "]}), False)
    check('["", ""] -> False (empty strings for B2b)',
          _has_inputs("B2b", {"startUrls": ["", ""]}), False)
    check('RD searchTerms=[""] only -> False (blank term)',
          _has_inputs("RD", {"searchTerms": [""], "subredditUrls": [], "startUrls": []}), False)
    check('RD all keys blank -> False',
          _has_inputs("RD", {"searchTerms": [""], "subredditUrls": [""], "startUrls": [""]}), False)
    check('[] -> False (empty list, existing behavior preserved)',
          _has_inputs("TP", {"companyUrls": []}), False)

    # --- Cases that MUST return True (valid inputs must still pass) ---
    check('[{"url": "https://example.com"}] -> True (dict entry)',
          _has_inputs("B2b", {"startUrls": [{"url": "https://example.com"}]}), True)
    check('["324684580"] -> True (valid appId string)',
          _has_inputs("AS", {"appIds": ["324684580"]}), True)
    check('["https://trustpilot.com/review/acme.com"] -> True (valid URL)',
          _has_inputs("TP", {"companyUrls": ["https://trustpilot.com/review/acme.com"]}), True)
    check('RD valid searchTerm -> True',
          _has_inputs("RD", {"searchTerms": ["CRM software"], "subredditUrls": [], "startUrls": []}),
          True)
    check('RD valid subredditUrl only -> True',
          _has_inputs("RD", {"searchTerms": [], "subredditUrls": ["r/sales"], "startUrls": []}),
          True)
    check('Unknown stage / key not in list -> True (default passthrough)',
          _has_inputs("XX", {"unknownKey": []}), True)
    check('Mixed valid+blank: ["", "real-id"] -> True (at least one real entry)',
          _has_inputs("AS", {"appIds": ["", "real-id"]}), True)

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL


def _run_recovery_tests():
    """Offline tests for run recovery. No network: A.reattach and A.run_actor
    are monkeypatched with fakes so we test the routing, not Apify."""
    import tempfile
    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        nonlocal PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}  got={got!r} expected={expected!r}")
            FAIL += 1

    print("\n=== run recovery tests ===")
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        check("_input_hash order-stable", _input_hash({"a": 1, "b": 2}),
              _input_hash({"b": 2, "a": 1}))
        check("_input_hash input-sensitive",
              _input_hash({"a": 1}) != _input_hash({"a": 2}), True)
        check("no pending initially", _read_pending(sd), None)
        _write_pending(sd, {"runId": "x", "subkey": "full", "input_hash": "h"})
        check("pending roundtrip", _read_pending(sd)["runId"], "x")
        _clear_pending(sd)
        check("clear removes pending", _read_pending(sd), None)

        spec = {"actor": "acme/act"}
        inp = {"q": 1}
        ih = _input_hash(inp)
        calls = {"reattach": 0, "run_actor": 0}
        _real_reattach, _real_run_actor = A.reattach, A.run_actor
        try:
            # HIT: pending matches and reattach recovers -> no launch.
            _write_pending(sd, {"runId": "r9", "datasetId": "d9", "input_hash": ih,
                                "subkey": "full", "actor": "acme/act"})
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1)
                or {"status": "SUCCEEDED", "id": rid})
            A.run_actor = lambda *a, **k: (
                calls.__setitem__("run_actor", calls["run_actor"] + 1)
                or {"status": "SUCCEEDED"})
            _launch_recover(spec, inp, sd, "full")
            check("recover HIT reattaches", calls["reattach"], 1)
            check("recover HIT does not launch", calls["run_actor"], 0)

            # MISS: pending present but reattach returns None -> re-launch.
            calls["reattach"] = calls["run_actor"] = 0
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1) or None)
            _write_pending(sd, {"runId": "r9", "datasetId": "d9", "input_hash": ih,
                                "subkey": "full", "actor": "acme/act"})
            _launch_recover(spec, inp, sd, "full")
            check("recover MISS re-launches", calls["run_actor"], 1)

            # SUBKEY/HASH MISMATCH must not reattach a different run's handle.
            calls["reattach"] = calls["run_actor"] = 0
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1)
                or {"status": "SUCCEEDED", "id": rid})
            _write_pending(sd, {"runId": "rOther", "datasetId": "d", "input_hash": ih,
                                "subkey": "test", "actor": "acme/act"})
            _launch_recover(spec, inp, sd, "full")  # subkey 'full' != pending 'test'
            check("subkey mismatch does NOT reattach", calls["reattach"], 0)
            check("subkey mismatch launches fresh", calls["run_actor"], 1)

            # No pending -> launch, and on_start persists the handle for next time.
            _clear_pending(sd)

            def _fake_run_actor(actor, actor_input, on_start=None, **kw):
                on_start("rNEW", "dNEW")  # simulate the launch callback firing
                return {"status": "SUCCEEDED", "id": "rNEW"}
            A.run_actor = _fake_run_actor
            _launch_recover(spec, inp, sd, "full")
            pend = _read_pending(sd)
            check("launch persists runId at on_start", pend["runId"], "rNEW")
            check("pending records subkey", pend["subkey"], "full")
            check("pending records input_hash", pend["input_hash"], ih)
        finally:
            A.reattach, A.run_actor = _real_reattach, _real_run_actor

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    import sys as _sys
    # If called with --test or no args while not being a phase run, run tests.
    if len(_sys.argv) == 1 or (len(_sys.argv) == 2 and _sys.argv[1] == "--test"):
        fails = _run_has_inputs_tests()
        fails += _run_recovery_tests()
        _sys.exit(0 if fails == 0 else 1)
    main()
