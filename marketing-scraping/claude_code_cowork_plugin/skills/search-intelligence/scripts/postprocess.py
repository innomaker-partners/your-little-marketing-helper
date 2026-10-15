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


# The notice the keyword-ideas step leaves next to its output when what came back
# was not what was asked for. search_intel reads it into the run's gap
# list, which the closing lines and the unattended run's notification print.
# It is a file of its own, never .run_notice.json: that one means "the scraper said
# it is not working", and its reader words it that way.
A1_ROWS_NOTICE = ".a1_rows_notice.json"


def _write_a1_rows_notice(stage_dir, unreadable, region):
    """Write <stage>/.a1_rows_notice.json when the step has something to say, and
    remove a stale one otherwise (a --force re-run that was fine). The sentences
    are made once, here, where the counts are known; search_intel only repeats them."""
    p = Path(stage_dir) / A1_ROWS_NOTICE
    if not unreadable and not region:
        try:
            p.unlink()
        except OSError:
            pass
        return
    doc = {}
    if unreadable:
        doc["unreadable"] = unreadable
    if region:
        doc["region"] = region
    try:
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    for text in (unreadable, region):
        if text:
            print(f"  \u26a0 Keyword ideas: {text}.")


def _region_notice(asked, countries):
    """The sentence for rows that name another country than the one asked for, or
    None. asked: the region the call was sent with (stages._iso_country, uppercase).
    countries: the `country` of each new-shape row that carries one. A row with no
    country says nothing either way and is not counted. Counts are exact; the
    country codes are the ones the rows carry, never turned into names."""
    seen = [c for c in countries if c]
    other = {}
    for c in seen:
        if c != asked:
            other[c] = other.get(c, 0) + 1
    if not other:
        return None
    n_other = sum(other.values())
    if len(other) == 1:
        named = next(iter(other))
    else:
        named = " and ".join(f"{c} ({k} row{'' if k == 1 else 's'})"
                         for c, k in sorted(other.items()))
    if n_other == len(seen):
        return (f"you asked for {asked}, but all {len(seen)} keyword idea rows are for "
                f"{named}, so these ideas are not for the market you asked for")
    return (f"you asked for {asked}, but {n_other} of the {len(seen)} keyword idea rows that "
            f"name a country are for {named}, not {asked}, so the list mixes markets")


def _pp_a1(stage_dir, workdir, cfg):
    """Extract the keyword universe from A1's raw autocomplete run.

    Seeds come first (preserving the user's order), then expanded suggestions
    sorted by relevance descending. Deduplication is case-insensitive; seeds
    win over any matching suggestion. Final list is capped at
    cfg["keyword_cap"] (default 300).

    Two things the step used to leave unsaid are now said: rows came
    back but none could be read (the run goes on with the seeds, and says so), and
    rows are marked for another country than the one asked for.
    """
    raw = _latest_run(stage_dir)

    # Normalize seed terms: strip, drop blanks, preserve order.
    seeds = [s.strip() for s in cfg.get("seed_terms", []) if s.strip()]

    # Collect valid suggestion records, keeping their original position for
    # stable tie-breaking within equal relevance values.
    suggestions = []
    countries = []  # `country` of each new-shape row that names one (region check)
    for i, rec in enumerate(raw):
        if not isinstance(rec, dict):
            continue
        # Two row shapes are read, decided per row (never "accept everything"):
        #   old, recorded runs of the withdrawn actor and the fixtures built from
        #   them: recordType "suggestion", the text in `text`;
        #   new, memo23/google-suggest-scraper: NO recordType, the
        #   text in `suggestion` (the seed in `seedKeyword`, the rank in
        #   `relevance`).
        # WHY both: runs already saved in users' folders hold the old shape and
        # are re-read by the app and by repeats. WHY a row is chosen by its own
        # shape: the new rows carry no recordType, so the old test alone dropped
        # every one of them and the step reported 0 keyword ideas while billing
        # for them. A row that is neither shape is still dropped, as before.
        #   A row that carries both (recordType "suggestion" AND a non-empty
        #   `suggestion`, no `text`) is read as the new shape: the new field wins
        #   whenever it is present and non-empty (the old test came first
        #   and read the empty `text`, dropping the row). A row with only the old
        #   fields is read exactly as before.
        new_shape = isinstance(rec.get("suggestion"), str) and rec["suggestion"].strip()
        if new_shape:
            text = rec["suggestion"]
        elif rec.get("recordType") == "suggestion":
            text = rec.get("text", "")
        else:
            continue
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
        if new_shape:
            # Only the new actor's rows say which market they are for; the old
            # recorded rows carry a `country` that was never the market asked for.
            countries.append(str(rec.get("country") or "").strip().upper())

    raw_count = len(suggestions)

    # Rows came back and none could be read: the next steps would run on the seeds
    # alone while the run reports nothing. Said plainly, with the count; the run goes
    # on (the same as a volumes or results step that
    # ran and returned nothing, which search_intel records as a gap).
    unreadable = None
    n_rows = len(raw) if isinstance(raw, list) else (1 if raw else 0)
    if n_rows and not suggestions:
        unreadable = (f"the scraper returned {n_rows} row{'' if n_rows == 1 else 's'}, but the "
                      f"tool could not read any of them, so only your own seed terms are used "
                      f"for the next steps")
    region = None
    if suggestions:
        # The region the call was sent with, by the function build_A1 uses (UK -> GB).
        # Not caught on purpose: a check that cannot run must fail loudly, not skip.
        from stages import _iso_country
        asked = _iso_country(cfg.get("country_code"))
        if asked:
            region = _region_notice(asked, countries)
    _write_a1_rows_notice(stage_dir, unreadable, region)

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
        f"Keyword ideas postprocess: {raw_count} raw suggestions in, "
        f"{len(result)} unique keywords out{cap_note}."
    )


