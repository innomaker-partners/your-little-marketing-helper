"""
Derived artifacts between stages — the "glue" that turns one stage's raw output
into the next stage's input. Called by run_phase.py after a full run.

For this tool the only glue is the B2a -> B2b bridge: rank the places
B2a found and write review_targets.json, the list of business URLs B2b scrapes
reviews for. Actor output field names vary; parsers try the common keys and
degrade gracefully (empty artifact) rather than crashing the pipeline.
"""

import json
import re
from pathlib import Path


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


def _guard_raw_is_list(raw, adapter_label):
    """Return True if raw is not a list (caller must return without writing).

    Data-quality guard: when an actor returns an error object instead of a list,
    iterating the dict keys produces an empty corpus with no signal.  Returning
    early here surfaces the problem rather than silently writing [] as the corpus
    for that source.
    """
    if not isinstance(raw, list):
        print(f"   WARNING [{adapter_label}]: actor returned a non-list payload "
              f"({type(raw).__name__!r}); likely an error object — "
              f"skipping to avoid a silent empty corpus")
        return True
    return False


def _redact_pii(records, source):
    """Return a redacted copy of records suitable for the .raw.json audit file.

    Reviewer identity at rest: reviewer-IDENTITY field values are replaced with the
    sentinel '[redacted-pii]'; keys are kept so the file still documents the
    actor's field shape for drift-debugging.  The same per-source rules that
    govern the normalized corpus are applied here.

    source must be one of: 'trustpilot', 'appstore', 'googleplay', 'g2',
    'capterra', 'reddit'.
    """
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            out.append(rec)
            continue
        r = dict(rec)  # shallow copy — only top-level identity values are mutated
        if source == "trustpilot":
            for f in _TP_PII_FIELDS:
                if f in r:
                    r[f] = "[redacted-pii]"
        elif source in ("appstore", "googleplay"):
            for f in _APP_PII_FIELDS:
                if f in r:
                    r[f] = "[redacted-pii]"
        elif source == "g2":
            for f in _G2_PII_FIELDS:
                if f in r:
                    r[f] = "[redacted-pii]"
        elif source == "capterra":
            rv = r.get("reviewer")
            if isinstance(rv, dict):
                rv = dict(rv)
                for f in _CAPTERRA_PII_SUBFIELDS:
                    if f in rv:
                        rv[f] = "[redacted-pii]"
                r["reviewer"] = rv
        elif source == "reddit":
            for k in list(r.keys()):
                if k.lower().startswith(_REDDIT_PII_PREFIXES):
                    r[k] = "[redacted-pii]"
        out.append(r)
    return out


def postprocess(stage, stage_dir, workdir, cfg):
    if stage == "B2a":
        _pp_b2a(stage_dir, cfg)
    elif stage == "TP":
        _pp_trustpilot(stage_dir, cfg)
    elif stage == "AS":
        _pp_app_reviews(stage_dir, cfg, "appstore")
    elif stage == "GP":
        _pp_app_reviews(stage_dir, cfg, "googleplay")
    elif stage == "RD":
        _pp_reddit(stage_dir, cfg)
    elif stage == "G2":
        _pp_g2capterra(stage_dir, cfg, "g2")
    elif stage == "CP":
        _pp_g2capterra(stage_dir, cfg, "capterra")


# --- B2a -> review targets (businesses ranked by review count) ----------------

