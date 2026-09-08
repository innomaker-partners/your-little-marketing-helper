"""
Config loader + empirical cost model for the ad-intelligence tool.

AD_INTEL_CONFIG.json (copy from AD_INTEL_CONFIG.example.json) is the single file
that drives every stage. This module loads + validates it and exposes the
empirical Apify rates measured on initial pilot runs.

Stdlib only — no pip install needed.
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Empirical per-unit Apify cost (USD), measured on initial pilot runs,
# INCLUDING platform overhead. Treat as a STARTING estimate — your first test
# run recalibrates the rate for your own market/account (run_phase.py does this
# automatically).
# ---------------------------------------------------------------------------
EMPIRICAL_RATES = {
    # stage -> (actor, unit, usd_per_unit, unverified?)
    "B1": ("apify/google-search-scraper",                "query", 0.0076,  False),
    "B4": ("curious_coder/facebook-ads-library-scraper", "ad",    0.0007,  False),
    # B5 STARTING estimate is ALL-IN. This actor uses a combined pricing model:
    # per-ad event $0.00124 PLUS platform compute/proxy passed through to the
    # user (isPPEPlatformUsagePaidByUser). The verified residential run billed
    # ~$0.076 for 25 ads ≈ $0.003/ad; a larger run amortizes the per-run compute
    # floor toward ~$0.002/ad. 0.0025 is a realistic pre-test seed — run_phase
    # then recalibrates from the actual test charge (via actual_charge_usd).
    "B5": ("lexis-solutions/google-ads-scraper",         "ad",    0.0025,  False),
    # LI: scrapesage/linkedin-ad-library-scraper, scoped by NUMERIC company id
    # (companyIds) — the only precise advertiser scoping (keyword/slug are fuzzy
    # and return other advertisers; earlier actors using name search returned 0 results). Rate is a pay-per-result seed marked UNVERIFIED; run_phase
    # recalibrates from the actual test charge. Every stage in STAGES needs a rate
    # here or C.estimate raises KeyError and the stage silently returns 0.
    "LI": ("scrapesage/linkedin-ad-library-scraper",     "ad",    0.003,   True),
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
    cfg.setdefault("budget_caps", {"phase1": 10.0})
    cfg.setdefault("meta_limit_per_source", 200)  # full-run cap: Meta ads per search URL
    cfg.setdefault("google_max_items", 2000)      # full-run cap: total Google ads per run (bound cost)
    cfg.setdefault("include_linkedin", True)      # opt-in LinkedIn Ad Library scrape (LI stage)
    cfg.setdefault("linkedin_max_items", 100)     # full-run cap: total LinkedIn ads per run
    cfg.setdefault("download_creatives", True)
    cfg.setdefault("ocr_google_text", True)
    cfg.setdefault("vision_ocr_model", "")   # blank => cheapest model (haiku) via claude CLI subagent
    cfg.setdefault("ocr_top_fraction", 0.2)  # blank/absent => OCR only the longest-running 20% of Google ads
    cfg.setdefault("stages", [])
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
