"""
Config loader + empirical cost model for the ad-intelligence tool.

AD_INTEL_CONFIG.json (copy from AD_INTEL_CONFIG.example.json) is the single file
that drives every stage. This module loads + validates it and exposes the
empirical Apify rates measured on initial pilot runs.

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
# Empirical per-unit Apify cost (USD), measured on initial pilot runs,
# INCLUDING platform overhead. Treat as a STARTING estimate — your first test
# run recalibrates the rate for your own market/account (run_phase.py does this
# automatically).
# ---------------------------------------------------------------------------
#
# FLOOR RULE: every rate here is at
# least the highest price observed for it, on any Apify plan tier: the forecast
# is a spending gate and must never be low, so a user on a cheaper plan sees an
# estimate a little above their bill rather than one below it. Never lower a
# rate. Observed = a real run's pricingInfo, or the actor's published pricing
# (its FREE tier, the highest). A development test holds the observed table with its sources and
# fails if a rate drops below it.
EMPIRICAL_RATES = {
    # stage -> (actor, unit, usd_per_unit, unverified?)
    # B1: one results page per query (maxPagesPerQuery 1); highest observed page
    # price $0.0045 (FREE tier), so 0.0076 stays (above every observed price).
    "B1": ("apify/google-search-scraper",                "query", 0.0076,  False),
    # B4: $0.00075 per ad, flat on every plan (was 0.0007, below it).
    "B4": ("curious_coder/facebook-ads-library-scraper", "ad",    0.00075, False),
    # B5 STARTING estimate is ALL-IN. This actor uses a combined pricing model:
    # per-ad event $0.00124 PLUS platform compute/proxy passed through to the
    # user (isPPEPlatformUsagePaidByUser). The verified residential run billed
    # ~$0.076 for 25 ads ≈ $0.003/ad; a larger run amortizes the per-run compute
    # floor toward ~$0.002/ad. 0.0025 is a realistic pre-test seed — run_phase
    # then recalibrates from the actual test charge (via actual_charge_usd).
    # Highest observed per-ad price is $0.00148 (FREE tier); the most a real
    # run paid all-in was $0.00171/ad, so 0.0025 stays.
    "B5": ("lexis-solutions/google-ads-scraper",         "ad",    0.0025,  False),
    # LI: scrapesage/linkedin-ad-library-scraper, scoped by NUMERIC company id
    # (companyIds) — the only precise advertiser scoping (keyword/slug are fuzzy
    # and return other advertisers; earlier actors using name search returned 0 results). Rate is a pay-per-result seed marked UNVERIFIED; run_phase
    # recalibrates from the actual test charge. Every stage in STAGES needs a rate
    # here or C.estimate raises KeyError and the stage silently returns 0.
    # Per ad the tool is charged "ad" + "adDetails" (build_LI asks for the ad
    # details). The actor's published pricing is, on the highest (free) tier, ad $0.002 + adDetails
    # $0.012 = $0.014; a paid run was priced ad $0.0018 + adDetails $0.011 = $0.0128. The old
    # $0.003 was below either, so the forecast and its spend cap were ~4x low.
    "LI": ("scrapesage/linkedin-ad-library-scraper",     "ad",    0.014,   True),
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
    "B1": ("actor-start",       0.001,   False),  # "Flat fee for starting an Actor run"
    "B4": ("apify-actor-start", 0.00005, True),
    # B5: priced once per actor RUN (estimate's `runs`, from actor_runs): each
    # batch of B5_BATCH_SIZE domains is its own run with its own start charge.
    # Real ledgers: 4 events of $0.00005 per run at 4096 MB.
    "B5": ("apify-actor-start", 0.00005, True),
    # LI (scrapesage): no start charge.
}
STAGE_MEMORY_MB = {"B4": 1024}  # stages.STAGES["B4"]["memory_mb"] (mem_per_url caps it lower)

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

# Every officially assigned ISO 3166-1 alpha-2 code: what the ad libraries take
# (Meta `country=` / `scrapePageAds.countryCode`, Google `region=` and the proxy
# country, LinkedIn `countries`, the lookup search's `countryCode`).
_ISO_3166_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM
BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX
CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG
GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR
IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE
NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF
TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF
WS YE YT ZA ZM ZW
""".split())

# The settings that hold country codes. Each reaches an actor input.
_COUNTRY_KEYS = ("country_code", "country_codes", "linkedin_countries")