def _pp_b2a(stage_dir, cfg):
    """Read B2a places, rank by review count, cap at review_target_count, and
    write review_targets.json (the {url} list B2b reads).

    This function is the MODE-3 path only (category discovery).
    For MODE-2 (competitor_names non-empty), subjects.select_mode2_targets()
    writes review_targets.json after best-match selection — _pp_b2a returns
    early so it does not clobber that file.  pain_intel.py calls
    subjects.select_mode2_targets() after run_phase B2a completes.

    A broken bridge produces a silent empty corpus.  The mode-2 skip is explicit
    and logged so the caller can verify the file was written.

    Field names CONFIRMED against real compass/crawler-google-places output in
    live testing: `url` (a `?api=1&query=...&query_place_id
    =<id>` link that the B2b reviews scraper accepts as a startUrl) and
    `reviewsCount` are the canonical keys; the fallbacks are kept for actor drift.
    Some places carry `reviewsCount: null`, which `or 0` handles.
    """
    # MODE 2: subjects.py owns the bridge; return early without touching the file.
    if cfg.get("competitor_names"):
        print("   _pp_b2a: mode 2 (named competitors) — "
              "subjects.select_mode2_targets() handles review_targets.json (skipping)")
        return

    items = _latest_run(stage_dir)
    biz = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or it.get("placeUrl") or it.get("searchPageUrl")   # CONFIRMED: url
        reviews = it.get("reviewsCount") or it.get("reviewCount") or 0          # CONFIRMED: reviewsCount
        if url:
            biz.append({"url": url, "title": it.get("title", ""), "reviews": reviews or 0})
    biz.sort(key=lambda b: -b["reviews"])
    cap = int(cfg.get("review_target_count", 60))
    targets = [{"url": b["url"]} for b in biz[:cap]]
    (Path(stage_dir) / "review_targets.json").write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(stage_dir) / "businesses_ranked.json").write_text(
        json.dumps(biz, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   → {len(biz)} businesses; top {len(targets)} queued for review extraction (B2b)")


# --- TP -> normalized corpus (Trustpilot) ------------------------------------

# Reviewer identity fields that must be dropped at the source boundary (privacy rule).
_TP_PII_FIELDS = frozenset({
    "authorId", "authorName", "authorImage", "authorReviewCount",
    "country",  # reviewer country (distinct from company's country)
})


def _pp_trustpilot(stage_dir, cfg):
    """Normalize raw automation-lab/trustpilot records into the canonical shape
    that extractor._parse_reviews expects.

    Key mapping:
      text        <- headline folded into body: "<title>. <body>" when title is
                     non-empty, else body alone.  Rationale: Trustpilot headlines
                     often carry the sharpest pain statement; the extractor only
                     counts the `text` field, so folding enriches the signal.
      stars       <- rating  (int 1-5 in real actor output)
      businessName<- companyName  (NOT `title`, which is the review HEADLINE --
                     this is the whole reason the adapter exists: feeding raw
                     records to the extractor would make it read review headlines
                     as business names.  FROZEN: do not change this mapping.)
      source      <- "trustpilot"  (overwrites the actor's own `source` field,
                     which means "review acquisition type" e.g. "Organic" -- a
                     completely different concept from our pipeline's source tag)

    Non-PII extras kept for audit: date, language, isVerified, id (reviewId),
    companyDomain, companyTrustScore, companyTotalReviews.

    Writes run_02_full.json (normalized, read by load_source_corpus) and saves
    the untouched raw to run_02_full.raw.json as an audit trail.
    """
    stage_dir = Path(stage_dir)
    raw = _latest_run(stage_dir)
    if _guard_raw_is_list(raw, "_pp_trustpilot"):
        return
    if not raw:
        print("   _pp_trustpilot: no records found — skipping "
              "(stage may not have run yet)")
        return

    # Save raw as audit trail before overwriting (identity values redacted).
    raw_path = stage_dir / "run_02_full.raw.json"
    raw_path.write_text(
        json.dumps(_redact_pii(raw, "trustpilot"), ensure_ascii=False, indent=2),
        encoding="utf-8")

    normalized = []
    skipped = 0
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        try:
            headline = str(rec.get("title") or "").strip()
            body     = str(rec.get("text") or "").strip()
            text     = f"{headline}. {body}" if headline else body

            try:
                stars = int(rec.get("rating"))
            except (TypeError, ValueError):
                stars = None

            company = str(rec.get("companyName") or "").strip()
            date    = rec.get("experienceDate") or rec.get("publishedDate")

            norm = {
                "text":                text,
                "stars":               stars,
                "businessName":        company,
                "source":              "trustpilot",
                # Non-PII audit extras (not read by the extractor).
                "date":                date,
                "language":            rec.get("language"),
                "isVerified":          rec.get("isVerified"),
                "id":                  rec.get("reviewId"),
                "companyDomain":       rec.get("companyDomain"),
                "companyTrustScore":   rec.get("companyTrustScore"),
                "companyTotalReviews": rec.get("companyTotalReviews"),
            }
            # Privacy hard stop: ensure no PII key leaked in (defensive).
            for pii in _TP_PII_FIELDS:
                norm.pop(pii, None)

            normalized.append(norm)
        except Exception:
            skipped += 1

    out_path = stage_dir / "run_02_full.json"
    out_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    skip_msg = f"; {skipped} records skipped (per-record error)" if skipped else ""
    print(f"   _pp_trustpilot: {len(normalized)} records normalized, "
          f"{len(_TP_PII_FIELDS)} PII field types dropped; "
          f"raw saved to run_02_full.raw.json{skip_msg}")


# --- AS / GP -> normalized corpus (App Store + Google Play) ------------------

# Reviewer identity fields for app-review sources (must be dropped, privacy rule).
_APP_PII_FIELDS = frozenset({
    "userName",  # both stores
    "userUrl",   # App Store
    "userImage", # Google Play
})
# Developer reply fields are the dev's voice, not the customer's (not pain signal).
# scoreText is a redundant string copy of score (e.g. "5") -- not needed.
_APP_DROP_FIELDS = _APP_PII_FIELDS | frozenset({"replyText", "replyDate", "scoreText"})

# --- Reddit (harshmaur/reddit-scraper) ---------------------------------------
# Reddit records carry the commenter's identity in many author*/user*
# fields (authorName, authorId, authorFlairText, authorPremium, parsedAuthorId,
# authorFullname, ...). We drop EVERY key whose name starts with "author" or
# "user" -- a prefix rule, not a fixed list, because the actor's schema is rich
# and adds identity fields we should never let through by omission (privacy rule).
_REDDIT_PII_PREFIXES = ("author", "user")
# [anchor](https://url) -> anchor : keep the human-readable anchor text (real
# signal, e.g. a linked headline), drop the URL (noise for phrase extraction).
_REDDIT_MDLINK = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+)\)")
_REDDIT_SUBURL = re.compile(r"/r/([A-Za-z0-9_]+)")