def _drop_stale_outputs(base, rel_paths, label, what):
    """Remove the derived files a step writes, and say so, when the step's latest
    result gave nothing to write them from. One rule for every step that cleans its
    raw result into a file of its own (generalised from _drop_stale_metrics; the convention is
    one helper, not one per step).

    base: the folder the paths are under (the run folder, or the step's folder).
    rel_paths: this step's own derived files, relative to base. Never a raw
        run_*.json: those are the step's result as it stands, and are not touched.
    label: the step's function name, for the console line. what: the plain name of
        what was removed ("keyword volumes", "search results").

    WHY: each of those files is the step's cleaned copy of its raw result and
    nothing else. When the step is run again in the same folder (--force, a resume)
    and returns nothing or an error, the step has nothing to write, and an earlier
    run's files would stay in place, looking like this run's. The report, the app
    and the month comparison read them without asking which run wrote them. Removing
    them keeps one rule true: a derived file is there only when it was written from
    the step's result as it stands in this folder. A step that ran and found
    nothing, honestly, still writes its own empty file (that is not this path), and a
    step that is not run again keeps its files (this is only called by the step
    itself, after a run)."""
    removed = False
    for rel in rel_paths:
        p = Path(base) / rel
        try:
            p.unlink()
        except FileNotFoundError:
            continue  # nothing left over: the usual case
        except OSError as exc:
            print(f"{label}: WARNING: could not remove the {what} an earlier run "
                  f"left in this folder ({exc}); they may be old")
            continue
        removed = True
    if removed:
        print(f"{label}: removed the {what} an earlier run left in this folder, "
              "so they cannot be mistaken for this run's")


def _drop_stale_metrics(workdir):
    """The volumes step's file. The Google search-results step picks its
    keywords by the volumes in it (stages.serp_keywords), so an old file would make
    it pull, and bill, for the "highest-volume" keywords of an older run, with no
    note. With the file gone the step takes the keywords in list order and says so
    (stages.serp_selection_note)."""
    _drop_stale_outputs(workdir, ["metrics/keyword_metrics.json"], "_pp_a2",
                        "keyword volumes")


def _drop_stale_serp(workdir):
    """The search-results step's two files: serp/rankings.json and
    serp/domain_map.json. They are written together, so they go together. The report
    facts, the app's index and the month comparison all read them, so old ones would
    show an earlier run's rankings and competitors as this run's."""
    _drop_stale_outputs(workdir, ["serp/rankings.json", "serp/domain_map.json"], "_pp_b1",
                        "search results")


