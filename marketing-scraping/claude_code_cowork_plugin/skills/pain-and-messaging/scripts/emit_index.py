"""
emit_index.py -- write frontend_index.json for a pain-and-messaging run.

The Local Viewer contract (see marketing-viewer/references/DATA_MODEL.md):
each run emits ONE normalized frontend_index.json that the viewer renders, so the
frontend never parses raw actor shapes. This step computes NOTHING new -- it reshapes
what the pipeline already produced (post-processed canonical review records +
synthesis_facts.json) into the shared canonical envelope. It reads only files already
on disk, spends no money, and makes no network call.

Runs at the end of pain_intel.py (after extract + report). Idempotent: re-running
regenerates the index from whatever is on disk. A partial run yields a valid, smaller
index flagged partial.

Self-test:  python3 scripts/emit_index.py   (in-memory, no files/network)
Re-index:   python3 scripts/emit_index.py --workdir <run> [--config <file>]   (free, disk only)
"""

import base64
import binascii
import json
import os
import shutil
import re
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 1
TOOL_ID = "pain"          # the index's "tool" field; reindex refuses another tool's run

# Review sources whose run_02_full.json holds post-processed CANONICAL records
# ({text, stars, businessName, source, date, id, +extras}). "maps" lives in B2b_reviews/.
_REVIEW_FOLDERS = {
    "maps": "B2b_reviews",
    "trustpilot": "trustpilot_reviews",
    "appstore": "appstore_reviews",
    "googleplay": "googleplay_reviews",
    "reddit": "reddit_reviews",
    "g2": "g2_reviews",
    "capterra": "capterra_reviews",
}

# What the reviewed ENTITY is called, per source -- so a source snapshot reads
# "App overview" / "Company overview", never a Maps-shaped label bolted onto every
# source. Every source the tool can scrape (SKILL.md "Choose sources") is listed, not
# just the two we happen to have calibration data for -- a source without an entry
# still gets a snapshot (it falls back to the generic "source"), so adding a source
# never silently drops it from the snapshot rail.
_SOURCE_SCOPE = {
    "maps": "place",
    "trustpilot": "company",
    "appstore": "app",
    "googleplay": "app",
    "g2": "product",
    "capterra": "product",
    # reddit has no reviewed entity -- open discussion, synthetic banded stars -- so it
    # is deliberately excluded from snapshots (see _source_snapshots).
}

# Per-source canonical review key -> envelope data key. Only keys that exist on a
# record are copied, so a source that never sets a field simply omits it.
_REVIEW_DATA_MAP = {
    # trustpilot
    "isVerified": "verified",
    "companyDomain": "company_domain",
    "companyTrustScore": "company_trust_score",
    "companyTotalReviews": "company_total_reviews",
    "language": "lang_raw",
    # app stores
    "version": "app_version",
    "appId": "app_id",
    # reddit
    "score": "reddit_score",
    "is_comment": "is_comment",
    "subreddit": "subreddit",
    # g2 / capterra
    "companySize": "company_size",
    "industry": "industry",
}


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


def _polarity(stars, low_thresh, high_thresh):
    """low | high | mid | None -- the per-record polarity tag the UI filters on."""
    if stars is None:
        return None
    try:
        s = float(stars)
    except (TypeError, ValueError):
        return None
    if s <= low_thresh:
        return "low"
    if s >= high_thresh:
        return "high"
    return "mid"


def _review_date(rec):
    """Canonical review date. Most sources ship `date`; Google Maps (B2b) is stored in the raw
    actor shape whose date field is `publishedAtDate` — so fall back to it. This is the fix for
    every Maps card showing a blank date (emit read `date`, which Maps records do not have)."""
    return rec.get("date") or rec.get("publishedAtDate")


def _review_url(rec, source, raw_by_id):
    """A working link to the INDIVIDUAL review, not the business profile. Post-processing dropped
    the per-review permalink for Trustpilot and Reddit, so the "source" link pointed nowhere for
    75 of 95 records. We recover it, preferring the actor's OWN recorded url over anything we
    reconstruct (a reconstructed format breaks silently if the platform changes it; the recorded
    value does not):
      - Trustpilot: the raw actor record carries `reviewUrl` verbatim -> use it. If the raw file
        is absent (older runs), fall back to constructing trustpilot.com/reviews/<reviewId>, since
        the processed `id` IS the reviewId (verified identical to reviewUrl across a real run).
      - Reddit: POST records carry postUrl or contentUrl; COMMENT records carry their own
        per-comment permalink in raw `url`. Both are recovered. The order matters: this
        actor never puts anything but a comment permalink in `url` (posts have url=None,
        even link posts -- verified on a live corpus with an external-link post). A
        different actor that omitted postUrl and reused `url` for a link post's external
        target would emit a wrong "source" link here -- re-verify the shape if the actor
        is ever swapped.
      - Maps: see _maps_review_url. Falls back to the processed `url` (the business's place
        search page) only when no per-review link can be recovered.
      - Others: the processed `url`."""
    raw = (raw_by_id or {}).get(rec.get("id")) or {}
    if source == "trustpilot":
        u = raw.get("reviewUrl")
        if u and u != "None":
            return u
        if rec.get("id"):
            return f"https://www.trustpilot.com/reviews/{rec['id']}"
    if source == "reddit":
        u = raw.get("postUrl") or raw.get("contentUrl") or raw.get("url")
        if u and u != "None":
            return u
    if source == "maps":
        return _maps_review_url(rec) or rec.get("url")
    return rec.get("url")


# Google Maps link to ONE review, built from the reviewId + the place's cid. Verified on a real
# run: 3/3 built links opened exactly that review.
_MAPS_REVIEW_URL = ("https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5!1s{rid}"
                    "!2m1!1s0x0:0x{cid:x}!3m1!1s2@1:{inner}")
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


def _maps_review_url(rec):
    """Per-review link for a Maps record (the raw actor shape), or None.

    The actor's own `reviewUrl` wins when present, but it is only filled when the scrape runs
    with personalData on, and this tool runs it off: the same switch that
    withholds reviewer names also withholds the review URL. `reviewId` is always included, so
    the link is rebuilt from it without collecting any personal data. The reviewId is base64
    of a small protobuf whose field 1 is the inner token the URL also needs. Anything not in
    exactly that verified shape returns None, so the caller keeps the business page instead
    of emitting a broken link. The format is Google's, undocumented, and could change; the
    fallback keeps a change from breaking the card."""
    u = rec.get("reviewUrl")
    if u and u != "None":
        return u
    rid, cid = rec.get("reviewId"), rec.get("cid")
    if not isinstance(rid, str) or not _B64URL.fullmatch(rid) or cid in (None, "", "None"):
        return None
    try:
        cid_n = int(cid)
        raw = base64.urlsafe_b64decode(rid + "=" * (-len(rid) % 4))
    except (TypeError, ValueError, binascii.Error):
        return None
    if cid_n <= 0 or len(raw) < 3 or raw[0] != 0x0A or raw[1] > 127 or len(raw) < 2 + raw[1]:
        return None
    inner = raw[2:2 + raw[1]].decode("ascii", "replace")
    if not _B64URL.fullmatch(inner):
        return None
    return _MAPS_REVIEW_URL.format(rid=rid, cid=cid_n, inner=inner)


def _review_extras(rec, source, raw):
    """Source-specific rich fields the minimal processed record drops. Trustpilot and Reddit keep
    them ONLY in the raw actor record (`raw`, joined by id); Maps keeps them in the processed rec
    itself (its processed shape is already rich). Everything here is verified present on the
    calibration run except where noted -- a field simply stays absent when its value is empty.

      - review_title: the headline above the quote. Trustpilot/Reddit raw `title` (Maps `title`
        is the PLACE name, not a review title, so Maps is excluded on purpose).
      - company_reply: Trustpilot raw `replyMessage` (9/20) / Maps `responseFromOwnerText`
        (0/20 on this run -- the key is correct but unexercised until a run has owner replies).
      - is_local_guide / reviewer_review_count: Maps reviewer credibility.
      - upvote_ratio / comments_count: Reddit post engagement (posts only; comments lack them).
        reddit_score is already mapped elsewhere."""
    out = {}
    def put(k, v):
        if isinstance(v, str):
            v = v.strip()
        if v not in (None, "", [], {}):
            out[k] = v
    if source == "trustpilot":
        put("review_title", raw.get("title"))
        put("company_reply", raw.get("replyMessage"))
    elif source == "reddit":
        put("review_title", raw.get("title"))
        put("upvote_ratio", raw.get("upvoteRatio"))
        put("comments_count", raw.get("commentsCount"))
    elif source == "maps":
        # Which place this review was collected from: the actor's own placeId and place
        # name (Maps `title`). The per-place snapshot cards attribute reviews by these, so a
        # place card only ever counts reviews that really came from that place.
        put("place_id", rec.get("placeId"))
        put("place_name", rec.get("title"))
        put("company_reply", rec.get("responseFromOwnerText"))
        put("is_local_guide", rec.get("isLocalGuide"))
        put("reviewer_review_count", rec.get("reviewerNumberOfReviews"))
    return out


