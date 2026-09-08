"""
Stage registry — the config-driven definition of every Apify stage the
pain & messaging tool runs.

Two stages, inherited from the Maps pipeline:
  B2a  compass/crawler-google-places        — find the businesses (places), by
                                               name (mode 2) or category+location
                                               (mode 3). No reviews here.
  B2b  compass/google-maps-reviews-scraper  — scrape reviews for the businesses
                                               queued in review_targets.json.

The two are a dependency chain (NEEDS["B2b"] = ["B2a"]): B2a's places are ranked
and capped into review_targets.json by postprocess._pp_b2a, which B2b then reads.
This is the "Maps two-stage dependency chain" — the one place the
pipeline can produce an empty corpus without an error, so B2a must run first and
its bridge must fire.

Each stage knows: which actor to call, how to BUILD its Apify input from the
config (+ earlier stages' output on disk), and how many records to EXPECT (for
the pre-run budget gate). run_phase.py consumes this registry. Nothing here
spends money — run_phase.py does, one stage at a time.
"""

import json
from pathlib import Path


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


# --- per-stage input builders -------------------------------------------------
# Each returns (actor_input_dict, expected_record_count). `test` truncates inputs.

def build_B2a(cfg, workdir, test):
    """Google Maps places. Search strings come from `maps_search_queries` (mode 3
    category+location), or fall back to '<vertical> <city>'. Mode-2 name
    resolution writes the business-name queries into the same key.
    `maxReviews: 0` here — this stage finds places; B2b scrapes their reviews.
    """
    q = cfg.get("maps_search_queries") or [f'{cfg.get("vertical","")} {cfg.get("city","")}'.strip()]
    q = [s for s in q if s]
    q = q[:2] if test else q
    per_search = 20 if test else 120   # CAP — do not remove
    # Mode-2 (named competitors) crawls one search per NAME but keeps only the
    # single best-match place per name (subjects.select_mode2_targets), so a full
    # 120-place crawl per name is wasted spend. Live named searches return at least
    # 20 places, and the correct listing sits in the top handful by Maps relevance,
    # so 25 is ample to disambiguate it and skip aggregators. Mode 3 (category
    # discovery) keeps the full 120 because it ranks and queues many places, not one.
    if not test and cfg.get("competitor_names"):
        per_search = 25
    inp = {
        "searchStringsArray": q,
        "language": cfg.get("language", "en"),
        "maxCrawledPlacesPerSearch": per_search,
        "maxReviews": 0, "maxImages": 0, "skipClosedPlaces": False,
    }
    # locationQuery must be a SINGLE geocodable place, or the actor
    # resolves it via Nominatim and FAILS the whole run ("LOCATION NOT FOUND").
    # Config semantics: None (absent) => default to "city, country"; "" (explicit
    # empty) => omit locationQuery entirely and let the search strings carry geo
    # ("plumber Chicago" already contains the city); a non-empty string => use it.
    loc = cfg.get("maps_location_query")
    if loc is None:
        loc = f'{cfg.get("city","")}, {cfg.get("country","")}'.strip().strip(",").strip()
    if loc:
        inp["locationQuery"] = loc
    return (inp, per_search * len(q))


def build_trustpilot(cfg, workdir, test):
    """Trustpilot reviews via automation-lab/trustpilot.

    Resolves company URLs from trustpilot_domains config (bare domains are
    normalized to full /review/ URLs by subjects.resolve_trustpilot_targets).
    Language is locked to cfg["language"] (default "en") -- this is the whole
    reason automation-lab won the bake-off over theagents/trustpilot-reviews,
    which returns mixed-locale reviews with no language field and poisons the
    English corpus.
    """
    # Local import avoids a module-level dependency; subjects is in the same dir.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_trustpilot_targets
    company_urls = resolve_trustpilot_targets(cfg, workdir)
    if test:
        company_urls = company_urls[:1]
    max_reviews = 5 if test else int(cfg.get("trustpilot_max_reviews", 100))
    inp = {
        "companyUrls":          company_urls,
        "maxReviewsPerCompany": max_reviews,
        "languages":            [cfg.get("language", "en")],
        "sort":                 "recency",
        "includeCompanyInfo":   True,
    }
    return (inp, max_reviews * len(company_urls))