def _read_countries(cfg, path):
    """Read every country code in the settings the way the ad libraries need it.

    WHY: users write "uk" for the United Kingdom, but ISO 3166 and the
    ad libraries call it "gb", so "uk" reached Meta, Google and LinkedIn as a
    country none of them knows. "uk" (any case) is read as "gb", in the case it
    was written, and the run says so once (ad_intel). Any other code that is not
    a country is refused here, before anything is spent, never silently dropped.
    A code that is already a country is kept exactly as written, so the paid
    calls of every existing config are unchanged. An empty entry stays absent,
    as the stage builders already read it."""
    read_as_gb = []

    def one(v, key):
        if not isinstance(v, str):
            raise SystemExit(f"Config {path}: {key} holds {v!r}, which is not a country "
                             f"code. Use the two-letter code, for example 'gb' for the "
                             f"United Kingdom or 'us' for the United States.")
        s = v.strip().upper()
        if s == "UK":
            read_as_gb.append(v)
            return "GB" if v.strip().isupper() else "gb"
        if s not in _ISO_3166_ALPHA2:
            raise SystemExit(f"Config {path}: '{v}' in {key} is not a country code this "
                             f"tool knows, so nothing was run and nothing was spent. Use "
                             f"the two-letter code, for example 'gb' for the United "
                             f"Kingdom, 'us' for the United States or 'de' for Germany.")
        return v

    twice = False
    for key in _COUNTRY_KEYS:
        val = cfg.get(key)
        if isinstance(val, list):
            mapped = [one(v, key) if v else v for v in val]
            # ["uk", "gb"] is one country written twice, and
            # LinkedIn was sent "gb" twice. A country listed more than once is kept
            # once (its first spelling). Only a list that has a repeat changes, so
            # the paid calls of every config without one stay exactly as they were.
            seen, kept = set(), []
            for v in mapped:
                k = v.strip().upper() if isinstance(v, str) and v else None
                if k and k in seen:
                    twice = True
                    continue
                if k:
                    seen.add(k)
                kept.append(v)
            cfg[key] = kept
        elif val:
            cfg[key] = one(val, key)
    if read_as_gb:
        # Read by ad_intel for its one line; "_" keys are never fingerprinted.
        cfg["_uk_read_as_gb"] = True
    if twice:
        cfg["_country_listed_twice"] = True
    return cfg


def repo_default_config_path():
    """AD_INTEL_CONFIG.json sitting next to the module's parent folder."""
    return Path(__file__).resolve().parent.parent / "AD_INTEL_CONFIG.json"


def load_config(path=None):
    """Load + validate an AD_INTEL_CONFIG.json."""
    p = Path(path) if path else repo_default_config_path()
    if not p.exists():
        raise SystemExit(
            f"No config at {p}. Copy AD_INTEL_CONFIG.example.json to "
            f"AD_INTEL_CONFIG.json and fill it in."
        )
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Config {p} is missing required keys: {', '.join(missing)}")

    # Defaults for optional keys the pipeline relies on.
    # country_codes (list) is preferred; country_code (str) remains required for back-compat.
    # An empty list means "use country_code only" — build_B4/B5 handle this transparently.
    cfg.setdefault("country_codes", [])
    cfg.setdefault("language", "en")
    cfg.setdefault("vertical", "")
    cfg.setdefault("meta_search_terms", [])
    cfg.setdefault("seed_terms", [])
    cfg.setdefault("competitor_domains", [])
    cfg.setdefault("competitor_names", [])
    # 12.0 holds the forecast of a 3-competitor run at these defaults ($11.74 at
    # the floor rates above with Meta's up to 30 extra ads per page counted; it was $11.58); the
    # skill calls 2 to 3
    # competitors a normal run, and a default that refused one teaches raising
    # caps blindly.
    cfg.setdefault("budget_caps", {"phase1": 12.0})
    cfg.setdefault("meta_limit_per_source", 200)  # full-run cap: Meta ads per search URL
    cfg.setdefault("google_max_items", 2000)      # full-run cap: total Google ads per run (bound cost)
    cfg.setdefault("include_linkedin", True)      # opt-in LinkedIn Ad Library scrape (LI stage)
    cfg.setdefault("linkedin_max_items", 100)     # full-run cap: total LinkedIn ads per run
    cfg.setdefault("download_creatives", True)
    cfg.setdefault("ocr_google_text", True)
    cfg.setdefault("vision_ocr_model", "")   # blank => cheapest model (haiku) via claude CLI subagent
    cfg.setdefault("ocr_top_fraction", 0.2)  # blank/absent => OCR only the longest-running 20% of Google ads
    cfg.setdefault("stages", [])
    _read_numbers(cfg, p, _NUMBER_SETTINGS)
    _read_budget_caps(cfg, p, 12.0)  # the default set above
    return _read_countries(cfg, p)


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
# meta_limit_per_source is 1 or more: Meta's ad library actor reads a
# limitPerSource of 0 as "no limit" and returns every ad the page has, but the
# forecast prices limit + 30 ads a page (stages.build_B4), so a 0 was priced at 30
# ads a page while the spend cap let the actor bill far more. The others keep 0
# (their actors are not documented to read 0 as "no limit").
_NUMBER_SETTINGS = {
    "meta_limit_per_source": (200, 1, None, True,
                              "0 would let Meta return every ad it has, which the "
                              "forecast cannot price"),
    "google_max_items": (2000, 0, None, True, ""),
    "linkedin_max_items": (100, 0, None, True, ""),
    "ocr_top_fraction": (0.2, 0, 1, False, ""),
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
