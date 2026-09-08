"""
Derived artifacts between stages — the "glue" that turns one stage's raw output
into the next stage's input. Called by run_phase.py after a full run.

Per-stage glue is built against a verified sample of each actor's actual output
(field names are not guessed):

  * A1 -> `keywords/keywords.json` (the expanded keyword universe A2/B1 consume);
    until A1 has run, A2/B1 fall back to seed_terms.
  * A2 -> `metrics/keyword_metrics.json` (normalized per-keyword volume/difficulty/CPC,
    source-tagged "semrush", nulls preserved exactly — no invented values).
  * B1 -> serp/rankings.json (per-keyword ranked organic results) +
    serp/domain_map.json (domain ownership across all keywords).

The helpers below implement that glue.
"""

import json
import re
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
    except Exception:  # noqa: BLE001
        return ""


def _pp_a1(stage_dir, workdir, cfg):
    """Extract the keyword universe from A1's raw autocomplete run.

    Seeds come first (preserving the user's order), then expanded suggestions
    sorted by relevance descending. Deduplication is case-insensitive; seeds
    win over any matching suggestion. Final list is capped at
    cfg["keyword_cap"] (default 300).
    """
    raw = _latest_run(stage_dir)

    # Normalize seed terms: strip, drop blanks, preserve order.
    seeds = [s.strip() for s in cfg.get("seed_terms", []) if s.strip()]

    # Collect valid suggestion records, keeping their original position for
    # stable tie-breaking within equal relevance values.
    suggestions = []
    for i, rec in enumerate(raw):
        if not isinstance(rec, dict):
            continue
        if rec.get("recordType") != "suggestion":
            continue
        text = rec.get("text", "")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        # Missing or non-numeric relevance sorts last (treat as 0).
        try:
            rel_val = int(rec.get("relevance", 0) or 0)
        except (TypeError, ValueError):
            rel_val = 0
        suggestions.append((rel_val, i, text))

    raw_count = len(suggestions)

    # Sort by relevance DESC; original index breaks ties (stable within same relevance).
    suggestions.sort(key=lambda t: (-t[0], t[1]))

    # Build the deduped result: seeds first, then suggestions.
    # First spelling seen (case-insensitive) wins.
    seen = set()
    result = []

    for kw in seeds:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            result.append(kw)

    for _rel, _idx, text in suggestions:
        key = text.lower()
        if key not in seen:
            seen.add(key)
            result.append(text)

    cap = int(cfg.get("keyword_cap", 300))
    capped = len(result) > cap
    result = result[:cap]

    # Write the file that A2 and B1 consume via stages._keyword_list().
    out_dir = Path(workdir) / "keywords"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "keywords.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cap_note = f" (capped at {cap})" if capped else ""
    print(
        f"A1 postprocess: {raw_count} raw suggestions in, "
        f"{len(result)} unique keywords out{cap_note}."
    )


