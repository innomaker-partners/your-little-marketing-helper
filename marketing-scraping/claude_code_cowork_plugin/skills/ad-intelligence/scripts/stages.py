"""
Stage registry — the config-driven definition of every Apify stage the
ad-intelligence tool runs.

Four stages this tool supports:
  B1  google-search-scraper      — SERP, used for competitor DISCOVERY and for
                                    resolve→validate→confirm (name/domain →
                                    verified domain / Facebook Page / LinkedIn
                                    name), the pre-flight before any paid stage.
  B4  facebook-ads-library       — Meta Ad Library, by ADVERTISER PAGE (primary;
                                    resolved Facebook Page URLs) or by keyword
                                    (explicit `meta_search_terms` sweep only).
  B5  google-ads-scraper         — Google Ads Transparency, by VERIFIED domain.
  LI  linkedin-ads-scraper       — LinkedIn Ad Library, by full company name,
                                    worldwide (no country filter by default).

Each stage knows: which actor to call, how to BUILD its Apify input from the
config + the resolution report on disk (competitors/resolution.json,
competitors/domains.json), and how many records to EXPECT (for the pre-run
budget gate). run_phase.py consumes this registry. Nothing here spends money —
run_phase.py does, one stage at a time.

Supports both `country_codes` (list, preferred) and the legacy `country_code`
scalar. `country_codes` overrides `country_code`; both keys are back-compat.
The keyword-sweep B4 and B5 produce one URL per (country × term|domain); by-Page
B4 uses a single `scrapePageAds.countryCode` ("ALL" when multi-country). LI is
keyed by advertiser name and does not filter by country unless asked.
"""

import json
from pathlib import Path


# --- small helpers to read the competitor list from the workdir ---------------

def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _bare_domain(d):
    """Normalize a URL or domain string to a bare host (no scheme, no www, no path)."""
    if not d:
        return ""
    return (d.replace("https://", "").replace("http://", "")
             .replace("www.", "").rstrip("/").split("/")[0])


def _derive_company_name(domain):
    """
    Derive a human-readable company name from a competitor domain.

    E.g. 'acmeplumbing.com' → 'Acmeplumbing', 'best-drains.com' → 'Best Drains'.

    Algorithm: strip scheme/www → remove TLD → split on hyphens/dots →
    Title Case each token → join with space.
    Used by build_LI when `competitor_names` is not supplied — a rough
    derived name is better than nothing, and the user can override via
    competitor_names in the config.
    """
    bare = _bare_domain(domain)
    if not bare:
        return domain
    # Strip TLD: drop the last dot-segment (e.g. "acmeplumbing.com" → "acmeplumbing")
    parts = bare.split(".")
    name_part = parts[0] if len(parts) == 1 else ".".join(parts[:-1])
    # Split remaining on hyphens and dots, Title Case each token
    tokens = [t for t in name_part.replace(".", "-").split("-") if t]
    return " ".join(t.capitalize() for t in tokens)


def _competitor_domains(workdir):
    """Domains from a B1 discovery run (competitor_domains.json). The fallback
    pool when the user supplied no list; prefer the resolved list below."""
    f = Path(workdir) / "B1_serp_competitors" / "competitor_domains.json"
    return _read_json(f, []) or []


def _curated_domains(workdir):
    """The competitor domain list B5 scrapes. Written by the pipeline from
    whichever input mode the user chose (domains / resolved names / discovery),
    to `competitors/domains.json`; falls back to a raw B1 discovery run.
    Entries may be {"url": ...} objects or plain domain strings."""
    resolved = _read_json(Path(workdir) / "competitors" / "domains.json", None)
    raw = ([t.get("url") if isinstance(t, dict) else t for t in resolved]
           if resolved else _competitor_domains(workdir))
    out, seen = [], set()
    for d in raw:
        b = _bare_domain(d)
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _resolution(workdir):
    """The resolve→validate→confirm entries written by resolve.write_resolution
    (competitors/resolution.json). [] before resolution has run. Each entry
    carries the chosen `use` identifier + candidates + flags per ad source."""
    return _read_json(Path(workdir) / "competitors" / "resolution.json", []) or []


