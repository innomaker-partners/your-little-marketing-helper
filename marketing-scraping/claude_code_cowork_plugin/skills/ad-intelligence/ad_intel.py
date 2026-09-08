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

import config as C      # noqa: E402
import resolver         # noqa: E402
import resolve as R     # noqa: E402  (unified resolve→validate→confirm layer)
import creatives        # noqa: E402
import ocr as OCR       # noqa: E402
import selector         # noqa: E402
import e3_report        # noqa: E402


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run_phase(phase, stage, cfg_path, workdir, dry_run):
    """
    Invoke run_phase.py for one stage. Streams output live.
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

    lines = [
        "# Discovered Meta Advertisers — Review Checklist",
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
        lines.append(f"| {i} | {d['page_id']} | {name} | [ ] |")

    lines += [
        "",
        f"Total distinct advertisers in scrape: {len(seen_ids)}",
        f"Already in your list (approx, heuristic): {len(seen_ids) - len(discovered)}",
        f"Surfaced for review: {len(discovered)}",
    ]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n[curation] {len(discovered)} new advertiser(s) surfaced for review -> {md_path}")
    if discovered:
        print("  Add confirmed competitors to competitor_domains in your config and re-run.")

    return discovered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Ad-intelligence pipeline runner. "
                    "Chains competitor resolution -> Meta -> Google -> creatives -> OCR."
    )
    ap.add_argument("--config", default=None,
                    help="Path to AD_INTEL_CONFIG.json (default: auto-detect beside this script)")
    ap.add_argument("--workdir", required=True,
                    help="Working folder for this run — all outputs land here")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan + inputs, spend nothing "
                         "(passes --dry-run to all run_phase.py subprocesses; "
                         "Python steps operate on existing fixtures or skip gracefully)")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="Skip the post-resolution confirmation gate and proceed "
                         "straight to the paid ad scrapes. Use only when the "
                         "resolution report has ALREADY been reviewed (e.g. an "
                         "owner-approved validation run). Default is to STOP after "
                         "resolution so a human can confirm the targets before spend.")
    ap.add_argument("--force", action="store_true",
                    help="Re-resolve even if competitors/resolution.json exists")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else C.repo_default_config_path()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("ad_intel — Ad Intelligence Pipeline")
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
    print(f"  budget cap  : ${cfg['budget_caps'].get('phase1', 999):.2f}")

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

    if identities:
        reused = resolution_path.exists() and not args.force
        if reused:
            entries = _read_json(resolution_path, []) or []
            print(f"Reusing resolution for {len(entries)} competitor(s) "
                  f"(competitors/resolution.json exists; pass --force to re-resolve).")
        else:
            print(f"Resolving {len(identities)} competitor(s) via SERP: {identities}")
            # Each competitor gets a small query set (bare + site:facebook.com +
            # site:linkedin.com) so the verified domain, the advertiser Facebook
            # Page, and the full LinkedIn name are each surfaced reliably.
            R.write_queries(identities, workdir)
            _run_phase(1, "B1", cfg_path, workdir, dry_run=args.dry_run)
            if args.dry_run:
                print("[dry-run] would resolve each competitor from SERP → verified "
                      "domain / Facebook Page / full LinkedIn name, then write "
                      "competitors/resolution.json + domains.json and show a "
                      "resolution report at this confirmation gate.")
            else:
                entries = R.resolve_all(identities, workdir)
                # Fill each entry's LinkedIn NUMERIC company id (a free GET of the
                # public company page) BEFORE writing + reporting. The LI stage
                # scopes only by numeric id; resolving it here keeps the id inside
                # the confirmation gate, so the human sees "[id 68529]" (or the
                # ⚠ no-id warning) and approves the real target before any spend.
                R.enrich_linkedin_ids(entries)
                R.write_resolution(entries, workdir)

        if entries:
            print(R.format_resolution_report(entries))
        competitor_set = [e["domain"]["use"] for e in entries
                          if (e.get("domain") or {}).get("use")]

        # ---- CONFIRMATION GATE — never spend on unconfirmed targets ----
        confirmed = (args.auto_confirm or reused
                     or bool(cfg.get("resolution_confirmed"))
                     or (comp_dir / "CONFIRMED").exists())
        if not args.dry_run and not confirmed:
            print("\n" + "#" * 64)
            print("# STOP — resolution complete. Confirm the targets before any spend.")
            print("#" * 64)
            print("Review the RESOLUTION REPORT above — especially any ⚠ lines.")
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
        print(f"Mode: seed-term discovery ({len(seed_terms)} seed term(s))")
        q_dir = workdir / "B1_serp_competitors"
        q_dir.mkdir(parents=True, exist_ok=True)
        (q_dir / "queries.json").write_text(
            json.dumps({"queries": seed_terms}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _run_phase(1, "B1", cfg_path, workdir, dry_run=args.dry_run)
        if args.dry_run:
            print("[dry-run] would surface discovered competitor domains for review, "
                  "then stop for confirmation (no paid scrape on unverified domains).")
            return
        discovered = _read_json(
            workdir / "B1_serp_competitors" / "competitor_domains.json", []) or []
        top = discovered[:20]
        print("\n" + "!" * 60)
        print(f"! DISCOVERY: {len(top)} candidate competitor domain(s) — VERIFY before spend")
        print("!" * 60)
        for d in top:
            print(f"    {d}")
        print("\nThese are auto-discovered and may include false positives.")
        print("Add the confirmed ones to `competitor_domains` in your config and")
        print("re-run — they will be resolved, validated, and confirmed before any")
        print("paid Meta / Google / LinkedIn scrape.")
        print("!" * 60)
        return

    else:
        print("[warn] No competitor_names, competitor_domains, or seed_terms in the "
              "config — nothing to resolve. Add competitors and re-run.")
        return

    # Which paid ad stages actually have a target (never spend on an empty one).
    def _has_meta_target():
        has_page = any((e.get("facebook") or {}).get("use") for e in entries)
        return has_page or bool(cfg.get("meta_search_terms"))

    def _has_google_target():
        return bool(competitor_set)

    # ------------------------------------------------------------------
    # 3. Meta Ad Library (B4)
    # ------------------------------------------------------------------
    print("\n--- Meta Ad Library ---")
    if args.dry_run or _has_meta_target():
        _run_phase(1, "B4", cfg_path, workdir, dry_run=args.dry_run)
    else:
        print("  ⤫ No Meta target: no competitor resolved to a Facebook Page and no "
              "`meta_search_terms` sweep was requested — skipping Meta. (The tool "
              "will not fall back to an unfiltered keyword market sweep on its own.)")

    meta_path = workdir / "B4_meta_ad_library" / "run_02_full.json"
    if args.dry_run:
        meta_items: list[dict] = []
        print("[dry-run] would read B4_meta_ad_library/run_02_full.json")
    else:
        meta_items = _read_json(meta_path, []) or []
        print(f"  Loaded {len(meta_items)} Meta ad record(s)")

    # ------------------------------------------------------------------
    # 4. Meta creative download IMMEDIATELY (fbcdn.net URLs expire)
    # ------------------------------------------------------------------
    print("\n--- Meta Creative Download ---")
    meta_saved = 0
    if cfg.get("download_creatives"):
        if args.dry_run:
            print("[dry-run] would call creatives.download_meta_creatives() "
                  "(Meta fbcdn URLs expire — must run immediately after B4)")
        elif meta_items:
            manifest = creatives.download_meta_creatives(
                meta_items, str(workdir / "creatives")
            )
            meta_saved = sum(
                1 for v in manifest.values() for s in v if not s.startswith("FAILED")
            )
        else:
            print("  No Meta items — nothing to download.")
    else:
        print("  download_creatives=false — skipping.")

    # ------------------------------------------------------------------
    # 5. Advertiser curation pass (surface, don't block)
    # ------------------------------------------------------------------
    print("\n--- Advertiser Curation Pass ---")
    if args.dry_run:
        print("[dry-run] would diff Meta advertisers against supplied set")
        print("[dry-run] would write competitors/DISCOVERED_advertisers.md")
        discovered_advertisers: list[dict] = []
    else:
        discovered_advertisers = _curation_pass(meta_items, competitor_set, workdir)

    # ------------------------------------------------------------------
    # 6. Google Ads Transparency (B5)
    # ------------------------------------------------------------------
    print("\n--- Google Ads Transparency ---")
    if args.dry_run or _has_google_target():
        _run_phase(1, "B5", cfg_path, workdir, dry_run=args.dry_run)
    else:
        print("  ⤫ No verified competitor domain resolved — skipping Google Ads "
              "Transparency (it searches ONLY by verified domain). See the "
              "resolution report for which competitors did not resolve a domain.")

    google_path = workdir / "B5_google_ads" / "run_02_full.json"
    if args.dry_run:
        google_items: list[dict] = []
        print("[dry-run] would read B5_google_ads/run_02_full.json")
    else:
        google_items = _read_json(google_path, []) or []
        print(f"  Loaded {len(google_items)} Google ad record(s)")
        if not google_items:
            # B5 uses RESIDENTIAL proxies; without them Google's SearchService
            # RPC blocks datacenter IPs with HTTP 429, zeroing out the run.
            # A 0 result can still mean a block (Google throttles even
            # residential IPs occasionally) OR that these competitors genuinely
            # run no Google ads — we cannot tell them apart from the empty
            # result alone, so flag it without over-claiming, and suggest a retry.
            print("  ⚠ 0 Google ads — either these competitors run no Google ads, "
                  "or the scrape was blocked. If you expected results, re-run the Google scrape "
                  "(it uses residential proxies; a repeat 0 points to genuinely no ads).")

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
                google_items, str(workdir / "creatives")
            )
            google_saved = sum(
                1 for v in manifest.values() for s in v if not s.startswith("FAILED")
            )
        else:
            print("  No Google items — nothing to download.")
    else:
        print("  download_creatives=false — skipping.")

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
            ocr_ids = selector.top_creative_ids_by_duration(google_items, frac)
            print(f"  OCR scope: top {int(frac * 100)}% by duration → {len(ocr_ids)} of {len(google_items)} ads")

            for creative_id, filenames in google_manifest.items():
                if creative_id not in ocr_ids:
                    continue
                for fname in filenames:
                    if not isinstance(fname, str) or fname.startswith("FAILED"):
                        continue
                    img_path = creatives_dir / fname
                    if not img_path.exists():
                        continue
                    try:
                        text = OCR.ocr_ad_image(str(img_path), model)
                        if text:
                            ocr_results[creative_id] = text
                            ocr_count += 1
                    except Exception as exc:
                        print(f"  [ocr] Skipped {fname}: {exc} — continuing")

            if ocr_results:
                ocr_out = workdir / "google_ocr.json"
                _write_json(ocr_out, ocr_results)
                print(f"  {ocr_count} image(s) transcribed -> {ocr_out}")
            elif not google_manifest:
                print("  No Google creative manifest found — skipping OCR.")
            else:
                print("  No images to transcribe (all failed or empty manifest).")
    else:
        print("  ocr_google_text=false — skipping.")

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
        print("  include_linkedin=false — skipping.")
    elif not args.dry_run and not li_targets:
        # No numeric id => no precise target. Do NOT spend on a fuzzy name search
        # that would scrape the wrong advertiser. Distinguish the two gap causes.
        if not li_named:
            print("  Skipping LinkedIn: NO competitor resolved to a LinkedIn company. "
                  "This is a RESOLUTION gap, not a finding. Add/confirm the LinkedIn "
                  "company in competitors/resolution.json, then re-run.")
        else:
            names = ", ".join(e['linkedin']['use'] for e in li_named)
            print(f"  Skipping LinkedIn: resolved a company NAME ({names}) but NO numeric "
                  "company id — the Ad Library scopes only by id, so a scrape now would "
                  "return the wrong advertiser. The id lookup (public company page) failed "
                  "or was blocked; add `company_id` to the entry in "
                  "competitors/resolution.json and re-run.")
    else:
        _run_phase(1, "LI", cfg_path, workdir, dry_run=args.dry_run)

        li_path = workdir / "LI_linkedin_ads" / "run_02_full.json"
        if args.dry_run:
            print("[dry-run] would read LI_linkedin_ads/run_02_full.json")
        else:
            li_items = _read_json(li_path, []) or []
            print(f"  Loaded {len(li_items)} LinkedIn ad record(s)")
            if not li_items:
                # A numeric id resolved but the scrape returned nothing. Because the
                # id is EXACT (not a fuzzy name), this is a genuine zero, not a
                # wrong-target — state it plainly with the ids that were scraped.
                ids = ", ".join(f"{e['linkedin']['use']} (id {e['linkedin']['company_id']})"
                                for e in li_targets)
                print(f"  0 LinkedIn ads for: {ids}. The numeric company id resolved and the "
                      "search is worldwide (no country filter), so this is a genuine zero — "
                      "the company runs no LinkedIn ads in the Ad Library right now.")

        print("\n--- LinkedIn Creative Download ---")
        if cfg.get("download_creatives"):
            if args.dry_run:
                print("[dry-run] would call creatives.download_linkedin_creatives() "
                      "(some licdn.com URLs carry real expiry tokens — download promptly)")
            elif li_items:
                li_manifest = creatives.download_linkedin_creatives(
                    li_items, str(workdir / "creatives")
                )
                li_saved = sum(
                    1 for v in li_manifest.values() for s in v
                    if not s.startswith("FAILED")
                )
            else:
                print("  No LinkedIn items — nothing to download.")
        else:
            print("  download_creatives=false — skipping.")

    # ------------------------------------------------------------------
    # 10. Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Competitors resolved  : {len(competitor_set)}")
    print(f"  Meta ads collected    : {len(meta_items)}")
    print(f"  Google ads collected  : {len(google_items)}")
    print(f"  LinkedIn ads collected: {len(li_items)}")
    print(f"  Creatives saved       : {meta_saved + google_saved + li_saved}"
          f"  (meta={meta_saved}, google={google_saved}, linkedin={li_saved})")
    print(f"  OCR transcriptions    : {ocr_count}")
    print(f"  Outputs in            : {workdir.resolve()}")
    if args.dry_run:
        print("\n  [dry-run] No real data was fetched or charged.")
        print("\n  [dry-run] would assemble report/ bundle")
    else:
        e3_report.assemble_report(workdir, cfg)
    print("=" * 60)


if __name__ == "__main__":
    main()
