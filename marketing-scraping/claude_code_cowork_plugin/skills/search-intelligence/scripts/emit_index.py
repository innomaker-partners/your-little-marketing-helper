"""
emit_index.py -- write frontend_index.json for a search-intelligence run.

The Local Viewer contract (see marketing-viewer/references/DATA_MODEL.md):
each run emits ONE normalized frontend_index.json the viewer renders. This step computes
NOTHING new -- it reshapes the pipeline's own post-processed outputs into the shared
canonical envelope:

  metrics/keyword_metrics.json  -> kind "keyword"  (volume/cpc/difficulty per keyword)
  serp/rankings.json            -> kind "serp"     (ranked organic results per query)
  serp/domain_map.json          -> kind "domain"   (domain ownership across keywords)

Reads only files already on disk; spends no money; makes no network call.

Self-test:  python3 scripts/emit_index.py   (in-memory, no files/network)
Re-index:   python3 scripts/emit_index.py --workdir <run> [--config <file>]   (free, disk only)
"""

import json
import os
import shutil
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
TOOL_ID = "search"          # the index's "tool" field; reindex refuses another tool's run

_KEYWORD_METRIC_KEYS = ("volume", "cpc_usd", "competition", "keyword_difficulty",
                        "referring_domains_median", "organic_results_count", "database")


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Semrush ships every metric as a STRING ("110", "8.25"). Coerce to a real number so the
# viewer can sort and compare numerically and the difficulty band actually computes (it
# never did on the string values). Non-numeric stays as-is; this is presentation-neutral.
_KEYWORD_NUMERIC = {"volume", "cpc_usd", "competition", "keyword_difficulty",
                    "referring_domains_median", "organic_results_count"}


def _num(v):
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s == "":
            return v
        try:
            f = float(s)
            return int(f) if f.is_integer() else f
        except ValueError:
            return v
    return v


def _host(u):
    """Display host of a URL, without the scheme or a leading www. None if not a URL."""
    if not u or not isinstance(u, str):
        return None
    from urllib.parse import urlparse
    try:
        h = urlparse(u if "//" in u else "//" + u).netloc.lower()
    except ValueError:
        return None
    if h.startswith("www."):
        h = h[4:]
    return h or None


def _paid_item(x):
    """A paid (ad) result: name the advertiser (from the DISPLAYED url) and link to the
    advertiser's real page (directUrl) rather than the google.com/aclk tracking redirect,
    so the user sees WHO is advertising without clicking. Also carries the ad's DESCRIPTION
    (the competitor's copy) and its SITELINK TITLES (their priority messaging -- "Free Plan",
    "ROI Calculator"). Sitelink URLs are all aclk tracking redirects, so only the titles are
    kept (as text): the messaging is the signal, and we never surface a tracking link."""
    link = x.get("directUrl") or x.get("url")
    advertiser = _host(x.get("displayedUrl")) or _host(x.get("directUrl"))
    if not (x.get("title") or link):
        return None
    item = {"title": x.get("title"), "url": link, "advertiser": advertiser}
    if x.get("description"):
        item["description"] = x["description"]
    sitelinks = [(s.get("title") or "").strip() for s in (x.get("siteLinks") or [])
                 if isinstance(s, dict) and (s.get("title") or "").strip()]
    if sitelinks:
        item["sitelinks"] = sitelinks
    return item


def _serp_features(raw):
    """Turn the raw actor's SERP-feature LISTS into {count, items} so the viewer can both
    show the count and expand the actual questions/queries. The post-processed rankings.json
    kept only counts; the items live in the raw B1 output (paidResults/peopleAlsoAsk/
    relatedQueries), so we read them from there when it is available."""
    def collect(key, mapper):
        out = []
        for x in (raw.get(key) or []):
            if isinstance(x, dict):
                m = mapper(x)
                if m:
                    out.append(m)
        return out
    paid = collect("paidResults", _paid_item)
    paa = collect("peopleAlsoAsk", lambda x: x.get("question") or None)
    rel = collect("relatedQueries", lambda x: {"title": x.get("title"), "url": x.get("url")}
                  if x.get("title") else None)
    return {
        "paid_results": {"count": len(paid), "items": paid},
        "people_also_ask": {"count": len(paa), "items": paa},
        "related_queries": {"count": len(rel), "items": rel},
    }