def build_appstore(cfg, workdir, test):
    """App Store reviews via thewolves/appstore-reviews-scraper.

    Resolves app ids from appstore_app_ids config (bare numeric ids or App Store
    URLs are normalized to ids by subjects.resolve_appstore_targets).
    GOTCHA (FROZEN): country is lowercase 'us' -- App Store API enum.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_appstore_targets
    # Prefer the persisted, human-confirmed resolution (names→ids + validated ids)
    # written by pain_intel at the gate; falls back to fresh resolution if absent.
    app_ids = resolve_appstore_targets(cfg, workdir)
    if test:
        app_ids = app_ids[:1]
    max_items = 5 if test else int(cfg.get("app_max_reviews", 100))
    inp = {
        "appIds":   app_ids,
        "country":  cfg.get("appstore_country", "us"),   # FROZEN: lowercase (App Store enum)
        "maxItems": max_items,
    }
    return (inp, max_items * len(app_ids))


def build_googleplay(cfg, workdir, test):
    """Google Play reviews via thewolves/google-play-reviews-scraper.

    Resolves package names from googleplay_app_ids config (bare packages or Play
    URLs are normalized by subjects.resolve_googleplay_targets).
    GOTCHA (FROZEN): country is UPPERCASE 'US' -- Google Play API enum.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_googleplay_targets
    # Prefer the persisted, human-confirmed resolution (names→packages + validated
    # packages) written by pain_intel at the gate; pure regex fallback otherwise.
    app_ids = resolve_googleplay_targets(cfg, workdir)
    if test:
        app_ids = app_ids[:1]
    max_items = 5 if test else int(cfg.get("app_max_reviews", 100))
    inp = {
        "appIds":   app_ids,
        "country":  cfg.get("googleplay_country", "US"),  # FROZEN: UPPERCASE (Google Play enum)
        "language": cfg.get("language", "en"),
        "sort":     "NEWEST",
        "maxItems": max_items,
    }
    return (inp, max_items * len(app_ids))