def _pp_app_reviews(stage_dir, cfg, source):
    """Normalize raw thewolves/appstore-reviews-scraper or
    thewolves/google-play-reviews-scraper records into the canonical shape
    that extractor._parse_reviews expects.

    source must be "appstore" or "googleplay" (kept distinct -- a user may
    want per-store signal; a shared source tag would merge them invisibly).

    Key mapping:
      text        <- headline fold: "<title>. <text>" when title is a non-empty
                     string, else text alone.  App Store titles are punchy;
                     Google Play titles are usually null so the fold no-ops.
      stars       <- score  (int 1-5)
      businessName<- app_labels[appId] if configured, else appId (raw numeric/
                     package).  FROZEN: app-review records have no app-name
                     field, only appId (numeric "324684580" for App Store);
                     without a label map the report would show the raw id.
                     Using businessName (not title) keeps _parse_reviews from
                     reading the review headline as the business name.
      source      <- source arg ("appstore" or "googleplay")

    Non-PII extras kept: date, version, id, appId; for Google Play also
    language and thumbsUp (App Store has no language field).

    Drops userName/userUrl (App Store) and userName/userImage
    (Google Play) — reviewer identity fields (privacy rule).  Also drops
    replyText/replyDate (dev reply, not customer voice) and scoreText (redundant).
    """
    stage_dir = Path(stage_dir)
    raw = _latest_run(stage_dir)
    if _guard_raw_is_list(raw, f"_pp_app_reviews [{source}]"):
        return
    if not raw:
        print(f"   _pp_app_reviews [{source}]: no records found -- skipping "
              "(stage may not have run yet)")
        return

    # Save raw as audit trail before overwriting (identity values redacted).
    raw_path = stage_dir / "run_02_full.raw.json"
    raw_path.write_text(
        json.dumps(_redact_pii(raw, source), ensure_ascii=False, indent=2),
        encoding="utf-8")

    app_labels = cfg.get("app_labels", {}) or {}
    pii_count  = 0
    skipped    = 0

    normalized = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        try:
            headline = str(rec.get("title") or "").strip()
            body     = str(rec.get("text") or "").strip()
            text     = f"{headline}. {body}" if headline else body

            try:
                stars = int(rec.get("score"))
            except (TypeError, ValueError):
                stars = None

            app_id        = str(rec.get("appId") or "").strip()
            business_name = app_labels.get(app_id, app_id)

            # Count PII fields present in this raw record.
            pii_count += sum(1 for f in _APP_PII_FIELDS if f in rec)

            norm = {
                "text":         text,
                "stars":        stars,
                "businessName": business_name,
                "source":       source,
                # Non-PII audit extras (not read by the extractor).
                "date":         rec.get("date"),
                "version":      rec.get("version"),
                "id":           rec.get("id"),
                "appId":        app_id,
            }
            # Google Play extras (absent on App Store records -- conditional to avoid
            # polluting App Store normalized records with null keys).
            if "language" in rec:
                norm["language"] = rec["language"]
            if "thumbsUp" in rec:
                norm["thumbsUp"] = rec["thumbsUp"]

            # Privacy hard stop: ensure no PII or dev-reply key leaked in (defensive).
            for drop in _APP_DROP_FIELDS:
                norm.pop(drop, None)

            normalized.append(norm)
        except Exception:
            skipped += 1

    out_path = stage_dir / "run_02_full.json"
    out_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    skip_msg = f"; {skipped} records skipped (per-record error)" if skipped else ""
    print(f"   _pp_app_reviews [{source}]: {len(normalized)} records normalized, "
          f"{pii_count} PII field instances dropped; "
          f"raw saved to run_02_full.raw.json{skip_msg}")


