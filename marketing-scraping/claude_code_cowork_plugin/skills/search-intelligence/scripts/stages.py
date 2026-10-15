"""
Stage registry — the config-driven definition of every Apify stage the
search-intelligence tool runs.

Three stages:
  A1  google-suggest-scraper     — keyword expansion (autocomplete). seeds -> universe.
  A2  semrush-scraper            — volume / difficulty / CPC per keyword.
  B1  google-search-scraper      — SERP for the highest-volume keywords (who ranks).

Each stage knows: which actor to call, how to BUILD its Apify input from the
config (+ any upstream output on disk), and how many records to EXPECT (for the
pre-run budget gate). run_phase.py consumes this registry. Nothing here spends
money — run_phase.py does, one stage at a time.

Dependency chain: A1 expands seeds -> a keyword list; A2 prices that list; B1
pulls the SERP for the highest-volume slice. A2 and B1 read the expanded keyword list that
A1's postprocess writes to `keywords/keywords.json`; before A1 has run (e.g. a
dry-run) they fall back to `cfg["seed_terms"]`. B1 picks its keywords by the
volumes A2 found (serp_keywords below), not by their place in that list.
"""

import json
import math
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
# require. "UK" is the usual one: Google uses "GB" (A1's `regions` take GB, not
# UK, as one real run sent it; B1's `countryCode` enum has gb not uk - verified against the live
# actor input schema). Semrush is the exception (it names GB's database "uk")
# and is handled on top of this by _semrush_database.
_ISO_COUNTRY_ALIASES = {"UK": "GB"}


def _iso_country(country_code):
    """Canonical UPPERCASE ISO country for the Google actors (A1/B1).

    Uppercases and maps common non-ISO inputs (UK->GB) so a user who writes
    either "GB" or "UK" reaches the code Google actually accepts. A1 uses this
    value as-is (uppercase); B1 lowercases it (its enum is lowercase).
    """
    cc = (country_code or "").strip().upper()
    return _ISO_COUNTRY_ALIASES.get(cc, cc)


# --- per-stage input builders -------------------------------------------------
# Each returns (actor_input_dict, expected_record_count). `test` truncates inputs.

# Suggestions the keyword-ideas step plans for per seed. One number, used twice:
# as the step's expected record count (the forecast the user approves) and as the
# actor's own row limit (`maxItems`) in build_A1, so the limit sent can never be
# larger than what the forecast priced. The recorded real runs gave 12 to 15 per
# seed, so this is a ceiling, not a typical count.
A1_SUGGESTIONS_PER_SEED = 30


def build_A1(cfg, workdir, test):
    """Keyword expansion via Google autocomplete. Seeds -> up to ~30 suggestions each.

    Actor: memo23/google-suggest-scraper, which replaced the withdrawn
    crawlerbros actor. The input below is the exact input that was run for real (once, and once
    more with another region), so the output and the
    charge are measured, not assumed. Do not change a field without a new
    measured run.

    - `regions` is a list of UPPERCASE ISO codes, UK mapped to GB (_iso_country).
      K2 run 4 showed the region changes the output, so the market the user asked
      for is the market searched (the withdrawn actor's `country` was ignored
      when sent under a wrong name; this one is not).
    - Every expansion option is off and every per-keyword limit is 0 (none): the
      tool wants each seed's own autocomplete suggestions and nothing multiplied.
    - `includeMetadata` is true because the `relevance` field that
      postprocess._pp_a1 sorts by only arrives with it.
    - `maxItems` is the actor's total row limit, so it is the most rows the run
      can bill: seeds x A1_SUGGESTIONS_PER_SEED, the count the forecast prices.
    """
    seeds = list(cfg.get("seed_terms", []))
    seeds = seeds[:5] if test else seeds
    expected = len(seeds) * A1_SUGGESTIONS_PER_SEED
    return ({"keywords": seeds,
             "regions": [_iso_country(cfg["country_code"])],
             "languages": [cfg.get("language", "en")],
             "expandAlphabet": False,
             "expandNumbers": False,
             "expandQuestions": False,
             "expansionDepth": 0,
             "maxSuggestionsPerKeyword": 0,
             "deduplicateSuggestions": False,
             "includeMetadata": True,
             "maxItems": expected},
            expected)


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


def _keyword_volumes(workdir):
    """{keyword (stripped, lowercase): monthly searches} from the volumes step's
    cleaned file (metrics/keyword_metrics.json, written by postprocess._pp_a2).

    Only a real number counts as a volume. A null (the step returned no volume for
    that keyword), a missing row, a text value or a non-finite number is left out,
    so the keyword ranks as "no volume" and is never read as 0. Returns {} when
    the file is missing, unreadable or holds no volume at all: the volumes step
    did not run, failed, or found nothing. The first row wins for a repeated
    keyword."""
    try:
        import postprocess as _PP  # noqa: E402 (one judge for every reader of the cleaned file)
        rows = _PP.judge_keyword_rows(
            _read_json(Path(workdir) / "metrics" / "keyword_metrics.json", None), workdir)
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict) or _PP.semrush_no_data(r):   # No data is no volume (the one rule)
            continue
        v = r.get("volume")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        out.setdefault(str(r.get("keyword") or "").strip().lower(), v)
    out.pop("", None)
    return out