def _record_envelope(rec, source, subject, folder, raw_index, thresholds, raw_by_id=None):
    """Map ONE canonical review dict -> the shared envelope. Pure; no I/O."""
    low_thresh, high_thresh = thresholds
    raw = (raw_by_id or {}).get(rec.get("id")) or {}
    is_post = source == "reddit"
    native_id = rec.get("id")
    # Maps records carry the review identity in `reviewId`, not `id` (which is always null
    # in the actor output). When `id` is absent, check `reviewId` for Maps so the uid is
    # `maps:<reviewId>` (the documented contract) rather than a positional fallback.
    if native_id in (None, "") and source == "maps":
        native_id = rec.get("reviewId")
    uid_is_native = native_id not in (None, "")
    uid = f"{source}:{native_id if uid_is_native else raw_index}"

    data = {}
    for src_key, out_key in _REVIEW_DATA_MAP.items():
        if src_key in rec and rec.get(src_key) not in (None, ""):
            data[out_key] = rec.get(src_key)
    if rec.get("stars") is not None:
        data["stars"] = rec.get("stars")
    if rec.get("businessName"):
        data["business_name"] = rec.get("businessName")
    data.update(_review_extras(rec, source, raw))

    tags = {}
    pol = _polarity(rec.get("stars"), low_thresh, high_thresh)
    if pol:
        tags["polarity"] = pol
    if is_post:
        # Reddit polarity comes from the LOCKED banding (synthetic stars, 3-way neutral
        # set-aside). Praise/pain records carry stars -> high/low via `pol`; the neutral
        # set-aside (reddit_neutral.json) has no stars, so fall back to the record's own
        # `band` field ("neutral") -- this is what gives those set-aside records a visible,
        # filterable sentiment instead of the old "unbanded" dead-end.
        tags["banding"] = pol or rec.get("band") or "unbanded"
        # Deleted Reddit posts come back as the literal text "[removed]" / "[deleted]": no
        # opinion left to read. Flag them rather than drop them (the record and its raw_ref
        # stay, so the count still reconciles with the raw file); the viewer hides flagged
        # records, and an agent reading this index must skip them too, or its counts drift
        # from what the user sees.
        if (rec.get("text") or "").strip().lower() in ("[removed]", "[deleted]"):
            tags["removed"] = True
        # One subreddit, one name: the actor mixes "r/acmefitness" and "acmefitness".
        if isinstance(data.get("subreddit"), str):
            data["subreddit"] = re.sub(r"^r/", "", data["subreddit"], flags=re.I)

    return {
        "uid": uid,
        # uid_native: True means the uid was built from a native (non-positional) id and is
        # stable across re-scrapes. False means it fell back to position in the list -- that
        # uid is only stable within this run. Readers must treat the absent field as unknown,
        # never as native.
        "uid_native": uid_is_native,
        "kind": "post" if is_post else "review",
        "platform": source,
        "subject": rec.get("businessName") or subject,
        "text": rec.get("text") or "",
        "date": _review_date(rec),
        "url": _review_url(rec, source, raw_by_id),
        "lang": rec.get("language"),
        "media": [],  # reviews/posts are field-reconstructed (design fork E); no assets on disk
        "tags": tags,
        "raw_ref": {"file": f"{folder}/run_02_full.json", "index": raw_index},
        "data": data,
    }


def build_index(workdir, cfg):
    """Assemble and write <workdir>/frontend_index.json. Returns the Path.

    Reuses the skill's own corpus loader so records are read exactly as the
    extractor read them (no second, drifting reader). Reads synthesis_facts.json
    for the run-level taxonomy (the pain/trust phrase clusters + counts).
    """
    workdir = Path(workdir)
    subject = (cfg or {}).get("subject") or _subject_from_dirname(workdir)
    sources = (cfg or {}).get("sources") or ["maps"]

    facts = _read_json(workdir / "synthesis_facts.json", {}) or {}
    meta = facts.get("meta", {}) if isinstance(facts, dict) else {}
    low_thresh = meta.get("low_star_threshold", 3)
    high_thresh = meta.get("high_star_threshold", 4)

    records = []
    by_platform = {}
    for source in sources:
        folder = _REVIEW_FOLDERS.get(source, f"{source}_reviews")
        raw = _read_json(workdir / folder / "run_02_full.json", [])
        if not isinstance(raw, list):
            raw = []
        if source == "reddit":
            # The LOCKED banding sets aside 3-way "neutral" Reddit records into a separate file
            # (reddit_neutral.json). They were invisible in the viewer, so the researcher could
            # not audit the neutral call. Fold them in here -- their `band` == "neutral" gives
            # them a filterable sentiment (see _record_envelope).
            neutral = _read_json(workdir / folder / "reddit_neutral.json", [])
            if isinstance(neutral, list):
                raw = raw + neutral
        # Per-review permalinks were dropped in post-processing; recover them from the
        # untouched actor output (run_02_full.raw.json) by joining on record id. Loaded for
        # the sources that carry a recoverable url in raw (Trustpilot reviewUrl, Reddit
        # postUrl); Maps has no per-review permalink so it needs no raw read. Absent file =>
        # empty map => graceful fallback (Trustpilot then constructs from id; Reddit keeps url).
        raw_by_id = {}
        if source in ("reddit", "trustpilot"):
            raw_full = _read_json(workdir / folder / "run_02_full.raw.json", [])
            if isinstance(raw_full, list):
                for rr in raw_full:
                    if isinstance(rr, dict):
                        rid = rr.get("id") or rr.get("reviewId")
                        if rid not in (None, ""):
                            raw_by_id[rid] = rr
        by_platform[source] = 0
        for i, rec in enumerate(raw):
            if not isinstance(rec, dict):
                continue
            records.append(
                _record_envelope(rec, source, subject, folder, i,
                                 (low_thresh, high_thresh), raw_by_id)
            )
            by_platform[source] += 1

    snapshot_notes = []
    index = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_ID,
        "run_id": workdir.name,
        "subject": subject,
        "repeat_of": (cfg or {}).get("repeat_of") or None,
        "generated_at": _now_iso(),
        "config_summary": {
            "sources": sources,
            "country_code": (cfg or {}).get("country_code"),
            "max_reviews_per_place": (cfg or {}).get("max_reviews_per_place"),
        },
        "counts": {"records": len(records), "by_platform": by_platform},
        # Carry the counted taxonomy forward verbatim so the frontend renders the
        # pain/trust view without re-deriving anything (or fetching a second file).
        "synthesis": {
            "meta": meta,
            "pain_points_low_star": facts.get("pain_points_low_star", []),
            "trust_signals_high_star": facts.get("trust_signals_high_star", []),
        } if facts else None,
        "snapshots": _source_snapshots(workdir, sources, records, snapshot_notes),
        # What the snapshot cards do NOT cover, stated rather than hidden (e.g. places the
        # Maps search found whose reviews this run did not collect). Read by the viewer.
        "snapshot_notes": snapshot_notes,
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
          f"({len(records)} records across {sum(1 for v in by_platform.values() if v)} "
          f"source(s); taxonomy={'yes' if facts else 'MISSING'})")
    return out


def _sample_stats(recs):
    """Aggregate the reviews WE ACTUALLY HOLD for one source into (size, avg, histogram).

    This is the safe universal baseline: it is computed only from `data.stars`,
    which the pipeline already normalised for every star-rated source, so it works for a
    source we have never seen a run of (App Store, G2, ...) the day its data first lands,
    and it can never show a wrong number -- it is literally a count of the cards on screen.
    Records with no integer 1-5 star (e.g. Reddit's None neutral set-aside) are ignored in
    the histogram but still counted in size. Returns ({"1":n,...} may be empty)."""
    size = len(recs)
    hist = {}
    starred = []
    for r in recs:
        s = (r.get("data") or {}).get("stars")
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            si = int(round(s))
            if 1 <= si <= 5:
                hist[str(si)] = hist.get(str(si), 0) + 1
                starred.append(si)
    avg = round(sum(starred) / len(starred), 1) if starred else None
    return size, avg, hist


def _base_snapshot(src, recs):
    """A source snapshot from ONLY the records the viewer holds -- no external file, no
    unseen field, no guessing. The floor every non-Reddit source gets."""
    size, avg, hist = _sample_stats(recs)
    subject = next((r.get("subject") for r in recs if r.get("subject")), None)
    return {
        "source": src,
        "scope": _SOURCE_SCOPE.get(src, "source"),
        "title": subject,
        "subtitle": None,
        "url": None,
        "sample_size": size,           # reviews analysed for this source (always honest)
        "score": avg,                  # headline number...
        "score_label": "average of the reviews analysed",
        "score_basis": "sample",       # ...whose provenance is stated, not implied
        "total_reviews": None,         # the source's OWN corpus total (enrichment only)
        "distribution": hist or None,  # histogram of the sample...
        "distribution_basis": "sample",
        "tags": [],
    }


def _snapshot_worth_showing(snap):
    """A snapshot earns its card only if it can show a number or a theme."""
    return bool(snap.get("sample_size")) and bool(
        snap.get("score") is not None or snap.get("distribution") or snap.get("tags"))