# --- a Semrush row that carries no data --------------------
# A paid run got "kanban board tools microsoft" back with cost per click, competition and
# 12-month trend all empty and volume 0; the tool showed "0 searches" and counted it as
# priced. Volume cannot tell "no data" from "nobody searches" (4 of the 6 real no-data
# rows carry 0, 2 carry null), so the rule looks at the other three fields too. But a volume
# above 0 is a measurement and always wins ("if we have data, it should
# get saved, and only the zero data should be retried"): no data means NO volume (0 or
# empty) AND the other three fields empty. In all 67 recorded real rows no row with a volume
# above 0 has the three fields empty, so this changes no recorded answer.
NO_DATA_FIELDS = ("cpc_usd", "competition", "volume_trend_12mo")


def semrush_no_data(row):
    """True when a Semrush keyword row carries no data: its volume is 0 or empty AND its cost per
    click, competition and 12-month trend are all empty. A volume above 0 is measured, whatever
    else is empty. ONE rule for every counter and every place a volume is shown. `row` is the
    scraper's own record (all three fields present, null when empty) or a cleaned row, which
    carries the answer as "no_data" (_pp_a2 works it out from the raw record, which has the
    trend). An ABSENT field is not a null: a row lacking the three fields is not judged here."""
    if not isinstance(row, dict):
        return False
    if row.get("volume") not in (None, 0):   # A real volume wins, before the
        return False                         # cleaned row's own flag, which an old build may have got wrong
    if "no_data" in row:
        return row["no_data"] is True
    return all(k in row and row[k] is None for k in NO_DATA_FIELDS)


def judge_keyword_rows(rows, workdir):
    """The cleaned rows of metrics/keyword_metrics.json, each judged by the one rule.

    A folder made by an earlier version has cleaned rows with no "no_data" key, and a cleaned
    row never
    keeps the trend, so semrush_no_data cannot judge it from the row: it answers "measured" and a
    report, the extractor and the search-results ranking showed "0" where the app (which also
    reads the raw record) said "no data". The raw record (A2_keyword_metrics) always carries the
    trend: in all 577 recorded Semrush rows (a real run and the recorded folders) the
    key is present, null when empty. So the raw row is asked, by keyword, for every cleaned row
    that does not already say. A real zero keeps its 0 (the raw row has a cost per click). With no
    raw record to ask, a row is left exactly as it was: absent is not null. Every reader of the
    cleaned file calls this, so every surface gives one answer."""
    if not isinstance(rows, list):
        return rows
    open_rows = [r for r in rows if isinstance(r, dict) and "no_data" not in r]
    if not open_rows:
        return rows
    try:
        raw = _latest_run(Path(workdir) / "A2_keyword_metrics")
    except (OSError, ValueError):
        return rows
    if not isinstance(raw, list):
        return rows
    by_kw = {rr["keyword"]: rr for rr in raw if isinstance(rr, dict) and rr.get("keyword")}
    return [{**r, "no_data": True} if isinstance(r, dict) and "no_data" not in r
            and semrush_no_data(by_kw.get(r.get("keyword"))) else r for r in rows]


def is_empty_item(stage, item):
    """True when `item` is one its step may ask for once more: a volumes row with no data,
    or a search-results page marked "#error": true (UNEXERCISED: no real page has had it)."""
    if stage == "A2":
        return semrush_no_data(item)
    return stage == "B1" and isinstance(item, dict) and item.get("#error") is True


def item_key(stage, item):
    """What the step was asked for to get this item (lowercase, trimmed); "" if it does not say."""
    if not isinstance(item, dict):
        return ""
    sq = item.get("searchQuery")
    v = item.get("keyword") if stage == "A2" else (sq.get("term") if isinstance(sq, dict) else None)
    return str(v or "").strip().lower()


