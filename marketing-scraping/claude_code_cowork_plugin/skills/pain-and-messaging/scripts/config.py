"""
Config loader + empirical cost model for the pain & messaging tool.

PAIN_CONFIG.json (copy from PAIN_CONFIG.example.json) is the single file that
drives every stage. This module loads + validates it and exposes the empirical
Apify rates measured on live runs.

Stdlib only — no pip install needed.
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Empirical per-unit Apify cost (USD), measured on live runs.
# INCLUDING platform overhead. Treat as a STARTING estimate — your first test
# run recalibrates the rate for your own market/account (run_phase.py does this
# automatically, via apify_client.actual_charge_usd). Only the two stages this
# tool uses.
#
# Every stage in stages.STAGES needs a matching entry here, or C.estimate()
# raises KeyError, run_phase's budget gate swallows it, and the stage silently
# returns 0 records. Keep the two dicts in sync to avoid silent KeyErrors.
# ---------------------------------------------------------------------------
EMPIRICAL_RATES = {
    # stage -> (actor, unit, usd_per_unit, unverified?)
    "B2a": ("compass/crawler-google-places",       "place",  0.003,   False),
    "B2b": ("compass/google-maps-reviews-scraper", "review", 0.00045, False),
    # automation-lab/trustpilot: $0.005 fixed start fee per run is NOT modeled
    # here (negligible; run_phase recalibrates per-unit rate from the actual test
    # run). Keep in sync with STAGES["TP"] in stages.py.
    "TP":  ("automation-lab/trustpilot",           "review", 0.0005,  False),
    # thewolves/appstore-reviews-scraper + google-play-reviews-scraper:
    # $0.0001/review, no fixed start fee. Keep in sync with STAGES["AS"/"GP"].
    "AS":  ("thewolves/appstore-reviews-scraper",      "review", 0.0001, False),
    "GP":  ("thewolves/google-play-reviews-scraper",   "review", 0.0001, False),
    # harshmaur/reddit-scraper: $0.0018/result + $0.02/GB fixed start fee (start
    # fee NOT modeled here -- negligible; run_phase recalibrates per-unit from the
    # test run). Winner of the actor comparison on best-scored data for the money;
    # fatihtahta returned 0 items twice, trudax cost 1.9x more.
    # "result" = a post OR a comment (both billed the same). Keep in sync with
    # STAGES["RD"] in stages.py.
    "RD":  ("harshmaur/reddit-scraper",                "result", 0.0018, False),
    # B2B-SaaS review pair (actor comparison, tested on Slack G2+Capterra):
    #   G2 = factden/g2-reviews-scraper  $0.0035/review, no start fee. Chosen over
    #        the cheaper azzouzana/g2-scraper because factden takes a startUrls
    #        ARRAY (multi-competitor in one run, parity with TP/AS/GP) whereas
    #        azzouzana G2 takes a single productUrl -- which would silently drop
    #        competitors 2..N (silent empty corpus class of bug). factden also takes arrays and
    #        was 9x slower (66s vs 24.7s). factden's reviewText is null; the real
    #        content is in pros/cons/problemsSolved, which the adapter folds.
    #   CP = azzouzana/capterra-reviews-scraper  $0.0009/review, no start fee.
    #        Cheapest, fastest (4.7s), cleanest, and takes a profileUrls ARRAY.
    #        Other evaluated actors returned garbage pros fields or carried start fees.
    #        Keep in sync with STAGES.
    "G2":  ("factden/g2-reviews-scraper",              "review", 0.0035, False),
    "CP":  ("azzouzana/capterra-reviews-scraper",      "review", 0.0009, False),
}

# Result volumes ran high in the pilot; pad every forecast.
SAFETY_MARGIN = 1.2

# Spend ceiling: the hard cap passed to Apify as maxTotalChargeUsd -- and the
# threshold for our own poll-and-abort -- is forecast x this factor. The forecast
# already carries SAFETY_MARGIN (1.2); this rides on top, so the ceiling sits
# above a normal run's variance yet still catches a runaway (a forecast that is
# >1.5x too low is stopped). maxTotalChargeUsd is a real platform hard cap,
# verified live: a run stopped at $0.0596 under a $0.06 cap.
CHARGE_CEILING_FACTOR = 1.5

REQUIRED_KEYS = ["country_code"]

# ---------------------------------------------------------------------------
# Known review sources (v1.1). Validated in load_config so a typo fails loud
# (a misspelled source silently scrapes nothing -- a class of bug that must be
# caught at config load, not discovered after a paid run).
# ---------------------------------------------------------------------------
KNOWN_SOURCES = ["maps", "trustpilot", "appstore", "googleplay", "reddit", "g2", "capterra"]


def repo_default_config_path():
    """PAIN_CONFIG.json sitting next to the module's parent folder."""
    return Path(__file__).resolve().parent.parent / "PAIN_CONFIG.json"


