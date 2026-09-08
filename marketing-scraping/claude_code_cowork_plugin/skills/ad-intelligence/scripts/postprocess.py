"""
Derived artifacts between stages — the "glue" that turns one stage's raw output
into the next stage's input. Called by run_phase.py after a full run.

For this tool the only glue is B1 -> competitor domains (discovery / name
resolution). Actor output field names vary; parsers try the common keys and
degrade gracefully (empty artifact) rather than crashing the pipeline.
"""

import json
from pathlib import Path
from urllib.parse import urlparse


def _read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _latest_run(stage_dir):
    """The full run's raw JSON (falls back to the test run)."""
    for name in ("run_02_full.json", "run_01_test.json"):
        p = Path(stage_dir) / name
        if p.exists():
            return _read_json(p, [])
    return []


def _domain(url):
    try:
        net = urlparse(url if url.startswith("http") else f"https://{url}").netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


# Marketplaces / socials / directories that aren't a competitor's own site.
# Extended with common business directories to prevent mistaking a directory
# listing for a competitor's official domain (used by the name->domain resolver).
_SKIP_DOMAINS = {
    "facebook.com", "instagram.com", "youtube.com", "google.com", "goo.gl",
    "linkedin.com", "tiktok.com", "wikipedia.org", "yelp.com", "x.com",
    "twitter.com", "pinterest.com", "reddit.com",
    # directories / aggregators
    "bark.com", "thumbtack.com", "angi.com", "houzz.com", "trustpilot.com",
    "yell.com", "checkatrade.com", "clutch.co", "g2.com", "capterra.com",
}


def postprocess(stage, stage_dir, workdir, cfg):
    if stage == "B1":
        _pp_b1(stage_dir)
    elif stage == "LI":
        _pp_li(stage_dir)


# --- B1 -> competitor domains (discovery + resolution) ------------------------

def _pp_b1(stage_dir):
    items = _latest_run(stage_dir)
    domains = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        results = it.get("organicResults") or it.get("results") or []  # VERIFY
        for r in results:
            url = (r or {}).get("url") or (r or {}).get("link")
            d = _domain(url or "")
            if d and d not in _SKIP_DOMAINS:
                domains[d] = domains.get(d, 0) + 1
    ranked = [d for d, _ in sorted(domains.items(), key=lambda kv: -kv[1])]
    (Path(stage_dir) / "competitor_domains.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   → {len(ranked)} competitor domains extracted")


# --- LI -> canonical field names (scrapesage actor) ---------------------------
# The LI stage uses scrapesage/linkedin-ad-library-scraper (numeric-id scoping). scrapesage
# names some fields differently; the downstream report (e3_report.py) and creative
# downloader (creatives.py) read the canonical names below. This adapter adds the
# canonical alias to each record IN PLACE, non-destructively — the raw field is
# kept, and an alias is written only when the source key is present and the
# canonical key is absent. So a record that already uses a canonical name (or a
# future actor change) is left untouched rather than clobbered.
#
# source (scrapesage)   -> canonical (report/creatives)
_LI_ALIASES = {
    "bodyText": "body",
    "creativeTypeLabel": "format",
    "creativeType": "format",
    "firstShownDate": "firstShownAt",
    "lastShownDate": "lastShownAt",
    "totalImpressions": "totalImpressionsMax",
}


def _pp_li(stage_dir):
    """Add canonical field aliases to every LinkedIn ad record, in place, for each
    run file present. Idempotent: re-running never overwrites an existing canonical
    value. Reports how many records/fields were bridged so a mapping miss is loud,
    not silent (the whole point of the rebuild)."""
    touched_files = 0
    for name in ("run_01_test.json", "run_02_full.json"):
        p = Path(stage_dir) / name
        if not p.exists():
            continue
        items = _read_json(p, [])
        if not isinstance(items, list):
            continue
        bridged = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            for src, canon in _LI_ALIASES.items():
                if it.get(src) not in (None, "") and it.get(canon) in (None, ""):
                    it[canon] = it[src]
                    bridged += 1
            # imageUrl (singular) is the first of imageUrls, for the creative digest.
            imgs = it.get("imageUrls")
            if isinstance(imgs, list) and imgs and it.get("imageUrl") in (None, ""):
                it["imageUrl"] = imgs[0]
                bridged += 1
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        touched_files += 1
        print(f"   → LI {name}: {len(items)} records, {bridged} canonical fields bridged")
    if not touched_files:
        print("   → LI: no run files to post-process")
