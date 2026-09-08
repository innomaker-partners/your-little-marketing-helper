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
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
import extractor
import report
from seeds import resolve_seeds


# ---------------------------------------------------------------------------
# Subprocess helper (mirrors ad_intel.py._run_phase, but runs the WHOLE phase)
# ---------------------------------------------------------------------------

def _run_phase(cfg_path, workdir, dry_run=False, test_only=False, force=False):
    """
    Invoke run_phase.py for phase 1 (all stages: A1 -> A2 -> B1).

    Does NOT pass --only so run_phase.py runs the full sequence with
    postprocessing between stages (which is exactly the scrape chain we want).
    Streams output live. Returns subprocess.CompletedProcess.
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
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    sys.stdout.flush()
    return subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Synthesis + report tail (unit-testable without the scrape)
# ---------------------------------------------------------------------------

def _synthesize_and_report(workdir, cfg):
    """
    Run extraction then the S1 report on the artifacts in workdir.

    Defensive: if synthesis_facts.json ends up empty or missing after extraction,
    prints a clear message rather than crashing.

    Returns the Path to S1_search_landscape.md, or None when the report was not
    generated (e.g. the claude CLI is absent or returned no output).
    """
    workdir = Path(workdir)

    print("\n--- Synthesis ---")
    try:
        facts_path = extractor.extract(workdir, cfg)
    except Exception as exc:
        print(f"[search-intel] extractor failed: {exc} -- skipping report")
        return None

    # Sanity check: make sure the file is non-empty before calling the report step.
    if not facts_path.exists() or facts_path.stat().st_size == 0:
        print("[search-intel] synthesis_facts.json is empty or missing -- skipping report")
        return None

    print("\n--- Report (S1 search landscape) ---")
    try:
        report_path = report.generate(workdir, cfg)
    except Exception as exc:
        print(f"[search-intel] report.generate failed: {exc}")
        return None

    if report_path is None:
        print("[search-intel] S1 report not generated (claude CLI absent or no valid output)")
    else:
        print(f"\nS1 report -> {report_path}")

    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="search-intelligence pipeline: "
                    "A1 -> A2 -> B1 -> synthesis -> S1 report."
    )
    ap.add_argument("--config", default=None, help="path to SEARCH_CONFIG.json")
    ap.add_argument("--workdir", required=True, help="run output directory (created if absent)")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="print plan + inputs, spend nothing ($0); skips synthesis/report",
    )
    ap.add_argument(
        "--test-only", action="store_true",
        help="run only the small test pass for each stage; still runs synthesis/report on test data",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="re-run stages even if already complete in this workdir",
    )
    args = ap.parse_args()

    # Resolve the config path once so we can pass it cleanly to the subprocess.
    cfg_path = Path(args.config).resolve() if args.config else C.repo_default_config_path()

    # Load and validate the config.
    cfg = C.load_config(cfg_path)

    # Create the workdir so downstream stages can write into it immediately.
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # Resolve seeds -- determines which input mode the user has configured.
    # resolve_seeds raises SystemExit if NEITHER seed_terms nor vertical is set,
    # so past this point `seeds` is guaranteed non-empty (the FROZEN "non-empty
    # keyword universe OR stop loudly" requirement, satisfied at resolution time).
    seeds, mode = resolve_seeds(cfg)

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

    print("search-intelligence - pipeline start")
    print(f"  config     : {cfg_path}")
    print(f"  resolved   : {resolved_cfg_path}")
    print(f"  workdir    : {workdir.resolve()}")
    print(f"  input mode : {mode}")
    print(f"  seeds ({len(seeds)}) : {seeds}")
    if mode == "vertical":
        print("  (vertical mode: these seeds will be expanded by A1 autocomplete)")
    if args.dry_run:
        print("  [dry-run] No Apify runs, no API calls, $0 spend.")
    if args.test_only:
        print("  [test-only] Small test pass only; synthesis will run on test-sized data.")

    # ------------------------------------------------------------------
    # Scrape chain: A1 -> A2 -> B1 (run_phase.py handles sequencing +
    # postprocessing between stages). Pass the RESOLVED config so the
    # subprocess sees the seeds we just resolved, not the on-disk original.
    # ------------------------------------------------------------------
    result = _run_phase(
        resolved_cfg_path, workdir,
        dry_run=args.dry_run,
        test_only=args.test_only,
        force=args.force,
    )

    if result.returncode != 0:
        print(
            f"\n[search-intel] run_phase.py exited with code {result.returncode} "
            f"-- aborting pipeline. Fix the scrape error and re-run.",
            flush=True,
        )
        sys.exit(result.returncode)

    # ------------------------------------------------------------------
    # Synthesis + report tail.
    # Skipped on --dry-run (nothing was scraped, nothing to synthesise).
    # Runs on --test-only (small artifacts make a useful smoke test).
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n[dry-run] Scrape chain plan printed. Synthesis/report skipped ($0 spent).")
        return

    if args.test_only:
        print("\n[test-only] Running synthesis/report on test-sized data (directional only).")

    _synthesize_and_report(workdir, cfg)


if __name__ == "__main__":
    main()