def _resolved_facebook_pages(workdir):
    """Chosen Facebook Page URLs (one per competitor that resolved to a Page),
    de-duped, order-preserving. Drives Meta by-advertiser scraping."""
    out, seen = [], set()
    for e in _resolution(workdir):
        u = ((e.get("facebook") or {}).get("use") or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _resolved_linkedin_names(workdir):
    """Chosen full registered LinkedIn company names, de-duped. Kept for the
    resolution report and as a legacy fallback only — the LI STAGE now scopes by
    NUMERIC id (see _resolved_linkedin_ids), because name/keyword matching in the
    Ad Library is fuzzy and returns OTHER advertisers."""
    out, seen = [], set()
    for e in _resolution(workdir):
        n = ((e.get("linkedin") or {}).get("use") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _resolved_linkedin_ids(workdir):
    """Chosen NUMERIC LinkedIn company ids, de-duped, order-preserving. Filled by
    resolve.enrich_linkedin_ids AFTER resolution (a free GET of the public company
    page). The LinkedIn Ad Library scraper scopes to an advertiser ONLY by numeric
    id; a name/slug search is fuzzy content-matching that surfaces unrelated
    advertisers. [] before enrichment or when no id resolved — the caller
    then skips LI rather than scraping the wrong target."""
    out, seen = [], set()
    for e in _resolution(workdir):
        cid = str((e.get("linkedin") or {}).get("company_id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


# --- per-stage input builders -------------------------------------------------
# Each returns (actor_input_dict, expected_record_count). `test` truncates inputs.

def build_B1(cfg, workdir, test):
    """SERP. Queries come from an explicit list the caller writes to
    B1_serp_competitors/queries.json (company names for mode-2 resolution, or
    seed terms for mode-3 discovery); falls back to cfg['seed_terms']."""
    qf = Path(workdir) / "B1_serp_competitors" / "queries.json"
    queries = (_read_json(qf, {}) or {}).get("queries", []) or cfg.get("seed_terms", [])
    queries = queries[:5] if test else queries
    inp = {
        "queries": "\n".join(queries),
        "resultsPerPage": 10, "maxPagesPerQuery": 1,
        "countryCode": cfg["country_code"], "languageCode": cfg.get("language", "en"),
    }
    return inp, len(queries)


def build_B4(cfg, workdir, test):
    """Meta Ad Library — by ADVERTISER PAGE (primary) or by KEYWORD (explicit sweep).

    Primary path (the company-research intent): scrape the resolved competitors'
    own Facebook Pages. The curious_coder actor's `urls` field accepts a Page URL
    directly (verified against its input schema), and `scrapePageAds.*` (flat
    dot-keys, per the actor's own example) controls the per-page scrape. Country
    rides `scrapePageAds.countryCode`, which accepts a 2-letter code or "ALL" —
    so a single-country run pins the market and a multi-country run uses "ALL"
    (the advertiser's ads across every country) rather than inventing per-URL
    country behavior the actor does not document.

    Keyword sweep (secondary, EXPLICIT opt-in via `meta_search_terms`): the old
    whole-market keyword search. It is no longer the default — an unfiltered
    keyword sweep is exactly what returned ~45 off-target hitchhikers and never
    the target's own ads. It runs only when the user asks for it by supplying
    `meta_search_terms`.

    Returns ({"urls": []}, 0) when NO advertiser page resolved and NO sweep terms
    were supplied — the caller skips B4 in that case rather than silently
    sweeping. Never falls back to a market sweep on its own.
    """
    countries = [c.upper() for c in (cfg.get("country_codes") or [cfg["country_code"]])]
    cap = 20 if test else int(cfg.get("meta_limit_per_source", 200))

    pages = _resolved_facebook_pages(workdir)
    sweep_terms = [t for t in (cfg.get("meta_search_terms") or []) if t]

    # --- primary: by advertiser Page ---
    if pages:
        pages = pages[:3] if test else pages
        country_code = countries[0] if len(countries) == 1 else "ALL"
        inp = {
            "urls": [{"url": p} for p in pages],
            "scrapePageAds.activeStatus": "all",
            "scrapePageAds.sortBy": "impressions_desc",
            "scrapePageAds.countryCode": country_code,
            "limitPerSource": cap,
        }
        return inp, cap * len(pages)

    # --- secondary: explicit keyword sweep ---
    if sweep_terms:
        sweep_terms = sweep_terms[:3] if test else sweep_terms
        urls = [{"url": f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                        f"&country={cc}&q={t}&search_type=keyword_unordered&media_type=all"}
                for cc in countries for t in sweep_terms]
        return {"urls": urls, "limitPerSource": cap}, cap * len(urls)

    # --- no target: never sweep silently ---
    return {"urls": []}, 0


def build_LI(cfg, workdir, test):
    """LinkedIn Ad Library scoped by NUMERIC company id (scrapesage actor).

    Why numeric id and not name (a hard-won lesson): the LinkedIn Ad
    Library's name/keyword/slug search is fuzzy CONTENT matching — searching
    "hubspot" returns thousands of ads, but many belong to OTHER advertisers who
    merely mention HubSpot, and searching "HubSpot"/"HubSpot, Inc." returned a
    silent 0 (name/keyword search is unreliable by design).
    The only precise advertiser scoping the Ad Library offers is the advertiser's
    numeric company id. `companyIds:["68529"]` returns HubSpot's ads and only
    HubSpot's — verified live, 6/6.

    Where the id comes from: resolve.enrich_linkedin_ids fills each entry's
    `linkedin.company_id` after the resolution report, by one free GET of the
    public company page (the id is embedded there — no paid actor, no login). So
    the id is available on disk by the time this stage runs.

    Returns ({"companyIds": []}, 0) when NO numeric id resolved — the caller
    then SKIPS LinkedIn rather than falling back to a fuzzy name search that would
    scrape the wrong advertiser. This is the whole point of the rebuild: no silent
    wrong-target. There is no name/keyword fallback by design.

    enrichAdDetails:True is required — without it the actor returns only ad ids,
    not advertiserName/body/impressions. No OCR needed: copy comes back as text.
    Creatives (imageUrls, videoUrl) are media.licdn.com URLs downloaded right
    after the stage (some carry expiry timestamps).
    """
    ids = _resolved_linkedin_ids(workdir)
    ids = ids[:3] if test else ids
    if not ids:
        # No numeric id => no precise target. Never fall back to fuzzy name search.
        return {"companyIds": []}, 0

    max_ads = 25 if test else int(cfg.get("linkedin_max_items", 100))
    inp = {
        "companyIds": ids,
        "maxAds": max_ads,
        "maxAdsPerQuery": max_ads,
        "enrichAdDetails": True,
    }
    # Country filtering is OPT-IN only (default: worldwide). scrapesage accepts an
    # optional `countries` list; omit it unless the user explicitly asks.
    explicit = [c.upper() for c in (cfg.get("linkedin_countries") or []) if c]
    if explicit:
        inp["countries"] = explicit
    return inp, max_ads * len(ids)


def build_B5(cfg, workdir, test):
    """Google Ads Transparency, one by-domain URL per competitor. `region` rides
    country code(s). Batched (see STAGES["B5"]) — ~50 domain-searches otherwise
    overrun the actor timeout. Ad DURATION (firstShownAt->lastShownAt) is the
    profitability proxy.
    Supports multi-country: `country_codes` list preferred; falls back to
    single `country_code` for back-compat. Produces one URL per (country × domain).
    """
    countries = [c.upper() for c in (cfg.get("country_codes") or [cfg["country_code"]])]
    domains = _curated_domains(workdir)
    domains = domains[:2] if test else domains
    start = [{"url": f"https://adstransparency.google.com/?region={cc}&domain={d}"}
             for cc in countries for d in domains]
    # In test mode, cap maxItems — not just the domain count. Without this cap
    # the actor can scrape hundreds of ads instead of the intended test sample
    # (823 ads at $1.10 vs the expected ~$0.04 in one early test run).
    # Keep 25 in test so a sample arrives fast and cheaply.
    # Full-run cap is config-overridable (`google_max_items`). Default 2000.
    max_items = 25 if test else int(cfg.get("google_max_items", 2000))
    # Google Ads Transparency's SearchService RPC blocks DATACENTER IPs with
    # HTTP 429. Passing bare {"useApifyProxy": True} selects the datacenter
    # pool by default, so every request is 429'd (0 ads, ~6 min of retries,
    # wasted compute). The actor's OWN input-schema default is RESIDENTIAL —
    # match it. Where the run is a single country, pin the proxy exit country
    # to the market so requests look local (Apify guidance for this target);
    # with multiple countries in one run we let residential rotate.
    proxy = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    if len(countries) == 1:
        proxy["apifyProxyCountry"] = countries[0]
    return ({"startUrls": start, "maxItems": max_items,
             "proxyConfiguration": proxy}, len(start) * 15)


# --- registry -----------------------------------------------------------------

# ~50 Google-Ads domain-searches in one run overrun the actor's timeout;
# batch B5's startUrls into chunks of this size.
B5_BATCH_SIZE = 10

STAGES = {
    "B1": {"actor": "apify/google-search-scraper",                "phase": "1", "folder": "B1_serp_competitors", "build": build_B1, "output": "dataset"},
    "B4": {"actor": "curious_coder/facebook-ads-library-scraper", "phase": "1", "folder": "B4_meta_ad_library",  "build": build_B4, "output": "dataset", "memory_mb": 1024, "mem_per_url": 512},
    "B5": {"actor": "lexis-solutions/google-ads-scraper",         "phase": "1", "folder": "B5_google_ads",       "build": build_B5, "output": "dataset", "batch": {"key": "startUrls", "size": B5_BATCH_SIZE}},
    # LI is NOT in PHASE_ORDER — it is invoked explicitly (like B4/B5)
    # because it is opt-in (include_linkedin config flag) and has no mem_per_url constraint.
    "LI": {"actor": "scrapesage/linkedin-ad-library-scraper",     "phase": "1", "folder": "LI_linkedin_ads",    "build": build_LI, "output": "dataset"},
}

# Default run order. B1 (discovery) is invoked on demand, not in the default ad-scrape order.
PHASE_ORDER = {
    "1": ["B4", "B5"],
}

QUESTION = {
    "B1": "Who ranks/advertises for these queries? (discovery + name->domain)",
    "B4": "Who advertises on Meta? What angles? How long running?",
    "B5": "Who bids on Google Ads and for how long (profitability proxy)?",
    "LI": "Who advertises on LinkedIn? What copy/angles? How many impressions?",
}

# Input dependencies. B5 needs a competitor domain list — supplied via config,
# resolver, or B1 discovery (not necessarily a B1 run).
NEEDS = {
    "B1": [], "B4": [], "B5": ["B1"], "LI": [],
}


def selected_stages(cfg):
    """The config's task allow-list, or None to mean 'run all defaults'."""
    sel = cfg.get("stages")
    return set(sel) if sel else None