def _place_contradicts(pid, place):
    """True when a review's own placeId names a DIFFERENT place than `place`. A review with no
    placeId, or a place with no placeId, cannot contradict -- only two known, unequal ids do."""
    ppid = place.get("placeId")
    return bool(pid) and bool(ppid) and pid != ppid


def _attribute_maps_reviews(places, recs):
    """Split the run's Maps reviews by the place each was collected from.

    Returns (buckets, unmatched): buckets[i] holds the reviews of places[i]. A review goes to a
    place only when it provably came from there:
      1. its own placeId equals the place's placeId (the actor records both);
      2. else its place name (Maps `title`) or subject equals the title of exactly ONE place,
         and no placeId contradicts it (chains share a title, so a shared title proves nothing);
      3. else, only when the search found exactly ONE place and nothing contradicts it: then
         every Maps review in the run really is that place's (the one-place run whose reviews
         carry another name, or none).
    Anything else stays unmatched. It is never spread over every place: a card says
    "analysed here", so it may only count reviews collected from that place."""
    by_id = {p.get("placeId"): i for i, p in enumerate(places) if p.get("placeId")}
    by_name = {}
    for i, p in enumerate(places):
        by_name.setdefault(p.get("title") or p.get("name"), []).append(i)
    buckets = [[] for _ in places]
    unmatched = []
    for r in recs:
        d = r.get("data") or {}
        pid = d.get("place_id")
        i = by_id.get(pid) if pid else None
        if i is None:
            for name in (d.get("place_name"), r.get("subject")):
                hits = by_name.get(name) if name else None
                if hits and len(hits) == 1 and not _place_contradicts(pid, places[hits[0]]):
                    i = hits[0]
                    break
        if i is None and len(places) == 1 and not _place_contradicts(pid, places[0]):
            i = 0
        if i is None:
            unmatched.append(r)
        else:
            buckets[i].append(r)
    return buckets, unmatched


def _maps_snapshots(workdir, recs, notes=None):
    """Google Maps: one snapshot PER place whose reviews this run collected. The place file
    (B2a_maps_places/run_02_full.json) gives Google's own headline rating, corpus review count,
    and topic tags; the star HISTOGRAM is the SAMPLE we hold, not Google's full-corpus
    reviewsDistribution.

    Why sample and not corpus: the histogram must read the same way on every source's card, and
    Trustpilot (and the other sources) can only ever provide a sample histogram -- the scrape has
    no full-corpus per-star breakdown for them. Google DOES expose reviewsDistribution over all
    N reviews, but showing 306-review bars on the Maps card beside 20-review bars on the
    Trustpilot card is the exact inconsistency a reviewer flagged: same visual, two different
    Ns.
    So every card's histogram is "the reviews analysed here"; the corpus scale still shows as the
    rating + total.

    Sample size / histogram come ONLY from the reviews attributed to that place
    (_attribute_maps_reviews). A Maps search finds a page of places but collects reviews for only
    the top few, so most listed places have none: they get no card (a card with no collected
    reviews would have nothing "analysed here" to show). Instead, when `notes` is a list, one
    note records how many listed places had no reviews collected and how many reviews could not
    be tied to a listed place, so the page can say so in one plain line.

    Returns None when there is no place file or it lists no place (older runs, and runs given
    place links; the caller then shows the record-derived cards, _maps_record_snapshots), else
    the list of cards (possibly empty)."""
    raw = _read_json(Path(workdir) / "B2a_maps_places" / "run_02_full.json", [])
    places = [p for p in raw if isinstance(p, dict) and (p.get("title") or p.get("name"))] \
        if isinstance(raw, list) else []
    if not places:
        return None
    buckets, unmatched = _attribute_maps_reviews(places, recs)
    out = []
    for p, attributed in zip(places, buckets):
        if not attributed:
            continue  # no review collected from this place -> nothing analysed here -> no card
        tags = p.get("reviewsTags")
        _size, _avg, hist = _sample_stats(attributed)
        snap = {
            "source": "maps",
            "scope": "place",
            "title": p.get("title") or p.get("name"),
            "subtitle": " · ".join(x for x in (p.get("categoryName"), p.get("address")) if x)
                        or None,
            "url": p.get("url"),
            "sample_size": len(attributed),
            "score": p.get("totalScore"),
            "score_label": "Google rating",
            "score_basis": "source",             # Google's own star average (over all reviews)
            "total_reviews": p.get("reviewsCount"),
            "distribution": hist or None,         # histogram of the reviews WE hold (consistent)
            "distribution_basis": "sample",
            "tags": [{"title": t.get("title"), "count": t.get("count")}
                     for t in tags if isinstance(t, dict) and t.get("title")]
                    if isinstance(tags, list) else [],
        }
        out.append(snap)
    not_reviewed = sum(1 for b in buckets if not b)
    if notes is not None and (not_reviewed or unmatched):
        notes.append({"source": "maps",
                      "places_found": len(places),
                      "places_not_reviewed": not_reviewed,
                      "reviews_unmatched": len(unmatched)})
    return out


# The characters JavaScript's String.prototype.trim() removes (the ECMAScript WhiteSpace and
# LineTerminator sets). Python's str.strip() removes a different set: it also strips \x1c to
# \x1f and \x85, and keeps \ufeff. So strip() alone cannot mirror app.js.
_JS_TRIM = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
            "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def _js_truthy(v):
    """JavaScript truthiness of a JSON value: null, false, 0, NaN and "" are falsy. Everything
    else is truthy, including "0", [] and {} (which Python treats as false)."""
    if v is None or isinstance(v, bool):
        return bool(v)
    if isinstance(v, (int, float)):
        return v == v and v != 0
    if isinstance(v, str):
        return v != ""
    return True


