"""
Config loader + empirical cost model for the pain & messaging tool.

PAIN_CONFIG.json (copy from PAIN_CONFIG.example.json) is the single file that
drives every stage. This module loads + validates it and exposes the empirical
Apify rates measured on live runs.

Stdlib only — no pip install needed.
"""

import json
import math
import os
import sys
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

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
#
# FLOOR RULE: every rate here is at
# least the highest price observed for it, on any Apify plan tier: the forecast
# is a spending gate and must never be low, so a user on a cheaper plan sees an
# estimate a little above their bill rather than one below it. Never lower a
# rate. Observed = a real run's pricingInfo, or the actor's published pricing
# (its FREE tier, the highest). A development test holds the observed table with its sources and
# fails if a rate drops below it.
# The per-review prices quoted in comments below are the paid runs' own; the
# rates are the FREE-tier prices where those are higher.
# ---------------------------------------------------------------------------
EMPIRICAL_RATES = {
    # stage -> (actor, unit, usd_per_unit, unverified?)
    # B2a: $0.003 per place on the paid runs; no FREE-tier price was recorded.
    "B2a": ("compass/crawler-google-places",       "place",  0.003,   False),
    # B2b: $0.00045 paid, $0.0006 on the FREE tier (was 0.00045).
    "B2b": ("compass/google-maps-reviews-scraper", "review", 0.0006,  False),
    # automation-lab/trustpilot: plus a $0.005 fixed start fee per run, priced
    # in START_EVENTS below. Keep in sync with STAGES["TP"] in stages.py.
    # $0.0005 paid, $0.000575 on the FREE tier (was 0.0005).
    "TP":  ("automation-lab/trustpilot",           "review", 0.000575, False),
    # thewolves/appstore-reviews-scraper + google-play-reviews-scraper:
    # $0.0001/review, no fixed start fee. Keep in sync with STAGES["AS"/"GP"].
    "AS":  ("thewolves/appstore-reviews-scraper",      "review", 0.0001, False),
    "GP":  ("thewolves/google-play-reviews-scraper",   "review", 0.0001, False),
    # harshmaur/reddit-scraper: $0.0018/result + $0.02/GB "init" start fee (priced
    # in START_EVENTS below). Winner of the actor comparison on best-scored data for the money;
    # fatihtahta returned 0 items twice, trudax cost 1.9x more.
    # "result" = a post OR a comment (both billed the same). Keep in sync with
    # STAGES["RD"] in stages.py.
    # $0.0018 paid, $0.002 on the FREE tier (was 0.0018).
    "RD":  ("harshmaur/reddit-scraper",                "result", 0.002,  False),
    # B2B-SaaS review pair (actor comparison, tested on Slack G2+Capterra):
    #   G2 = factden/g2-reviews-scraper  $0.0035/review + $0.005/GB
    #        apify-actor-start (the old "no start fee" was wrong: a paid run charged 2 of them).
    #        Chosen over
    #        the cheaper azzouzana/g2-scraper because factden takes a startUrls
    #        ARRAY (multi-competitor in one run, parity with TP/AS/GP) whereas
    #        azzouzana G2 takes a single productUrl -- which would silently drop
    #        competitors 2..N (silent empty corpus class of bug). factden also takes arrays and
    #        was 9x slower (66s vs 24.7s). factden's reviewText is null; the real
    #        content is in pros/cons/problemsSolved, which the adapter folds.
    #   CP = azzouzana/capterra-reviews-scraper  $0.0009/review + $0.005/GB
    #        apify-actor-start (the old "no start fee" was wrong: a paid run charged 1, $0.005 of its
    #        $0.041).
    #        Cheapest, fastest (4.7s), cleanest, and takes a profileUrls ARRAY.
    #        Other evaluated actors returned garbage pros fields or carried start fees.
    #        Keep in sync with STAGES.
    #   FREE-tier prices: G2 $0.004 (was 0.0035), Capterra $0.001 (was 0.0009).
    "G2":  ("factden/g2-reviews-scraper",              "review", 0.004,  False),
    "CP":  ("azzouzana/capterra-reviews-scraper",      "review", 0.001,  False),
}