# Google flattens its AI-overview bullet LIST into one string and drops the spaces between items
# ("...billed annually.Asana: Best for...") — so the viewer showed a wall of text. We restore the
# structure with two deliberately narrow guards, because a blanket ".(?=[A-Z])" split would wreck
# ordinary prose ("U.S.Based", "Best.Drains", "...tracking. Paid plans..."):
#   guard 1 — NO space after the period. A real sentence reads ". Asana" (space); a flattened
#             bullet reads ".Asana" (no space). `\.(?=[A-Z]...)` only matches when a capital letter
#             abuts the period with no gap.
#   guard 2 — a COLON must follow within a short label window. Bullets are "Label: description";
#             an ordinary next sentence (".Paid plans start around $8...") has no early colon, so
#             it is left alone.
# If fewer than 2 bullets are found (static overviews are flowing paragraphs) we emit one text
# block — the transform degrades to a no-op rather than inventing structure.
_AI_MARKER = re.compile(r"\s*\[\d[\d.,\s]*\]")            # inline citation coords: "[0.15, 0.15]"
_AI_BULLET = re.compile(r"\.(?=[A-Z][A-Za-z0-9][A-Za-z0-9 /&+.\-]{0,28}:)")
_AI_SENT = ""


def _ai_dedup(text):
    """Live AI overviews repeat a whole span verbatim (a streaming artefact — the video callout and
    the 'If you'd like, tell me:' follow-up each appear twice in 7/10 real records). Drop a span the
    SECOND time it is seen. Threshold 30 chars so ordinary short repeats are never touched."""
    parts = re.split(r"(?<=[.!?:])", text or "")
    seen, out = set(), []
    for p in parts:
        key = p.strip()
        if len(key) >= 30:
            if key in seen:
                continue
            seen.add(key)
        out.append(p)
    return "".join(out)


def _ai_clean(content):
    """Deduped, citation-marker-free version of the raw content — the fallback shown if structuring
    finds no bullets, and the string used for the presence check."""
    return _ai_dedup(_AI_MARKER.sub("", content or "")).strip()


def _ai_blocks(content):
    """Restore the flattened structure: a 'lead' paragraph then 'bullet' items, or a single 'text'
    block when there is no bullet structure to restore (see guards above)."""
    text = _ai_clean(content)
    segs = [s.strip() for s in _AI_BULLET.sub("." + _AI_SENT, text).split(_AI_SENT) if s.strip()]
    if len(segs) < 2:
        return [{"kind": "text", "text": text}]
    return [{"kind": "lead", "text": segs[0]}] + [{"kind": "bullet", "text": s} for s in segs[1:]]


def _source_domain(s):
    """Show a source's main domain the way paid results do — but ONLY where the data allows it
    honestly. Static overviews carry clean https urls (host-parsed -> "zapier.com"). Live overviews
    carry opaque Google `/goto?url=CAES...` redirects that ENCODE the domain unrecoverably, so we
    fall back to a conservative title suffix ("... - Zapier", "... | Miro" -> the brand), and return
    None rather than fabricate a domain when even that is ambiguous (e.g. a "YouTube·..." title)."""
    url = s.get("url") or ""
    if url.startswith(("http://", "https://")):
        h = _host(url)
        if h and not h.endswith("google.com"):
            return h
    # Opaque goto URL: fall back to the title. Publications sit on EITHER side of the separator
    # ("Article - Zapier" = brand last; "Todoist | A To-Do List..." and "YouTube · channel" = brand
    # first), so we take the SHORT side as the brand (a brand is a short token; a tagline is long)
    # and return None when neither side is short enough to be confidently a source name.
    title = s.get("title") or ""
    for sep in (" - ", " | ", " · ", "·", " — "):
        if sep in title:
            left = title.split(sep, 1)[0].strip()
            right = title.rsplit(sep, 1)[-1].strip()
            for side in (right, left):        # prefer the trailing side on a tie
                if 1 <= len(side) <= 20 and side.count(" ") <= 1:
                    return side
            break
    return None


