"""
Stage registry — the config-driven definition of every Apify stage the
search-intelligence tool runs.

Three stages:
  A1  google-keywords-suggest    — keyword expansion (autocomplete). seeds -> universe.
  A2  semrush-scraper            — volume / difficulty / CPC per keyword.
  B1  google-search-scraper      — SERP for the top keywords (who ranks).

Each stage knows: which actor to call, how to BUILD its Apify input from the
config (+ any upstream output on disk), and how many records to EXPECT (for the
pre-run budget gate). run_phase.py consumes this registry. Nothing here spends
money — run_phase.py does, one stage at a time.

Dependency chain: A1 expands seeds -> a keyword list; A2 prices that list; B1
pulls the SERP for the top slice. A2 and B1 read the expanded keyword list that
A1's postprocess writes to `keywords/keywords.json`; before A1 has run (e.g. a
dry-run) they fall back to `cfg["seed_terms"]`.
"""

import json
from pathlib import Path

from uule import uule  # canonical location name -> Google UULE string


# --- helpers ------------------------------------------------------------------

def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _keyword_list(cfg, workdir):
    """The keyword universe A2/B1 operate on: A1's expanded list if present, else
    the raw seed terms (dry-run / pre-A1 fallback)."""
    kf = Path(workdir) / "keywords" / "keywords.json"
    kws = _read_json(kf, None)
    if isinstance(kws, list) and kws:
        return kws
    return list(cfg.get("seed_terms", []))


# Common non-ISO country codes users type, mapped to the ISO code Google's actors
# require. "UK" is the usual one: Google uses "GB" (A1's `country` enum has GB not
# UK; B1's `countryCode` enum has gb not uk - both verified against the live actor
# input schemas 2026-09-02). Semrush is the exception (it names GB's database "uk")
# and is handled on top of this by _semrush_database.
_ISO_COUNTRY_ALIASES = {"UK": "GB"}


def _iso_country(country_code):
    """Canonical UPPERCASE ISO country for the Google actors (A1/B1).

    Uppercases and maps common non-ISO inputs (UK->GB) so a user who writes
    either "GB" or "UK" reaches the code Google actually accepts. A1 uses this
    value as-is (its enum is uppercase); B1 lowercases it (its enum is lowercase).
    """
    cc = (country_code or "").strip().upper()
    return _ISO_COUNTRY_ALIASES.get(cc, cc)


# --- per-stage input builders -------------------------------------------------
# Each returns (actor_input_dict, expected_record_count). `test` truncates inputs.

def build_A1(cfg, workdir, test):
    """Keyword expansion via Google autocomplete. Seeds -> ~30 suggestions each.

    The crawlerbros actor's country field is `country` (UPPERCASE ISO enum,
    default US), NOT `countryCode`. Sending `countryCode` was silently ignored,
    so every run defaulted to US regardless of the user's market - verified live
    2026-09-02: `country:"GB"` returned British football suggestions (fixtures,
    on tv, results) while `countryCode:"GB"` returned the US default (cleats,
    field, positions). Use the documented field name and an ISO-normalized value.
    """
    seeds = list(cfg.get("seed_terms", []))
    seeds = seeds[:5] if test else seeds
    return ({"keywords": seeds,
             "language": cfg.get("language", "en"),
             "country": _iso_country(cfg["country_code"])},
            len(seeds) * 30)  # ~30 suggestions/seed observed in the kit pilot


def build_A2(cfg, workdir, test):
    """Keyword volume / difficulty / CPC via the Semrush scraper.

    `mode: "keyword"` is REQUIRED — this actor DEFAULTS to "domain" mode,
    which ignores `keywords` and fails with "No domains provided".

    The actor caps at ~100 keywords/run. STAGES["A2"] declares
    `batch={"key":"keywords","size":100}` so run_phase.py chunks larger lists and
    concatenates the datasets — do not remove it, or a >100-keyword run silently
    truncates to the first 100.
    """
    terms = _keyword_list(cfg, workdir)
    terms = terms[:5] if test else terms[: int(cfg.get("keyword_cap", 300))]
    # This actor's `database` field must be a LOWERCASE code from its allowed set
    # ("worldwide","us","uk","ca","au","de",...) - passing "US" (the country_code
    # convention A1/B1 accept as uppercase) 400s with "must be equal to one of
    # the allowed values". Lowercase it here. The actor also uses "uk" (not the
    # ISO "gb"), so a user on the ISO code "GB" (correct for A1/B1's Google
    # actors, which DO use "gb") must be remapped to "uk" for Semrush only -
    # otherwise a UK run 400s. This alias is Semrush-specific; do not apply it to
    # A1/B1.
    return ({"mode": "keyword", "keywords": terms,
             "database": _semrush_database(cfg["country_code"])},
            len(terms))