def serp_keywords(cfg, workdir, test=False):
    """The keywords the Google search-results step pulls, and how they were chosen.

    Returns (keywords, by_volume). With keyword volumes available, the step takes
    the `serp_top_n` keywords with the highest monthly searches out of the
    keywords that were priced (the first `keyword_cap`; a test pass takes 5).
    Equal volumes keep their place in the keyword list, so the same volumes always
    give the same keywords. A keyword with no volume ranks after every keyword that
    has one, also in list order. With no volumes at all (the volumes step did not
    run or found nothing) the first keywords in list order are taken, as before,
    and by_volume is False so the caller can say so.

    WHY: this step used to take the first `serp_top_n` keywords in list order (the
    user's own words, then Google's suggestions), so a keyword searched 3,600 times
    a month could be passed over for one searched 70 times. The order is "by volume". The
    number of keywords, and so the forecast, is
    exactly what it was: only which keywords changes.

    Reads the file the volumes step already wrote rather than working the
    volumes out again. Writes nothing and prints nothing: the forecast calls this
    too, with a folder that does not exist."""
    queries = _keyword_list(cfg, workdir)
    if test:
        limit = 5
    else:
        # keyword_cap bounds the keyword list here as it does for A2 and
        # for the forecast (search_intel._search_forecast). A1's list is already
        # capped by it; the seed-term fallback was not, so keyword_cap 0 left A2 at
        # 0 but sent the seed terms to this paid step, which the forecast priced
        # at $0.
        queries = queries[: int(cfg.get("keyword_cap", 300))]
        limit = int(cfg.get("serp_top_n", 100))
    # Rank on volumes only when this run's own stage list includes
    # the volumes step. With a stage list that leaves A2 out, a metrics file in the
    # folder is an earlier run's: ranking on it would pull the search results for
    # keywords chosen by volumes this run never bought, and say nothing. Without a
    # stage list (None) the default order includes A2, as before.
    sel = selected_stages(cfg)
    volumes = {} if (sel is not None and "A2" not in sel) else _keyword_volumes(workdir)
    if not volumes:
        return queries[:limit], False
    def _rank(i):
        v = volumes.get(str(queries[i]).strip().lower())
        return (v is None, -v if v is not None else 0, i)
    return [queries[i] for i in sorted(range(len(queries)), key=_rank)[:limit]], True


def serp_selection_note(cfg, workdir, dry_run=False):
    """One plain sentence when the search-results step falls back to list order,
    else None. The entry loop prints it once, where the step's other lines appear.
    Built from serp_keywords itself, so the sentence and the keywords agree."""
    queries, by_volume = serp_keywords(cfg, workdir)
    if by_volume or not queries:
        return None
    n = len(queries)
    what = f"the first {n} keyword{'' if n == 1 else 's'} in the order listed"
    sel = selected_stages(cfg)
    if dry_run and (sel is None or "A2" in sel):
        return (f"Google search results: keyword volumes are not in this folder yet, "
                f"so this preview shows {what}. A real run pulls the highest-volume "
                f"keywords once the volumes are in.")
    # Also the sentence for a stage list that leaves the volumes
    # step out. It is true of that run (no volumes are used, even if an earlier run left
    # a file), and the closing lines (search_intel) match it word for word.
    return (f"Google search results: no keyword volumes are available for this run, "
            f"so the search results are pulled for {what}, not for the "
            f"highest-volume ones.")


def build_B1(cfg, workdir, test):
    """SERP for the highest-volume keywords: who ranks for what. serp_keywords
    picks them (serp_top_n of the priced keywords, by volume); here we only build
    the actor input from them."""
    queries, _by_volume = serp_keywords(cfg, workdir, test)
    # This actor's `countryCode` enum is LOWERCASE ISO ("us","gb","de",... -
    # "gb" NOT "uk", verified against the live schema). Passing "US"
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
    "A1": {"name": "Keyword ideas", "actor": "memo23/google-suggest-scraper", "phase": "1", "folder": "A1_keyword_expansion", "build": build_A1, "output": "dataset", "memory_mb": 512},
    "A2": {"name": "Keyword volumes", "actor": "pro100chok/semrush-scraper",                      "phase": "1", "folder": "A2_keyword_metrics",   "build": build_A2, "output": "dataset", "batch": {"key": "keywords", "size": 100}},
    "B1": {"name": "Google search results", "actor": "apify/google-search-scraper",                     "phase": "1", "folder": "B1_serp_landscape",    "build": build_B1, "output": "dataset"},
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