def build_reddit(cfg, workdir, test):
    """Reddit posts + comments via harshmaur/reddit-scraper.

    Reddit has NO stars. postprocess._pp_reddit + banding.py synthesize the
    low/high signal AFTER the scrape (Component B), so this stage just collects a
    good post+comment corpus. Two frozen input choices:
      - aiAnalysis OFF: we band in our own code. An actor's black-box
        sentiment would put an uncounted judgment upstream of the extractor and
        break the "code counts, LLM only labels" spine.
      - crawlCommentsPerPost ON: comments are where the pain language lives; the
        titles/bodies of top posts alone are too thin.
    PII (author*/user*) is dropped by _pp_reddit -- this actor has no personalData
    toggle, so the strip happens at the adapter boundary (privacy rule).

    Targets (search terms / a single community / whole subreddits / direct URLs)
    come from subjects.resolve_reddit_targets. maxPostsCount is applied PER
    TARGET, not globally: the actor FAQ states "each term gets its own cap (for
    example, maxPostsCount: 10 means up to 10 posts per term)" -- the input-schema
    title's "across all search results" wording is a documentation defect. So
    expected items ~ (number of targets) * posts * (1 + comments); a single global
    posts*(1+comments) under-forecasts an N-target run N-fold, causing ~5x overspend
    on high-volume subreddits. withinCommunity is a MODIFIER that scopes searchTerms,
    NOT a separate target, so it is not counted.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_reddit_targets
    targets = resolve_reddit_targets(cfg)
    max_posts = 3 if test else int(cfg.get("reddit_max_posts", 50))
    max_comments = 3 if test else int(cfg.get("reddit_max_comments_per_post", 20))
    inp = {
        "searchSort":           cfg.get("reddit_sort", "top"),
        "searchTime":           cfg.get("reddit_timeframe", "year"),
        "maxPostsCount":        max_posts,
        "crawlCommentsPerPost": True,
        "maxCommentsPerPost":   max_comments,
        "aiAnalysis":           False,   # FROZEN: we run our own banding pass on the raw corpus
        "includeNSFW":          False,
    }
    inp.update(targets)  # searchTerms / withinCommunity / subredditUrls / startUrls
    # PER-TARGET forecast (see docstring): the cap fires once per target array
    # entry. withinCommunity scopes searchTerms and is not itself a target.
    n_targets = (len(targets.get("searchTerms") or [])
                 + len(targets.get("subredditUrls") or [])
                 + len(targets.get("startUrls") or []))
    return (inp, max(1, n_targets) * max_posts * (1 + max_comments))


def build_g2(cfg, workdir, test):
    """G2 reviews via factden/g2-reviews-scraper.

    Chosen over the cheaper azzouzana/g2-scraper because factden takes a
    startUrls ARRAY -- several competitors in one run, parity with TP/AS/GP.
    azzouzana G2 takes a single productUrl, which would silently drop competitors
    2..N (silent empty corpus class of bug). sortReviews is 'newest' (NOT 'rating_low') so the
    corpus carries BOTH low- and high-star reviews: the extractor's low/high
    split needs both bands to compare pain vs praise; sorting by lowest rating
    would starve the praise bucket. mode 'reviews' extracts reviews (not the
    'products' discovery mode). factden's reviewText is null -- the review content
    lives in pros/cons/problemsSolved, folded by postprocess._pp_g2capterra.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_g2_targets
    # Prefer the persisted resolution (names→slugs from the gate's discover run);
    # pure format-validation fallback for supplied urls/slugs.
    urls = resolve_g2_targets(cfg, workdir)
    if test:
        urls = urls[:1]
    max_reviews = 5 if test else int(cfg.get("saas_max_reviews", 100))
    inp = {
        "mode":                 "reviews",
        "startUrls":            urls,
        "maxReviewsPerProduct": max_reviews,
        "sortReviews":          "newest",
    }
    return (inp, max_reviews * len(urls))