def _pp_a2(stage_dir, workdir, cfg):  # noqa: ARG001 (cfg reserved for future cap/filter options)
    """Normalize A2's raw Semrush keyword-metrics records into a clean artifact.

    Reads the latest run from stage_dir, extracts 8 core metrics per keyword,
    and writes metrics/keyword_metrics.json in workdir.

    Sourcing-spine rule: nulls are preserved exactly as-is. We never coerce
    None -> 0 or invent a value. An LLM narrates later; it never counts. Only
    tag we add is source="semrush" — no wall-clock timestamps (non-deterministic).

    Output keys per row (in order):
      keyword, volume, cpc_usd, competition, keyword_difficulty,
      referring_domains_median, organic_results_count, source, database
    """
    raw = _latest_run(stage_dir)

    if not isinstance(raw, list):
        print(
            f"_pp_a2: WARNING: actor returned a non-list payload "
            f"({type(raw).__name__!r}); likely an error object — skipping"
        )
        return

    if not raw:
        print("_pp_a2: no records found — skipping (stage may not have run yet)")
        return

    normalized = []
    skipped = 0
    for rec in raw:
        if not isinstance(rec, dict):
            skipped += 1
            continue
        kw = rec.get("keyword")
        if not kw or not str(kw).strip():
            skipped += 1
            continue
        normalized.append({
            "keyword":                  str(kw),
            "volume":                   rec.get("volume"),
            "cpc_usd":                  rec.get("cpc_usd"),
            "competition":              rec.get("competition"),
            "keyword_difficulty":       rec.get("keyword_difficulty"),
            "referring_domains_median": rec.get("referring_domains_median"),
            "organic_results_count":    rec.get("organic_results_count"),
            "source":                   "semrush",
            "database":                 rec.get("database"),
        })

    out_dir = Path(workdir) / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "keyword_metrics.json"
    out_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    populated = sum(1 for r in normalized if r["volume"] is not None)
    null_count = len(normalized) - populated
    skip_msg = (
        f"; {skipped} records skipped (non-dict or missing keyword)" if skipped else ""
    )
    print(
        f"_pp_a2: {len(raw)} records in -> {len(normalized)} metric rows out; "
        f"{populated} populated, {null_count} no-data(null){skip_msg}"
    )


# Matches a valid parsed hostname: labels separated by dots, each label
# starting and ending with alphanumeric, at least two labels (requires a dot).
# Rejects social strings like '3,4k+ followers', '10+ comments' (no dot, special chars).
_DOMAIN_RE = re.compile(
    r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
)


def _is_valid_domain(d: str) -> bool:
    """Return True if d looks like a real hostname (has a dot, valid label chars).

    Rejects garbage strings that the actor emits in displayedUrl for social/video
    results — '3,4k+ followers', '10+ comments · 8 months ago', etc.
    Also rejects breadcrumb remnants; those are prevented by the caller stripping
    the displayedUrl to its first whitespace-delimited token before calling _domain().
    """
    return bool(d) and _DOMAIN_RE.match(d) is not None