def _js_number_str(x):
    """JavaScript String(n) for a JSON number (ECMAScript Number::toString): the shortest
    digits that round-trip, written out in full from 1e-6 up to 1e21, else as 1.5e+21."""
    try:
        x = float(x)                 # JSON numbers are doubles in the browser
    except OverflowError:
        x = float("inf") if x > 0 else float("-inf")
    if x != x:
        return "NaN"
    if x in (float("inf"), float("-inf")):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    t = Decimal(repr(abs(x))).normalize().as_tuple()
    s = "".join(map(str, t.digits))
    k, n = len(s), len(s) + t.exponent
    if k <= n <= 21:
        out = s + "0" * (n - k)
    elif 0 < n <= 21:
        out = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + s
    else:
        e = n - 1
        out = s[0] + ("." + s[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return ("-" if x < 0 else "") + out


def _js_str(v):
    """JavaScript String(v) for a JSON value: the text the page prints for it."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _js_number_str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ",".join("" if x is None else _js_str(x) for x in v)
    return "[object Object]"


def _maps_place_name(r):
    """The place name a Google Maps review card shows, or None when app.js reviewSource does not
    use data.place_name. It uses it only when it is a string whose JavaScript trim() is not
    empty, "unknown" or "n/a" (app.js has()), and then shows it untrimmed."""
    d = r.get("data")
    d = d if isinstance(d, dict) else {}
    name = d.get("place_name")
    if isinstance(name, str) and name.strip(_JS_TRIM) not in ("", "unknown", "n/a"):
        return name
    return None


def _maps_review_label(r):
    """The name a Google Maps review card shows under the review: an exact mirror of app.js
    reviewSource for a Maps record, returning the same value (business_name and subject pass
    through as they are, not always strings, like the JavaScript):
        place_name (see _maps_place_name)
        || business_name || (subreddit ? "r/" + subreddit : subject) || "Google Maps"
    with JavaScript truthiness at every "||". business_name is not checked for "unknown"
    because app.js does not check it. The subreddit branch is mirrored too: the envelope
    copies a raw `subreddit` field for every source (_REVIEW_DATA_MAP), so a Maps record can
    carry one. A Maps card built from records is titled with _js_str of this value, the text
    the review card prints, so the page never shows a card whose title no review card
    carries. A development test runs this and the real reviewSource over the same records."""
    name = _maps_place_name(r)
    if name is not None:
        return name
    d = r.get("data")
    d = d if isinstance(d, dict) else {}
    if _js_truthy(d.get("business_name")):
        return d["business_name"]
    sub = d.get("subreddit")
    alt = "r/" + _js_str(sub) if _js_truthy(sub) else r.get("subject")
    return alt if _js_truthy(alt) else "Google Maps"


def _maps_record_snapshots(recs):
    """Google Maps cards when there is NO place file: the run was given place links
    (competitor_place_urls), so no places step ran and Google's own rating, total and tags
    are unknown. The cards come from the records alone.

    The rule: every card counts exactly the reviews whose cards carry its title, and no two
    cards share a title. So the reviews are grouped by the name their review card shows
    (_maps_review_label), one card per name, in the order the names first appear. One card
    for all of them, titled with the run subject, presented three places as one place (the
    three-place link run: "Acme Fitness Multi, 60 analysed here" above 3 x 20 review cards).

    A card is a PLACE card only when all its reviews name that place and carry at most one
    place_id. Otherwise its scope is "source" and it does not call itself a place:
      - branches of a chain share one name (two place_ids, one name). Splitting them into
        two cards gave two identical "Brand Store" tiles above review cards that all read
        "Brand Store", so the reader could not tell which card covered which reviews. One
        card counts them all and says how many places share the name;
      - reviews that name no place (older raw data) are labelled with the run subject, so
        their card is titled with it and says they cannot be tied to one place.

    Every number is the sample (score_basis/distribution_basis "sample"), like the base card.

    Cards are grouped by the text a review card prints (_js_str of the label), so two labels
    that print the same (a business name of 5 and one of "5") share one card, never two
    tiles with one title.

    The Maps cards account for every Maps review, or there are none. A card with no stars,
    score or tags is normally not worth a tile (_snapshot_worth_showing), but dropping ONE card
    of several left the rest adding up to fewer than the run's Maps reviews with nothing on the
    page saying why. So a star-less card is kept beside the others (its title and "N reviews
    analysed here" are still true); only when no card has anything to show is the whole Maps
    rail left out, as for every other source."""
    groups, order = {}, []
    for r in recs:
        label = _js_str(_maps_review_label(r))
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(r)

    def named(r):
        return _maps_place_name(r) is not None
    # Cards for named places first, then the cards for reviews that name no place.
    order = [k for k in order if any(named(r) for r in groups[k])] + \
            [k for k in order if not any(named(r) for r in groups[k])]
    out = []
    for label in order:
        g = groups[label]
        ids = list(dict.fromkeys((r.get("data") or {}).get("place_id") for r in g
                                 if (r.get("data") or {}).get("place_id")))
        snap = _base_snapshot("maps", g)
        snap["title"] = label
        if all(named(r) for r in g) and len(ids) <= 1:
            pass                               # one place: a place card (scope "place")
        elif any(named(r) for r in g) and len(ids) >= 2:
            snap["scope"] = "source"
            gap = any(not (r.get("data") or {}).get("place_id") for r in g)
            snap["subtitle"] = (f"Reviews from {'at least ' if gap else ''}{len(ids)} "
                                "places with this name")
        else:
            snap["scope"] = "source"
            if len(order) > 1:
                snap["subtitle"] = "Reviews that cannot be tied to one place"
        out.append(snap)
    return out if any(_snapshot_worth_showing(s) for s in out) else []


def _enrich_trustpilot(workdir, snap):
    """Trustpilot: overlay the company's OWN aggregate (companyTrustScore / companyTotalReviews
    / companyCategories) from the untouched actor record, where post-processing dropped it.
    Verified against the Acme Fitness run (TrustScore 2.9, 6,760 total reviews, one category, all
    constant across the 20 records). The star HISTOGRAM stays sample-based -- the raw carries
    no full-corpus distribution, so we do not invent one; the sample histogram keeps its
    'sample' basis and the TrustScore/total carry the authoritative 'source' basis beside it."""
    raw = _read_json(Path(workdir) / "trustpilot_reviews" / "run_02_full.raw.json", [])
    if not isinstance(raw, list):
        return
    rec = next((r for r in raw if isinstance(r, dict) and r.get("companyName")), None)
    if not rec:
        return
    snap["title"] = rec.get("companyName") or snap["title"]
    snap["subtitle"] = rec.get("companyDomain") or snap["subtitle"]
    snap["url"] = rec.get("companyUrl") or snap["url"]
    ts = rec.get("companyTrustScore")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        snap["score"] = ts
        snap["score_label"] = "Trustpilot TrustScore"
        snap["score_basis"] = "source"
    total = rec.get("companyTotalReviews")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        snap["total_reviews"] = int(total)
    cats = rec.get("companyCategories")
    if isinstance(cats, list):
        snap["tags"] = [{"title": c} for c in cats if c]


def _source_snapshots(workdir, sources, records, notes=None):
    """One aggregate card per reviewed source -- the "fully rethought" snapshot rail.

    Design (see _sample_stats / _base_snapshot): every non-Reddit source gets a snapshot
    built from the records the viewer holds; sources whose authoritative full-corpus figures
    we have verified (Maps, Trustpilot) are enriched with those. This is intentionally NOT
    hardcoded to the two sources our test data covers -- App Store, Google Play, G2 and
    Capterra get the same record-derived card automatically, and each snapshot is labelled by
    its source so the UI never implies one source is 'the' snapshot. Reddit is excluded on
    purpose (open discussion, synthetic banded stars -> no honest star aggregate).
    `notes`, when a list, receives the Maps note (see _maps_snapshots)."""
    by_src = {}
    for r in records:
        by_src.setdefault(r.get("platform"), []).append(r)
    out = []
    for src in sources:
        if src == "reddit":
            continue
        recs = by_src.get(src) or []
        if not recs:
            continue
        if src == "maps":
            # Maps normally gets one ENRICHED card per scraped place (Google's own rating,
            # corpus total, topic tags) from B2a_maps_places. It is the ONLY source that
            # depends on an external file -- and it must not be the only source that can
            # silently show nothing. If the place file is absent or lists no place, fall back
            # to the SAME record-derived base card every other source gets (sample size +
            # histogram + sample mean, basis "sample"). Enrichment is a bonus, never a
            # precondition, so maps is now as robust as the rest. When the file DOES list
            # places, only places with collected reviews get a card, and anything else is
            # stated in the note, never folded into a card as if it were that place's.
            # With no place file (a run given place links), the record-derived cards are
            # still split by the place each review names (_maps_record_snapshots), so one
            # card never presents several places as one.
            maps_snaps = _maps_snapshots(workdir, recs, notes)
            if maps_snaps is None:
                maps_snaps = _maps_record_snapshots(recs)
            out.extend(maps_snaps)
            continue
        snap = _base_snapshot(src, recs)
        if src == "trustpilot":
            _enrich_trustpilot(workdir, snap)
        if _snapshot_worth_showing(snap):
            out.append(snap)
    return out


def _discover_reports(workdir):
    """Link the synthesised MD reports already in the run dir (rendered client-side)."""
    known = [("E1", "E1_pain_points.md", "Pain-point taxonomy"),
             ("E2", "E2_trust_signals.md", "Trust signals"),
             ("E3", "E3_reddit_topics.md", "Reddit topics")]
    return [{"id": rid, "title": title, "path": name}
            for rid, name, title in known if (Path(workdir) / name).exists()]


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE = re.compile(r'^v\d+$', re.IGNORECASE)


def _subject_from_dirname(workdir):
    """Best-effort subject from a run dir named pain_<slug>_<YYYY-MM-DD>[_vN].
    The slug may contain underscores: everything between the prefix and the date.
    Unconventional names (no date found, empty slug, unrecognized prefix) return as-is."""
    name = Path(workdir).name
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] == "pain":
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
    if not cfg.get("sources"):
        # No config and no earlier index: take the sources whose data is on disk.
        cfg["sources"] = [s for s, folder in _REVIEW_FOLDERS.items()
                          if (workdir / folder / "run_02_full.json").exists()] or ["maps"]
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
# Run with:  python3 scripts/emit_index.py
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

    th = (3, 4)

    # A Trustpilot low-star review -> review envelope, low polarity, extras mapped.
    tp = {"text": "billed twice, no refund", "stars": 1, "businessName": "Acme",
          "source": "trustpilot", "date": "2026-08-01", "id": "r1",
          "isVerified": True, "companyDomain": "acme.com", "language": "en"}
    env = _record_envelope(tp, "trustpilot", "Acme Market", "trustpilot_reviews", 0, th)
    check("kind is review", env["kind"], "review")
    check("platform carried", env["platform"], "trustpilot")
    check("uid uses native id", env["uid"], "trustpilot:r1")
    check("uid_native true when native id present", env["uid_native"], True)
    check("polarity low", env["tags"]["polarity"], "low")
    check("verified mapped", env["data"]["verified"], True)
    check("company_domain mapped", env["data"]["company_domain"], "acme.com")
    check("stars in data", env["data"]["stars"], 1)
    check("raw_ref points back", env["raw_ref"], {"file": "trustpilot_reviews/run_02_full.json", "index": 0})
    check("no media for reviews", env["media"], [])

    # A high-star review -> high polarity.
    hi = {"text": "fast and fair", "stars": 5, "businessName": "Acme",
          "source": "trustpilot", "id": "r2"}
    check("polarity high", _record_envelope(hi, "trustpilot", "Acme", "trustpilot_reviews", 1, th)["tags"]["polarity"], "high")

    # A reddit record with no id and no stars -> post kind, uid falls back to index, banding label.
    rd = {"text": "anyone else hate their billing?", "stars": None, "businessName": "",
          "source": "reddit", "subreddit": "sysadmin", "score": 42, "is_comment": False}
    renv = _record_envelope(rd, "reddit", "Market", "reddit_reviews", 7, th)
    check("reddit is post", renv["kind"], "post")
    check("uid falls back to index", renv["uid"], "reddit:7")
    check("uid_native false when no id", renv["uid_native"], False)
    check("subreddit mapped", renv["data"]["subreddit"], "sysadmin")
    check("reddit_score mapped", renv["data"]["reddit_score"], 42)
    check("banding present", renv["tags"]["banding"], "unbanded")
    check("subject falls back to run subject when business blank", renv["subject"], "Market")

    # B1 -- Maps date fallback. Maps (B2b) records carry publishedAtDate, not date.
    check("date passthrough when present", _review_date({"date": "2026-08-01"}), "2026-08-01")
    check("maps date falls back to publishedAtDate",
          _review_date({"publishedAtDate": "2026-07-15T09:30:00.000Z"}), "2026-07-15T09:30:00.000Z")
    check("date None when neither present", _review_date({}), None)
    maps_rec = {"text": "clean store", "stars": 5, "businessName": "Acme",
                "publishedAtDate": "2026-07-15T09:30:00.000Z", "id": "m1"}
    check("maps envelope date populated",
          _record_envelope(maps_rec, "maps", "Acme", "B2b_reviews", 0, th)["date"],
          "2026-07-15T09:30:00.000Z")

    # Maps uid: reviewId takes precedence over positional fallback.
    # A real Maps record has id=null and a non-empty reviewId: uid must be maps:<reviewId>.
    real_maps = {"text": "t", "stars": 4, "id": None,
                 "reviewId": "Ci9DQUlRQUNvZENodGxlR0Z0Y0d4bFgzSmxkbWxsZDE5MGIydGxibDh3TURBeGVIZxAB"}
    rm_env = _record_envelope(real_maps, "maps", "Acme", "B2b_reviews", 0, th)
    check("maps uid uses reviewId when id is null",
          rm_env["uid"],
          "maps:Ci9DQUlRQUNvZENodGxlR0Z0Y0d4bFgzSmxkbWxsZDE5MGIydGxibDh3TURBeGVIZxAB")
    check("maps uid_native true when reviewId present", rm_env["uid_native"], True)
    # An Acme Plumbing-style Maps record (id=null, reviewId=null): must fall back to position.
    stripped_maps = {"text": "bad", "stars": 1, "id": None, "reviewId": None}
    sm_env = _record_envelope(stripped_maps, "maps", "Acme", "B2b_reviews", 5, th)
    check("maps uid falls back to position when both id and reviewId absent",
          sm_env["uid"], "maps:5")
    check("maps uid_native false when both id and reviewId absent", sm_env["uid_native"], False)
    # A Maps record that somehow has a non-null id (for completeness): id wins.
    id_maps = {"text": "good", "stars": 5, "id": "legacy_id", "reviewId": "rid"}
    id_env = _record_envelope(id_maps, "maps", "Acme", "B2b_reviews", 0, th)
    check("maps uid uses id when id is non-null", id_env["uid"], "maps:legacy_id")
    check("maps uid_native true when id present", id_env["uid_native"], True)

    # B4 -- permalink recovery.
    # Trustpilot: constructed from the processed id (which IS the reviewId), no raw read.
    check("trustpilot url constructed from id when no raw",
          _review_url({"id": "6a1b2c"}, "trustpilot", None),
          "https://www.trustpilot.com/reviews/6a1b2c")
    # Prefer the actor's own reviewUrl when the raw file is present.
    check("trustpilot prefers raw reviewUrl over construction",
          _review_url({"id": "6a1b2c"}, "trustpilot",
                      {"6a1b2c": {"reviewUrl": "https://www.trustpilot.com/reviews/6a1b2c?x=1"}}),
          "https://www.trustpilot.com/reviews/6a1b2c?x=1")
    check("trustpilot envelope url constructed",
          _record_envelope({"text": "x", "id": "6a1b2c"}, "trustpilot", "Acme", "trustpilot_reviews", 0, th)["url"],
          "https://www.trustpilot.com/reviews/6a1b2c")
    # Reddit: joined by exact id to the raw actor record's postUrl.
    reddit_raw = {"t3_abc": {"id": "t3_abc", "postUrl": "https://www.reddit.com/r/x/comments/abc/t/"}}
    check("reddit post url from raw join",
          _review_url({"id": "t3_abc"}, "reddit", reddit_raw),
          "https://www.reddit.com/r/x/comments/abc/t/")
    check("reddit falls back to contentUrl when no postUrl",
          _review_url({"id": "t3_d"}, "reddit", {"t3_d": {"contentUrl": "https://ext.example/y"}}),
          "https://ext.example/y")
    check("reddit comment (id absent from raw) -> no url, no crash",
          _review_url({"id": "t1_comment"}, "reddit", reddit_raw), None)
    check("reddit 'None' string treated as missing",
          _review_url({"id": "t3_e"}, "reddit", {"t3_e": {"postUrl": "None"}}), None)
    check("reddit comment url recovered from raw url field",
          _review_url({"id": "c9"}, "reddit",
                      {"c9": {"id": "c9", "url": "https://www.reddit.com/r/x/comments/abc/t/c9/"}}),
          "https://www.reddit.com/r/x/comments/abc/t/c9/")
    check("reddit raw url 'None' string treated as missing",
          _review_url({"id": "c10"}, "reddit", {"c10": {"url": "None"}}), None)
    # Maps: per-review link rebuilt from reviewId + cid (values from a real run; the expected URL is
    # the one verified to open that exact review).
    acme_rid = "Ci9DQUlRQUNvZENodGxlR0Z0Y0d4bFgzSmxkbWxsZDE5MGIydGxibDh3TURBeGVIZxAB"
    acme_url = ("https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5!1s" + acme_rid +
                   "!2m1!1s0x0:0x112210f47de98115!3m1!1s2@1:"
                   "CAIQACodChtleGFtcGxlX3Jldmlld190b2tlbl8wMDAxeHg")
    place = "https://www.google.com/maps/search/?api=1&query=X&query_place_id=abc"
    check("maps per-review link rebuilt from reviewId + cid",
          _review_url({"reviewId": acme_rid, "cid": "1234567890123456789", "url": place},
                      "maps", None), acme_url)
    check("maps prefers the actor's own reviewUrl",
          _review_url({"reviewUrl": "https://www.google.com/maps/reviews/own", "reviewId": acme_rid,
                       "cid": "1234567890123456789", "url": place}, "maps", None),
          "https://www.google.com/maps/reviews/own")
    check("maps 'None' reviewUrl string ignored",
          _review_url({"reviewUrl": "None", "reviewId": acme_rid, "cid": "1234567890123456789",
                       "url": place}, "maps", None), acme_url)
    # Guards: anything not in the verified shape keeps the business page, never a broken link.
    check("maps no reviewId -> business page", _review_url({"cid": "1", "url": place}, "maps", None), place)
    check("maps no cid -> business page", _review_url({"reviewId": acme_rid, "url": place}, "maps", None), place)
    check("maps non-numeric cid -> business page",
          _review_url({"reviewId": acme_rid, "cid": "abc", "url": place}, "maps", None), place)
    check("maps reviewId outside base64url alphabet -> business page",
          _review_url({"reviewId": "Ci9D+QUl/", "cid": "1", "url": place}, "maps", None), place)
    check("maps reviewId not the expected protobuf -> business page",
          _review_url({"reviewId": "aGVsbG8gd29ybGQ", "cid": "1", "url": place}, "maps", None), place)
    check("maps zero cid -> business page",
          _review_url({"reviewId": acme_rid, "cid": "0", "url": place}, "maps", None), place)
    check("maps protobuf ok but inner token not base64url -> business page",
          _review_url({"reviewId": "CgVhYiFjZBAB", "cid": "1", "url": place}, "maps", None), place)
    check("maps nothing at all -> None", _review_url({}, "maps", None), None)
    check("maps envelope carries the per-review link",
          _record_envelope({"text": "t", "reviewId": acme_rid, "cid": "1234567890123456789",
                            "url": place}, "maps", "Acme", "B2b_reviews", 0, th)["url"], acme_url)

    # Reviews-depth extras. Trustpilot title + company reply come from the RAW record.
    tp_raw = {"tp1": {"reviewUrl": "https://www.trustpilot.com/reviews/tp1",
                      "title": "Billed twice", "replyMessage": "We are sorry to hear this."}}
    tpx = _record_envelope({"text": "x", "id": "tp1", "stars": 1}, "trustpilot", "Acme",
                           "trustpilot_reviews", 0, th, tp_raw)
    check("trustpilot review_title from raw", tpx["data"]["review_title"], "Billed twice")
    check("trustpilot company_reply from raw", tpx["data"]["company_reply"], "We are sorry to hear this.")
    # Maps credibility + owner reply come from the PROCESSED record itself.
    mx = _record_envelope({"text": "y", "id": "m1", "stars": 5, "isLocalGuide": True,
                           "reviewerNumberOfReviews": 60, "responseFromOwnerText": "Thank you!",
                           "title": "Acme Fitness Studios New York"}, "maps", "Acme", "B2b_reviews", 0, th)
    check("maps is_local_guide", mx["data"]["is_local_guide"], True)
    check("maps reviewer_review_count", mx["data"]["reviewer_review_count"], 60)
    check("maps company_reply from responseFromOwnerText", mx["data"]["company_reply"], "Thank you!")
    check("maps place-name title NOT used as review_title", "review_title" in mx["data"], False)
    # Reddit engagement from raw; neutral set-aside gets banding=neutral (no stars).
    rd_raw = {"t3_a": {"title": "Guide discontinued", "upvoteRatio": 0.92, "commentsCount": 47}}
    rdx = _record_envelope({"text": "z", "id": "t3_a", "stars": 5, "band": "praise"},
                           "reddit", "M", "reddit_reviews", 0, th, rd_raw)
    check("reddit review_title from raw", rdx["data"]["review_title"], "Guide discontinued")
    check("reddit upvote_ratio", rdx["data"]["upvote_ratio"], 0.92)
    check("reddit comments_count", rdx["data"]["comments_count"], 47)
    check("reddit praise keeps star-derived banding", rdx["tags"]["banding"], "high")
    neutral = _record_envelope({"text": "meh", "id": "cx", "band": "neutral"},
                               "reddit", "M", "reddit_reviews", 1, th)
    check("reddit neutral set-aside banding from band field", neutral["tags"]["banding"], "neutral")
    check("reddit neutral has no polarity tag", "polarity" in neutral["tags"], False)

    # Subject inference from dir name.
    check("subject from dirname", _subject_from_dirname(Path("/x/pain_acme-plumbing_2026-09-19")), "Acme Plumbing")
    # Folder parser: _vN suffix is stripped, multi-word slug is preserved, unconventional falls back
    check("dirname: normal name (no suffix)", _subject_from_dirname(Path("/x/pain_acme-fitness_2026-09-28")), "Acme Fitness")
    check("dirname: _v2 suffix is stripped — same subject as base run", _subject_from_dirname(Path("/x/pain_acme-fitness_2026-09-28_v2")), "Acme Fitness")
    check("dirname: _v12 suffix is stripped", _subject_from_dirname(Path("/x/pain_acme-fitness_2026-09-28_v12")), "Acme Fitness")
    check("dirname: slug with underscores parsed correctly", _subject_from_dirname(Path("/x/pain_acme-fitness_multi_2026-08-28")), "Acme Fitness Multi")
    check("dirname: unconventional name (no date) returns as-is", _subject_from_dirname(Path("/x/pain_acme-fitness_nodatehereXYZ")), "pain_acme-fitness_nodatehereXYZ")

    # --- Source snapshots (the "fully rethought" rail): universal record-derived baseline. ---
    def _rec(platform, subject, stars):
        return {"platform": platform, "subject": subject, "data": {"stars": stars}}
    # _sample_stats: honest count + histogram + mean from the records we hold, any source.
    size, avg, hist = _sample_stats([_rec("g2", "Acme", 5), _rec("g2", "Acme", 5),
                                     _rec("g2", "Acme", 1), _rec("g2", "Acme", None)])
    check("sample size counts all records incl. star-less", size, 4)
    check("sample histogram ignores star-less", hist, {"5": 2, "1": 1})
    check("sample avg over starred only", avg, 3.7)  # (5+5+1)/3
    check("sample avg None when no stars", _sample_stats([_rec("x", "A", None)])[1], None)
    # A source we have NO calibration data for still gets a labelled baseline card.
    g2snap = _base_snapshot("g2", [_rec("g2", "Acme Software", 4), _rec("g2", "Acme Software", 5)])
    check("unseen source scope labelled", g2snap["scope"], "product")
    check("unseen source title from subject", g2snap["title"], "Acme Software")
    check("unseen source score is the sample mean", g2snap["score"], 4.5)
    check("unseen source score basis stated as sample", g2snap["score_basis"], "sample")
    check("unseen source distribution basis stated as sample", g2snap["distribution_basis"], "sample")
    check("appstore scope labelled app", _base_snapshot("appstore", [_rec("appstore", "A", 3)])["scope"], "app")
    check("unknown source falls back to generic scope",
          _base_snapshot("newsource", [_rec("newsource", "A", 3)])["scope"], "source")
    # _snapshot_worth_showing: needs a number or a theme, not just a name.
    check("snapshot with a score is worth showing", _snapshot_worth_showing(g2snap), True)
    check("empty-of-signal snapshot is dropped",
          _snapshot_worth_showing({"sample_size": 5, "score": None, "distribution": None, "tags": []}),
          False)
    check("zero-record snapshot is dropped",
          _snapshot_worth_showing({"sample_size": 0, "score": 4.0, "distribution": {"4": 1}, "tags": []}),
          False)
    # Reddit is excluded from the rail by construction (synthetic banded stars).
    reddit_only = _source_snapshots(Path("/nonexistent"), ["reddit"],
                                    [_rec("reddit", "M", 5), _rec("reddit", "M", None)])
    check("reddit produces no snapshot", reddit_only, [])

    # --- Maps snapshot: the special path + its new fallback (was entirely UNTESTED). ---
    # Maps is the only source that depends on an external place file; both branches are
    # pinned here so it can never again be the one source that silently shows nothing.
    import tempfile
    maps_recs = [_rec("maps", "Joe's Plumbing", 1), _rec("maps", "Joe's Plumbing", 5),
                 _rec("maps", "Joe's Plumbing", 2)]
    # (a) No place file -> record-derived FALLBACK card (never silently empty). These records
    # name no place, so the card may not claim to be one place: its scope is "source".
    fb = _source_snapshots(Path("/nonexistent"), ["maps"], maps_recs)
    check("maps with no place file still yields one card", len(fb), 1)
    check("maps fallback for reviews naming no place is not a place card", fb[0]["scope"], "source")
    check("maps fallback score basis is sample", fb[0]["score_basis"], "sample")
    check("maps fallback score is the sample mean", fb[0]["score"], 2.7)  # (1+5+2)/3
    check("maps fallback distribution present", bool(fb[0]["distribution"]), True)
    # (b) Place file present -> ENRICHED per-place card (Google's rating, basis source).
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td) / "B2a_maps_places"; pdir.mkdir()
        (pdir / "run_02_full.json").write_text(json.dumps(
            [{"title": "Joe's Plumbing", "totalScore": 4.6, "reviewsCount": 812,
              "categoryName": "Plumber", "reviewsTags": [{"title": "price", "count": 12}]}]))
        en = _source_snapshots(Path(td), ["maps"], maps_recs)
    check("maps with place file yields one card", len(en), 1)
    check("maps enriched score is Google rating", en[0]["score"], 4.6)
    check("maps enriched score basis is source", en[0]["score_basis"], "source")
    check("maps enriched carries corpus total", en[0]["total_reviews"], 812)
    check("maps enriched histogram stays sample basis", en[0]["distribution_basis"], "sample")
    # (c) Maps records with no stars and no place file -> no false card.
    nostars = _source_snapshots(Path("/nonexistent"), ["maps"], [_rec("maps", "X", None)])
    check("maps with no stars and no place file shows nothing", nostars, [])

    # --- Maps per-place attribution. A Maps search lists a page of places but
    # collects reviews for only the top few. A place card says "N analysed here", so it may
    # count ONLY reviews collected from that place. The old `... or recs` fallback gave every
    # unmatched place the whole run's reviews (24 cards each claiming the run's 20). Records
    # are built through _record_envelope from the RAW Maps review shape (place `title` +
    # `placeId`, no businessName -> subject is the run subject), exactly as a real run is.
    def _maps_raw(pid, place, stars, n):
        return {"title": place, "placeId": pid, "stars": stars, "text": f"synthetic {n}",
                "reviewId": f"rv{n}", "id": None}

    def _maps_world(places, raw_reviews, subject="Brand"):
        envs = [_record_envelope(r, "maps", subject, "B2b_reviews", i, th)
                for i, r in enumerate(raw_reviews)]
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "B2a_maps_places"; pdir.mkdir()
            (pdir / "run_02_full.json").write_text(json.dumps(places))
            notes = []
            snaps = _source_snapshots(Path(td), ["maps"], envs, notes)
        return snaps, notes, envs

    five = [{"title": f"Brand Place {k}", "placeId": f"PID{k}", "totalScore": 4.0 + k / 10,
             "reviewsCount": 100 * (k + 1)} for k in range(5)]
    # (d) 5 places, reviews collected for place 2 only (the phase8_e2e shape): 4x5, 1x3, 2x1.
    stars2 = [5, 5, 5, 5, 3, 1, 1]
    s5, n5, e5 = _maps_world(five, [_maps_raw("PID2", "Brand Place 2", s, i)
                                    for i, s in enumerate(stars2)])
    check("envelope carries the review's placeId", e5[0]["data"].get("place_id"), "PID2")
    check("envelope carries the review's place name", e5[0]["data"].get("place_name"), "Brand Place 2")
    check("5 places, reviews for 1: exactly one card", [s["title"] for s in s5], ["Brand Place 2"])
    check("5 places, reviews for 1: that card counts only its reviews", s5[0]["sample_size"] if s5 else None, 7)
    check("5 places, reviews for 1: that card's histogram is its own",
          s5[0]["distribution"] if s5 else None, {"5": 4, "3": 1, "1": 2})
    check("5 places, reviews for 1: no other place claims a sample",
          [s for s in s5 if s["title"] != "Brand Place 2"], [])
    check("5 places, reviews for 1: the note counts the 4 unreviewed places",
          n5, [{"source": "maps", "places_found": 5, "places_not_reviewed": 4,
                "reviews_unmatched": 0}])
    # (e) ONE place, reviews carry a different name and no placeId: the one legitimate
    # fallback -- the search found only that place, so they are provably its reviews.
    one = [{"title": "Brand Flagship", "placeId": "PIDX", "totalScore": 4.4, "reviewsCount": 306}]
    s1, n1, _ = _maps_world(one, [{"title": "Brand", "stars": 4, "text": "a", "reviewId": "a"},
                                  {"stars": 2, "text": "b", "reviewId": "b"}])
    check("1 place, unmatched names: fallback attributes them", [(s["title"], s["sample_size"]) for s in s1],
          [("Brand Flagship", 2)])
    check("1 place, unmatched names: histogram from them", s1[0]["distribution"] if s1 else None, {"4": 1, "2": 1})
    check("1 place, all attributed: no note", n1, [])
    # (e2) ONE place, but the reviews name ANOTHER placeId: provably not its reviews.
    s1b, n1b, _ = _maps_world(one, [_maps_raw("PIDOTHER", "Elsewhere", 5, 1)])
    check("1 place, contradicting placeId: no card claims them", s1b, [])
    check("1 place, contradicting placeId: stated as unmatched",
          n1b, [{"source": "maps", "places_found": 1, "places_not_reviewed": 1,
                 "reviews_unmatched": 1}])
    # (f) Multi-place, every place has reviews: each gets exactly its own.
    three = five[:3]
    sm, nm, _ = _maps_world(three, [_maps_raw("PID0", "Brand Place 0", 5, 0),
                                    _maps_raw("PID1", "Brand Place 1", 1, 1),
                                    _maps_raw("PID1", "Brand Place 1", 2, 2),
                                    _maps_raw("PID2", "Brand Place 2", 4, 3),
                                    _maps_raw("PID2", "Brand Place 2", 4, 4),
                                    _maps_raw("PID2", "Brand Place 2", 3, 5)])
    check("multi-place: each place its own count",
          [(s["title"], s["sample_size"]) for s in sm],
          [("Brand Place 0", 1), ("Brand Place 1", 2), ("Brand Place 2", 3)])
    check("multi-place: each place its own histogram", [s["distribution"] for s in sm],
          [{"5": 1}, {"1": 1, "2": 1}, {"4": 2, "3": 1}])
    check("multi-place, all reviewed: no note", nm, [])
    # (g) Two branches share a title and the reviews carry no placeId: the name proves
    # nothing, so neither branch may claim them.
    twins = [{"title": "Brand", "placeId": "A", "totalScore": 4.0, "reviewsCount": 9},
             {"title": "Brand", "placeId": "B", "totalScore": 3.0, "reviewsCount": 9}]
    st, nt, _ = _maps_world(twins, [{"title": "Brand", "stars": 5, "text": "x", "reviewId": "x"}])
    check("shared title, no placeId: no card claims the review", st, [])
    check("shared title, no placeId: stated as unmatched", nt[0]["reviews_unmatched"], 1)
    # (h) Matching by name when the review has no placeId and the title is unique.
    sn, _, _ = _maps_world(three, [{"title": "Brand Place 1", "stars": 5, "text": "y", "reviewId": "y"}])
    check("unique name, no placeId: attributed by name",
          [(s["title"], s["sample_size"]) for s in sn], [("Brand Place 1", 1)])
    # (i) The placeId wins over the name: a review whose place name differs from the listing
    # (a renamed venue) still belongs to the place its placeId names.
    si, ni, _ = _maps_world(three, [_maps_raw("PID1", "Brand Place 1 (old name)", 5, 1)])
    check("placeId match despite a different name",
          [(s["title"], s["sample_size"]) for s in si], [("Brand Place 1", 1)])

    # --- Maps cards with NO place file: a run given place links has no places
    # step. It showed ONE "place" card titled with the run subject counting all three
    # places' reviews ("Acme Fitness Multi", 60) above review cards naming 3 places x 20. Each
    # place now gets its own card from the records' own place_id / place_name.
    def _link_recs(raws, subject="Brand Multi"):
        return [_record_envelope(r, "maps", subject, "B2b_reviews", i, th) for i, r in enumerate(raws)]
    lk = _link_recs([_maps_raw("PA", "Brand A", 5, 0), _maps_raw("PB", "Brand B", 1, 1),
                     _maps_raw("PA", "Brand A", 3, 2), _maps_raw("PC", "Brand C", 4, 3),
                     _maps_raw("PB", "Brand B", 2, 4)])
    lnotes = []
    ls_ = _source_snapshots(Path("/nonexistent"), ["maps"], lk, lnotes)
    check("links: one card per place, own counts, first-seen order",
          [(s["title"], s["scope"], s["sample_size"]) for s in ls_],
          [("Brand A", "place", 2), ("Brand B", "place", 2), ("Brand C", "place", 1)])
    check("links: each card's histogram is its own", [s["distribution"] for s in ls_],
          [{"5": 1, "3": 1}, {"1": 1, "2": 1}, {"4": 1}])
    check("links: each card's score is its own sample mean", [s["score"] for s in ls_], [4.0, 1.5, 4.0])
    check("links: scores stated as sample, never as Google's rating",
          {(s["score_basis"], s["score_label"], s["total_reviews"]) for s in ls_},
          {("sample", "average of the reviews analysed", None)})
    check("links: the cards' counts add up to the run's Maps reviews",
          sum(s["sample_size"] for s in ls_), len(lk))
    check("links: no place-search note (no search ran)", lnotes, [])
    # Name missing on one review: its card is labelled with the subject, not "Brand A", so the
    # "Brand A" card may not count it (the card's count must equal the cards carrying its name).
    nm_ = _source_snapshots(Path("/nonexistent"), ["maps"],
                            _link_recs([{"placeId": "PA", "stars": 5, "text": "a", "reviewId": "a"},
                                        _maps_raw("PA", "Brand A", 3, 1)]))
    check("links: a review without a place name is not counted on the named card",
          [(s["title"], s["scope"], s["sample_size"]) for s in nm_],
          [("Brand A", "place", 1), ("Brand Multi", "source", 1)])
    # A review with the name but no place id joins that name's one place.
    ni_ = _source_snapshots(Path("/nonexistent"), ["maps"],
                            _link_recs([_maps_raw("PA", "Brand A", 5, 0),
                                        {"title": "Brand A", "stars": 3, "text": "b", "reviewId": "b"}]))
    check("links: same name, one place id: one card",
          [(s["title"], s["scope"], s["sample_size"]) for s in ni_], [("Brand A", "place", 2)])
    # A chain: one name, two place ids (two "Brand" branches). Every review card of both
    # branches reads "Brand", so two "Brand" cards could not say which reviews each covered
    # (the first version split them into two identical tiles). One card counts every review
    # named "Brand", including one with no id, and does not call itself a place.
    def _rail_agrees(recs, snaps):
        """The rule the page must keep: titles are unique, and each card counts exactly the
        reviews whose card shows its title (_maps_review_label mirrors app.js reviewSource)."""
        titles = [s["title"] for s in snaps]
        return len(titles) == len(set(titles)) and all(
            s["sample_size"] == sum(1 for r in recs if _js_str(_maps_review_label(r)) == s["title"])
            for s in snaps)
    chr_ = _link_recs([_maps_raw("P1", "Brand", 5, 0), _maps_raw("P2", "Brand", 1, 1),
                       _maps_raw("P1", "Brand", 4, 2),
                       {"title": "Brand", "stars": 2, "text": "c", "reviewId": "c"}])
    ch_ = _source_snapshots(Path("/nonexistent"), ["maps"], chr_)
    check("links, chain with an id-less review: one card for the name, not a place card",
          [(s["title"], s["scope"], s["sample_size"], s["subtitle"]) for s in ch_],
          [("Brand", "source", 4, "Reviews from at least 2 places with this name")])
    check("links, chain: its numbers are the whole name's sample",
          (ch_[0]["score"], ch_[0]["distribution"]) if ch_ else None,
          (3.0, {"5": 1, "1": 1, "4": 1, "2": 1}))
    check("links, chain with an id-less review: the rail agrees with the review cards",
          _rail_agrees(chr_, ch_), True)
    # A chain beside distinct places, all reviews carrying ids: the chain gets one card that
    # says how many places share its name; the other places keep their own place cards.
    cmr_ = _link_recs([_maps_raw("P1", "Brand Store", 5, 0), _maps_raw("P3", "Other Place", 2, 1),
                       _maps_raw("P2", "Brand Store", 1, 2), _maps_raw("P3", "Other Place", 4, 3),
                       _maps_raw("P1", "Brand Store", 3, 4), _maps_raw("P4", "Third Place", 5, 5)])
    cm_ = _source_snapshots(Path("/nonexistent"), ["maps"], cmr_)
    check("links, chain plus distinct places: one card per name, first-seen order",
          [(s["title"], s["scope"], s["sample_size"], s["subtitle"]) for s in cm_],
          [("Brand Store", "source", 3, "Reviews from 2 places with this name"),
           ("Other Place", "place", 2, None), ("Third Place", "place", 1, None)])
    check("links, chain plus distinct places: the rail agrees with the review cards",
          _rail_agrees(cmr_, cm_), True)
    # A chain beside reviews that name no place: those keep their own non-place card.
    clr_ = _link_recs([_maps_raw("P1", "Brand Store", 5, 0), _maps_raw("P2", "Brand Store", 1, 1),
                       {"stars": 2, "text": "n", "reviewId": "n"}])
    cl_ = _source_snapshots(Path("/nonexistent"), ["maps"], clr_)
    check("links, chain plus reviews naming no place",
          [(s["title"], s["scope"], s["sample_size"], s["subtitle"]) for s in cl_],
          [("Brand Store", "source", 2, "Reviews from 2 places with this name"),
           ("Brand Multi", "source", 1, "Reviews that cannot be tied to one place")])
    check("links, chain plus reviews naming no place: the rail agrees", _rail_agrees(clr_, cl_), True)
    # A place named like the run subject, beside reviews that name no place (their cards
    # also read the subject): one card, never two tiles with one title.
    ssr_ = _link_recs([_maps_raw("P1", "Brand Multi", 5, 0), {"stars": 1, "text": "m", "reviewId": "m"}])
    ss_ = _source_snapshots(Path("/nonexistent"), ["maps"], ssr_)
    check("links, place named like the subject plus unnamed reviews: one card, not a place card",
          [(s["title"], s["scope"], s["sample_size"]) for s in ss_], [("Brand Multi", "source", 2)])
    check("links, place named like the subject: the rail agrees", _rail_agrees(ssr_, ss_), True)
    # A place name without an id still groups by the name.
    nn_ = _source_snapshots(Path("/nonexistent"), ["maps"],
                            _link_recs([{"title": "Brand D", "stars": 2, "text": "d", "reviewId": "d"},
                                        {"title": "Brand D", "stars": 4, "text": "e", "reviewId": "e"}]))
    check("links: name without id groups by name",
          [(s["title"], s["scope"], s["sample_size"]) for s in nn_], [("Brand D", "place", 2)])
    # Mixed: reviews that name no place get one card that does not call itself a place.
    mx_ = _source_snapshots(Path("/nonexistent"), ["maps"],
                            _link_recs([_maps_raw("PA", "Brand A", 5, 0),
                                        {"stars": 1, "text": "n1", "reviewId": "n1"},
                                        {"stars": 2, "text": "n2", "reviewId": "n2"}]))
    check("links, mixed: place card plus one non-place card for the rest",
          [(s["title"], s["scope"], s["sample_size"]) for s in mx_],
          [("Brand A", "place", 1), ("Brand Multi", "source", 2)])
    check("links, mixed: the non-place card says why it is separate", mx_[1]["subtitle"] if len(mx_) > 1 else None,
          "Reviews that cannot be tied to one place")
    # One place link: one card named after the place.
    one_ = _source_snapshots(Path("/nonexistent"), ["maps"],
                             _link_recs([_maps_raw("PA", "Brand A", 5, 0), _maps_raw("PA", "Brand A", 4, 1)]))
    check("links, one place: one card named after the place",
          [(s["title"], s["scope"], s["sample_size"], s["subtitle"]) for s in one_],
          [("Brand A", "place", 2, None)])
    # A place whose reviews carry no stars shows no card, as before.
    check("links: star-less place shows no card",
          _source_snapshots(Path("/nonexistent"), ["maps"],
                            _link_recs([{"title": "X", "placeId": "PX", "text": "t", "reviewId": "t"}])), [])
    # A star-less place beside a rated one keeps its card: dropping it left the rail
    # claiming 2 of the run's 4 Maps reviews with nothing on the page saying why.
    nsr_ = _link_recs([_maps_raw("P1", "Normal Place", 5, 0), _maps_raw("P1", "Normal Place", 3, 1),
                       {"title": "Quiet Place", "placeId": "P2", "text": "q1", "reviewId": "q1"},
                       {"title": "Quiet Place", "placeId": "P2", "text": "q2", "reviewId": "q2"}])
    ns_ = _source_snapshots(Path("/nonexistent"), ["maps"], nsr_)
    check("links, star-less place beside a rated one: both cards kept",
          [(s["title"], s["scope"], s["sample_size"], s["score"], s["distribution"]) for s in ns_],
          [("Normal Place", "place", 2, 4.0, {"5": 1, "3": 1}), ("Quiet Place", "place", 2, None, None)])
    check("links, star-less place beside a rated one: the cards count every Maps review",
          sum(s["sample_size"] for s in ns_), len(nsr_))
    check("links, star-less place beside a rated one: the rail agrees", _rail_agrees(nsr_, ns_), True)
    # Names app.js does not show (has(): "unknown", "n/a", blank after trim) are labelled like
    # a review with no name, so their card is the subject's, never a card titled "unknown".
    sen_ = _link_recs([_maps_raw("P1", "Brand A", 5, 0), _maps_raw("P2", "unknown", 4, 1),
                       _maps_raw("P3", "n/a", 2, 2), _maps_raw("P4", " unknown ", 1, 3)])
    se_ = _source_snapshots(Path("/nonexistent"), ["maps"], sen_)
    check("links, sentinel place names: counted on the subject's card, not a place card",
          [(s["title"], s["scope"], s["sample_size"]) for s in se_],
          [("Brand A", "place", 1), ("Brand Multi", "source", 3)])
    check("links, sentinel place names: the rail agrees", _rail_agrees(sen_, se_), True)
    # The label mirror itself (the full parity with app.js is checked by a development test).
    _lab = lambda data, subject="S": _maps_review_label({"platform": "maps", "data": data, "subject": subject})
    check("label: 'unknown' place name falls through to the subject", _lab({"place_name": "unknown"}), "S")
    check("label: ' n/a ' place name falls through (trimmed before the check)", _lab({"place_name": " n/a "}), "S")
    check("label: accepted place name is returned untrimmed", _lab({"place_name": " Brand "}), " Brand ")
    check("label: no name, no business, falsy subject: the platform", _lab({}, ""), "Google Maps")
    check("label: subject 0 is falsy in JavaScript", _lab({}, 0), "Google Maps")
    check("label: subject '0' is truthy in JavaScript", _lab({}, "0"), "0")
    check("label: business_name 'unknown' is shown (app.js does not check it)",
          _lab({"business_name": "unknown"}), "unknown")
    check("label: an empty list subject is truthy in JavaScript", _lab({}, []), [])
    check("label: subreddit branch", _lab({"subreddit": "cats"}), "r/cats")
    check("printed text of numbers, as JavaScript String()",
          [_js_str(v) for v in (5, 1.5, 1e21, 1e-7, 0.000001, 1e16, -0.0, [1, None, "a"], {"a": 1}, True)],
          ["5", "1.5", "1e+21", "1e-7", "0.000001", "10000000000000000", "0", "1,,a",
           "[object Object]", "true"])
    # The index carries the note beside the cards.
    with tempfile.TemporaryDirectory() as _td:
        _wd = Path(_td) / "pain_brand_2026-02-01"
        (_wd / "B2a_maps_places").mkdir(parents=True)
        (_wd / "B2b_reviews").mkdir()
        (_wd / "B2a_maps_places" / "run_02_full.json").write_text(json.dumps(five))
        (_wd / "B2b_reviews" / "run_02_full.json").write_text(json.dumps(
            [_maps_raw("PID2", "Brand Place 2", s, i) for i, s in enumerate(stars2)]))
        _ix = json.loads(build_index(_wd, {"sources": ["maps"]}).read_text())
        check("index: one Maps card", [(s["title"], s["sample_size"]) for s in _ix["snapshots"]],
              [("Brand Place 2", 7)])
        check("index: note for the 4 unreviewed places", _ix.get("snapshot_notes"),
              [{"source": "maps", "places_found": 5, "places_not_reviewed": 4,
                "reviews_unmatched": 0}])

    # Deleted Reddit posts are flagged (kept, not dropped) and subreddit names normalized, so
    # an agent reading the index counts what the viewer shows.
    gone = _record_envelope({"text": " [Removed] ", "source": "reddit", "id": "p9",
                             "subreddit": "r/AcmeFitness"}, "reddit", "M", "reddit_reviews", 0, th)
    check("reddit [removed] flagged, record kept", gone["tags"].get("removed"), True)
    check("reddit r/ prefix stripped (case-insensitive)", gone["data"]["subreddit"], "AcmeFitness")
    check("live reddit post not flagged", "removed" in renv["tags"], False)
    check("trustpilot text '[deleted]' is not a reddit tombstone",
          "removed" in _record_envelope({"text": "[deleted]", "stars": 1, "source": "trustpilot", "id": "t"},
                                        "trustpilot", "M", "trustpilot_reviews", 0, th)["tags"], False)

    # P2: repeat_of emitted into index
    with tempfile.TemporaryDirectory() as _td:
        _wd = Path(_td) / "pain_acme-fitness_2026-09-28"
        _wd.mkdir()
        (_wd / "PAIN_CONFIG.json").write_text(
            '{"repeat_of":"pain_acme-fitness_2026-08-28","subject":"Acme Fitness","sources":["maps"]}',
            encoding="utf-8")
        _idx = json.loads(build_index(_wd, {"repeat_of": "pain_acme-fitness_2026-08-28", "sources": ["maps"]}).read_text())
        check("repeat_of emitted into index", _idx.get("repeat_of"), "pain_acme-fitness_2026-08-28")
        _idx2 = json.loads(build_index(_wd, {"sources": ["maps"]}).read_text())
        check("repeat_of is null when absent from cfg", _idx2.get("repeat_of"), None)

    print(f"\n  {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