def build_capterra(cfg, workdir, test):
    """Capterra reviews via azzouzana/capterra-reviews-scraper.

    Cheapest ($0.0009/review, no start fee), fastest (4.7s), and cleanest of the
    Capterra bake-off -- and takes a profileUrls ARRAY (multi-competitor).
    Other evaluated actors returned garbage pros fields or carried start fees. sortOrder MOST_RECENT gives the natural low/high star mix (like
    build_g2's 'newest'); HIGHEST_COMPLETENESS_SCORE would bias toward long
    detailed reviews rather than a representative star spread.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from subjects import resolve_capterra_targets
    urls = resolve_capterra_targets(cfg, workdir)
    if test:
        urls = urls[:1]
    # azzouzana/capterra-reviews-scraper REJECTS maxReviewsPerProfile < 10 with a
    # 400 (its own input validation, not documented in the schema). The test run
    # therefore floors at 10, not the usual 5 -- caught live; factden G2 has no
    # such floor, so build_g2 keeps 5.
    max_reviews = 10 if test else int(cfg.get("saas_max_reviews", 100))
    inp = {
        "profileUrls":          urls,
        "maxReviewsPerProfile": max_reviews,
        "sortOrder":            "MOST_RECENT",
    }
    return (inp, max_reviews * len(urls))


def build_B2b(cfg, workdir, test):
    """Reviews for the businesses queued in review_targets.json (written by
    postprocess._pp_b2a after B2a). `personalData` defaults False — the analysis
    needs review text + stars + which business, not reviewer identities.
    """
    urls = _read_json(Path(workdir) / "B2a_maps_places" / "review_targets.json", []) or []
    urls = urls[:3] if test else urls
    max_reviews = 5 if test else int(cfg.get("max_reviews_per_place", 20))
    return ({"startUrls": urls, "maxReviews": max_reviews, "reviewsSort": "newest",
             "language": cfg.get("language", "en"),
             "personalData": bool(cfg.get("personal_data", False))},
            len(urls) * max_reviews)


# --- registry -----------------------------------------------------------------

STAGES = {
    "B2a": {"actor": "compass/crawler-google-places",       "phase": "1", "folder": "B2a_maps_places",   "build": build_B2a,        "output": "dataset"},
    "B2b": {"actor": "compass/google-maps-reviews-scraper", "phase": "1", "folder": "B2b_reviews",       "build": build_B2b,        "output": "dataset"},
    # FROZEN: folder MUST be "trustpilot_reviews" -- extractor.load_source_corpus
    # reads <workdir>/trustpilot_reviews/run_02_full.json for source="trustpilot".
    # A different folder silently yields an empty corpus (silent empty corpus class of bug).
    "TP":  {"actor": "automation-lab/trustpilot",           "phase": "1", "folder": "trustpilot_reviews", "build": build_trustpilot, "output": "dataset"},
    # FROZEN: folders MUST be "appstore_reviews" and "googleplay_reviews" --
    # extractor.load_source_corpus reads <workdir>/<source>_reviews/run_02_full.json.
    "AS":  {"actor": "thewolves/appstore-reviews-scraper",    "phase": "1", "folder": "appstore_reviews",   "build": build_appstore,   "output": "dataset"},
    "GP":  {"actor": "thewolves/google-play-reviews-scraper", "phase": "1", "folder": "googleplay_reviews", "build": build_googleplay, "output": "dataset"},
    # FROZEN: folder MUST be "reddit_reviews" -- load_source_corpus reads
    # <workdir>/reddit_reviews/run_02_full.json for source="reddit". _pp_reddit
    # writes the BANDED corpus there (and reddit_neutral.json alongside).
    "RD":  {"actor": "harshmaur/reddit-scraper",             "phase": "1", "folder": "reddit_reviews",     "build": build_reddit,     "output": "dataset"},
    # FROZEN: folders MUST be "g2_reviews" and "capterra_reviews" --
    # extractor.load_source_corpus reads <workdir>/<source>_reviews/run_02_full.json
    # for source="g2"/"capterra". A different folder silently yields an empty
    # corpus (silent empty corpus class of bug). G2 and Capterra are STAR-RATED, so no banding --
    # postprocess._pp_g2capterra maps the star straight onto the extractor's split.
    "G2":  {"actor": "factden/g2-reviews-scraper",         "phase": "1", "folder": "g2_reviews",       "build": build_g2,       "output": "dataset"},
    "CP":  {"actor": "azzouzana/capterra-reviews-scraper", "phase": "1", "folder": "capterra_reviews", "build": build_capterra, "output": "dataset"},
}

# B2a must precede B2b — it builds review_targets.json (via postprocess._pp_b2a),
# which B2b reads. run_phase runs the phase list in order.
# NOTE: "TP", "AS", "GP" are deliberately NOT in PHASE_ORDER. They are opt-in
# per source; pain_intel.py invokes them via --only <stage>. Adding them here
# would make a plain `run_phase 1` run them unconditionally, regardless of
# cfg["sources"].
PHASE_ORDER = {
    "1": ["B2a", "B2b"],
}

QUESTION = {
    "B2a": "Which businesses match, and how many reviews does each have?",
    "B2b": "What do customers actually say in reviews? (the corpus for the pain/trust extractor)",
    "TP":  "What do Trustpilot reviewers say about this company? (second source corpus)",
    "AS":  "What do App Store reviewers say about this app? (third source corpus)",
    "GP":  "What do Google Play reviewers say about this app? (fourth source corpus)",
    "RD":  "What do Reddit posters say about this brand/topic? (no-star corpus, banded low/high)",
    "G2":  "What do G2 reviewers say about this B2B software? (SaaS source corpus)",
    "CP":  "What do Capterra reviewers say about this B2B software? (SaaS source corpus)",
}

# Hard input dependencies -- skipping B2a means B2b has no review_targets.json.
# TP, AS, GP, RD are self-contained: they read targets from config.
NEEDS = {
    "B2a": [], "B2b": ["B2a"], "TP": [], "AS": [], "GP": [], "RD": [],
    "G2": [], "CP": [],
}


def selected_stages(cfg):
    """The config's task allow-list, or None to mean 'run all defaults'."""
    sel = cfg.get("stages")
    return set(sel) if sel else None