def empty_item_keys(stage, items):
    """The keys (first spelling kept) of a result's empty items, in order, without repeats."""
    seen, out = set(), []
    for it in items if isinstance(items, list) else []:
        k = item_key(stage, it)
        if k and k not in seen and is_empty_item(stage, it):
            seen.add(k)
            out.append(str(it["keyword"] if stage == "A2" else it["searchQuery"]["term"]))
    return out


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
            f"({type(raw).__name__!r}); likely an error object, skipping"
        )
        _drop_stale_metrics(workdir)
        return

    if not raw:
        print("_pp_a2: no records found, skipping (the step may not have run yet)")
        _drop_stale_metrics(workdir)
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
        row = {
            "keyword":                  str(kw),
            "volume":                   rec.get("volume"),
            "cpc_usd":                  rec.get("cpc_usd"),
            "competition":              rec.get("competition"),
            "keyword_difficulty":       rec.get("keyword_difficulty"),
            "referring_domains_median": rec.get("referring_domains_median"),
            "organic_results_count":    rec.get("organic_results_count"),
            "source":                   "semrush",
            "database":                 rec.get("database"),
        }
        # Worked out from the raw record (it has the trend) and passed forward;
        # the key exists only when True, so every other row is what it was.
        if semrush_no_data(rec):
            row["no_data"] = True
        normalized.append(row)

    out_dir = Path(workdir) / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "keyword_metrics.json"
    out_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # A no-data row is counted apart (with volume 0 it used to count as populated).
    no_data = sum(1 for r in normalized if semrush_no_data(r))
    populated = sum(1 for r in normalized if r["volume"] is not None and not semrush_no_data(r))
    null_count = len(normalized) - populated - no_data
    skip_msg = (
        f"; {skipped} records skipped (non-dict or missing keyword)" if skipped else ""
    )
    print(
        f"_pp_a2: {len(raw)} records in -> {len(normalized)} metric rows out; "
        f"{populated} populated, {no_data} no data from Semrush, {null_count} with no volume"
        f"{skip_msg}"
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
            "likely an error object, skipping"
        )
        _drop_stale_serp(workdir)
        return

    if not raw:
        print("_pp_b1: no records found, skipping (the step may not have run yet)")
        _drop_stale_serp(workdir)
        return

    out_dir = Path(workdir) / "serp"
    out_dir.mkdir(parents=True, exist_ok=True)

    rankings = []
    # domain -> {"keywords": set of keyword strings, "positions": list of int}
    domain_appearances: dict = {}

    total_organic = 0  # count of type=="organic" items encountered
    total_skips = 0    # count skipped (empty displayedUrl OR invalid parsed domain)

    error_pages = 0    # Pages the scraper marked as an error (kept out below)
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        # A page marked "#error": true has no results; left in, it counted as covered.
        if rec.get("#error") is True:
            error_pages += 1
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
        # The count that is KEPT leads. The old line said "25 organic results
        # parsed ... 2 results skipped" while 23 were kept, so a reader took 25 for the
        # number in the files. Every organic item is either kept or skipped.
        f"_pp_b1: {num_keywords} keywords, {total_organic - total_skips} organic results kept "
        f"({total_organic} found, {total_skips} skipped as empty or non-URL displayedUrl, "
        f"e.g. social/video panels), {unique_domains} unique domains"
        + (f"; {error_pages} error page{'' if error_pages == 1 else 's'} left out" if error_pages else "")
    )


# --- the steps of a run that did not finish ----------------------
# The same block, word for word, in the postprocess.py of all three tools (a test
# pins that): it reads the records run_phase already wrote and nothing else.

_ENDED_HOW = {"ABORTED": "was stopped", "FAILED": "failed",
              "TIMED-OUT": "ran out of time", "TIMED_OUT": "ran out of time"}