def _ai_overview(raw):
    """Google's AI Overview for the query: the generated summary (STRUCTURED — Google ships it as a
    flattened wall; see _ai_blocks) + the sources it cites (with a display domain + the excerpt).
    Present in the raw B1 output (aiOverview.content/sources), dropped by post-processing."""
    ai = raw.get("aiOverview")
    if not isinstance(ai, dict):
        return None
    content = ai.get("content")
    if not content:
        return None
    sources = []
    for s in (ai.get("sources") or []):
        if isinstance(s, dict) and (s.get("url") or s.get("title")):
            sources.append({"title": s.get("title"), "url": s.get("url"),
                            "domain": _source_domain(s), "description": s.get("description")})
    return {"content": _ai_clean(content), "blocks": _ai_blocks(content), "sources": sources}


def _merge_organic(reduced, raw_organic):
    """Add the full page URL + description (present in the raw actor output, dropped by
    post-processing) onto each ranked result, joined by position. Without this the viewer
    can only show a truncated breadcrumb, never a working link to the page."""
    by_pos = {r.get("position"): r for r in (raw_organic or []) if isinstance(r, dict)}
    out = []
    for r in (reduced or []):
        raw = by_pos.get(r.get("position")) or {}
        merged = dict(r)
        if raw.get("url"):
            merged["url"] = raw["url"]
        if raw.get("description"):
            merged["description"] = raw["description"]
        if raw.get("date"):
            merged["date"] = raw["date"]   # page freshness (30/87 carry it)
        out.append(merged)
    return out


def _keyword_envelope(rec, subject, raw_index, a2=None):
    kw = rec.get("keyword", "")
    data = {}
    # A keyword Semrush had no data for (the one rule, postprocess.semrush_no_data)
    # carries data.no_data = true and NO data.volume, so the app shows "no data from
    # Semrush" and never the 0 the scraper put there. The cleaned row says it; the raw
    # record (which has the trend) is asked too, for a folder made before this rule.
    import postprocess as _PP  # noqa: E402
    no_data = _PP.semrush_no_data(rec) or (isinstance(a2, dict) and _PP.semrush_no_data(a2))
    for k in _KEYWORD_METRIC_KEYS:
        v = rec.get(k)
        if v is None or (no_data and k == "volume"):
            continue
        data[k] = _num(v) if k in _KEYWORD_NUMERIC else v
    if no_data:
        data["no_data"] = True
    # A2 enrichment (joined by keyword). The processed keyword_metrics.json drops these; the
    # A2 raw keeps them: search INTENT, a 12-month VOLUME TREND (sparkline), and example ADS
    # (Semrush's own ad-copy sightings for the term). All optional -- absent when A2 is missing
    # or the term has no such data.
    if isinstance(a2, dict):
        intents = a2.get("intents")
        if isinstance(intents, list) and intents:
            data["intents"] = [str(x) for x in intents if x]
        trend = a2.get("volume_trend_12mo")
        if isinstance(trend, list) and len(trend) >= 2:
            data["volume_trend"] = [_num(x) for x in trend]
        ads = a2.get("ads")
        if isinstance(ads, list) and ads:
            ex = [{"title": a.get("title"), "description": a.get("description"),
                   "url": a.get("displayUrl") or a.get("url")}
                  for a in ads[:5] if isinstance(a, dict) and (a.get("title") or a.get("description"))]
            if ex:
                data["ad_examples"] = ex
    tags = {}
    diff = data.get("keyword_difficulty")
    if isinstance(diff, (int, float)) and not isinstance(diff, bool):
        tags["difficulty_band"] = "high" if diff >= 60 else "medium" if diff >= 30 else "low"
    if isinstance(data.get("intents"), list) and data["intents"]:
        tags["intent"] = data["intents"][0]
    return {
        "uid": f"semrush:{kw}",
        "kind": "keyword",
        "platform": "semrush",
        "subject": subject,
        "text": str(kw),
        "date": None,
        "url": None,
        "lang": None,
        "media": [],
        "tags": tags,
        "raw_ref": {"file": "metrics/keyword_metrics.json", "index": raw_index},
        "data": data,
    }