# ---------------------------------------------------------------------------
# Inline verification tests (run with: python3 stages.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}")
            print(f"        got:      {got!r}")
            print(f"        expected: {expected!r}")
            FAIL += 1

    # ==========================================================================
    # TEST 1 — build_trustpilot: test mode (1 URL, max 5 reviews)
    # ==========================================================================
    print("\n=== TEST 1: build_trustpilot test mode ===")
    cfg_tp = {
        "trustpilot_domains": ["vans.com", "nike.com"],
        "language": "en",
        "trustpilot_max_reviews": 50,
    }
    inp_test, exp_test = build_trustpilot(cfg_tp, "/tmp/workdir", test=True)
    check("test: companyUrls has 1 entry (capped)",
          len(inp_test["companyUrls"]), 1)
    check("test: companyUrls[0] is normalized Trustpilot URL",
          inp_test["companyUrls"][0], "https://www.trustpilot.com/review/vans.com")
    check("test: maxReviewsPerCompany is 5",
          inp_test["maxReviewsPerCompany"], 5)
    check("test: languages is ['en']",
          inp_test["languages"], ["en"])
    check("test: sort is recency",
          inp_test["sort"], "recency")
    check("test: includeCompanyInfo is True",
          inp_test["includeCompanyInfo"], True)
    check("test: expected_count = 5 * 1",
          exp_test, 5)

    # ==========================================================================
    # TEST 2 — build_trustpilot: full mode (all URLs, max from config)
    # ==========================================================================
    print("\n=== TEST 2: build_trustpilot full mode ===")
    inp_full, exp_full = build_trustpilot(cfg_tp, "/tmp/workdir", test=False)
    check("full: companyUrls has 2 entries",
          len(inp_full["companyUrls"]), 2)
    check("full: first URL is vans.com",
          inp_full["companyUrls"][0], "https://www.trustpilot.com/review/vans.com")
    check("full: second URL is nike.com",
          inp_full["companyUrls"][1], "https://www.trustpilot.com/review/nike.com")
    check("full: maxReviewsPerCompany from config (50)",
          inp_full["maxReviewsPerCompany"], 50)
    check("full: expected_count = 50 * 2",
          exp_full, 100)

    # Default max reviews (100) when key not in config
    cfg_tp_default = {"trustpilot_domains": ["vans.com"], "language": "en"}
    inp_d, exp_d = build_trustpilot(cfg_tp_default, "/tmp/workdir", test=False)
    check("full: default maxReviewsPerCompany is 100",
          inp_d["maxReviewsPerCompany"], 100)
    check("full: default expected_count = 100 * 1",
          exp_d, 100)

    # ==========================================================================
    # TEST 3 — registry completeness (TP + AS + GP)
    # ==========================================================================
    print("\n=== TEST 3: registry completeness ===")
    check("TP in STAGES", "TP" in STAGES, True)
    check("TP actor", STAGES["TP"]["actor"], "automation-lab/trustpilot")
    check("TP folder (FROZEN: must be trustpilot_reviews)",
          STAGES["TP"]["folder"], "trustpilot_reviews")
    check("TP output", STAGES["TP"]["output"], "dataset")
    check("TP in QUESTION", "TP" in QUESTION, True)
    check("TP in NEEDS", "TP" in NEEDS, True)
    check("NEEDS['TP'] is empty (self-contained)", NEEDS["TP"], [])
    check("TP NOT in PHASE_ORDER['1'] (opt-in only)",
          "TP" not in PHASE_ORDER.get("1", []), True)

    check("AS in STAGES", "AS" in STAGES, True)
    check("AS actor", STAGES["AS"]["actor"], "thewolves/appstore-reviews-scraper")
    check("AS folder (FROZEN: must be appstore_reviews)",
          STAGES["AS"]["folder"], "appstore_reviews")
    check("AS output", STAGES["AS"]["output"], "dataset")
    check("AS in QUESTION", "AS" in QUESTION, True)
    check("AS in NEEDS", "AS" in NEEDS, True)
    check("NEEDS['AS'] is empty (self-contained)", NEEDS["AS"], [])
    check("AS NOT in PHASE_ORDER['1'] (opt-in only)",
          "AS" not in PHASE_ORDER.get("1", []), True)

    check("GP in STAGES", "GP" in STAGES, True)
    check("GP actor", STAGES["GP"]["actor"], "thewolves/google-play-reviews-scraper")
    check("GP folder (FROZEN: must be googleplay_reviews)",
          STAGES["GP"]["folder"], "googleplay_reviews")
    check("GP output", STAGES["GP"]["output"], "dataset")
    check("GP in QUESTION", "GP" in QUESTION, True)
    check("GP in NEEDS", "GP" in NEEDS, True)
    check("NEEDS['GP'] is empty (self-contained)", NEEDS["GP"], [])
    check("GP NOT in PHASE_ORDER['1'] (opt-in only)",
          "GP" not in PHASE_ORDER.get("1", []), True)

    check("RD in STAGES", "RD" in STAGES, True)
    check("RD actor", STAGES["RD"]["actor"], "harshmaur/reddit-scraper")
    check("RD folder (FROZEN: must be reddit_reviews)",
          STAGES["RD"]["folder"], "reddit_reviews")
    check("RD output", STAGES["RD"]["output"], "dataset")
    check("RD in QUESTION", "RD" in QUESTION, True)
    check("RD in NEEDS", "RD" in NEEDS, True)
    check("NEEDS['RD'] is empty (self-contained)", NEEDS["RD"], [])
    check("RD NOT in PHASE_ORDER['1'] (opt-in only)",
          "RD" not in PHASE_ORDER.get("1", []), True)

    # ==========================================================================
    # TEST 3b — build_reddit: targets, frozen knobs, test vs full mode
    # ==========================================================================
    print("\n=== TEST 3b: build_reddit ===")
    rd_cfg = {"reddit_queries": ["Spotify"], "reddit_subreddit": "spotify",
              "reddit_labels": {"spotify": "Spotify"}}
    inp_rd_t, exp_rd_t = build_reddit(rd_cfg, "/tmp/x", test=True)
    check("RD test: searchTerms from reddit_queries", inp_rd_t["searchTerms"], ["Spotify"])
    check("RD test: withinCommunity from reddit_subreddit", inp_rd_t["withinCommunity"], "spotify")
    check("RD test: aiAnalysis OFF (FROZEN)", inp_rd_t["aiAnalysis"], False)
    check("RD test: comments ON", inp_rd_t["crawlCommentsPerPost"], True)
    check("RD test: small post cap", inp_rd_t["maxPostsCount"], 3)
    check("RD test: expected count = posts*(1+comments)", exp_rd_t, 3 * (1 + 3))
    inp_rd_f, exp_rd_f = build_reddit(rd_cfg, "/tmp/x", test=False)
    check("RD full: default post cap 50", inp_rd_f["maxPostsCount"], 50)
    check("RD full: default comments 20", inp_rd_f["maxCommentsPerPost"], 20)
    check("RD full: expected count 50*(1+20)", exp_rd_f, 50 * (1 + 20))
    # URL + subreddit-scrape targets
    inp_rd_u, _ = build_reddit(
        {"reddit_urls": ["https://www.reddit.com/r/example-topic/comments/x/y/"],
         "reddit_subreddits": ["spotify"]}, "/tmp/x", test=True)
    check("RD: startUrls wrapped as {url}",
          inp_rd_u["startUrls"], [{"url": "https://www.reddit.com/r/example-topic/comments/x/y/"}])
    check("RD: subredditUrls passthrough", inp_rd_u["subredditUrls"], ["spotify"])

    # ==========================================================================
    # TEST 3c — build_reddit PER-TARGET forecast (regression guard for the
    # ~5x overspend on high-volume subreddits). maxPostsCount fires PER target, so the
    # forecast must multiply by the target count. The single-target cases above
    # (n_targets=1) can't catch a global-vs-per-target error -- these can.
    # ==========================================================================
    print("\n=== TEST 3c: build_reddit per-target forecast ===")
    _, exp_multi = build_reddit({"reddit_queries": ["CRM", "helpdesk", "sales"]},
                                "/tmp/x", test=False)
    check("RD 3 searchTerms: forecast = 3 * 50*(1+20)", exp_multi, 3 * 50 * (1 + 20))
    # withinCommunity is a MODIFIER scoping searchTerms, NOT a separate target:
    # one term plus a community scope is still ONE target.
    _, exp_within = build_reddit({"reddit_queries": ["CRM"], "reddit_subreddit": "sales"},
                                 "/tmp/x", test=False)
    check("RD withinCommunity not counted: 1 term = 1 target", exp_within, 1 * 50 * (1 + 20))
    # Mixed target arrays all add: 2 searchTerms + 2 subredditUrls + 1 startUrl = 5.
    _, exp_mixed = build_reddit(
        {"reddit_queries": ["CRM", "sales"],
         "reddit_subreddits": ["crm", "salesforce"],
         "reddit_urls": ["https://www.reddit.com/r/crm/comments/a/b/"]},
        "/tmp/x", test=False)
    check("RD mixed targets: forecast = 5 * 50*(1+20)", exp_mixed, 5 * 50 * (1 + 20))

    # ==========================================================================
    # TEST 4 — build_appstore: test mode and full mode
    # ==========================================================================
    print("\n=== TEST 4: build_appstore ===")
    cfg_as = {
        "appstore_app_ids": ["324684580", "389801252"],
        "appstore_country": "us",
        "app_max_reviews":  50,
        "language":         "en",
    }
    inp_as_test, exp_as_test = build_appstore(cfg_as, "/tmp/workdir", test=True)
    check("AS test: appIds capped to 1", len(inp_as_test["appIds"]), 1)
    check("AS test: appIds[0]", inp_as_test["appIds"][0], "324684580")
    check("AS test: maxItems is 5", inp_as_test["maxItems"], 5)
    check("AS test: country is lowercase 'us' (FROZEN)", inp_as_test["country"], "us")
    check("AS test: no language key (App Store takes no language param)",
          "language" not in inp_as_test, True)
    check("AS test: expected_count = 5 * 1", exp_as_test, 5)

    inp_as_full, exp_as_full = build_appstore(cfg_as, "/tmp/workdir", test=False)
    check("AS full: appIds has 2 entries", len(inp_as_full["appIds"]), 2)
    check("AS full: maxItems from config (50)", inp_as_full["maxItems"], 50)
    check("AS full: expected_count = 50 * 2", exp_as_full, 100)

    # Default max (100) when key absent
    cfg_as_def = {"appstore_app_ids": ["324684580"]}
    _, exp_as_def = build_appstore(cfg_as_def, "/tmp/workdir", test=False)
    check("AS full: default expected_count = 100 * 1", exp_as_def, 100)

    # ==========================================================================
    # TEST 5 — build_googleplay: test mode, full mode, country + language
    # ==========================================================================
    print("\n=== TEST 5: build_googleplay ===")
    cfg_gp = {
        "googleplay_app_ids": ["com.spotify.music", "com.example.app"],
        "googleplay_country": "US",
        "app_max_reviews":    50,
        "language":           "en",
    }
    inp_gp_test, exp_gp_test = build_googleplay(cfg_gp, "/tmp/workdir", test=True)
    check("GP test: appIds capped to 1", len(inp_gp_test["appIds"]), 1)
    check("GP test: appIds[0]", inp_gp_test["appIds"][0], "com.spotify.music")
    check("GP test: maxItems is 5", inp_gp_test["maxItems"], 5)
    check("GP test: country is UPPERCASE 'US' (FROZEN)", inp_gp_test["country"], "US")
    check("GP test: language present", inp_gp_test["language"], "en")
    check("GP test: sort is NEWEST", inp_gp_test["sort"], "NEWEST")
    check("GP test: expected_count = 5 * 1", exp_gp_test, 5)

    inp_gp_full, exp_gp_full = build_googleplay(cfg_gp, "/tmp/workdir", test=False)
    check("GP full: appIds has 2 entries", len(inp_gp_full["appIds"]), 2)
    check("GP full: maxItems from config (50)", inp_gp_full["maxItems"], 50)
    check("GP full: expected_count = 50 * 2", exp_gp_full, 100)

    # Country case distinction: App Store 'us' vs Google Play 'US' are distinct keys
    check("country case: AS uses 'us', GP uses 'US'",
          inp_as_full["country"] != inp_gp_full["country"], True)

    # ==========================================================================
    # TEST 6 — build_g2 + build_capterra (Phase E)
    # ==========================================================================
    print("\n=== TEST 6: build_g2 + build_capterra ===")
    cfg_g2 = {
        "g2_urls": ["https://www.g2.com/products/slack/reviews", "notion"],
        "saas_max_reviews": 60,
    }
    inp_g2_t, exp_g2_t = build_g2(cfg_g2, "/tmp/x", test=True)
    check("G2 test: startUrls capped to 1", len(inp_g2_t["startUrls"]), 1)
    check("G2 test: mode reviews", inp_g2_t["mode"], "reviews")
    check("G2 test: maxReviewsPerProduct 5", inp_g2_t["maxReviewsPerProduct"], 5)
    check("G2 test: sortReviews newest (both bands, not rating_low)",
          inp_g2_t["sortReviews"], "newest")
    check("G2 test: expected 5*1", exp_g2_t, 5)
    inp_g2_f, exp_g2_f = build_g2(cfg_g2, "/tmp/x", test=False)
    check("G2 full: both urls kept", len(inp_g2_f["startUrls"]), 2)
    check("G2 full: maxReviewsPerProduct from cfg (60)", inp_g2_f["maxReviewsPerProduct"], 60)
    check("G2 full: expected 60*2", exp_g2_f, 120)
    # default cap
    _, exp_g2_def = build_g2({"g2_urls": ["slack"]}, "/tmp/x", test=False)
    check("G2 full: default expected 100*1", exp_g2_def, 100)

    cfg_cp = {
        "capterra_urls": ["https://www.capterra.com/p/135003/Slack/",
                          "https://www.capterra.com/p/188405/Notion/"],
        "saas_max_reviews": 60,
    }
    inp_cp_t, exp_cp_t = build_capterra(cfg_cp, "/tmp/x", test=True)
    check("CP test: profileUrls capped to 1", len(inp_cp_t["profileUrls"]), 1)
    # Floored at 10 (NOT 5): azzouzana rejects maxReviewsPerProfile < 10.
    check("CP test: maxReviewsPerProfile 10 (actor floor)", inp_cp_t["maxReviewsPerProfile"], 10)
    check("CP test: sortOrder MOST_RECENT", inp_cp_t["sortOrder"], "MOST_RECENT")
    check("CP test: expected 10*1", exp_cp_t, 10)
    inp_cp_f, exp_cp_f = build_capterra(cfg_cp, "/tmp/x", test=False)
    check("CP full: both urls kept", len(inp_cp_f["profileUrls"]), 2)
    check("CP full: maxReviewsPerProfile from cfg (60)", inp_cp_f["maxReviewsPerProfile"], 60)
    check("CP full: expected 60*2", exp_cp_f, 120)

    # Registry completeness for G2 + CP
    for st, actor, folder in (
        ("G2", "factden/g2-reviews-scraper", "g2_reviews"),
        ("CP", "azzouzana/capterra-reviews-scraper", "capterra_reviews"),
    ):
        check(f"{st} in STAGES", st in STAGES, True)
        check(f"{st} actor", STAGES[st]["actor"], actor)
        check(f"{st} folder (FROZEN)", STAGES[st]["folder"], folder)
        check(f"{st} in QUESTION", st in QUESTION, True)
        check(f"NEEDS['{st}'] empty (self-contained)", NEEDS[st], [])
        check(f"{st} NOT in PHASE_ORDER['1'] (opt-in only)",
              st not in PHASE_ORDER.get("1", []), True)

    # ==========================================================================
    # Summary
    # ==========================================================================
    print(f"\n{'='*50}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