def _record_of(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def only_error_items(items):
    """A sentence for a step whose dataset is nothing but error items, else None.
    Apify reports a failed request as an item {"#error": true,
    "#debug": {"errorMessage": ...}} inside the dataset; a good item carries
    "#error": false (recorded search results do). Such a dataset is a failed step, never
    an empty answer. A dataset with some good items among the error items is not this
    (it is treated as the results it holds)."""
    if not isinstance(items, list) or not items:
        return None
    if not all(isinstance(i, dict) and i.get("#error") is True for i in items):
        return None
    dbg = items[0].get("#debug")
    why = dbg.get("errorMessage") if isinstance(dbg, dict) else None
    why = f" ({str(why).strip()[:150]})" if why and str(why).strip() else ""
    return f"the scraper returned only an error and no results{why}"


def unfinished_steps(workdir):
    """The steps of this run that did not finish, each as one plain sentence:
    [{"step": <name>, "sentence": <text>}] in step order, [] when every step that ran
    finished.

    WHY: a step that failed, was stopped by the budget cap, or whose scraper run did
    not finish left no results, and everything downstream (the index, the written
    report's facts, the app, the month comparison) then read that as "found nothing".
    run_phase already records each such step next to its output
    (.run_incomplete.json, .run_notice.json); the closing lines of the entry script
    name them. This reads the same records once, so that every other place repeats one
    sentence and none of them works it out again. An honest empty result (the step ran
    and found nothing) has no record and is not listed; a step the settings leave out
    is not listed either (the closing lines say that).

    The sentence for a failed or budget-stopped step also says that any results shown
    for it are an earlier run's, when the folder still holds a finished result file for
    it: the failed step wrote none of its own. A step whose record says it saved its
    result before it stopped (result_saved) is the exception: the result in
    the folder is this run's, and the sentence says it may be incomplete.

    A step a later run skipped carries the record kind
    "kept_after_failed_attempt" (run_phase._note_kept_after_failed_attempt). It is not
    an unfinished step: its finished result is in the folder, and the sentence says
    the latest attempt to fetch it again failed. It is listed under "kept_step", not
    "step": the month comparison takes "step" to mean records may be missing."""
    import stages as _stages
    out = []
    for key, spec in _stages.STAGES.items():
        d = Path(workdir) / spec["folder"]
        name = spec.get("name") or key
        inc = _record_of(d / ".run_incomplete.json") or {}
        by_kind = {}
        for e in inc.get("entries") or []:
            if isinstance(e, dict):
                by_kind.setdefault(e.get("kind"), e)
        earlier = (" Any results shown for this step are from an earlier run."
                   if (d / "run_02_full.json").exists() else "")
        missing = " That is not the same as finding nothing."
        k = by_kind.get("kept_after_failed_attempt")
        if k:
            if k.get("result_saved"):
                s = (f"{name}: the latest attempt to fetch it again saved its results and "
                     f"then stopped with an error, so what is shown for it comes from that "
                     f"attempt and may be incomplete.")
            else:
                why = ("stopped with an error" if k.get("was") != "budget_stop" else
                       "started, but the run reached the budget cap in your settings "
                       "part-way through it and nothing it collected was kept"
                       if k.get("part_way") else
                       "did not run, because the run reached the budget cap in your settings")
                s = (f"{name}: the latest attempt to fetch it again {why}, so the results "
                     f"shown for it were kept from an earlier fetch.")
            out.append({"kept_step": name, "sentence": s})
            continue
        if (by_kind.get("step_failed") or {}).get("result_saved"):
            s = (f"{name} stopped with an error after it had saved this run's results, "
                 f"so what is shown for it comes from this run but may be incomplete.")
        elif "step_failed" in by_kind:
            s = (f"{name} stopped with an error before it finished, so its results "
                 f"are missing from this run.{missing}{earlier}")
        elif "budget_stop" in by_kind:
            if by_kind["budget_stop"].get("part_way"):
                s = (f"{name} started, but the run reached the budget cap in your "
                     f"settings part-way through it, and nothing it collected was "
                     f"kept, so its results are missing from this run.{missing}{earlier}")
            else:
                s = (f"{name} did not run, because the run reached the budget cap in "
                     f"your settings, so its results are missing from this "
                     f"run.{missing}{earlier}")
        elif "run_ended" in by_kind:
            e = by_kind["run_ended"]
            how = _ENDED_HOW.get(str(e.get("status")), "did not finish")
            s = (f"{name}: the scraper run {how} before it finished, so what it "
                 f"collected is partial.{missing}" if e.get("records")
                 else f"{name}: the scraper run {how} before it finished and "
                      f"collected nothing, so its results are missing from this "
                      f"run.{missing}")
        elif _record_of(d / ".run_notice.json"):
            s = (f"{name}: the scraper reported that it did not work, so its "
                 f"results are missing from this run.{missing}")
        else:
            continue
        out.append({"step": name, "sentence": s})
    return out


def drop_stage_outputs(stage, stage_dir, workdir):  # noqa: ARG001 (same signature in all three tools)
    """Remove the files an earlier run's step left behind, for a step that ended
    without a result in this run (run_phase calls it where a step is not
    run, fails, or is stopped, and only when this run would have run it again). The
    step's raw run files are its result as it stands and are never touched.

    The volumes step's file is read by the search-results step to pick its keywords,
    and the search-results step's two files are read by the report, the app and the
    month comparison: an old one would be shown as this run's. The keyword ideas step
    keeps its list: later steps fall back on the seed terms only when it is missing,
    and a list is not shown as a finding."""
    if stage == "A2":
        _drop_stale_metrics(workdir)
    elif stage == "B1":
        _drop_stale_serp(workdir)


_REBUILT = {"A2": (["metrics/keyword_metrics.json"], "keyword volumes"),
            "B1": (["serp/rankings.json", "serp/domain_map.json"], "search results")}


def _retry_stale(stage_dir, clear=False):
    """True when run_phase left "stale_files" in the retry record: a kill while the second
    request's answers were being saved, so the files may be behind the result.
    clear=True removes the mark (the files were just built from the result)."""
    p = Path(stage_dir) / ".empty_retry.json"
    doc = _record_of(p) or {}
    if clear and doc.pop("stale_files", None):
        try:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass
    return doc.get("stale_files") is True


def _rebuild_gate(stage, stage_dir, workdir):
    """(files, plain name) when the step's files are missing and its record says its
    latest attempt failed or was stopped (a record a later run re-labelled counts), or when a
    second request left the files behind its answers, else None."""
    own = _REBUILT.get(stage)
    if own and _retry_stale(stage_dir):
        return own
    if not own or all((Path(workdir) / r).exists() for r in own[0]):
        return None
    entries = (_record_of(Path(stage_dir) / ".run_incomplete.json") or {}).get("entries")
    if not any(isinstance(e, dict) and e.get("kind") in
               ("step_failed", "budget_stop", "kept_after_failed_attempt")
               for e in entries or []):
        return None
    return own


def _holds_result_to_build_from(stage_dir):
    """True when the kept result is not nothing and not nothing but errors."""
    kept = _latest_run(stage_dir)
    return isinstance(kept, list) and bool(kept) and not only_error_items(kept)


def will_rebuild_dropped_outputs(stage, stage_dir, workdir):
    """True when rebuild_dropped_outputs would build this step's files again: the same
    test, not a second one. The search closing promises the rebuild
    "at no charge" only when this is True; before, it promised it for every kept
    result, including one of nothing and a step that has no files to build."""
    if not _rebuild_gate(stage, stage_dir, workdir):
        return False
    try:
        return _holds_result_to_build_from(stage_dir)
    except Exception:  # noqa: BLE001 - the rebuild would report it and build nothing
        return False


def rebuild_dropped_outputs(stage, stage_dir, workdir, cfg):
    """Build again the files drop_stage_outputs took out of a finished step, from the
    result the folder kept. Called by run_phase for a step it skips
    as already complete; it spends nothing.

    WHY: a --force run of the step that failed or hit the budget cap removed the step's
    files and kept its earlier result (run_02_full.json). A plain run of the folder then
    skipped the step, as the closing lines said it would, and nothing built the files
    again: the app, the report facts and the comparison showed no rankings for a step
    whose result was right there. Only a step with a record of that failed or stopped
    attempt is rebuilt: a step the settings leave out, or one with no inputs yet, was
    emptied on purpose and has no record, and stays as it is. A kept result of nothing, or
    of nothing but errors, has nothing to build from."""
    own = _rebuild_gate(stage, stage_dir, workdir)
    if not own:
        return
    try:
        if not _holds_result_to_build_from(stage_dir):
            return
        postprocess(stage, stage_dir, workdir, cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"   could not build the {own[1]} again from the result this folder kept "
              f"({exc}); they stay missing")
        return
    _retry_stale(stage_dir, clear=True)
    print(f"   built the {own[1]} again from the result this folder kept; nothing was charged")


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