def load_config(path=None):
    """Load + validate a PAIN_CONFIG.json."""
    p = Path(path) if path else repo_default_config_path()
    if not p.exists():
        raise SystemExit(
            f"No config at {p}. Copy PAIN_CONFIG.example.json to "
            f"PAIN_CONFIG.json and fill it in."
        )
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Config {p} is missing required keys: {', '.join(missing)}")

    # Defaults for optional keys the stages rely on.
    cfg.setdefault("language", "en")
    cfg.setdefault("vertical", "")
    cfg.setdefault("city", "")
    cfg.setdefault("country", "")
    # Subject-input spine (the keys are declared here so a config need not carry
    # all three modes). All converge on the
    # review_targets.json B2b consumes.
    cfg.setdefault("competitor_place_urls", [])   # mode 1: Maps place URLs, straight to B2b
    cfg.setdefault("competitor_names", [])        # mode 2: business names, resolved via B2a
    cfg.setdefault("maps_search_queries", [])     # mode 3: category+location discovery via B2a
    # B2a geo: locationQuery must be a SINGLE geocodable place or the whole run
    # fails "LOCATION NOT FOUND". "" omits it and lets the search strings carry
    # geo; absent falls back to "city, country".
    cfg.setdefault("maps_location_query", None)
    cfg.setdefault("review_target_count", 60)     # cap on businesses queued for B2b (mode 3)
    cfg.setdefault("max_reviews_per_place", 20)   # full-run cap per business
    # Pull review text + stars, NOT reviewer identities (privacy rule). Default off.
    cfg.setdefault("personal_data", False)
    # Model for the E1/E2 report-writing step. Blank => a capable default
    # (sonnet), NOT the cheapest: clustering + writing the analyst report is a
    # judgment task, unlike the trivial OCR transcription that uses haiku in the
    # companion tool. Set to a specific alias to override.
    cfg.setdefault("report_model", "")
    cfg.setdefault("budget_caps", {"phase1": 10.0})
    cfg.setdefault("stages", [])

    # Multi-source corpus machinery (v1.1).
    # Default is Maps-only for full v1 back-compatibility. An empty list is
    # treated as Maps-only (same as absent) so a config that sets "sources": []
    # by accident is not silently broken. FROZEN: default + empty->maps is the
    # invariant that makes parity proofs possible.
    cfg.setdefault("sources", ["maps"])
    if not cfg["sources"]:
        cfg["sources"] = ["maps"]
    _bad_sources = [s for s in cfg["sources"] if s not in KNOWN_SOURCES]
    if _bad_sources:
        raise SystemExit(
            f"Config {p} has unknown source(s): {', '.join(_bad_sources)}. "
            f"Known sources: {', '.join(KNOWN_SOURCES)}"
        )

    # Per-source subject keys. All default empty; later phases populate them.
    # Declared here so a config need not carry all keys (mirrors the pattern
    # for competitor_place_urls / competitor_names / maps_search_queries above).
    cfg.setdefault("trustpilot_domains",   [])
    cfg.setdefault("appstore_app_ids",     [])
    # appstore_names: plain app/company names a non-technical user can type instead
    # of hunting for a numeric App Store id. Resolved to a numeric id (+ validated)
    # for free via the iTunes Search API at the resolution gate — see
    # subjects.resolve_appstore. Supplied ids in appstore_app_ids are VALIDATED the
    # same way (iTunes lookup) so a wrong id surfaces instead of scraping nothing.
    cfg.setdefault("appstore_names",       [])
    cfg.setdefault("googleplay_app_ids",   [])
    # googleplay_names: plain app/company names, resolved to a package id (+ the
    # id validated) for free via the public Play store pages at the resolution
    # gate — see subjects.resolve_googleplay. Supplied googleplay_app_ids are
    # validated the same way so a wrong package surfaces instead of scraping nothing.
    cfg.setdefault("googleplay_names",     [])
    cfg.setdefault("reddit_queries",       [])
    # G2 / Capterra. Both are star-rated B2B-SaaS review sources kept
    # DISTINCT (source "g2" vs "capterra"), like appstore vs googleplay -- a user
    # may want per-platform signal. Subjects are product URLs/slugs (arrays, so
    # several competitors scrape in one run). Optional *_labels override the
    # businessName the adapter derives from each record (G2 productName / Capterra
    # slug) for the rare product whose name field is missing or ugly.
    cfg.setdefault("g2_urls",         [])   # G2 product URLs or bare slugs
    # g2_names: plain product names, resolved to a canonical G2 slug at the gate.
    # G2 hard-blocks a free GET, so resolution runs factden in its 'products'
    # discover mode — a small PAID run ($0.004/product) — hence it happens on the
    # real run, not dry-run. g2_discover_max_products caps candidates per name.
    cfg.setdefault("g2_names",        [])
    cfg.setdefault("g2_discover_max_products", 5)
    cfg.setdefault("capterra_urls",   [])   # Capterra product URLs (/p/<id>/<slug>)
    cfg.setdefault("g2_labels",       {})   # productSlug -> human businessName
    cfg.setdefault("capterra_labels", {})   # slug -> human businessName
    cfg.setdefault("saas_max_reviews", 100) # full-run cap per G2/Capterra product

    # App Store + Google Play shared settings.
    cfg.setdefault("app_labels",         {})   # appId -> human label for businessName
    cfg.setdefault("appstore_country",   "us") # lowercase per App Store API enum (FROZEN)
    cfg.setdefault("googleplay_country", "US") # UPPERCASE per Google Play API enum (FROZEN)
    cfg.setdefault("app_max_reviews",    100)  # full-run cap per app

    # Reddit settings. Reddit has no stars: banding.py synthesizes a
    # low/high signal, so these feed both the scrape and the banding pass.
    cfg.setdefault("reddit_subreddit",  "")    # limit a keyword search to ONE subreddit
    cfg.setdefault("reddit_subreddits", [])    # full-scrape these whole subreddits
    cfg.setdefault("reddit_urls",       [])    # direct post / subreddit / search URLs
    cfg.setdefault("reddit_labels",     {})    # subreddit -> human label for businessName
    cfg.setdefault("reddit_sort",       "top") # top of the window = highest-engagement
    cfg.setdefault("reddit_timeframe",  "year")
    cfg.setdefault("reddit_max_posts",  50)    # posts per input on a full run
    cfg.setdefault("reddit_max_comments_per_post", 20)  # comments carry the pain language
    # Banding synthetic star values (Component B). Must straddle the extractor's
    # split (low_star_threshold 3 / high_star_threshold 4): 2 is safely low, 5 high.
    cfg.setdefault("band_low_stars",  2)
    cfg.setdefault("band_high_stars", 5)
    # LLM banding batch + adaptive degradation guard (banding.py). If a wide batch
    # comes back with explicit labels for < band_min_match_rate of its items, it is
    # re-run split in half down to band_min_batch (a too-big batch that degraded
    # would otherwise silently default the missing items to neutral).
    cfg.setdefault("band_batch_size",     40)
    cfg.setdefault("band_min_match_rate", 0.8)
    cfg.setdefault("band_min_batch",      8)

    return cfg