def _pp_b1(stage_dir, workdir, cfg):  # noqa: ARG001 (cfg reserved for future cap/filter options)
    """Build per-keyword SERP rankings + domain-ownership map from B1 raw output.

    B1 stage uses apify/google-search-scraper. Each record in the raw JSON is one
    SERP page for one keyword; the keyword is record["searchQuery"]["term"].

    Outputs written under Path(workdir)/"serp"/):
      serp/rankings.json   — list of per-keyword objects with ranked organic results
      serp/domain_map.json — competitive ownership map, sorted by keyword coverage

    Sourcing-spine rules (FROZEN):
    - Domain is extracted from displayedUrl, NOT from url. The url field is a
      google.com/goto?url=... redirect; _domain(url) returns "google.com" — wrong.
    - Positions are preserved as-is from the actor (int). No re-ranking.
    - serp_features counts come from len() of actor-returned lists only.
      No keys are invented for features the actor does not expose as discrete fields
      (no featured_snippet, local_pack, AI_overview, etc.).
    - Nulls and missing fields are never invented; source tag is "google_serp".

    For domain_map aggregation:
    - keywords_owned counts DISTINCT keywords where the domain appears.
    - avg_position averages over ALL appearances (including multiple per keyword),
      giving a full-distribution mean rather than per-keyword-then-average.
    - Both choices are the simplest correct interpretation and are documented here.
    """
    raw = _latest_run(stage_dir)

    if not isinstance(raw, list):
        print(
            f"_pp_b1: WARNING: actor returned non-list ({type(raw).__name__!r}); "
            "likely an error object — skipping"
        )
        return

    if not raw:
        print("_pp_b1: no records found — skipping (stage may not have run yet)")
        return

    out_dir = Path(workdir) / "serp"
    out_dir.mkdir(parents=True, exist_ok=True)

    rankings = []
    # domain -> {"keywords": set of keyword strings, "positions": list of int}
    domain_appearances: dict = {}

    total_organic = 0  # count of type=="organic" items encountered
    total_skips = 0    # count skipped (empty displayedUrl OR invalid parsed domain)

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        sq = rec.get("searchQuery") or {}
        keyword = sq.get("term", "")
        if not keyword:
            continue

        organic_raw = rec.get("organicResults") or []
        paid_raw = rec.get("paidResults") or []
        paa_raw = rec.get("peopleAlsoAsk") or []
        related_raw = rec.get("relatedQueries") or []

        results = []
        for item in organic_raw:
            if not isinstance(item, dict):
                continue
            # Defensive type guard — fixture values are all "organic".
            if item.get("type") != "organic":
                continue
            total_organic += 1

            displayed = item.get("displayedUrl") or ""
            if not displayed:
                # Missing or empty displayedUrl: skip, count.
                total_skips += 1
                continue

            # Strip the breadcrumb suffix before parsing. The actor emits
            # displayedUrl values like 'https://www.trustpilot.com › ... › Plumber'
            # where the path is a human-readable breadcrumb, not a real URL path.
            # urlparse does NOT terminate netloc on spaces, so parsing the full string
            # gives netloc 'trustpilot.com › ... › plumber' (garbage). Taking the
            # first whitespace-delimited token strips the suffix before urlparse runs.
            token = displayed.split()[0] if displayed.split() else ""
            domain = _domain(token)
            # Guard: reject results whose token doesn't parse to a real hostname
            # (no dot, invalid chars) — catches social strings like '3,4K+' that
            # survive the empty check but have no valid domain.
            if not _is_valid_domain(domain):
                total_skips += 1
                continue

            pos = item.get("position")
            results.append({
                "position": pos,
                "domain": domain,
                "title": item.get("title") or "",
                "displayed_url": displayed,
            })

            if domain not in domain_appearances:
                domain_appearances[domain] = {"keywords": set(), "positions": []}
            domain_appearances[domain]["keywords"].add(keyword)
            if pos is not None:
                domain_appearances[domain]["positions"].append(pos)

        # Sort by position ascending (None positions sort last).
        results.sort(key=lambda r: (r["position"] if r["position"] is not None else 999999))

        rankings.append({
            "keyword": keyword,
            "source": "google_serp",
            "results": results,
            "serp_features": {
                # ONLY what this actor actually returns as discrete list fields.
                # Do NOT add featured_snippet, local_pack, AI_overview, etc. —
                # this actor does not expose them; adding them would be invented data.
                "paid_results": len(paid_raw),
                "people_also_ask": len(paa_raw),
                "related_queries": len(related_raw),
            },
        })

    # Build domain_map sorted by keywords_owned desc, then avg_position asc.
    domain_map = []
    for domain, data in domain_appearances.items():
        positions = data["positions"]
        avg_pos = round(sum(positions) / len(positions), 1) if positions else None
        best_pos = min(positions) if positions else None
        domain_map.append({
            "domain": domain,
            "keywords_owned": len(data["keywords"]),
            "avg_position": avg_pos,
            "best_position": best_pos,
        })
    # Deterministic sort: keywords_owned desc, then avg_position asc (None last).
    domain_map.sort(
        key=lambda d: (-(d["keywords_owned"]), d["avg_position"] if d["avg_position"] is not None else 999999)
    )

    (out_dir / "rankings.json").write_text(
        json.dumps(rankings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "domain_map.json").write_text(
        json.dumps(domain_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    unique_domains = len(domain_appearances)
    num_keywords = len(rankings)
    print(
        f"_pp_b1: {num_keywords} keywords, {total_organic} organic results parsed, "
        f"{unique_domains} unique domains; {total_skips} results skipped (empty or non-URL displayedUrl, e.g. social/video panels)"
    )


def postprocess(stage, stage_dir, workdir, cfg):
    """Route to the per-stage postprocess function."""
    if stage == "A1":
        _pp_a1(stage_dir, workdir, cfg)
        return
    if stage == "A2":
        _pp_a2(stage_dir, workdir, cfg)
        return
    if stage == "B1":
        _pp_b1(stage_dir, workdir, cfg)
        return
    # Other stages are no-ops until their phases implement them.


# ---------------------------------------------------------------------------
# Self-tests (run with: python3 scripts/postprocess.py)
# ---------------------------------------------------------------------------

def _selftest():
    import tempfile

    checks_passed = 0

    # ------------------------------------------------------------------
    # Batching partition correctness
    # Asserts that the chunking expression the engine uses never drops or
    # duplicates a keyword, never exceeds the cap, and produces the right
    # number of chunks — for edge-case sizes including 0, 1, 100, 101, 250, 999.
    # ------------------------------------------------------------------
    size = 100
    for n in (0, 1, 100, 101, 250, 999):
        arr = list(range(n))
        chunks = [arr[i:i + size] for i in range(0, max(len(arr), 1), size)]
        # right number of chunks (empty list produces 1 empty chunk via max guard)
        if n == 0:
            assert chunks == [[]], f"n=0: expected [[]], got {chunks}"
        else:
            expected_chunks = (n + size - 1) // size
            assert len(chunks) == expected_chunks, (
                f"n={n}: expected {expected_chunks} chunks, got {len(chunks)}"
            )
        # no keyword dropped or duplicated
        flat = [x for c in chunks for x in c]
        assert flat == arr, f"n={n}: flat != arr after chunking"
        # no chunk exceeds the cap
        assert all(len(c) <= size for c in chunks), f"n={n}: chunk exceeds cap"
        checks_passed += 1

    # ------------------------------------------------------------------
    # _pp_a2 behaviour tests — inline synthetic data, no fixture dependency
    # ------------------------------------------------------------------

    def _run_pp_a2(raw_records):
        """Helper: run _pp_a2 against an inline list, return the written rows."""
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td) / "stage"
            stage_dir.mkdir()
            workdir   = Path(td) / "work"
            workdir.mkdir()
            # Write a fake run_01_test.json so _latest_run picks it up.
            (stage_dir / "run_01_test.json").write_text(
                json.dumps(raw_records, ensure_ascii=False), encoding="utf-8"
            )
            _pp_a2(stage_dir, workdir, {})
            out = workdir / "metrics" / "keyword_metrics.json"
            return json.loads(out.read_text(encoding="utf-8")) if out.exists() else None

    # Case 1: populated record — all metrics non-null.
    populated_raw = [{
        "keyword": "project management software",
        "database": "us",
        "volume": 5400,
        "cpc_usd": 12.50,
        "competition": 0.72,
        "keyword_difficulty": 68,
        "referring_domains_median": 89,
        "organic_results_count": 142,
        # bulky fields that must be dropped
        "related_keywords": [{"keyword": "pm software", "volume": 200}],
        "questions": [],
        "serp": [],
    }]
    rows = _run_pp_a2(populated_raw)
    assert rows is not None and len(rows) == 1, "Case 1: expected 1 row"
    r = rows[0]
    assert r["keyword"] == "project management software"
    assert r["volume"] == 5400
    assert r["cpc_usd"] == 12.50
    assert r["competition"] == 0.72
    assert r["keyword_difficulty"] == 68
    assert r["referring_domains_median"] == 89
    assert r["organic_results_count"] == 142
    assert r["source"] == "semrush"
    assert r["database"] == "us"
    # bulky fields must NOT appear
    assert "related_keywords" not in r
    assert "questions" not in r
    assert "serp" not in r
    checks_passed += 1

    # Case 2: no-data record — cpc_usd/competition/keyword_difficulty=None, volume=0.
    # The row must survive (not be dropped); nulls stay None, volume stays 0.
    nodata_raw = [{
        "keyword": "agile sprint tool",
        "database": "us",
        "volume": 0,
        "cpc_usd": None,
        "competition": None,
        "keyword_difficulty": None,
        "referring_domains_median": None,
        "organic_results_count": None,
    }]
    rows = _run_pp_a2(nodata_raw)
    assert rows is not None and len(rows) == 1, "Case 2: expected 1 row (no-data must survive)"
    r = rows[0]
    assert r["volume"] == 0,                          "Case 2: volume must be 0, not None"
    assert r["cpc_usd"] is None,                      "Case 2: cpc_usd must remain None"
    assert r["competition"] is None,                  "Case 2: competition must remain None"
    assert r["keyword_difficulty"] is None,            "Case 2: keyword_difficulty must remain None"
    assert r["referring_domains_median"] is None,      "Case 2: referring_domains_median must remain None"
    assert r["organic_results_count"] is None,         "Case 2: organic_results_count must remain None"
    assert r["source"] == "semrush"
    checks_passed += 1

    # Case 3: non-dict element in raw list -> skipped, no crash.
    mixed_raw = [
        "not a dict",
        42,
        None,
        {"keyword": "real keyword", "volume": 10, "database": "us"},
    ]
    rows = _run_pp_a2(mixed_raw)
    assert rows is not None and len(rows) == 1, (
        f"Case 3: expected 1 row (non-dicts skipped), got {len(rows) if rows else None}"
    )
    assert rows[0]["keyword"] == "real keyword"
    checks_passed += 1

    # Case 4: record missing 'keyword' field -> skipped.
    missing_kw_raw = [
        {"volume": 100, "database": "us"},          # no keyword key
        {"keyword": "", "volume": 200, "database": "us"},   # empty keyword
        {"keyword": "valid kw", "volume": 300, "database": "us"},
    ]
    rows = _run_pp_a2(missing_kw_raw)
    assert rows is not None and len(rows) == 1, (
        f"Case 4: expected 1 row (missing/empty keyword skipped), got {len(rows) if rows else None}"
    )
    assert rows[0]["keyword"] == "valid kw"
    checks_passed += 1

    # Case 5: determinism — run normalization twice on the same input,
    # assert byte-identical JSON output.
    determ_raw = [
        {"keyword": "task tracker", "volume": 140, "cpc_usd": 9.24,
         "competition": 0.39, "keyword_difficulty": 51,
         "referring_domains_median": 34, "organic_results_count": 134,
         "database": "us"},
        {"keyword": "kanban board", "volume": 390, "cpc_usd": 8.20,
         "competition": 0.43, "keyword_difficulty": 31,
         "referring_domains_median": 4, "organic_results_count": 151,
         "database": "us"},
    ]
    rows_a = _run_pp_a2(determ_raw)
    rows_b = _run_pp_a2(determ_raw)
    json_a = json.dumps(rows_a, ensure_ascii=False, indent=2)
    json_b = json.dumps(rows_b, ensure_ascii=False, indent=2)
    assert json_a == json_b, "Case 5: two runs produced different JSON (not deterministic)"
    checks_passed += 1

    # ------------------------------------------------------------------
    # _pp_b1 behaviour tests — inline synthetic SERP data, no fixture dependency
    # ------------------------------------------------------------------

    def _run_pp_b1(raw_records):
        """Helper: run _pp_b1 against an inline list, return (rankings, domain_map)."""
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td) / "stage"
            stage_dir.mkdir()
            workdir = Path(td) / "work"
            workdir.mkdir()
            (stage_dir / "run_01_test.json").write_text(
                json.dumps(raw_records, ensure_ascii=False), encoding="utf-8"
            )
            _pp_b1(stage_dir, workdir, {})
            serp_dir = workdir / "serp"
            rankings = json.loads((serp_dir / "rankings.json").read_text("utf-8"))
            domain_map = json.loads((serp_dir / "domain_map.json").read_text("utf-8"))
            return rankings, domain_map

    # Synthetic SERP records — two keywords, overlapping domain (bestdrains.com).
    # Record 2 also tests: google.com/goto url with real displayedUrl, and empty displayedUrl.
    _SYNTH_SERP = [
        {
            "searchQuery": {"term": "plumber near me"},
            "organicResults": [
                {
                    "type": "organic",
                    "position": 1,
                    "title": "Best Drains",
                    # url is a google redirect — using _domain(url) gives "google.com"
                    # but we use displayedUrl instead → "bestdrains.com"
                    "url": "https://www.google.com/goto?url=XXXXXXXXXXX",
                    "displayedUrl": "https://www.bestdrains.com",
                },
                {
                    "type": "organic",
                    "position": 2,
                    "title": "Acme Plumbing",
                    "url": "https://www.google.com/goto?url=YYYYYYYYYYY",
                    "displayedUrl": "https://www.acmeplumbing.com",
                },
                {
                    "type": "organic",
                    "position": 3,
                    "title": "Yelp",
                    "url": "https://www.google.com/goto?url=ZZZZZZZZZZZ",
                    "displayedUrl": "https://www.yelp.com",
                },
            ],
            "paidResults": [{"title": "Paid Ad"}],
            "peopleAlsoAsk": [{"question": "Q1"}, {"question": "Q2"}],
            "relatedQueries": [{"text": "related 1"}],
        },
        {
            "searchQuery": {"term": "emergency plumber"},
            "organicResults": [
                {
                    # proves displayedUrl is used, not url (url is a google redirect)
                    "type": "organic",
                    "position": 1,
                    "title": "Example Plumbing Co",
                    "url": "https://www.google.com/goto?url=AAAAAAAAAAA",
                    "displayedUrl": "https://www.example.com",
                },
                {
                    # bestdrains.com appears in both keywords → keywords_owned == 2
                    "type": "organic",
                    "position": 2,
                    "title": "Best Drains Emergency",
                    "url": "https://www.google.com/goto?url=BBBBBBBBBBB",
                    "displayedUrl": "https://www.bestdrains.com",
                },
                {
                    # empty displayedUrl → skipped and counted
                    "type": "organic",
                    "position": 3,
                    "title": "Result with no displayedUrl",
                    "url": "https://www.google.com/goto?url=CCCCCCCCCCC",
                    "displayedUrl": "",
                },
            ],
            "paidResults": [],
            "peopleAlsoAsk": [],
            "relatedQueries": [],
        },
    ]

    # Case B1-1: overlapping domain → domain_map counts distinct keywords and correct avg.
    rankings, domain_map = _run_pp_b1(_SYNTH_SERP)
    assert len(rankings) == 2, f"B1-1: expected 2 keyword entries, got {len(rankings)}"
    bestdrains = next((d for d in domain_map if d["domain"] == "bestdrains.com"), None)
    assert bestdrains is not None, "B1-1: bestdrains.com missing from domain_map"
    assert bestdrains["keywords_owned"] == 2, (
        f"B1-1: bestdrains.com should own 2 distinct keywords, got {bestdrains['keywords_owned']}"
    )
    # avg_position: pos 1 (plumber near me) + pos 2 (emergency plumber) → (1+2)/2 = 1.5
    assert bestdrains["avg_position"] == 1.5, (
        f"B1-1: expected avg_position 1.5, got {bestdrains['avg_position']}"
    )
    assert bestdrains["best_position"] == 1, (
        f"B1-1: expected best_position 1, got {bestdrains['best_position']}"
    )
    checks_passed += 1

    # Case B1-2: displayedUrl is used, NOT url (google.com/goto redirect).
    # The second keyword's first result has a google.com/goto url but
    # displayedUrl = https://www.example.com → domain must be example.com.
    kw2 = rankings[1]
    assert kw2["keyword"] == "emergency plumber", (
        f"B1-2: unexpected keyword {kw2['keyword']!r}"
    )
    first_result = kw2["results"][0]
    assert first_result["domain"] == "example.com", (
        f"B1-2: expected domain 'example.com' (from displayedUrl), "
        f"got {first_result['domain']!r}; "
        "'google.com' would mean url was used instead of displayedUrl — wrong"
    )
    checks_passed += 1

    # Case B1-3: empty displayedUrl is skipped (position 3 of kw2).
    # kw2 has 3 organic entries; position 3 has empty displayedUrl → only 2 in output.
    assert len(kw2["results"]) == 2, (
        f"B1-3: expected 2 results (1 skipped due to empty displayedUrl), "
        f"got {len(kw2['results'])}"
    )
    checks_passed += 1

    # Case B1-4: serp_features counts come from actor lists only.
    kw1 = rankings[0]
    assert kw1["serp_features"]["paid_results"] == 1, "B1-4: paid_results"
    assert kw1["serp_features"]["people_also_ask"] == 2, "B1-4: people_also_ask"
    assert kw1["serp_features"]["related_queries"] == 1, "B1-4: related_queries"
    # No invented feature keys.
    unexpected_keys = set(kw1["serp_features"]) - {"paid_results", "people_also_ask", "related_queries"}
    assert not unexpected_keys, f"B1-4: unexpected serp_features keys: {unexpected_keys}"
    checks_passed += 1

    # Case B1-5: determinism — two runs produce byte-identical JSON.
    r1, dm1 = _run_pp_b1(_SYNTH_SERP)
    r2, dm2 = _run_pp_b1(_SYNTH_SERP)
    json1_r = json.dumps(r1, ensure_ascii=False, indent=2)
    json2_r = json.dumps(r2, ensure_ascii=False, indent=2)
    assert json1_r == json2_r, "B1-5: rankings.json is not deterministic across runs"
    json1_dm = json.dumps(dm1, ensure_ascii=False, indent=2)
    json2_dm = json.dumps(dm2, ensure_ascii=False, indent=2)
    assert json1_dm == json2_dm, "B1-5: domain_map.json is not deterministic across runs"
    checks_passed += 1

    # Case B1-6: breadcrumb displayedUrl — regression lock.
    # 'https://www.trustpilot.com › ... › Plumber' must extract to 'trustpilot.com',
    # NOT be dropped (old bug: urlparse returned garbage netloc, _is_valid_domain rejected it).
    # Also covers en.wikipedia.org-style subdomains — subdomain must be preserved.
    _BREADCRUMB_SERP = [
        {
            "searchQuery": {"term": "plumber reviews"},
            "organicResults": [
                {
                    "type": "organic",
                    "position": 1,
                    "title": "Trustpilot Plumbers",
                    "url": "https://www.google.com/goto?url=XXX",
                    "displayedUrl": "https://www.trustpilot.com › ... › Plumber",
                },
                {
                    "type": "organic",
                    "position": 2,
                    "title": "Wikipedia",
                    "url": "https://www.google.com/goto?url=YYY",
                    "displayedUrl": "https://en.wikipedia.org › wiki › Plumber",
                },
            ],
            "paidResults": [],
            "peopleAlsoAsk": [],
            "relatedQueries": [],
        },
    ]
    rankings_bc, domain_map_bc = _run_pp_b1(_BREADCRUMB_SERP)
    assert len(rankings_bc) == 1, f"B1-6: expected 1 keyword, got {len(rankings_bc)}"
    bc_results = rankings_bc[0]["results"]
    assert len(bc_results) == 2, (
        f"B1-6: expected 2 results (both breadcrumb URLs should be kept), "
        f"got {len(bc_results)} — breadcrumb stripping may be broken"
    )
    domains_bc = {r["domain"] for r in bc_results}
    assert "trustpilot.com" in domains_bc, (
        f"B1-6: trustpilot.com missing — breadcrumb suffix not stripped before parse; "
        f"got domains {domains_bc}"
    )
    assert "en.wikipedia.org" in domains_bc, (
        f"B1-6: en.wikipedia.org missing — subdomain must be preserved; "
        f"got domains {domains_bc}"
    )
    checks_passed += 1

    print(f"OK: {checks_passed} checks passed")


if __name__ == "__main__":
    _selftest()
