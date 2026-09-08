"""
Deterministic search-intelligence synthesis extractor.

Reads the stage artifacts from a workdir and writes synthesis_facts.json,
the single file the report step consumes.
Every number is counted in code from real scraped data.
No LLM, no network, no estimates, no datetime.now, no random.

Entry point:
    import extractor
    path = extractor.extract(workdir, cfg)  # -> Path to synthesis_facts.json

Architecture: _extract() is the pure in-memory core; extract() wraps it
with file I/O.  Selftests hit _extract() directly via synthetic in-memory
workdirs -- no real files, no network, no Apify needed.
"""

import json
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    """Read JSON from path; return default if absent.  Matches pain extractor."""
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_date_from_runlog(runlog_path):
    """Extract the first YYYY-MM-DD date from a RUN_LOG.md.

    Looks for lines matching '### YYYY-MM-DD ...' (the standard run-log date-heading format).
    Returns the date string, or None if the file is missing or unparseable.

    FROZEN: never stamps today's date -- that would be fabricated provenance
    and break determinism between runs.
    """
    p = Path(runlog_path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^###\s+(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


def _volume_sort_key(row):
    """Sort key: volume desc (None last), then keyword asc for stable ties."""
    vol = row.get("volume")
    if vol is None:
        # Sort nulls last: use a sentinel that exceeds any real volume tuple
        return (1, 0, row.get("keyword", ""))
    return (0, -vol, row.get("keyword", ""))


# ---------------------------------------------------------------------------
# Core extraction (pure in-memory, testable seam)
# ---------------------------------------------------------------------------

def _extract(workdir, cfg):
    """Run extraction on a workdir; return the facts dict (no file I/O)."""
    workdir = Path(workdir)
    opp_kd_max = cfg.get("opportunity_kd_max", 40)

    # ------------------------------------------------------------------
    # Load artifacts (robust to missing files -- partial runs are valid)
    # ------------------------------------------------------------------
    keywords_raw = _read_json(workdir / "keywords" / "keywords.json", [])
    keyword_universe = len(keywords_raw) if isinstance(keywords_raw, list) else 0

    metrics_raw = _read_json(workdir / "metrics" / "keyword_metrics.json", None)
    metrics_list = metrics_raw if isinstance(metrics_raw, list) else []

    rankings_raw = _read_json(workdir / "serp" / "rankings.json", None)
    rankings_list = rankings_raw if isinstance(rankings_raw, list) else []

    # competitive_map: pass through verbatim (_pp_b1 already counted and sorted
    # it; re-deriving it here would be less reliable than trusting the upstream
    # computation)
    domain_map = _read_json(workdir / "serp" / "domain_map.json", [])
    if not isinstance(domain_map, list):
        domain_map = []

    # ------------------------------------------------------------------
    # Dates from RUN_LOGs (read from logs, never from wall clock)
    # ------------------------------------------------------------------
    metrics_date = _parse_date_from_runlog(
        workdir / "A2_keyword_metrics" / "RUN_LOG.md"
    )
    serp_date = _parse_date_from_runlog(
        workdir / "B1_serp_landscape" / "RUN_LOG.md"
    )

    # ------------------------------------------------------------------
    # Coverage
    # ------------------------------------------------------------------
    priced_keywords = len(metrics_list)
    serp_keywords   = len(rankings_list)

    metrics_kw_set  = {
        r["keyword"] for r in metrics_list
        if isinstance(r, dict) and "keyword" in r
    }
    rankings_kw_set = {
        r["keyword"] for r in rankings_list
        if isinstance(r, dict) and "keyword" in r
    }
    priced_and_serp = len(metrics_kw_set & rankings_kw_set)

    # ------------------------------------------------------------------
    # keyword_table: one row per priced keyword, volume desc (nulls last)
    # Preserve nulls; never coerce to 0
    # ------------------------------------------------------------------
    keyword_table = []
    for r in metrics_list:
        if not isinstance(r, dict):
            continue
        kw = r.get("keyword", "")
        keyword_table.append({
            "keyword":            kw,
            "volume":             r.get("volume"),        # null preserved
            "keyword_difficulty": r.get("keyword_difficulty"),
            "cpc_usd":            r.get("cpc_usd"),
            "competition":        r.get("competition"),
            # median referring-domain count of the pages ranking for this keyword
            # (Semrush) — a link-competitiveness signal, NOT a domain-authority
            # score and NOT a backlink index. Surfaced as a difficulty input.
            "referring_domains_median": r.get("referring_domains_median"),  # null preserved
            "source":             r.get("source", "semrush"),
            "has_serp":           kw in rankings_kw_set,
        })
    keyword_table.sort(key=_volume_sort_key)

    # ------------------------------------------------------------------
    # low_difficulty_opportunities
    # null kd is excluded (cannot assess winnability), never treated as 0
    # ------------------------------------------------------------------
    opportunities = []
    for r in metrics_list:
        if not isinstance(r, dict):
            continue
        kd = r.get("keyword_difficulty")
        if kd is None:
            continue   # null difficulty: excluded
        if kd <= opp_kd_max:
            opportunities.append({
                "keyword":            r.get("keyword", ""),
                "volume":             r.get("volume"),
                "keyword_difficulty": kd,
            })
    opportunities.sort(key=_volume_sort_key)

    # ------------------------------------------------------------------
    # serp_feature_summary: counted from rankings (each ranking = 1 keyword)
    # ------------------------------------------------------------------
    kw_paid = 0
    kw_paa  = 0
    kw_rq   = 0
    for r in rankings_list:
        if not isinstance(r, dict):
            continue
        sf = r.get("serp_features") or {}
        if (sf.get("paid_results") or 0) > 0:
            kw_paid += 1
        if (sf.get("people_also_ask") or 0) > 0:
            kw_paa += 1
        if (sf.get("related_queries") or 0) > 0:
            kw_rq += 1

    # ------------------------------------------------------------------
    # caveats: deterministic honesty strings built from counted facts
    # ------------------------------------------------------------------
    kw_pl = "keyword" if serp_keywords == 1 else "keywords"
    caveats = []

    caveats.append(
        f"Volume/difficulty/CPC available for {priced_keywords}/{keyword_universe} "
        f"keywords; the rest were expanded but not priced."
    )
    caveats.append(f"SERP landscape covers {serp_keywords} {kw_pl}.")

    if priced_keywords <= 5 or serp_keywords <= 5:
        caveats.append(
            # House copy rule: no em-dash in our authored voice (caveats are
            # ours, not a verbatim quote). Use a plain hyphen.
            "Small sample - treat rankings as directional, not exhaustive."
        )

    # Date caveat: compose only the phrases whose date is non-null
    date_parts = []
    if metrics_date:
        date_parts.append(f"Metrics scraped {metrics_date} (Semrush)")
    if serp_date:
        date_parts.append(f"SERP scraped {serp_date} (Google)")
    if date_parts:
        caveats.append("; ".join(date_parts) + ". SERP rankings drift over time.")

    # ------------------------------------------------------------------
    # Assemble facts
    # ------------------------------------------------------------------
    return {
        "coverage": {
            "keyword_universe": keyword_universe,
            "priced_keywords":  priced_keywords,
            "serp_keywords":    serp_keywords,
            "priced_and_serp":  priced_and_serp,
            # Pre-counted count of distinct SERP-owning domains, so the report's
            # narrative can cite "N domains" as a sourced fact rather than the LLM
            # counting the competitive_map itself.
            "unique_domains":   len(domain_map),
        },
        "sources": {
            "metrics": {"source": "semrush",     "scraped_date": metrics_date},
            "serp":    {"source": "google_serp", "scraped_date": serp_date},
        },
        "keyword_table":                keyword_table,
        "low_difficulty_opportunities": opportunities,
        "competitive_map":              domain_map,   # verbatim pass-through from _pp_b1
        "serp_feature_summary": {
            "serp_keywords_total":           serp_keywords,
            "keywords_with_paid_results":    kw_paid,
            "keywords_with_people_also_ask": kw_paa,
            "keywords_with_related_queries": kw_rq,
        },
        "caveats": caveats,
        "params":  {"opportunity_kd_max": opp_kd_max},
    }


# ---------------------------------------------------------------------------
# File-facing seam (called by search_intel.py)
# ---------------------------------------------------------------------------

def extract(workdir, cfg):
    """Read stage artifacts from workdir, write synthesis_facts.json.

    Returns the Path to synthesis_facts.json.
    """
    workdir = Path(workdir)
    facts   = _extract(workdir, cfg)
    out     = workdir / "synthesis_facts.json"
    out.write_text(
        json.dumps(facts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cov = facts["coverage"]
    print(
        f"   extractor -> synthesis_facts.json  "
        f"({cov['priced_keywords']} priced / {cov['serp_keywords']} SERP "
        f"of {cov['keyword_universe']} keywords, "
        f"{cov['priced_and_serp']} have both)"
    )
    return out


# ---------------------------------------------------------------------------
# Inline verification tests
# Run with:  python3 scripts/extractor.py
# Uses INLINE synthetic artifacts written to a tempfile workdir.
# No external fixture files, no network, no Apify, no LLM.
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    import tempfile, hashlib

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

    # -----------------------------------------------------------------------
    # Build a synthetic workdir with controlled data
    # -----------------------------------------------------------------------
    def _make_workdir(tmpdir, *,
                      keywords=None,
                      metrics=None,
                      rankings=None,
                      domain_map=None,
                      a2_runlog=None,
                      b1_runlog=None):
        """Write synthetic artifacts into tmpdir subdirectories."""
        root = Path(tmpdir)
        if keywords is not None:
            (root / "keywords").mkdir(exist_ok=True)
            (root / "keywords" / "keywords.json").write_text(
                json.dumps(keywords), encoding="utf-8"
            )
        if metrics is not None:
            (root / "metrics").mkdir(exist_ok=True)
            (root / "metrics" / "keyword_metrics.json").write_text(
                json.dumps(metrics), encoding="utf-8"
            )
        if rankings is not None:
            (root / "serp").mkdir(exist_ok=True)
            (root / "serp" / "rankings.json").write_text(
                json.dumps(rankings), encoding="utf-8"
            )
        if domain_map is not None:
            (root / "serp").mkdir(exist_ok=True)
            (root / "serp" / "domain_map.json").write_text(
                json.dumps(domain_map), encoding="utf-8"
            )
        if a2_runlog is not None:
            (root / "A2_keyword_metrics").mkdir(exist_ok=True)
            (root / "A2_keyword_metrics" / "RUN_LOG.md").write_text(
                a2_runlog, encoding="utf-8"
            )
        if b1_runlog is not None:
            (root / "B1_serp_landscape").mkdir(exist_ok=True)
            (root / "B1_serp_landscape" / "RUN_LOG.md").write_text(
                b1_runlog, encoding="utf-8"
            )
        return root

    # Synthetic keywords (10)
    SYN_KEYWORDS = [f"kw{i}" for i in range(10)]

    # Synthetic metrics: mix of null and real KD
    SYN_METRICS = [
        # null difficulty -- must be EXCLUDED from opportunities, PRESENT in table with null
        {"keyword": "kw0", "volume": 500, "cpc_usd": 5.0,
         "competition": 0.5, "keyword_difficulty": None,
         "source": "semrush", "database": "us"},
        # KD=20 (≤40) -- INCLUDED in opportunities
        {"keyword": "kw1", "volume": 200, "cpc_usd": 3.0,
         "competition": 0.3, "keyword_difficulty": 20,
         "source": "semrush", "database": "us"},
        # KD=60 (>40) -- NOT in opportunities
        {"keyword": "kw2", "volume": 100, "cpc_usd": 2.0,
         "competition": 0.2, "keyword_difficulty": 60,
         "source": "semrush", "database": "us"},
    ]

    # Synthetic rankings: only kw1 has SERP
    SYN_RANKINGS = [
        {
            "keyword": "kw1",
            "source": "google_serp",
            "results": [{"position": 1, "domain": "alpha.com", "title": "Alpha", "displayed_url": "alpha.com"}],
            "serp_features": {"paid_results": 2, "people_also_ask": 3, "related_queries": 0},
        }
    ]

    # Synthetic domain map (list of dicts, pre-sorted by B1)
    SYN_DOMAIN_MAP = [
        {"domain": "alpha.com", "keywords_owned": 1, "avg_position": 1.0, "best_position": 1},
        {"domain": "beta.com",  "keywords_owned": 1, "avg_position": 3.0, "best_position": 3},
    ]

    A2_LOG = "# Run log\n\n### 2026-07-15 10:00 UTC — test\n- Status: SUCCEEDED\n"
    B1_LOG = "# Run log\n\n### 2026-07-16 11:30 UTC — test\n- Status: SUCCEEDED\n"

    # =========================================================================
    # TEST 1 -- null difficulty: excluded from opportunities, present in table
    # =========================================================================
    print("\n=== TEST 1: null difficulty handling ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        facts = _extract(tmp, {})

        # kw0 has null KD → must be in keyword_table with null preserved
        kt_kw0 = [r for r in facts["keyword_table"] if r["keyword"] == "kw0"]
        check("kw0 present in keyword_table", len(kt_kw0), 1)
        check("kw0 keyword_difficulty is null/None", kt_kw0[0]["keyword_difficulty"] if kt_kw0 else "MISSING", None)

        # kw0 must NOT appear in low_difficulty_opportunities
        opp_kws = [r["keyword"] for r in facts["low_difficulty_opportunities"]]
        check("kw0 (null KD) excluded from opportunities", "kw0" in opp_kws, False)

        # kw1 (KD=20 ≤ 40) must appear in opportunities
        check("kw1 (KD=20) in opportunities", "kw1" in opp_kws, True)

        # kw2 (KD=60 > 40) must NOT appear
        check("kw2 (KD=60) excluded from opportunities", "kw2" in opp_kws, False)

    # =========================================================================
    # TEST 2 -- has_serp correct (true/false)
    # =========================================================================
    print("\n=== TEST 2: has_serp ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        facts = _extract(tmp, {})
        kt = {r["keyword"]: r["has_serp"] for r in facts["keyword_table"]}
        check("kw1 has_serp=True (in rankings)", kt.get("kw1"), True)
        check("kw0 has_serp=False (not in rankings)", kt.get("kw0"), False)
        check("kw2 has_serp=False (not in rankings)", kt.get("kw2"), False)

    # =========================================================================
    # TEST 3 -- competitive_map passed through unchanged (identity)
    # =========================================================================
    print("\n=== TEST 3: competitive_map pass-through ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        facts = _extract(tmp, {})
        check("competitive_map identical to domain_map input",
              facts["competitive_map"], SYN_DOMAIN_MAP)
        check("competitive_map length", len(facts["competitive_map"]), 2)

    # =========================================================================
    # TEST 4 -- coverage counts (including priced_and_serp intersection)
    # =========================================================================
    print("\n=== TEST 4: coverage counts ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        facts = _extract(tmp, {})
        cov = facts["coverage"]
        check("keyword_universe", cov["keyword_universe"], 10)
        check("priced_keywords",  cov["priced_keywords"],   3)  # SYN_METRICS has 3 rows
        check("serp_keywords",    cov["serp_keywords"],     1)  # SYN_RANKINGS has 1 row
        # Intersection: kw0∩{kw1}=0, kw1∩{kw1}=1, kw2∩{kw1}=0 → 1
        check("priced_and_serp",  cov["priced_and_serp"],   1)

    # =========================================================================
    # TEST 5 -- missing metrics file → keyword_table=[], no crash
    # =========================================================================
    print("\n=== TEST 5: missing metrics file ===")
    with tempfile.TemporaryDirectory() as tmp:
        # Only keywords + rankings; no metrics file at all
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        try:
            facts = _extract(tmp, {})
            check("no crash when metrics missing",   True, True)
            check("keyword_table=[]",                facts["keyword_table"], [])
            check("priced_keywords=0",               facts["coverage"]["priced_keywords"], 0)
            check("opportunities=[]",                facts["low_difficulty_opportunities"], [])
        except Exception as exc:
            check(f"no crash: CRASHED with {exc}", False, True)

    # =========================================================================
    # TEST 6 -- date parsing from RUN_LOG; missing RUN_LOG → null
    # =========================================================================
    print("\n=== TEST 6: date parsing ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        facts = _extract(tmp, {})
        check("metrics scraped_date from A2 log",
              facts["sources"]["metrics"]["scraped_date"], "2026-07-15")
        check("serp scraped_date from B1 log",
              facts["sources"]["serp"]["scraped_date"],    "2026-07-16")

    with tempfile.TemporaryDirectory() as tmp:
        # No RUN_LOG files → null dates
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS)
        facts = _extract(tmp, {})
        check("null metrics date when A2 RUN_LOG absent",
              facts["sources"]["metrics"]["scraped_date"], None)
        check("null serp date when B1 RUN_LOG absent",
              facts["sources"]["serp"]["scraped_date"],    None)

    # =========================================================================
    # TEST 7 -- determinism: two runs on the same workdir → byte-identical JSON
    # =========================================================================
    print("\n=== TEST 7: determinism (byte-identical runs) ===")
    with tempfile.TemporaryDirectory() as tmp:
        _make_workdir(tmp,
                      keywords=SYN_KEYWORDS,
                      metrics=SYN_METRICS,
                      rankings=SYN_RANKINGS,
                      domain_map=SYN_DOMAIN_MAP,
                      a2_runlog=A2_LOG,
                      b1_runlog=B1_LOG)
        out1 = extract(tmp, {})
        blob1 = Path(out1).read_bytes()
        h1 = hashlib.sha256(blob1).hexdigest()

        out2 = extract(tmp, {})
        blob2 = Path(out2).read_bytes()
        h2 = hashlib.sha256(blob2).hexdigest()

        check("sha256 identical across two extract() calls", h1, h2)
        check("output is non-empty", len(blob1) > 0, True)

    # =========================================================================
    # Summary
    # =========================================================================
    print(f"\nOK: {PASS} checks passed" if FAIL == 0 else f"\nFAIL: {FAIL} failed, {PASS} passed")
    if FAIL:
        raise SystemExit(1)