# Semrush database aliases: ISO country codes that this actor names differently.
# Only "gb" -> "uk" is known to differ from ISO across the actor's allowed set.
_SEMRUSH_DB_ALIASES = {"gb": "uk"}


def _semrush_database(country_code):
    """Map a user country_code to the Semrush actor's `database` value.

    Normalizes to ISO first (so "UK" and "GB" both arrive as GB), lowercases (the
    actor rejects uppercase), then applies the Semrush-specific alias gb->uk (the
    one code where Semrush diverges from ISO). So "GB"/"gb"/"UK"/"uk" all resolve
    to the actor's "uk"; "US" -> "us"; "DE" -> "de"."""
    code = _iso_country(country_code).lower()
    return _SEMRUSH_DB_ALIASES.get(code, code)


def build_B1(cfg, workdir, test):
    """SERP for the top keywords — who ranks for what. The top-N slice is applied
    by run_phase via serp_top_n; here we build over the full keyword list and the
    caller decides how many to pull."""
    queries = _keyword_list(cfg, workdir)
    if not test:
        queries = queries[: int(cfg.get("serp_top_n", 100))]
    else:
        queries = queries[:5]
    # This actor's `countryCode` enum is LOWERCASE ISO ("us","gb","de",... -
    # "gb" NOT "uk", verified against the live schema 2026-09-02). Passing "US"
    # 400s at validation, and "uk" is not in the enum, so normalize to ISO
    # (UK->GB) then lowercase. A1's actor wants UPPERCASE, so casing is per-actor.
    inp = {
        "queries": "\n".join(queries),
        "resultsPerPage": 10, "maxPagesPerQuery": 1,
        "countryCode": _iso_country(cfg["country_code"]).lower(), "languageCode": cfg.get("language", "en"),
    }
    # Localization: a UULE string localizes the SERP to a city. An explicit
    # `geo_uule` (a hand-supplied string) wins; otherwise derive it from a human
    # `location` name via the UULE generator. No location => no UULE => country-
    # level SERP (countryCode only), which is sufficient for non-local markets.
    # The actor consumes `locationUule`; live acceptance of a generated string
    # should be verified with a paid test run, not assumed from offline tests.
    uule_str = cfg.get("geo_uule") or (uule(cfg["location"]) if cfg.get("location") else "")
    if uule_str:
        inp["locationUule"] = uule_str
    return inp, len(queries)


# --- registry -----------------------------------------------------------------

STAGES = {
    "A1": {"actor": "crawlerbros/google-keywords-suggest-scraper-pro", "phase": "1", "folder": "A1_keyword_expansion", "build": build_A1, "output": "dataset"},
    "A2": {"actor": "pro100chok/semrush-scraper",                      "phase": "1", "folder": "A2_keyword_metrics",   "build": build_A2, "output": "dataset", "batch": {"key": "keywords", "size": 100}},
    "B1": {"actor": "apify/google-search-scraper",                     "phase": "1", "folder": "B1_serp_landscape",    "build": build_B1, "output": "dataset"},
}

# Default run order: expand -> price -> rank. Sequential; each reads the last.
PHASE_ORDER = {
    "1": ["A1", "A2", "B1"],
}

QUESTION = {
    "A1": "What does the market actually search for? (seed -> keyword universe)",
    "A2": "How much volume / how contested is each keyword? (demand + difficulty)",
    "B1": "Who already owns the SERP for these queries? (competitive landscape)",
}

# Input dependencies. A2 and B1 consume A1's expanded keyword list (or seed_terms
# as a fallback), so both depend on A1 in a full run.
NEEDS = {
    "A1": [], "A2": ["A1"], "B1": ["A1"],
}


def selected_stages(cfg):
    """The config's task allow-list, or None to mean 'run all defaults'."""
    sel = cfg.get("stages")
    return set(sel) if sel else None