def get_token():
    """
    Resolve the Apify token, in order:
      1. APIFY_TOKEN environment variable. Populate it however you like -- e.g.
         `export APIFY_TOKEN=...`, or `set -a; source scripts/.env; set +a` after
         filling scripts/.env. This module does NOT read scripts/.env itself;
         sourcing it is simply one way to put the value in the environment.
      2. ~/.apify/auth.json -- the file `apify login` writes and that
         persist_token() updates. If you've run `apify login`, or saved a token
         once via `python3 config.py --persist-token`, this just works with no
         re-entry on later runs.
    """
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if tok:
        try:
            persist_token(tok)  # save once so later runs in this env don't re-ask
        except Exception:  # noqa: BLE001 — persistence is a convenience, never fatal
            pass
        return tok

    auth_path = Path.home() / ".apify" / "auth.json"
    if auth_path.exists():
        try:
            tok = (json.loads(auth_path.read_text()).get("token") or "").strip()
        except (json.JSONDecodeError, OSError):
            tok = ""
        if tok:
            return tok

    raise SystemExit(
        "No Apify token found. Either run `apify login`, or copy scripts/.env.example "
        "to scripts/.env, fill APIFY_TOKEN, and `set -a; source scripts/.env; set +a`."
    )


def estimate(stage, expected_count, rate_override=None):
    """Return (usd_estimate, unit, rate_used, unverified)."""
    if stage not in EMPIRICAL_RATES:
        raise KeyError(f"Unknown stage {stage!r}. Known: {', '.join(EMPIRICAL_RATES)}")
    _actor, unit, rate, unverified = EMPIRICAL_RATES[stage]
    rate_used = rate_override if rate_override is not None else rate
    return expected_count * rate_used * SAFETY_MARGIN, unit, rate_used, unverified