def _reddit_text(rec, is_comment):
    """Fold a Reddit record into one text field. Posts often have an empty or
    link-only body -- the TITLE carries the signal there (like Trustpilot's
    headline), so posts fold "title. body"; comments are body only. Markdown
    links become their anchor text (URL stripped)."""
    body = _REDDIT_MDLINK.sub(r"\1", str(rec.get("body") or "").strip())
    if is_comment:
        return body
    title = str(rec.get("title") or "").strip()
    parts = [p for p in (title, body) if p]
    return ". ".join(parts)


def _pp_reddit(stage_dir, cfg):
    """Normalize raw harshmaur/reddit-scraper records into the canonical shape,
    then hand the no-star corpus to banding.py which synthesizes the low/high
    signal the extractor needs (Component B). Reddit has no stars, so this is the
    one source where an extra pass runs before the corpus is 'done'.

    Field mapping (posts and comments have DIFFERENT raw field names -- the two
    record shapes differ significantly):
      text         <- posts: "title. body"; comments: body   (_reddit_text)
      stars        <- None here; banding.band_records fills it (2=pain, 5=praise)
      businessName <- reddit_labels[subreddit] if configured, else the subreddit
                      ("r/example-topic"). The subreddit is the natural grouping.
      source       <- "reddit"
      score        <- score  (present on BOTH posts and comments; the universal
                      field -- upVotes exists only on posts). Carried as salience
                      context; NEVER used to decide polarity (an upvote is crowd
                      agreement, not sentiment).
      is_comment   <- dataType == "comment"
      date         <- createdAt (post) | commentCreatedAt (comment)
      subreddit    <- communityName (post) | subredditName (comment) | parsed
                      from the url (/r/<name>/) as a last resort.

    Writes TWO files:
      run_02_full.json   -- the banded pain/praise corpus (load_source_corpus
                            reads this; the extractor sees ordinary star records).
      reddit_neutral.json-- items that are neither complaint nor praise, set aside
                            with NO stars so they never enter the analysis. Kept
                            for rigour and offered to the user for general-topic /
                            question mining (report.py surfaces them).

    Every author*/user* field is dropped by prefix (privacy rule).
    """
    stage_dir = Path(stage_dir)
    raw = _latest_run(stage_dir)
    if _guard_raw_is_list(raw, "_pp_reddit"):
        return
    if not raw:
        print("   _pp_reddit: no records found -- skipping (stage may not have run yet)")
        return

    raw_path = stage_dir / "run_02_full.raw.json"
    raw_path.write_text(
        json.dumps(_redact_pii(raw, "reddit"), ensure_ascii=False, indent=2),
        encoding="utf-8")

    labels = cfg.get("reddit_labels", {}) or {}
    pii_count = 0
    skipped   = 0
    normalized = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        try:
            is_comment = str(rec.get("dataType") or "").lower() == "comment"
            text = _reddit_text(rec, is_comment)

            sub = str(rec.get("communityName") or rec.get("subredditName") or "").strip()
            if not sub:
                m = _REDDIT_SUBURL.search(str(rec.get("url") or rec.get("postUrl") or ""))
                sub = f"r/{m.group(1)}" if m else "reddit"
            sub_key = sub[2:] if sub.lower().startswith("r/") else sub
            business = labels.get(sub_key, labels.get(sub, sub))

            try:
                score = int(rec.get("score"))
            except (TypeError, ValueError):
                score = None

            pii_count += sum(1 for f in rec
                             if f.lower().startswith(_REDDIT_PII_PREFIXES))

            norm = {
                "text":         text,
                "stars":        None,          # banding fills this
                "businessName": business,
                "source":       "reddit",
                "score":        score,
                "is_comment":   is_comment,
                "date":         rec.get("createdAt") or rec.get("commentCreatedAt"),
                "subreddit":    sub,
                "id":           rec.get("id"),
            }
            # Defensive: never let an author*/user* key survive into the normalized
            # record (belt-and-suspenders on top of the explicit field list above).
            for k in [k for k in norm if k.lower().startswith(_REDDIT_PII_PREFIXES)]:
                norm.pop(k, None)
            normalized.append(norm)
        except Exception:
            skipped += 1

    # Component B: synthesize the low/high band; set neutral items aside.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import banding  # noqa: E402
    # band_adjudicate False -> deterministic lexicon only (ambiguous -> neutral, no
    # LLM). Default True: the LLM adjudicates the ambiguous middle.
    main, neutral = banding.band_records(
        normalized, cfg, adjudicate=cfg.get("band_adjudicate", True))

    out_path = stage_dir / "run_02_full.json"
    out_path.write_text(json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
    neutral_path = stage_dir / "reddit_neutral.json"
    neutral_path.write_text(json.dumps(neutral, ensure_ascii=False, indent=2), encoding="utf-8")

    n_pain = sum(1 for r in main if r.get("band") == "pain")
    n_praise = sum(1 for r in main if r.get("band") == "praise")
    skip_msg = f"; {skipped} records skipped (per-record error)" if skipped else ""
    print(f"   _pp_reddit: {len(normalized)} records normalized "
          f"({pii_count} PII field instances dropped); banded -> "
          f"{n_pain} pain / {n_praise} praise into the corpus, "
          f"{len(neutral)} neutral set aside to reddit_neutral.json; "
          f"raw saved to run_02_full.raw.json{skip_msg}")


# --- G2 / Capterra -> normalized corpus (B2B SaaS reviews) ------------------

# Reviewer-identity fields per platform (privacy rule). G2 (factden) is FLAT
# (reviewerName at top level); Capterra (azzouzana) NESTS identity under a
# `reviewer` object. Non-identifying demographics (companySize, industry) are
# kept as audit extras -- they are aggregate buckets, not identity.
_G2_PII_FIELDS = frozenset({"reviewerName"})
_CAPTERRA_PII_SUBFIELDS = frozenset({"fullName", "profilePicUrl", "verifiedLinkedIn"})


def _pp_g2capterra(stage_dir, cfg, source):
    """Normalize raw factden/g2-reviews-scraper (source="g2") or
    azzouzana/capterra-reviews-scraper (source="capterra") records into the
    canonical shape extractor._parse_reviews expects.

    G2 and Capterra are BOTH star-rated, so -- unlike Reddit -- they need no
    banding; the star maps straight onto the extractor's low/high split. Their
    raw field names differ completely (the two bake-off winners are different
    devs), so this one adapter branches per source, the way _pp_app_reviews
    branches appstore/googleplay but across more divergent shapes.

    text  <- a fold of the review's substantive parts. G2 reviews have NO single
             body -- the content is split across pros/cons -- so G2 folds
             "reviewTitle. pros. cons. problemsSolved"; Capterra folds
             "title. generalComments. prosText. consText". Folding matches the
             other adapters (Trustpilot/apps fold headline+body) and enriches the
             phrase signal the extractor counts.
    stars <- overallRating. G2 returns it numeric (possibly a half-star like 4.5);
             Capterra returns it as a STRING ("5.0"). float() handles both, and
             the extractor splits low/high on the float directly (no rounding, so
             a 4.5 is correctly 'high' and a 2.5 'low').
    businessName <- G2: productName ("Slack"); Capterra: slug ("Slack", taken
             from the /p/<id>/<slug> URL). A per-source *_labels map overrides
             both for the rare product whose name field is missing/ugly. Using the
             product field (NOT `title`, which is the review HEADLINE) keeps
             _parse_reviews from reading a headline as the business.
    source <- "g2" or "capterra" (kept DISTINCT -- a user may want per-platform
             signal, same rationale as appstore vs googleplay).

    G2's reviewerName and Capterra's reviewer.{fullName, profilePicUrl,
    verifiedLinkedIn} are reviewer identity and are NEVER copied into the
    normalized record -- the adapter builds `norm` from named fields only, so PII
    is excluded by construction (privacy rule). The count below is for the report's disclosure.
    """
    stage_dir = Path(stage_dir)
    raw = _latest_run(stage_dir)
    if _guard_raw_is_list(raw, f"_pp_g2capterra [{source}]"):
        return
    if not raw:
        print(f"   _pp_g2capterra [{source}]: no records found -- skipping "
              "(stage may not have run yet)")
        return

    # Save raw as audit trail before overwriting (identity values redacted).
    raw_path = stage_dir / "run_02_full.raw.json"
    raw_path.write_text(
        json.dumps(_redact_pii(raw, source), ensure_ascii=False, indent=2),
        encoding="utf-8")

    labels = (cfg.get("g2_labels" if source == "g2" else "capterra_labels") or {})
    pii_count = 0
    skipped   = 0

    normalized = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        try:
            if source == "g2":
                parts = (rec.get("reviewTitle"), rec.get("pros"),
                         rec.get("cons"), rec.get("problemsSolved"))
                slug         = str(rec.get("productSlug") or "").strip()
                name_field   = str(rec.get("productName") or "").strip()
                business     = labels.get(slug, name_field or slug)
                rating_raw   = rec.get("overallRating")
                company_size = rec.get("companySize")
                industry     = rec.get("reviewerIndustry")
                date         = rec.get("submittedAt")
                rid          = rec.get("reviewId")
                pii_count   += sum(1 for f in _G2_PII_FIELDS if rec.get(f))
            else:  # capterra
                parts = (rec.get("title"), rec.get("generalComments"),
                         rec.get("prosText"), rec.get("consText"))
                slug         = str(rec.get("slug") or "").strip()
                business     = labels.get(slug, slug)
                rating_raw   = rec.get("overallRating")
                reviewer     = rec.get("reviewer")
                reviewer     = reviewer if isinstance(reviewer, dict) else {}
                company_size = reviewer.get("companySize")
                industry     = reviewer.get("industry")
                date         = rec.get("writtenOn")
                rid          = rec.get("reviewId")
                pii_count   += sum(1 for f in _CAPTERRA_PII_SUBFIELDS if reviewer.get(f))

            text = ". ".join(str(p).strip() for p in parts if p and str(p).strip())

            try:
                stars = float(rating_raw) if rating_raw is not None else None
            except (TypeError, ValueError):
                stars = None

            norm = {
                "text":         text,
                "stars":        stars,
                "businessName": business,
                "source":       source,
                # Non-PII audit extras (not read by the extractor).
                "date":         date,
                "companySize":  company_size,
                "industry":     industry,
                "id":           rid,
            }
            normalized.append(norm)
        except Exception:
            skipped += 1

    out_path = stage_dir / "run_02_full.json"
    out_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    skip_msg = f"; {skipped} records skipped (per-record error)" if skipped else ""
    print(f"   _pp_g2capterra [{source}]: {len(normalized)} records normalized, "
          f"{pii_count} PII field instances dropped; raw saved to run_02_full.raw.json{skip_msg}")