# ---------------------------------------------------------------------------
# Fixed charges per actor RUN ("start" events), priced on top of the per-unit
# rates above. WHY: a paid validation run billed more
# than its forecast because these were left out: Trustpilot's $0.005 start,
# Reddit's $0.02-per-GB init, and the per-GB apify-actor-start of the Maps,
# G2, Capterra and keyword-expander actors. With them out, a small stage's own
# start fee alone could break its spend cap (forecast x CHARGE_CEILING_FACTOR:
# Trustpilot at 5 reviews or fewer, Capterra at 12 or fewer), so it stopped at once.
# Source: each actor's pricing on Apify and the ledgers of paid runs. A per-GB event is charged
# once per GB of the
# memory the run is given, minimum one; the tool requests run_phase's default
# memory (DEFAULT_MEMORY_MB) unless a stage sets less (STAGE_MEMORY_MB), so it
# is priced at that. Apify gave some actors less than requested (the ledgers
# show G2 2, Capterra 1, Reddit 2 events at 4096 MB), so these figures are an
# upper bound: a forecast is a spending gate and must not be low.
# stage -> (event name, USD per event, charged per GB of run memory?)
# ---------------------------------------------------------------------------
START_EVENTS = {
    # B2a (compass/crawler-google-places, Maps place discovery): two real runs, read back from Apify,
    # showed apify-actor-start $0.00005, 4 events at 4096 MB, on
    # top of place-scraped $0.003. Left out before, so B2a's forecast was low.
    "B2a": ("apify-actor-start", 0.00005, True),
    "B2b": ("apify-actor-start", 0.00005, True),
    "TP":  ("start",             0.005,   False),
    "RD":  ("init",              0.02,    True),   # "per GB of memory"
    "G2":  ("apify-actor-start", 0.005,   True),
    "CP":  ("apify-actor-start", 0.005,   True),
    # AS / GP (thewolves): no start charge.
}
STAGE_MEMORY_MB = {}

DEFAULT_MEMORY_MB = 4096  # run_phase._mem_for's default when a stage sets none


