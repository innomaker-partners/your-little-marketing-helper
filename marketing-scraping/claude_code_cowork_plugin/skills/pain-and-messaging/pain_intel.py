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
import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the scripts directory and import local modules
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import config as C         # noqa: E402
import subjects as SBJ     # noqa: E402
import extractor as EXT    # noqa: E402  (deterministic facts extractor)
import report as RPT       # noqa: E402  (LLM clusters facts -> E1/E2)


# ---------------------------------------------------------------------------
# Subprocess helpers (mirrors ad_intel.py's _run_phase for package consistency)
# ---------------------------------------------------------------------------

def _run_phase(phase, stage, cfg_path, workdir, dry_run):
    """Invoke run_phase.py for one stage.  Streams output live.
    Returns subprocess.CompletedProcess (check returncode in caller).
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
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    sys.stdout.flush()
    return subprocess.run(cmd, check=False)


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

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Pain and messaging pipeline runner.  "
            "Chains subject resolution -> Maps discovery (B2a) -> "
            "bridge -> Maps reviews (B2b).  Analysis half (extractor + "
            "E1/E2 reports) wired in."
        )
    )
    ap.add_argument("--config", default=None,
                    help="Path to PAIN_CONFIG.json (default: auto-detect beside this script)")
    ap.add_argument("--workdir", required=True,
                    help="Working folder for this run — all outputs land here")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan + inputs, spend nothing "
                         "(passes --dry-run to run_phase subprocesses; "
                         "Python steps operate on existing fixtures or skip gracefully)")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else C.repo_default_config_path()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("pain_intel — Pain & Messaging Pipeline")
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

    print(f"\nConfig  : {cfg_path}")
    print(f"  vertical  : {cfg.get('vertical', '(none)')}")
    print(f"  city      : {cfg.get('city', '(none)')}")
    print(f"  country   : {cfg.get('country', '(none)')}")
    print(f"  mode      : {mode}  "
          f"({'place URLs supplied' if mode == '1' else 'names supplied' if mode == '2' else 'category discovery'})")
    print(f"  sources   : {', '.join(sources)}")
    print(f"  budget cap: ${cfg['budget_caps'].get('phase1', 999):.2f}")

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

        if mode == "1":
            # Mode 1: Maps place URLs provided directly
            urls = cfg.get("competitor_place_urls", [])
            print(f"Mode 1 (place URLs supplied): {len(urls)} URL(s)")
            for u in urls:
                print(f"  {u}")
            if args.dry_run:
                print("[dry-run] would write B2a_maps_places/review_targets.json with these URLs")
                print("[dry-run] B2a (Maps discovery) would be SKIPPED")
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
            print(f"\nMode-2 B2a config written: {mode2_cfg_path}")
            print(f"  search strings: {mode2_cfg['maps_search_queries']}")

            _run_phase(1, "B2a", mode2_cfg_path, workdir, dry_run=args.dry_run)

            if args.dry_run:
                print("\n[dry-run] would call subjects.select_mode2_targets() -> review_targets.json")
                print("          best-match selector picks ONE Maps place per name, "
                      "skips aggregators, keeps ALL names (no review-count cap)")
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
                        print(f"   {r['name']!r} - {r.get('note', '')}")
                    print("  Verify the name is findable on Google Maps, or supply "
                          "competitor_place_urls directly (mode 1).")

        else:
            # Mode 3: category + location -> B2a discovery
            queries = cfg.get("maps_search_queries") or \
                      ([f'{cfg.get("vertical","")} {cfg.get("city","")}'.strip()]
                       if cfg.get("vertical") or cfg.get("city") else [])
            print(f"Mode 3 (category discovery): {len(queries)} search query/queries")
            for q in queries:
                print(f"  {q}")
            if not queries:
                print("[warn] No search queries — B2a will produce no results. "
                      "Set maps_search_queries, or vertical+city, in your config.")

            _run_phase(1, "B2a", cfg_path, workdir, dry_run=args.dry_run)

            if args.dry_run:
                print("[dry-run] postprocess._pp_b2a would sort places by review count "
                      f"and cap at {cfg.get('review_target_count', 60)} -> review_targets.json")

        # ---- 3. empty corpus guard -------------------------------------------
        if not args.dry_run:
            targets = _read_json(rt_path, []) or []
            if not targets:
                print("\n" + "!" * 60)
                print("! GUARD: review_targets.json is EMPTY")
                print("! B2b will scrape ZERO reviews -- silent empty corpus.")
                print("! Possible causes:")
                # Location is the first thing to suspect when it was set: a region
                # or list geocodes to nothing and empties the whole B2a run.
                _loc = cfg.get("maps_location_query")
                if _loc is None:
                    _loc = f'{cfg.get("city","")}, {cfg.get("country","")}'.strip().strip(",").strip()
                if _loc and mode != "1":
                    print(f"!   location: {_loc!r} may not be a single place Google Maps")
                    print("!             can find -- a region or list fails the whole run.")
                    print("!             Try one city (e.g. 'Chicago, USA'), or set")
                    print("!             maps_location_query to \"\" to omit it.")
                print("!   mode 1: check competitor_place_urls are valid Maps URLs")
                print("!   mode 2: check names are findable on Maps; inspect B2a output")
                print("!   mode 3: check B2a ran, places were found, postprocess ran")
                print("!" * 60)
                sys.exit(1)
            else:
                print(f"\nreview_targets.json: {len(targets)} place(s) queued for B2b")
                for t in targets:
                    print(f"  {t.get('url', '(no url)')}")

        # ---- 4. Maps reviews (B2b) -------------------------------------------
        print("\n--- Maps Reviews (B2b) ---")
        _run_phase(1, "B2b", cfg_path, workdir, dry_run=args.dry_run)

        reviews_path = workdir / "B2b_reviews" / "run_02_full.json"
        if args.dry_run:
            print("[dry-run] would read B2b_reviews/run_02_full.json")
        else:
            review_items = _read_json(reviews_path, []) or []
            print(f"  Loaded {len(review_items)} review record(s)")
            if not review_items:
                print("  [warn] 0 reviews -- verify B2b ran successfully and "
                      "review_targets.json contained valid Maps URLs.")

    # ------------------------------------------------------------------
    # Trustpilot acquisition (Phase B).
    # Runs only when "trustpilot" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "trustpilot" in sources:
        print("\n--- Trustpilot Acquisition (TP): Domain Validation ---")
        # Validate-only: no name→domain search. Each supplied
        # domain is format-checked + a free reachability/redirect check surfaces a
        # dead domain or a domain redirect before spend.
        # Free, so it runs in dry-run too.
        if not cfg.get("trustpilot_domains"):
            print("  [warn] trustpilot_domains is empty -- TP stage will have no companyUrls; "
                  "add domains to config before running.")
        else:
            tp_entries = SBJ.resolve_trustpilot(cfg, workdir)
            print(SBJ.format_trustpilot_report(tp_entries))
            if not [e for e in tp_entries if e.get("url")]:
                print("  [warn] no valid Trustpilot domain -- TP stage would scrape nothing.")
            if args.dry_run:
                print("[dry-run] would run TP stage via run_phase --only TP "
                      "(reusing trustpilot_reviews/resolution.json)")
            else:
                _run_phase(1, "TP", cfg_path, workdir, dry_run=False)

    # ------------------------------------------------------------------
    # App Store acquisition (Phase C).
    # Runs only when "appstore" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "appstore" in sources:
        print("\n--- App Store Acquisition (AS): Subject Resolution ---")
        # Resolve names→ids and VALIDATE supplied ids via the free iTunes APIs,
        # BEFORE any spend. Runs in dry-run too (it costs nothing) so the human
        # confirms the exact apps from the evidence, then the real run reuses the
        # persisted resolution. Kills the "wrong/typo'd id scrapes nothing" lie.
        if not (cfg.get("appstore_names") or cfg.get("appstore_app_ids")):
            print("  [warn] neither appstore_names nor appstore_app_ids is set -- "
                  "AS stage will have no apps; add app names or ids to config.")
        else:
            as_entries = SBJ.resolve_appstore(cfg, workdir)
            print(SBJ.format_appstore_report(as_entries))
            # id non-empty == usable by the stage (see subjects: failures set id="").
            resolved_ids = [e["id"] for e in as_entries if e.get("id")]
            if not resolved_ids:
                print("  [warn] no App Store app resolved -- AS stage would scrape nothing; "
                      "fix the names/ids above before the real run.")
            if args.dry_run:
                print("[dry-run] would run AS stage via run_phase --only AS "
                      "(reusing appstore_reviews/resolution.json)")
            else:
                _run_phase(1, "AS", cfg_path, workdir, dry_run=False)

    # ------------------------------------------------------------------
    # Google Play acquisition (Phase C).
    # Runs only when "googleplay" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "googleplay" in sources:
        print("\n--- Google Play Acquisition (GP): Subject Resolution ---")
        # Resolve names→packages and VALIDATE supplied packages via the public
        # Play store pages (free), BEFORE any spend. Runs in dry-run too.
        if not (cfg.get("googleplay_names") or cfg.get("googleplay_app_ids")):
            print("  [warn] neither googleplay_names nor googleplay_app_ids is set -- "
                  "GP stage will have no apps; add app names or packages to config.")
        else:
            gp_entries = SBJ.resolve_googleplay(cfg, workdir)
            print(SBJ.format_googleplay_report(gp_entries))
            # id non-empty == usable by the stage (failures set id=""; the network
            # pass-through keeps its id on purpose).
            resolved = [e["id"] for e in gp_entries if e.get("id")]
            if not resolved:
                print("  [warn] no Google Play app resolved -- GP stage would scrape nothing; "
                      "fix the names/packages above before the real run.")
            if args.dry_run:
                print("[dry-run] would run GP stage via run_phase --only GP "
                      "(reusing googleplay_reviews/resolution.json)")
            else:
                _run_phase(1, "GP", cfg_path, workdir, dry_run=False)

    # ------------------------------------------------------------------
    # 4e. Reddit Acquisition (RD) — the first NO-STAR source.
    #     Runs only when "reddit" is in cfg["sources"]. The scrape collects a
    #     post+comment corpus; postprocess._pp_reddit + banding.py then
    #     synthesize the low/high signal (Component B) and set neutral items
    #     aside — so by the time the analysis half reads the corpus, it looks
    #     like any other star-rated source (frozen spine unchanged).
    # ------------------------------------------------------------------
    if "reddit" in sources:
        print("\n--- Reddit Acquisition (RD) — no-star source, banded after scrape ---")
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
            print("  [warn] no reddit targets -- set reddit_queries, reddit_subreddits, "
                  "or reddit_urls in config before running; RD stage will be skipped.")
        if args.dry_run:
            print("[dry-run] would run RD stage via run_phase --only RD, then band the corpus")
        else:
            _run_phase(1, "RD", cfg_path, workdir, dry_run=False)

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
            print("  [warn] neither g2_names nor g2_urls is set -- G2 stage will have "
                  "no products; add product names or G2 URLs/slugs to config.")
        else:
            try:
                g2_entries, g2_cost = SBJ.resolve_g2(cfg, workdir, discover=not args.dry_run)
            except RuntimeError as e:
                print(f"  [STOP] G2 name discovery failed: {e}")
                print("  Not proceeding to the G2 review scrape. Re-run after checking the "
                      "actor/token; do not retry blindly (money-path).")
                g2_entries = None
            if g2_entries is not None:
                print(SBJ.format_g2_report(g2_entries))
                if not args.dry_run and g2_cost:
                    print(f"  (G2 discovery spent ${g2_cost:.3f})")
                resolved = [e for e in g2_entries if e.get("slug") and not e.get("note")]
                if not resolved:
                    print("  [warn] no G2 product resolved -- G2 review scrape would have no "
                          "products; fix the names/URLs above before proceeding.")
                elif args.dry_run:
                    print("[dry-run] would run G2 review scrape via run_phase --only G2 "
                          "(reusing g2_reviews/resolution.json)")
                else:
                    _run_phase(1, "G2", cfg_path, workdir, dry_run=False)

    # ------------------------------------------------------------------
    # Capterra acquisition — second B2B-SaaS source, star-rated.
    # Runs only when "capterra" is in cfg["sources"].
    # ------------------------------------------------------------------
    if "capterra" in sources:
        print("\n--- Capterra Acquisition (CP): URL Validation ---")
        # Validate-only: Capterra bot-walls a free GET and its
        # actor has no search field, so supplied product URLs are structurally
        # validated and surfaced at the gate — a malformed URL is flagged, not
        # silently dropped at stage-build. Free, runs in dry-run too.
        if not cfg.get("capterra_urls"):
            print("  [warn] capterra_urls is empty -- CP stage will have no profileUrls; "
                  "add Capterra product URLs (/p/<id>/<slug>) to config before running.")
        else:
            cp_entries = SBJ.resolve_capterra(cfg, workdir)
            print(SBJ.format_capterra_report(cp_entries))
            if not [e for e in cp_entries if e.get("url")]:
                print("  [warn] no valid Capterra URL -- CP stage would scrape nothing.")
            if args.dry_run:
                print("[dry-run] would run CP stage via run_phase --only CP "
                      "(reusing capterra_reviews/resolution.json)")
            else:
                _run_phase(1, "CP", cfg_path, workdir, dry_run=False)

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
            merged.extend(records)
        if not merged:
            print(f"\n{'!'*60}")
            print("! WARNING: ALL enabled sources returned 0 records.")
            print("!   The merged corpus is empty -- no pain or trust phrases can be extracted.")
            print("!   Verify that at least one source ran successfully.")
            print(f"{'!'*60}\n")
        facts_path = EXT.extract_records(merged, workdir, cfg)
        print(f"  facts : {facts_path}")
        reports = RPT.generate(workdir, cfg)
        e1, e2  = reports.get("E1"), reports.get("E2")
        print(f"  E1    : {e1 or '(not generated)'}")
        print(f"  E2    : {e2 or '(not generated)'}")
        if not e1 and not e2:
            print("  [note] No reports were written. If the `claude` CLI is not "
                  "installed, install it (or set report_model to a model your CLI "
                  "has) and re-run. The corpus and synthesis_facts.json are on disk "
                  "regardless, so re-running only repeats the free analysis step.")
    print("=" * 60)


if __name__ == "__main__":
    main()