def _serp_envelope(rec, subject, raw_index, raw=None):
    kw = rec.get("keyword", "")
    results = rec.get("results", []) or []
    features = rec.get("serp_features", {}) or {}
    ai = None
    if raw is not None:  # enrich from the raw actor output: full urls + real feature items
        results = _merge_organic(results, raw.get("organicResults", []))
        features = _serp_features(raw)
        ai = _ai_overview(raw)
    data = {"results": results, "serp_features": features}
    if ai:
        data["ai_overview"] = ai
    return {
        "uid": f"google_serp:{kw}",
        "kind": "serp",
        "platform": "google_serp",
        "subject": subject,
        "text": str(kw),
        "date": None,
        "url": None,
        "lang": None,
        "media": [],
        "tags": {},
        "raw_ref": {"file": "serp/rankings.json", "index": raw_index},
        "data": data,
    }


def _domain_envelope(rec, subject, raw_index):
    dom = rec.get("domain", "")
    return {
        "uid": f"domain:{dom}",
        "kind": "domain",
        "platform": "google_serp",
        "subject": subject,
        "text": str(dom),
        "date": None,
        "url": f"https://{dom}" if dom else None,
        "lang": None,
        "media": [],
        "tags": {},
        "raw_ref": {"file": "serp/domain_map.json", "index": raw_index},
        "data": {
            "keywords_owned": _num(rec.get("keywords_owned")),
            "avg_position": _num(rec.get("avg_position")),
            "best_position": _num(rec.get("best_position")),
        },
    }


def build_index(workdir, cfg):
    """Assemble and write <workdir>/frontend_index.json. Returns the Path."""
    workdir = Path(workdir)
    subject = (cfg or {}).get("subject") or _subject_from_dirname(workdir)

    records = []
    counts = {}

    # The processed keyword_metrics.json is the minimal per-keyword shape; the A2 raw
    # (A2_keyword_metrics/run_02_full.json) additionally carries intents / volume trend / ads.
    # Join by keyword text so the keyword rows can show intent + a sparkline. Absent -> plain.
    a2_raw = _read_json(workdir / "A2_keyword_metrics" / "run_02_full.json", [])
    a2_by_kw = {}
    if isinstance(a2_raw, list):
        for rr in a2_raw:
            if isinstance(rr, dict) and rr.get("keyword"):
                a2_by_kw[rr["keyword"]] = rr

    import postprocess as _PP  # noqa: E402 (one judge for every reader of the cleaned file)
    keyword_metrics = _PP.judge_keyword_rows(
        _read_json(workdir / "metrics" / "keyword_metrics.json", []), workdir)
    if isinstance(keyword_metrics, list):
        for i, rec in enumerate(keyword_metrics):
            if isinstance(rec, dict):
                records.append(_keyword_envelope(rec, subject, i, a2_by_kw.get(rec.get("keyword"))))
        counts["keyword"] = sum(1 for r in records if r["kind"] == "keyword")

    # Raw SERP actor output holds the full result URLs + real feature items that
    # post-processing reduced to a breadcrumb + counts. Load it (if present) and join by
    # keyword so the SERP surface can link out and expand; graceful when it is absent.
    raw_serp = _read_json(workdir / "B1_serp_landscape" / "run_02_full.json", [])
    raw_by_kw = {}
    if isinstance(raw_serp, list):
        for rr in raw_serp:
            if isinstance(rr, dict):
                term = (rr.get("searchQuery") or {}).get("term")
                if term:
                    raw_by_kw[term] = rr

    rankings = _read_json(workdir / "serp" / "rankings.json", [])
    if isinstance(rankings, list):
        base = len(records)
        for i, rec in enumerate(rankings):
            if isinstance(rec, dict):
                records.append(_serp_envelope(rec, subject, i, raw_by_kw.get(rec.get("keyword"))))
        counts["serp"] = len(records) - base

    domain_map = _read_json(workdir / "serp" / "domain_map.json", [])
    if isinstance(domain_map, list):
        base = len(records)
        for i, rec in enumerate(domain_map):
            if isinstance(rec, dict):
                records.append(_domain_envelope(rec, subject, i))
        counts["domain"] = len(records) - base

    facts = _read_json(workdir / "synthesis_facts.json", None)

    index = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_ID,
        "run_id": workdir.name,
        "subject": subject,
        "repeat_of": (cfg or {}).get("repeat_of") or None,
        "generated_at": _now_iso(),
        "config_summary": {
            "database": (cfg or {}).get("database"),
            "country_code": (cfg or {}).get("country_code"),
        },
        "counts": {"records": len(records), "by_kind": counts},
        "synthesis": facts if isinstance(facts, dict) else None,
        "reports": _discover_reports(workdir),
        "records": records,
    }
    # Present only when a step of this run did not finish (failed, stopped
    # by the budget cap, or a scraper run that did not work), so the app and the month
    # comparison can tell that apart from a step that ran and found nothing. The
    # sentences are made once, in postprocess.unfinished_steps; a normal index is
    # byte-identical to what it was.
    import postprocess as _PP  # noqa: E402
    unfinished = _PP.unfinished_steps(workdir)
    if unfinished:
        index["unfinished_steps"] = unfinished
    out = workdir / "frontend_index.json"
    _write_atomic(out, index)
    print(f"   emit_index -> frontend_index.json  "
          f"({len(records)} records: {counts.get('keyword',0)} keyword / "
          f"{counts.get('serp',0)} serp / {counts.get('domain',0)} domain)")
    return out