def persist_token(token, auth_path=None):
    """Save an Apify token into ~/.apify/auth.json (mode 600), non-destructively.

    Reads any existing auth.json (the file `apify login` writes) and updates ONLY
    the `token` field, so other keys the CLI stores are preserved -- never a blind
    overwrite. get_token() reads this same file, so a token saved here is reused by
    every later run with no re-entry. Returns the Path written. Never logs the token.
    """
    token = (token or "").strip()
    if not token:
        raise SystemExit("persist_token: empty token -- nothing to save.")
    auth_path = Path(auth_path) if auth_path else Path.home() / ".apify" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text())
            if isinstance(existing, dict):
                data = existing
        except (json.JSONDecodeError, OSError):
            data = {}  # unreadable/corrupt: start clean rather than fail the save
    data["token"] = token
    auth_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        auth_path.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX modes
    return auth_path


def _mask_token(token):
    """A safe-to-print token fingerprint. NEVER print the token itself."""
    t = (token or "").strip()
    return f"****{t[-4:]}" if len(t) >= 4 else "****"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="config utilities (token persistence).")
    ap.add_argument(
        "--persist-token", action="store_true",
        help="Save $APIFY_TOKEN into ~/.apify/auth.json so later runs reuse it with "
             "no re-entry. Reads the token from the APIFY_TOKEN env var -- never the "
             "command line, so it cannot leak via the process list. (Normal runs also "
             "persist the token automatically on first use; this is the explicit form.)",
    )
    _args = ap.parse_args()
    if _args.persist_token:
        _tok = os.environ.get("APIFY_TOKEN", "").strip()
        if not _tok:
            raise SystemExit(
                "Set APIFY_TOKEN in the environment first "
                "(e.g. `export APIFY_TOKEN=...`), then re-run --persist-token."
            )
        _p = persist_token(_tok)
        print(f"Saved Apify token {_mask_token(_tok)} to {_p} (mode 600).")
        print(
            "Later runs in THIS environment now reuse it with no re-entry. In local "
            "Claude Code that is permanent; in a Cowork sandbox it lasts the "
            "conversation (a brand-new conversation asks once -- the sandbox is fresh)."
        )
    else:
        ap.print_help()