def start_fee_usd(stage, memory_mb=None):
    """USD one run of this stage's actor is charged for starting, before any
    record. 0.0 for an actor with no start charge."""
    ev = START_EVENTS.get(stage)
    if not ev:
        return 0.0
    _event, usd, per_gb = ev
    if not per_gb:
        return usd
    mem = memory_mb or STAGE_MEMORY_MB.get(stage, DEFAULT_MEMORY_MB)
    return usd * max(1, -(-int(mem) // 1024))

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
    # appstore_country / googleplay_country are NOT defaulted here: when absent
    # they follow country_code (subjects.store_country, lowercase for the App
    # Store, UPPERCASE for Google Play). A fixed "us"/"US" default here made a
    # country_code "gb" run silently scrape US reviews.
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

    _read_numbers(cfg, p, _NUMBER_SETTINGS)
    _read_budget_caps(cfg, p, 10.0)  # the default set above
    return cfg


# --- number settings ------------------------------------
# The same three functions in all three tools' config.py (test_client_drift).
# What the user wrote for a number setting that counts as "not set".
_BLANK = (None, "")


def _read_number(cfg, key, default, path, low=0, high=None, whole=True, why="", name=None):
    """Read one number setting the way the steps use it, before anything is spent.

    WHY (for meta_limit_per_source and now every number setting): a blank value crashed its
    step at run time ("invalid literal for
    int() with base 10: ''") and the run carried on without that step, so the user
    believed it had run. Blank or null keeps the default. A missing key is left
    missing: each step reads its own default, as before. A value that is not a
    usable number is refused here, in one plain sentence, before any step runs.
    A usable value is kept exactly as written, so every working config sends the
    same input as before: a whole number may be written as a number, as 25.0 or as
    digits in quotes (the steps turn it into a number themselves). A decimal
    setting written in quotes is stored as the number, because the code that
    reads it uses it as a number without converting it (and crashed on text).
    low/high: the smallest and largest value accepted. 0 stays accepted wherever
    the steps accepted it before: a new meaning is not this function's to give.
    why: an extra reason the sentence gives. name: how the sentence names the
    setting (default: the key)."""
    if key not in cfg:
        return
    v = cfg[key]
    if v in _BLANK or (isinstance(v, str) and not v.strip()):
        cfg[key] = default
        return
    n = None
    if isinstance(v, bool):
        n = None
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float) and math.isfinite(v):
        n = v if not whole else (int(v) if v.is_integer() else None)
    elif isinstance(v, str):
        s = v.strip()
        if whole:
            n = int(s) if s.isascii() and s.isdigit() else None
        else:
            try:
                n = float(s)
            except ValueError:
                n = None
            if n is not None and not math.isfinite(n):
                n = None
    if n is None or n < low or (high is not None and n > high):
        label = name or key
        kind = "a whole number" if whole else "a number"
        span = f"from {low} to {high}" if high is not None else f"of {low} or more"
        raise SystemExit(f"Config {path}: {label} is {v!r}. {label} must be {kind} {span}"
                         + (f"; {why}" if why else "")
                         + ". Nothing was run and nothing was spent.")
    if not whole and isinstance(v, str):
        cfg[key] = n


def _read_numbers(cfg, path, settings):
    """Read every number setting of this tool (settings: key -> (default, low,
    high, whole, why), the tool's own _NUMBER_SETTINGS) with _read_number."""
    for key, (default, low, high, whole, why) in settings.items():
        _read_number(cfg, key, default, path, low, high, whole, why)


def _read_budget_caps(cfg, path, default):
    """Read budget_caps, the spending cap in US dollars, before anything is spent.
    Blank keeps {"phase1": default}; a blank phase1 keeps the default; anything
    else that is not a dict, or a phase1 that is not a number of 0 or more, is
    refused in one plain sentence (it crashed the first paid step before)."""
    caps = cfg.get("budget_caps")
    if caps in _BLANK or (isinstance(caps, str) and not caps.strip()):
        cfg["budget_caps"] = {"phase1": default}
        return
    if not isinstance(caps, dict):
        raise SystemExit(f"Config {path}: budget_caps is {caps!r}. budget_caps must be "
                         f"written like {{\"phase1\": {default}}}, the most this run may "
                         f"spend in US dollars. Nothing was run and nothing was spent.")
    _read_number(caps, "phase1", default, path, whole=False, name="budget_caps phase1")


# Every number setting of this tool: key -> (default, low, high, whole, why).
# Each default is the one the step that reads the setting uses (stages.py,
# postprocess.py, extractor.py, banding.py, subjects.py); 0 stays accepted, as it
# was (a new meaning is not this check's to give).
_NUMBER_SETTINGS = {
    "review_target_count": (60, 0, None, True, ""),
    "max_reviews_per_place": (20, 0, None, True, ""),
    "trustpilot_max_reviews": (100, 0, None, True, ""),
    "app_max_reviews": (100, 0, None, True, ""),
    "reddit_max_posts": (50, 0, None, True, ""),
    "reddit_max_comments_per_post": (20, 0, None, True, ""),
    "saas_max_reviews": (100, 0, None, True, ""),
    "g2_discover_max_products": (5, 0, None, True, ""),
    "low_star_threshold": (3, 0, None, True, ""),
    "high_star_threshold": (4, 0, None, True, ""),
    "min_phrase_count": (2, 0, None, True, ""),
    "quote_cap": (400, 0, None, True, ""),
    "band_low_stars": (2, 0, None, True, ""),
    "band_high_stars": (5, 0, None, True, ""),
    "band_batch_size": (40, 0, None, True, ""),
    "band_min_match_rate": (0.8, 0, 1, False, ""),
    "band_min_batch": (8, 0, None, True, ""),
}

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
            # save once so later runs in this env don't re-ask; never over a
            # different saved login, which is then kept and named.
            if persist_token(tok)[1] == "kept":
                print(KEPT_LOGIN_LINE, file=sys.stderr)
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


def actor_runs(spec, actor_input):
    """How many actor runs run_phase._run_one launches for this stage's full
    input: one per batch of spec["batch"]["size"] when the batched array is
    longer than one batch (the same test _run_one makes), else one.

    WHY: every run pays its own start charge, so the
    forecast and the spend cap price one start fee per run. Since 82a40f7 the
    Google ad stage (B5) launches ceil(domains / B5_BATCH_SIZE) runs."""
    batch = (spec or {}).get("batch")
    arr = (actor_input or {}).get(batch["key"]) if batch else None
    if batch and isinstance(arr, list) and len(arr) > batch["size"]:
        return -(-len(arr) // batch["size"])
    return 1


def estimate(stage, expected_count, rate_override=None, runs=1):
    """Return (usd_estimate, unit, rate_used, unverified).

    runs: how many actor runs the work is spread over (actor_runs); each pays
    the stage's start charge once."""
    if stage not in EMPIRICAL_RATES:
        raise KeyError(f"Unknown step {stage!r}. Known: {', '.join(EMPIRICAL_RATES)}")
    _actor, unit, rate, unverified = EMPIRICAL_RATES[stage]
    rate_used = rate_override if rate_override is not None else rate
    # A run's start charge is exact, so it carries no safety margin; a stage
    # with nothing to do launches no run and pays none.
    start = start_fee_usd(stage) * max(1, int(runs)) if expected_count > 0 else 0.0
    return expected_count * rate_used * SAFETY_MARGIN + start, unit, rate_used, unverified


# What a run says when APIFY_TOKEN differs from the login
# already saved in ~/.apify/auth.json. Neither value is ever printed.
KEPT_LOGIN_LINE = ("A different Apify login is already saved here (~/.apify/auth.json), "
                   "so it was kept; the token set in APIFY_TOKEN is used for this run only.")


def _replace_auth_file(path, text):
    """Write `text` to the auth file at `path` whole or not at all: into a temporary file in the same
folder, then os.replace. Before,
    the file was written in place, so a run killed mid-write left the user's own
    Apify login file (which their `apify` command line uses too) half-written.
    WHY realpath: when auth.json is a link (a dotfile manager), the file it points
    to is replaced, as the in-place write did, never the link itself. The new file
    is mode 600 from the start, as the old code made it right after writing."""
    import tempfile
    real = os.path.realpath(str(path))
    fd, tmp = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=os.path.dirname(real))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def persist_token(token, auth_path=None):
    """Save an Apify token into ~/.apify/auth.json (mode 600), non-destructively.

    Reads any existing auth.json (the file `apify login` writes) and updates ONLY
    the `token` field, so other keys the CLI stores are preserved -- never a blind
    overwrite. get_token() reads this same file, so a token saved here is reused by
    every later run with no re-entry. Never logs the token.

    It never replaces a DIFFERENT saved token. Before, a run
    with another account's token in APIFY_TOKEN overwrote the saved login, so later
    runs and the user's own `apify` command line silently used the other account,
    against the decided design ("without disturbing an existing `apify login`",
    docs/SETUP.md). Returns (path, outcome):
      "saved" -- no token was saved (no file, an unreadable file, or no token key):
                 written, as before;
      "same"  -- this token is already saved: nothing is written;
      "kept"  -- a different token is saved: it is kept, nothing is written, and
                 the caller uses `token` for this run only.
    """
    token = (token or "").strip()
    if not token:
        raise SystemExit("persist_token: empty token, nothing to save.")
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
    saved = data.get("token")
    if isinstance(saved, str) and saved.strip():
        return auth_path, ("same" if saved.strip() == token else "kept")
    data["token"] = token
    _replace_auth_file(auth_path, json.dumps(data, indent=2))
    try:
        auth_path.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX modes
    return auth_path, "saved"


def _mask_token(token):
    """A safe-to-print token fingerprint. NEVER print the token itself."""
    t = (token or "").strip()
    return f"****{t[-4:]}" if len(t) >= 4 else "****"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="config utilities (token persistence).")
    ap.add_argument(
        "--persist-token", action="store_true",
        help="Save the Apify token into ~/.apify/auth.json so later runs reuse it with "
             "no re-entry. Reads the token from the APIFY_TOKEN env var -- never the "
             "command line, so it cannot leak via the process list. When APIFY_TOKEN is "
             "not set and a person is at a terminal, it asks for the token (hidden). "
             "(Normal runs also persist the token automatically on first use; this is "
             "the explicit form.)",
    )
    _args = ap.parse_args()
    if _args.persist_token:
        _tok = os.environ.get("APIFY_TOKEN", "").strip()
        if not _tok and sys.stdin.isatty():
            # Typing `export APIFY_TOKEN=...` into a Terminal writes the token to the shell's
            # history file in plain text, and a profile edit reaches only windows opened
            # after it (in a real buyer's run neither was explained). Asked for here, the
            # token is typed or pasted hidden, so it reaches no history and no process list,
            # and the window needs no setup. Only when a person is at a terminal: a script or
            # test with no APIFY_TOKEN and no terminal still gets the sentence further down.
            import getpass
            try:
                _tok = getpass.getpass("Paste your Apify API token (nothing is shown as you "
                                       "paste), then press Return: ").strip()
            except (EOFError, KeyboardInterrupt):
                _tok = ""
            if not _tok:
                raise SystemExit("No token was entered, so nothing was saved.")
        if not _tok:
            raise SystemExit(
                "Set APIFY_TOKEN in the environment first "
                "(not by typing it into Terminal, which keeps it in your shell history), "
                "or run this command in a Terminal window, where it asks for the token "
                "hidden and leaves no history. That is Route 1 in the plugin's SETUP.md, "
                "\"save it once with the plugin's own script\"."
            )
        # A person who pastes the whole `export APIFY_TOKEN=...` line (it is what the
        # older instructions had them type) means the value after the "=": take it,
        # then check its shape. In a real run a sentence typed here was saved and
        # reported as a success, and every later run failed to sign in. The same check
        # is made on a token that came from APIFY_TOKEN: in a real run a variable that
        # held a sentence was saved by this command and reported as "Saved Apify token
        # ****ence". (Only this explicit save is checked. Using the variable for a run is
        # a separate decision and is not changed here.)
        if _tok.startswith("export "):
            _tok = _tok[len("export "):].strip()
        if _tok.startswith("APIFY_TOKEN="):
            _tok = _tok[len("APIFY_TOKEN="):].strip()
        # A shell line ends in ";" after its closing quote, so the ";" comes off before
        # the quotes do. Only a matching pair is removed; a stray quote stays and fails
        # the shape check below.
        _tok = _tok.rstrip("; \t")
        if len(_tok) >= 2 and _tok[0] == _tok[-1] and _tok[0] in "\"'":
            _tok = _tok[1:-1]
        # Whatever is left must be exactly one token: the prefix once, then only ASCII
        # letters and digits. A token pasted twice, a trailing ";" or invisible
        # character, or the bare prefix would otherwise be saved and reported as a
        # success, and every later run would fail to sign in. Apify's docs give no
        # length or character set for a token, so the 20-character floor after the
        # prefix is a deliberately low guard against a truncated paste, not a documented
        # length; a real token is longer.
        _body = _tok[len("apify_api_"):]
        if not (_tok.startswith("apify_api_") and _tok.count("apify_api_") == 1
                and len(_body) >= 20 and _body.isascii() and _body.isalnum()):
            raise SystemExit(
                "That does not look like an Apify API token, so nothing was saved. Your "
                "token is on the Apify Console under Settings > API & Integrations and "
                "starts with apify_api_. Run the command again to try again.")
        _p, _outcome = persist_token(_tok)
        if _outcome == "kept":
            raise SystemExit(
                f"A different Apify login is already saved in {_p}, so it was kept and "
                "nothing was saved. A run with APIFY_TOKEN set uses that token for that "
                "run only. To make it the saved login instead, log in with it using "
                "`apify login`.")
        print(f"Saved Apify token {_mask_token(_tok)} to {_p} (mode 600)."
              if _outcome == "saved" else
              f"Apify token {_mask_token(_tok)} was already saved in {_p}.")
        print(
            "Later runs in THIS environment now reuse it with no re-entry. In local "
            "Claude Code that is permanent; in a Cowork sandbox it lasts the "
            "conversation (a brand-new conversation asks once -- the sandbox is fresh)."
        )
    else:
        ap.print_help()