def _discover_reports(workdir):
    candidates = [("S1", "Search landscape", "S1_search_landscape.md"),
                  ("S1", "Search landscape", "report/S1_search_landscape.md")]
    seen, out = set(), []
    for rid, title, rel in candidates:
        if rid in seen:
            continue
        if (Path(workdir) / rel).exists():
            out.append({"id": rid, "title": title, "path": rel})
            seen.add(rid)
    return out


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE = re.compile(r'^v\d+$', re.IGNORECASE)


def _subject_from_dirname(workdir):
    """Best-effort subject from a run dir named search_<slug>_<YYYY-MM-DD>[_vN].
    The slug may contain underscores: everything between the prefix and the date.
    Unconventional names (no date found, empty slug, unrecognized prefix) return as-is."""
    name = Path(workdir).name
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] == "search":
        for i, part in enumerate(parts):
            if _DATE_RE.match(part):
                slug_parts = parts[1:i]
                if slug_parts:
                    return " ".join(slug_parts).replace("-", " ").title()
                break
    return name


def _write_atomic(path, index):
    """Write the index to a temp file, then rename it over the old one. The local app may be
    reading frontend_index.json while a re-index runs; a half-written file would show the
    user an empty or broken run."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def reindex(workdir, config_path=None):
    """Rebuild <run>/frontend_index.json from the data already on disk. Free: no network, no
    scrape, no model. For a run indexed by an older version of this skill, or whose index is
    missing, so the local app shows it without paying for a new scrape.

    Settings, in order of precedence: the --config file when given (the same JSON the run
    used), then what the run's existing index already recorded (its subject and
    config_summary), then what the folder itself shows. The previous index is kept at
    .viewer/frontend_index.before_reindex.json, and a rebuild that finds nothing where the
    old index had records is refused and the old index put back, because that means the
    wrong folder or a missing file, not an empty run."""
    workdir = Path(workdir)
    if not workdir.is_dir():
        raise SystemExit(f"No run folder at {workdir}")
    cfg = {}
    if config_path:
        cfg = _read_json(Path(config_path), None)
        if not isinstance(cfg, dict):
            raise SystemExit(f"Could not read the config at {config_path} as a JSON object")
    out = workdir / "frontend_index.json"
    prev = _read_json(out, {}) if out.exists() else {}
    if not isinstance(prev, dict):
        prev = {}
    if prev.get("tool") not in (None, TOOL_ID):
        raise SystemExit(f"{workdir.name} was indexed by the '{prev.get('tool')}' tool, and this "
                         f"is the '{TOOL_ID}' tool's re-indexer: use that tool's emit_index.py")
    if prev.get("subject") and not cfg.get("subject"):
        cfg["subject"] = prev["subject"]
    for k, v in (prev.get("config_summary") or {}).items():
        if v not in (None, "", []) and cfg.get(k) in (None, "", []):
            cfg[k] = v
    backup = None
    if out.exists():
        (workdir / ".viewer").mkdir(exist_ok=True)
        backup = workdir / ".viewer" / "frontend_index.before_reindex.json"
        shutil.copy2(out, backup)
    path = build_index(workdir, cfg)
    new_n = len((_read_json(path, {}) or {}).get("records") or [])
    old_n = len(prev.get("records") or [])
    if new_n == 0 and old_n > 0:
        shutil.copy2(backup, out)
        raise SystemExit(f"Re-index found 0 records where the old index had {old_n}; the old "
                         f"index was kept. Check that this is the right run folder.")
    return path


def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="Free re-index of one run for the local app. Run with no arguments to "
                    "run this file's self-tests instead.")
    ap.add_argument("--workdir", required=True, help="the run folder")
    ap.add_argument("--config", default=None, help="the run's config JSON (optional)")
    a = ap.parse_args(argv)
    reindex(a.workdir, a.config)
    return 0


# ---------------------------------------------------------------------------
# Inline verification tests -- in-memory, no files/network/Apify/LLM.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1:              # --workdir <run> [--config <file>]: re-index
        sys.exit(_cli(sys.argv[1:]))
    PASS = FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}"); PASS += 1
        else:
            print(f"  FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")
            FAIL += 1

    kw = {"keyword": "crm software", "volume": 5400, "cpc_usd": 12.5, "competition": 0.7,
          "keyword_difficulty": 68, "database": "us"}
    kenv = _keyword_envelope(kw, "PM SaaS", 0)
    check("keyword kind", kenv["kind"], "keyword")
    check("keyword uid", kenv["uid"], "semrush:crm software")
    check("keyword text", kenv["text"], "crm software")
    check("volume in data", kenv["data"]["volume"], 5400)
    check("difficulty band high", kenv["tags"]["difficulty_band"], "high")

    kw2 = {"keyword": "niche tool", "keyword_difficulty": 20}
    check("difficulty band low", _keyword_envelope(kw2, "x", 1)["tags"]["difficulty_band"], "low")

    # Semrush ships strings: coerce so numbers sort AND the band computes off a string.
    kw3 = {"keyword": "kanban", "volume": "110", "cpc_usd": "8.25", "keyword_difficulty": "34",
           "competition": "0.3", "database": "us"}
    k3 = _keyword_envelope(kw3, "x", 2)
    check("volume string -> int", k3["data"]["volume"], 110)
    check("cpc string -> float", k3["data"]["cpc_usd"], 8.25)
    check("difficulty string -> int", k3["data"]["keyword_difficulty"], 34)
    check("band from string difficulty", k3["tags"]["difficulty_band"], "medium")
    check("database left as string", k3["data"]["database"], "us")

    # A2 enrichment: intents / volume trend / ad examples joined from the A2 raw record.
    a2 = {"keyword": "crm software", "intents": ["commercial", "transactional"],
          "volume_trend_12mo": [47, 80, "100", 67], "database": "us",
          "ads": [{"title": "Try Zoho CRM", "description": "Free tier", "displayUrl": "zoho.com"},
                  {"description": "no title still counts"}, {}]}
    ke = _keyword_envelope({"keyword": "crm software", "volume": 5400}, "x", 0, a2)
    check("a2 intents copied", ke["data"]["intents"], ["commercial", "transactional"])
    check("a2 intent tag = first", ke["tags"]["intent"], "commercial")
    check("a2 volume_trend numeric-coerced", ke["data"]["volume_trend"], [47, 80, 100, 67])
    check("a2 ad_examples trimmed", len(ke["data"]["ad_examples"]), 2)
    check("a2 ad_example fields", ke["data"]["ad_examples"][0],
          {"title": "Try Zoho CRM", "description": "Free tier", "url": "zoho.com"})
    check("no a2 -> no intents/trend", "intents" in _keyword_envelope({"keyword": "x"}, "y", 0)["data"], False)
    check("a2 single-point trend ignored",
          "volume_trend" in _keyword_envelope({"keyword": "x"}, "y", 0, {"volume_trend_12mo": [5]})["data"], False)

    # SERP with NO raw B1: counts pass through unchanged (backward compatible).
    serp = {"keyword": "crm software",
            "results": [{"position": 1, "domain": "hubspot.com", "title": "CRM", "displayed_url": "hubspot.com"}],
            "serp_features": {"organic_results": 10, "paid_results": 2, "people_also_ask": 3, "related_queries": 1}}
    senv = _serp_envelope(serp, "PM SaaS", 0)
    check("serp kind", senv["kind"], "serp")
    check("serp embeds results", len(senv["data"]["results"]), 1)
    check("serp features carried (no raw)", senv["data"]["serp_features"]["paid_results"], 2)
    check("no-raw result has no full url", "url" in senv["data"]["results"][0], False)

    # SERP WITH raw B1: full url + description merged onto the result by position; features
    # become {count, items} carrying the real questions/queries.
    raw = {"searchQuery": {"term": "crm software"},
           "organicResults": [{"position": 1, "url": "https://hubspot.com/crm",
                               "description": "The free CRM", "date": "2024-09-13T12:00:00.000Z"}],
           "paidResults": [{"title": "Ad", "url": "https://www.google.com/aclk?x=1",
                            "displayedUrl": "https://www.zoho.com",
                            "directUrl": "https://www.zoho.com/crm/lp",
                            "description": "Free CRM for teams",
                            "siteLinks": [{"title": "Pricing", "url": "https://g/aclk?a"},
                                          {"title": "", "url": "https://g/aclk?b"}]}],
           "peopleAlsoAsk": [{"question": "Is HubSpot free?"}, {"question": "Best CRM?"}],
           "relatedQueries": [{"title": "free crm", "url": "https://g.co/x"}],
           "aiOverview": {"type": "static", "content": "HubSpot and Zoho lead the free CRM space.",
                          "sources": [{"title": "Best CRM", "url": "https://z.co/best"},
                                      {"nourl": True}]}}
    senv2 = _serp_envelope(serp, "PM SaaS", 0, raw)
    check("raw merges full url", senv2["data"]["results"][0]["url"], "https://hubspot.com/crm")
    check("raw merges description", senv2["data"]["results"][0]["description"], "The free CRM")
    check("paa items surfaced", senv2["data"]["serp_features"]["people_also_ask"]["items"],
          ["Is HubSpot free?", "Best CRM?"])
    check("paa count", senv2["data"]["serp_features"]["people_also_ask"]["count"], 2)
    check("related items surfaced", senv2["data"]["serp_features"]["related_queries"]["items"][0]["url"], "https://g.co/x")
    paid0 = senv2["data"]["serp_features"]["paid_results"]["items"][0]
    check("paid names advertiser from displayedUrl", paid0["advertiser"], "zoho.com")
    check("paid links to real page not aclk", paid0["url"], "https://www.zoho.com/crm/lp")
    check("paid carries ad description", paid0["description"], "Free CRM for teams")
    check("paid sitelinks: titled only, text only", paid0["sitelinks"], ["Pricing"])
    check("organic date merged from raw", senv2["data"]["results"][0]["date"], "2024-09-13T12:00:00.000Z")
    check("ai overview content cleaned", senv2["data"]["ai_overview"]["content"], "HubSpot and Zoho lead the free CRM space.")
    check("ai overview single text block (no bullet structure)", senv2["data"]["ai_overview"]["blocks"],
          [{"kind": "text", "text": "HubSpot and Zoho lead the free CRM space."}])
    check("ai overview source gets domain + description", senv2["data"]["ai_overview"]["sources"],
          [{"title": "Best CRM", "url": "https://z.co/best", "domain": "z.co", "description": None}])
    check("no ai_overview key when raw absent", "ai_overview" in senv["data"], False)

    # AI-overview STRUCTURING guard — a flattened bullet blob with a long duplicate span + a marker.
    # WHY this test exists: the transform must restore Google's dropped bullets WITHOUT splitting an
    # ordinary sentence. If a future edit loosens the guards, "Paid plans start around $8" would get
    # torn out of its bullet and this test fails loudly.
    _blob = ("Todoist is the best overall app [0.15, 0.15].Todoist: Best for to-do lists. "
             "Paid plans start around $8.Asana: Best for teams billed annually."
             "Here is a video showcasing the top free apps to compare."
             "Here is a video showcasing the top free apps to compare.")
    _ao = _ai_overview({"aiOverview": {"type": "live", "content": _blob,
          "sources": [{"title": "I Tried 32 Apps", "url": "/goto?url=CAESxyz"},
                      {"title": "Best PM Software - Zapier", "url": "/goto?url=CAESabc",
                       "description": "The top tools"}]}})
    check("structuring: citation marker stripped", "[0.15" in _ao["content"], False)
    check("structuring: long duplicate span dropped",
          _ao["content"].count("Here is a video showcasing the top free apps to compare."), 1)
    check("structuring: lead + 2 bullets", [b["kind"] for b in _ao["blocks"]], ["lead", "bullet", "bullet"])
    check("structuring: real sentence stays inside its bullet (not split)",
          "Paid plans start around $8" in _ao["blocks"][1]["text"], True)
    check("structuring: bullet label preserved", _ao["blocks"][2]["text"].startswith("Asana:"), True)
    check("source domain from opaque goto url falls back to title suffix", _ao["sources"][1]["domain"], "Zapier")
    check("source domain None when unrecoverable (no clean url, no title suffix)", _ao["sources"][0]["domain"], None)
    check("source description carried", _ao["sources"][1]["description"], "The top tools")

    dom = {"domain": "hubspot.com", "keywords_owned": "42", "avg_position": "3.2", "best_position": "1"}
    denv = _domain_envelope(dom, "PM SaaS", 0)
    check("domain kind", denv["kind"], "domain")
    check("domain uid", denv["uid"], "domain:hubspot.com")
    check("domain url synthesised", denv["url"], "https://hubspot.com")
    check("keywords_owned string -> int", denv["data"]["keywords_owned"], 42)
    check("avg_position string -> float", denv["data"]["avg_position"], 3.2)

    check("subject from dirname", _subject_from_dirname(Path("/x/search_pm-saas_2026-09-19")), "Pm Saas")
    # Folder parser: _vN suffix is stripped, multi-word slug is preserved, unconventional falls back
    check("dirname: normal name (no suffix)", _subject_from_dirname(Path("/x/search_pm-saas_2026-09-28")), "Pm Saas")
    check("dirname: _v2 suffix is stripped — same subject as base run", _subject_from_dirname(Path("/x/search_pm-saas_2026-09-28_v2")), "Pm Saas")
    check("dirname: _v12 suffix is stripped", _subject_from_dirname(Path("/x/search_pm-saas_2026-09-28_v12")), "Pm Saas")
    check("dirname: slug with underscores parsed correctly", _subject_from_dirname(Path("/x/search_project-management_saas_2026-09-28")), "Project Management Saas")
    check("dirname: unconventional name (no date) returns as-is", _subject_from_dirname(Path("/x/search_pm-saas_nodatehereXYZ")), "search_pm-saas_nodatehereXYZ")

    # P2: repeat_of emitted into index
    with tempfile.TemporaryDirectory() as _td:
        _wd = Path(_td) / "search_pm-saas_2026-09-28"
        _wd.mkdir()
        (_wd / "SEARCH_CONFIG.json").write_text(
            '{"repeat_of":"search_pm-saas_2026-08-28","subject":"Pm Saas"}',
            encoding="utf-8")
        _idx = json.loads(build_index(_wd, {"repeat_of": "search_pm-saas_2026-08-28"}).read_text())
        check("repeat_of emitted into index", _idx.get("repeat_of"), "search_pm-saas_2026-08-28")
        _idx2 = json.loads(build_index(_wd, {}).read_text())
        check("repeat_of is null when absent from cfg", _idx2.get("repeat_of"), None)

    print(f"\n  {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
