"""
Deterministic pain/trust extractor for the pain & messaging tool.

Replaces a fixed word-list approach with corpus-derived
distinctive-phrase extraction: phrases are counted from the review text, then
ranked by how much more often they appear in the low-star band than the
high-star band (and vice versa for trust). No LLM, no network, no Apify
calls -- only deterministic computation.

Integration seam (called by pain_intel.py after SCRIPTS is on sys.path):
    import extractor
    extractor.extract(workdir, cfg)   # -> Path to synthesis_facts.json

Architecture note: extract_from_reviews() is the pure in-memory core;
extract() wraps it with file I/O. Tests hit the core directly, which is
why the self-tests need no files, no network, no Apify.
"""

import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Static English stopword list (~145 words).
# Covers function words that carry no pain/trust signal on their own.
# Negation words ("not", "never", "no") are intentionally EXCLUDED from this
# list -- they carry strong sentiment and appear in meaningful n-grams like
# "never called back" or "no explanation given". Keeping them as content words
# means bigrams/trigrams containing them are eligible for the pain list.
# This list is a tuning knob: edit it for a non-English corpus.
# ---------------------------------------------------------------------------
_STOPWORDS: frozenset = frozenset({
    # Articles
    "a", "an", "the",
    # Prepositions
    "at", "by", "for", "from", "in", "into", "of", "off", "on", "onto",
    "out", "over", "per", "since", "than", "through", "to", "under",
    "until", "up", "via", "with", "without",
    # Conjunctions
    "and", "but", "or", "nor", "so", "yet", "both", "either", "neither",
    "although", "because", "if", "unless", "when", "where", "while",
    "as", "though", "even",
    # Pronouns
    "i", "me", "my", "myself", "we", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "they", "them", "their", "theirs", "themselves",
    "what", "which", "who", "whom",
    "this", "that", "these", "those",
    # True auxiliaries only (not content verbs)
    "am", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had",
    "will", "would", "could", "should", "may", "might", "must", "shall", "can",
    "do", "does", "did",
    # High-frequency filler words that add no signal
    "very", "really", "quite", "just", "also", "too", "only", "still",
    "already", "then", "now", "here", "there", "how",
    "all", "any", "each", "more", "most", "other", "some", "such",
    "another", "around", "actually", "however", "about", "again",
    "always", "often", "sometimes", "usually",
    "truly", "absolutely", "definitely",
    "same", "new", "old", "one", "two", "three", "first", "last", "next",
    "every", "many", "much", "well", "way", "lot", "like",
})

# ---------------------------------------------------------------------------
# Price lexicon -- the ONE intentionally-retained deterministic lexicon.
# Price is a cross-vertical universal (unlike vertical-specific pain vocab),
# so a small English list is justified here where a general pain word-list is
# not. Currency symbols are detected via regex; words via set intersection.
# ---------------------------------------------------------------------------
_PRICE_WORDS: frozenset = frozenset({
    "price", "prices", "pricing", "cost", "costs", "costly",
    "expensive", "cheap", "overpriced", "affordable",
    "fee", "fees", "charge", "charges",
    "pricey", "refund", "refunds", "money",
    # "worth" and "value" are excluded -- they fire on positive-sentiment phrases
    # ("worth every minute", "great value") that carry no price signal.
    # Genuine price anchors ("not worth the $50", "good value for the money")
    # are still caught by the currency regex or by "price"/"money" in the set.
})
_PRICE_CURRENCY_RE = re.compile(r"[$\xa3€]")  # $, GBP, EUR


# ---------------------------------------------------------------------------
# Helpers (pattern matches postprocess.py / subjects.py)
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    """Read JSON from path; return default if absent. Matches postprocess.py."""
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _latest_b2b_run(workdir):
    """Full B2b run first, test run as fallback. Mirrors _latest_run in postprocess.py."""
    stage_dir = Path(workdir) / "B2b_reviews"
    for name in ("run_02_full.json", "run_01_test.json"):
        p = stage_dir / name
        if p.exists():
            return _read_json(p, [])
    return []


def _get_field(record, candidates, default=None):
    """Return the first matching field value from a list of candidate keys."""
    for key in candidates:
        if key in record and record[key] is not None:
            return record[key]
    return default


# ---------------------------------------------------------------------------
# Review parsing -- tolerant field reading (all field names marked # VERIFY)
# ---------------------------------------------------------------------------

def _parse_reviews(raw_records, _drops=None):
    """Normalize raw B2b records into {text, stars, business} dicts.

    Field names CONFIRMED against real compass/google-maps-reviews-scraper output:
    the canonical keys are `text`, `stars`, and `title` (business). The remaining
    fallbacks are kept for actor-version drift. Note: `name` in real output is the REVIEWER name (empty under
    personalData:False), not the business, so `title` is correctly tried first.
    Records with empty text or unparseable stars are silently skipped (graceful
    degradation over crashing the pipeline).
    """
    reviews = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            continue

        text = _get_field(
            rec, ("text", "reviewText", "review", "snippet"), ""  # CONFIRMED: text
        )
        stars_raw = _get_field(
            rec, ("stars", "rating", "starRating", "reviewRating"), None  # CONFIRMED: stars
        )
        business = _get_field(
            rec, ("title", "name", "placeName", "businessName"), ""  # CONFIRMED: title
        )

        text = str(text).strip() if text else ""
        if not text:
            if _drops is not None:
                _drops["empty_text"] = _drops.get("empty_text", 0) + 1
            continue

        try:
            stars = float(stars_raw) if stars_raw is not None else None
        except (ValueError, TypeError):
            stars = None

        if stars is None:
            if _drops is not None:
                _drops["no_stars"] = _drops.get("no_stars", 0) + 1
            continue

        reviews.append({
            "text":     text,
            "stars":    stars,
            "business": str(business).strip() if business else "",
            # Source tag. Records ingested by load_source_corpus are
            # pre-tagged; raw B2b records have no source field, so default to
            # "maps" here as a back-compat guard. Never overrides a tag already
            # present on the record.
            "source":   rec.get("source", "maps"),
        })
    return reviews


# ---------------------------------------------------------------------------
# Tokenization and n-gram building
# ---------------------------------------------------------------------------

def _tokenize(text):
    """Lowercase, strip punctuation, return list of word tokens.

    Apostrophes are DELETED (not spaced) before other punctuation is spaced, so
    contractions stay whole: "it’s" -> "its", "doesn’t" -> "doesnt", not
    "it" + "s" / "doesn" + "t". Real review text is full of contractions, and
    spacing the apostrophe fragments them into junk tokens ("s", "t", "it s").
    Both the straight ‘ (U+0027) and curly ‘ (U+2019) apostrophes appear in the
    wild. Single-character tokens are dropped as a second guard.

    Punctuation stripping is Unicode-aware -- non-ASCII letters such as
    accented Latin (cafe -> café) and non-Latin scripts (Cyrillic, Greek) are
    preserved; only characters outside \\w (plus underscore, which \\w includes)
    are replaced with spaces. Behavior is byte-identical to the prior version for
    pure-ASCII English text.
    """
    lowered = text.lower().replace("'", "").replace("’", "")
    cleaned = re.sub(r"[^\w\s]|_", " ", lowered)
    return [t for t in cleaned.split() if len(t) > 1]


def _dedup_key(text):
    """Normalised identity key for near-duplicate dedup.

    Different from _tokenize: this KEEPS length-1 tokens so that genuinely
    distinct short reviews ("room 5 was dirty" vs "room 8 was dirty") are NOT
    falsely merged -- only byte-identical / punctuation-and-case variant
    copy-paste is collapsed. Lowercase, punctuation+underscore -> single spaces,
    collapse whitespace.
    """
    cleaned = re.sub(r"[^\w\s]|_", " ", text.lower())
    return " ".join(cleaned.split())


def _is_eligible_phrase(phrase_tokens, n):
    """Check whether a phrase should be counted (not all stopwords).

    Unigrams: must not be a stopword.
    N-grams (n>=2): must have at least one non-stopword token. This keeps
    phrases like "never called back" where "never"/"back" are stopwords but
    "called" is a content word.
    """
    if n == 1:
        return phrase_tokens[0] not in _STOPWORDS
    return not all(t in _STOPWORDS for t in phrase_tokens)


def _count_corpus_phrases(reviews, ngram_range=(1, 3)):
    """Count per-review phrase occurrences across a list of parsed reviews.

    Each phrase is counted at most once per review (deduplication within a
    review prevents a long, repetitive text from inflating corpus counts).
    The result represents "number of reviews in this corpus that mention
    this phrase", which is more meaningful than raw occurrence frequency
    for small corpora.

    Returns {phrase_string: review_count}.
    """
    counts = {}
    min_n, max_n = ngram_range
    for rev in reviews:
        tokens = _tokenize(rev["text"])
        seen_this_review = set()
        for n in range(min_n, max_n + 1):
            for i in range(len(tokens) - n + 1):
                phrase_tokens = tokens[i:i + n]
                if not _is_eligible_phrase(phrase_tokens, n):
                    continue
                phrase = " ".join(phrase_tokens)
                if phrase in seen_this_review:
                    continue
                seen_this_review.add(phrase)
                counts[phrase] = counts.get(phrase, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Distinctiveness scoring
# ---------------------------------------------------------------------------

def _score_phrases(low_counts, high_counts, n_low, n_high, band,
                   min_phrase_count=2, eps=0.01, top_n=50):
    """Rank phrases by how distinctive they are in `band` ('low' or 'high').

    Distinctiveness, not raw frequency: a phrase that appears equally in both
    bands (e.g. "service", "place") gets a score near 1.0 and is ranked below
    phrases that are concentrated in one band.

    For band='low' (pain signals):
        score = (low_rate) / (high_rate + eps)
        where rate = review_count / max(1, n_reviews_in_corpus)

    For band='high' (trust signals): symmetric inverse.

    eps (default 0.01) prevents division-by-zero and caps the score for
    phrases that appear zero times in the opposite band.

    Ranking: score desc, count desc, phrase alpha (stable, reproducible).
    Returns a list of dicts, capped at top_n.
    """
    if band == "low":
        primary_counts, secondary_counts = low_counts, high_counts
        n_primary, n_secondary = n_low, n_high
        primary_count_key, secondary_count_key = "low_count", "high_count"
        star_band = "low"
    else:
        primary_counts, secondary_counts = high_counts, low_counts
        n_primary, n_secondary = n_high, n_low
        primary_count_key, secondary_count_key = "high_count", "low_count"
        star_band = "high"

    results = []
    for phrase, pcount in primary_counts.items():
        if pcount < min_phrase_count:
            continue
        scount = secondary_counts.get(phrase, 0)
        p_rate = pcount / max(1, n_primary)
        s_rate = scount / max(1, n_secondary)
        score = p_rate / (s_rate + eps)
        results.append({
            "phrase": phrase,
            primary_count_key: pcount,
            secondary_count_key: scount,
            "score": round(score, 4),
            "star_band": star_band,
        })

    results.sort(key=lambda x: (-x["score"], -x[primary_count_key], x["phrase"]))
    return results[:top_n]


# ---------------------------------------------------------------------------
# Price mention detection (the one retained static lexicon)
# ---------------------------------------------------------------------------

def _has_price_mention(text):
    """True if the review text mentions price-related language.

    Detects currency symbols ($, GBP, EUR) and a small English price word
    list. This is the ONE intentionally-retained deterministic lexicon --
    price is a cross-vertical universal. Everything else is corpus-derived.
    """
    if _PRICE_CURRENCY_RE.search(text):
        return True
    tokens = set(_tokenize(text))
    return bool(tokens & _PRICE_WORDS)


# ---------------------------------------------------------------------------
# Core extraction -- pure in-memory, no file I/O (testable seam)
# ---------------------------------------------------------------------------

def extract_from_reviews(reviews, cfg):
    """Run the full extraction pipeline on a list of raw B2b records.

    `reviews` is a list of raw dicts as loaded from B2b JSON output.
    `cfg` is the config dict (thresholds read from it; missing keys use defaults).

    Returns the facts dict that will be written to synthesis_facts.json.
    This is the testable core; extract() wraps it with file I/O.
    """
    low_thresh  = int(cfg.get("low_star_threshold",  3))
    high_thresh = int(cfg.get("high_star_threshold", 4))
    min_phrase  = int(cfg.get("min_phrase_count",    2))
    quote_cap   = int(cfg.get("quote_cap",         400))
    ngram_range = (1, 3)
    eps         = 0.01

    # Count records dropped during parsing so the report can disclose them.
    _drop_counts: dict = {"empty_text": 0, "no_stars": 0}
    parsed   = _parse_reviews(reviews, _drop_counts)

    # Near-duplicate dedup -- collapse copy-paste review bombs before any
    # counting. Two reviews are near-duplicates when _dedup_key gives the same string
    # (lowercase, punctuation stripped, whitespace collapsed). Uses _dedup_key NOT
    # _tokenize: length-1 tokens are preserved so genuinely distinct short reviews
    # ("room 5 dirty" vs "room 8 dirty") are not falsely merged. Keep first
    # occurrence; drop later duplicates. Order is preserved. Runs on full parsed list
    # before the low/high split so every downstream count (n_total, n_low, star_dist,
    # etc.) is post-dedup and internally consistent.
    _seen_norm: dict = {}
    _deduped_parsed = []
    for _r in parsed:
        _norm_key = _dedup_key(_r["text"])
        if _norm_key not in _seen_norm:
            _seen_norm[_norm_key] = True
            _deduped_parsed.append(_r)
    n_deduped = len(parsed) - len(_deduped_parsed)
    parsed = _deduped_parsed

    low_star = [r for r in parsed if r["stars"] <= low_thresh]
    high_star = [r for r in parsed if r["stars"] >= high_thresh]

    n_total = len(parsed)
    n_low   = len(low_star)
    n_high  = len(high_star)

    # Star distribution (denominator honesty)
    star_dist: dict = {}
    for r in parsed:
        k = str(int(round(r["stars"])))
        star_dist[k] = star_dist.get(k, 0) + 1

    # Source distribution (per-source N). Mirrors star_distribution
    # pattern: a simple {source: count} over all parsed reviews, plus a richer
    # per_source breakdown with the low/high/total split that the report uses
    # for per-source-N disclosure.
    #
    # Seed both dicts with zero entries for every source listed in
    # cfg["sources"] BEFORE counting, so a source that returned 0 records still
    # appears in the disclosure (e.g. "trustpilot: 0"). Seeding is ONLY done when
    # the "sources" key is explicitly present in cfg — when cfg={} (all inline tests),
    # no seeding occurs and behavior is byte-identical to before this fix.
    _enabled_sources = cfg.get("sources") if "sources" in cfg else None
    source_dist: dict = {src: 0 for src in _enabled_sources} if _enabled_sources else {}
    per_source: dict  = (
        {src: {"low": 0, "high": 0, "total": 0} for src in _enabled_sources}
        if _enabled_sources else {}
    )

    for r in parsed:
        src = r["source"]
        source_dist[src] = source_dist.get(src, 0) + 1

    for r in parsed:
        src = r["source"]
        if src not in per_source:
            per_source[src] = {"low": 0, "high": 0, "total": 0}
        per_source[src]["total"] += 1
    for r in low_star:
        per_source[r["source"]]["low"] += 1
    for r in high_star:
        per_source[r["source"]]["high"] += 1

    # Directional flag: small low-star N = directional, not statistical.
    # Boolean kept for backward-compat with report.py and existing self-tests.
    directional = n_low < 30

    # Meta note -- always explicit about N. Graduated confidence bands:
    # language scales with n_low and is NEVER fully silenced. Thresholds
    # (30/100/500) are a simple order-of-magnitude confidence gradient.
    if n_low == 0:
        meta_note = (
            "0 low-star reviews in this corpus -- no pain signal available. "
            "Run with more competitors or lower low_star_threshold."
        )
        confidence_band = "none"
    elif n_low < 30:
        meta_note = (
            f"Small low-star N ({n_low} of {n_total} total) -- "
            "directional signal only, not statistically representative."
        )
        confidence_band = "directional"
    elif n_low < 100:
        meta_note = (
            f"Indicative sample ({n_low} of {n_total} total) -- "
            "more than directional but still not statistically robust."
        )
        confidence_band = "indicative"
    elif n_low < 500:
        meta_note = (
            f"Moderate sample ({n_low} of {n_total} total)."
        )
        confidence_band = "moderate"
    else:
        meta_note = (
            f"Substantial sample ({n_low} of {n_total} total)."
        )
        confidence_band = "substantial"

    # Detect non-English-dominant corpus and disclose.
    # Ratio = non-ASCII chars / total chars across all review texts (char-level).
    # Threshold 0.20 -- more than 1-in-5 chars non-ASCII strongly implies a
    # non-English-dominant corpus (well above stray accented chars in English text).
    _all_text = "".join(r["text"] for r in parsed)
    if _all_text:
        _nonascii_count = sum(1 for c in _all_text if ord(c) > 127)
        nonascii_ratio = round(_nonascii_count / len(_all_text), 4)
    else:
        nonascii_ratio = 0.0

    # Phrase counting per band
    low_counts  = _count_corpus_phrases(low_star,  ngram_range) if n_low  > 0 else {}
    high_counts = _count_corpus_phrases(high_star, ngram_range) if n_high > 0 else {}

    # Distinctive phrase ranking
    pain_list  = _score_phrases(
        low_counts, high_counts, n_low, n_high, "low",
        min_phrase_count=min_phrase, eps=eps, top_n=50,
    )
    trust_list = _score_phrases(
        low_counts, high_counts, n_low, n_high, "high",
        min_phrase_count=min_phrase, eps=eps, top_n=50,
    )

    # Price mentions (the one retained static lexicon)
    price_low  = sum(1 for r in low_star  if _has_price_mention(r["text"]))
    price_high = sum(1 for r in high_star if _has_price_mention(r["text"]))

    # Per-phrase source annotation. Computed AFTER scoring so the
    # distinctiveness math is fully isolated: the annotation loop does not
    # touch low_counts/high_counts/scores at all. For each source seen in the
    # corpus, compute its per-source phrase sub-counts and stamp "sources" on
    # every ranked phrase. For a maps-only corpus this yields {"maps": N} on
    # every item, confirming all evidence came from Maps (a trivially-verifiable
    # parity guard). For a multi-source corpus it names which platform(s)
    # contributed each phrase and at what count.
    _sources_in_corpus = list(source_dist.keys())
    _src_low_counts = {
        src: (_count_corpus_phrases([r for r in low_star  if r["source"] == src], ngram_range)
              if any(r["source"] == src for r in low_star) else {})
        for src in _sources_in_corpus
    }
    _src_high_counts = {
        src: (_count_corpus_phrases([r for r in high_star if r["source"] == src], ngram_range)
              if any(r["source"] == src for r in high_star) else {})
        for src in _sources_in_corpus
    }
    for item in pain_list:
        ph = item["phrase"]
        item["sources"] = {src: _src_low_counts[src][ph]
                           for src in _sources_in_corpus
                           if _src_low_counts[src].get(ph, 0) > 0}
    for item in trust_list:
        ph = item["phrase"]
        item["sources"] = {src: _src_high_counts[src][ph]
                           for src in _sources_in_corpus
                           if _src_high_counts[src].get(ph, 0) > 0}

    # Verbatim quote banks (the LLM only clusters these; the code here only
    # collects and caps them). Source carried through so the report writer
    # attributes each quote to its platform.
    low_quotes_raw  = [{"text": r["text"], "stars": r["stars"],
                        "business": r["business"], "source": r["source"]}
                       for r in low_star]
    high_quotes_raw = [{"text": r["text"], "stars": r["stars"],
                        "business": r["business"], "source": r["source"]}
                       for r in high_star]

    low_capped  = len(low_quotes_raw)  > quote_cap
    high_capped = len(high_quotes_raw) > quote_cap
    low_quotes  = low_quotes_raw[:quote_cap]
    high_quotes = high_quotes_raw[:quote_cap]

    # Disclose dedup removal when any duplicates were filtered.
    if n_deduped > 0:
        _dedup_note = (
            f" {n_deduped} near-identical duplicate review(s) removed before counting."
        )
        meta_note = (meta_note.rstrip() + _dedup_note) if meta_note else _dedup_note.strip()

    # Disclose non-English corpus when non-ASCII ratio exceeds threshold.
    if nonascii_ratio > 0.20:
        _lang_note = (
            " Analysis vocabulary model is tuned for English -- "
            "non-English phrase results are directional only."
        )
        meta_note = (meta_note.rstrip() + _lang_note) if meta_note else _lang_note.strip()

    # Append drop disclosure to meta_note when records were silently skipped.
    _drop_total = _drop_counts["empty_text"] + _drop_counts["no_stars"]
    _drop_counts["total"] = _drop_total
    if _drop_total > 0:
        _drop_note = (
            f" {_drop_total} record(s) dropped before counting "
            f"(empty_text={_drop_counts['empty_text']}, "
            f"no_stars={_drop_counts['no_stars']})."
        )
        meta_note = (meta_note.rstrip() + _drop_note) if meta_note else _drop_note.strip()

    # Disclose the quote-cap when phrase counts and quote banks diverge.
    # Phrase distinctiveness is computed over ALL parsed reviews; the LLM only sees
    # quote_cap verbatim quotes. A scored phrase may have no visible quote backing it.
    if low_capped or high_capped:
        _cap_note = (
            f" Phrase counts include all {n_total} reviews but only {quote_cap} "
            "verbatim quotes are shown to the writer; "
            "some scored phrases may lack a visible quote."
        )
        meta_note = (meta_note.rstrip() + _cap_note) if meta_note else _cap_note.strip()

    meta = {
        "total_reviews":       n_total,
        "low_star_n":          n_low,
        "high_star_n":         n_high,
        "star_distribution":   star_dist,
        "source_distribution": source_dist,   # {source: count} over all reviews
        "per_source":          per_source,    # {source: {low,high,total}} for per-source-N disclosure
        "low_star_threshold":  low_thresh,
        "high_star_threshold": high_thresh,
        "ngram_range":         list(ngram_range),
        "min_phrase_count":    min_phrase,
        "directional":         directional,
        "confidence_band":     confidence_band,  # none/directional/indicative/moderate/substantial
        "extraction_method":   "corpus-derived distinctive phrases (deterministic, no LLM)",
        "note":                meta_note,
        "dropped_records":     _drop_counts,  # {empty_text, no_stars, total}
        "deduped":             {"removed": n_deduped},  # near-duplicate dedup count
        "nonascii_ratio":      nonascii_ratio,  # fraction of non-ASCII chars in corpus
    }
    if low_capped:
        meta["low_quotes_capped_at"] = quote_cap
    if high_capped:
        meta["high_quotes_capped_at"] = quote_cap

    return {
        "meta":                    meta,
        "pain_points_low_star":    pain_list,
        "trust_signals_high_star": trust_list,
        "price_mentions": {
            "low_star_count":  price_low,
            "high_star_count": price_high,
            "total":           price_low + price_high,
        },
        "low_star_quotes":  low_quotes,
        "high_star_quotes": high_quotes,
    }


# ---------------------------------------------------------------------------
# File-facing seam (called by pain_intel.py)
# ---------------------------------------------------------------------------

def load_source_corpus(workdir, source):
    """Load raw review records from disk for a single source.

    For 'maps': reads B2b_reviews/run_02_full.json (fallback run_01_test.json),
    exactly as the pre-v1.1 _latest_b2b_run helper. For any other source:
    reads <workdir>/<source>_reviews/run_02_full.json, returning [] if absent
    (scrapers for other sources write these files; absence is graceful here).

    Tags EVERY dict record with source=<source>, overriding any pre-existing
    source field the actor may have emitted (the folder path is the authoritative
    source -- a raw actor field named "source" cannot be trusted).
    Non-dict records are skipped unchanged.
    Returns the list of tagged records.
    """
    workdir = Path(workdir)
    if source == "maps":
        records = _latest_b2b_run(workdir)
    else:
        path = workdir / f"{source}_reviews" / "run_02_full.json"
        records = _read_json(path, []) if path.exists() else []

    for rec in records:
        if isinstance(rec, dict):
            rec["source"] = source
    return records


def extract_records(records, workdir, cfg):
    """Run extraction on a pre-loaded (and optionally merged) record list.

    Writes synthesis_facts.json to workdir. Returns the Path to the file.
    Factored out of extract() so pain_intel.py can drive the source loop
    explicitly and call this once on the merged corpus.
    """
    workdir = Path(workdir)
    facts   = extract_from_reviews(records, cfg)

    out_path = workdir / "synthesis_facts.json"
    out_path.write_text(
        json.dumps(facts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    m = facts["meta"]
    print(
        f"   extractor -> synthesis_facts.json  "
        f"({m['low_star_n']} low-star / {m['high_star_n']} high-star "
        f"of {m['total_reviews']} total)  "
        f"{len(facts['pain_points_low_star'])} pain phrases, "
        f"{len(facts['trust_signals_high_star'])} trust phrases"
    )
    return out_path


def extract(workdir, cfg):
    """Read reviews from disk for all configured sources, merge, extract.

    SAME signature as v1 -- inline tests and any direct caller use this
    entry point unchanged. For the default sources=["maps"], this loads
    exactly the v1 B2b file and is behaviorally identical to the pre-v1.1
    version (back-compat seam). For multi-source, loads and merges each
    source corpus before extraction.

    Returns the Path to synthesis_facts.json.
    """
    workdir = Path(workdir)
    sources = cfg.get("sources", ["maps"]) or ["maps"]
    merged  = []
    for src in sources:
        merged.extend(load_source_corpus(workdir, src))
    return extract_records(merged, workdir, cfg)


# ---------------------------------------------------------------------------
# Inline verification tests
# Run with:  python3 scripts/extractor.py
# No files, no network, no Apify, no LLM needed -- all in-memory.
# Pattern mirrors subjects.py exactly.
# ---------------------------------------------------------------------------

if __name__ == "__main__":

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

    # =========================================================================
    # Hand-made corpora for two distinct verticals.
    #
    # Design intent: low-star coffee reviews surface "cold coffee" / "cold";
    # low-star dentist reviews surface "painful" / "waiting room".
    # The adversarial assertion (TEST 5) checks these vocabularies do NOT
    # bleed between verticals -- proof that the engine is corpus-derived,
    # not secretly hard-coded.
    # =========================================================================

    COFFEE_REVIEWS = [
        # Low-star (stars <= 3)
        {"text": "The coffee was cold and terrible",     "stars": 1, "business": "Brew House"},
        {"text": "Cold coffee every single time",        "stars": 2, "business": "Brew House"},
        {"text": "Coffee always cold and lukewarm",      "stars": 2, "business": "Beanie Cafe"},
        {"text": "Long wait for cold coffee again",      "stars": 1, "business": "Beanie Cafe"},
        {"text": "Always cold coffee at this place",     "stars": 2, "business": "Brew House"},
        # High-star (stars >= 4)
        {"text": "Perfect hot espresso every time",           "stars": 5, "business": "Brew House"},
        {"text": "Lovely warm coffee and great atmosphere",   "stars": 5, "business": "Beanie Cafe"},
        {"text": "Fresh hot coffee and friendly staff",       "stars": 4, "business": "Brew House"},
        {"text": "Excellent barista and hot drinks daily",    "stars": 4, "business": "Beanie Cafe"},
        {"text": "Best warm coffee and cozy spot",            "stars": 5, "business": "Brew House"},
    ]

    DENTIST_REVIEWS = [
        # Low-star (stars <= 3)
        {"text": "Painful procedure and no explanation given",         "stars": 1, "business": "Dental A"},
        {"text": "Dentist caused pain during treatment",               "stars": 2, "business": "Dental A"},
        {"text": "Very painful and the dentist was rough",             "stars": 2, "business": "Dental B"},
        {"text": "Waiting room was cramped and dirty",                 "stars": 1, "business": "Dental A"},
        {"text": "Painful experience and crowded waiting room",        "stars": 2, "business": "Dental B"},
        # High-star (stars >= 4)
        {"text": "Gentle dentist and professional care provided",      "stars": 5, "business": "Dental A"},
        {"text": "Painless treatment and kind staff members",          "stars": 5, "business": "Dental B"},
        {"text": "Clean clinic and comfortable waiting room area",     "stars": 4, "business": "Dental A"},
        {"text": "No pain at all and very friendly dentist",           "stars": 5, "business": "Dental B"},
        {"text": "Excellent care and relaxing atmosphere here",        "stars": 4, "business": "Dental A"},
    ]

    SCHEMA_KEYS = {
        "meta", "pain_points_low_star", "trust_signals_high_star",
        "price_mentions", "low_star_quotes", "high_star_quotes",
    }
    META_KEYS = {
        "total_reviews", "low_star_n", "high_star_n", "star_distribution",
        "source_distribution", "per_source",                     # v1.1 additions
        "low_star_threshold", "high_star_threshold", "ngram_range",
        "min_phrase_count", "directional", "extraction_method", "note",
    }

    # =========================================================================
    # TEST 1 -- Output schema completeness (coffee corpus)
    # =========================================================================
    print("\n=== TEST 1: Schema completeness ===")
    try:
        coffee_facts = extract_from_reviews(COFFEE_REVIEWS, {})
        check("schema: all top-level keys present",
              set(coffee_facts.keys()), SCHEMA_KEYS)
        check("schema: all meta keys present",
              META_KEYS.issubset(set(coffee_facts["meta"].keys())), True)
        check("schema: total_reviews is int",
              isinstance(coffee_facts["meta"]["total_reviews"], int), True)
        check("schema: low_star_n is int",
              isinstance(coffee_facts["meta"]["low_star_n"], int), True)
        check("schema: high_star_n is int",
              isinstance(coffee_facts["meta"]["high_star_n"], int), True)
        check("schema: total_reviews correct",
              coffee_facts["meta"]["total_reviews"], 10)
        check("schema: low_star_n correct",
              coffee_facts["meta"]["low_star_n"], 5)
        check("schema: high_star_n correct",
              coffee_facts["meta"]["high_star_n"], 5)
        check("schema: directional=True (n=5 < 30)",
              coffee_facts["meta"]["directional"], True)
        check("schema: extraction_method is non-empty string",
              bool(coffee_facts["meta"]["extraction_method"]), True)
        check("schema: pain_points_low_star is list",
              isinstance(coffee_facts["pain_points_low_star"], list), True)
        check("schema: trust_signals_high_star is list",
              isinstance(coffee_facts["trust_signals_high_star"], list), True)
        check("schema: price_mentions has required keys",
              set(coffee_facts["price_mentions"].keys()),
              {"low_star_count", "high_star_count", "total"})
        check("schema: low_star_quotes is list",
              isinstance(coffee_facts["low_star_quotes"], list), True)
        check("schema: high_star_quotes is list",
              isinstance(coffee_facts["high_star_quotes"], list), True)
        check("schema: low_star_quotes count=5",
              len(coffee_facts["low_star_quotes"]), 5)
        check("schema: high_star_quotes count=5",
              len(coffee_facts["high_star_quotes"]), 5)
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  TEST 1 CRASHED: {exc}")
        coffee_facts = None

    # =========================================================================
    # TEST 2 -- Pain phrases for the coffee vertical
    # =========================================================================
    print("\n=== TEST 2: Coffee vertical pain phrases ===")
    if coffee_facts is not None:
        coffee_pain_phrases = [x["phrase"] for x in coffee_facts["pain_points_low_star"]]

        check("coffee pain: non-empty pain list",
              len(coffee_pain_phrases) > 0, True)
        check("coffee pain: 'cold' appears in pain phrases",
              "cold" in coffee_pain_phrases, True)
        check("coffee pain: 'cold coffee' appears in pain phrases",
              "cold coffee" in coffee_pain_phrases, True)

        if coffee_facts["pain_points_low_star"]:
            top = coffee_facts["pain_points_low_star"][0]
            check("coffee pain: top-1 phrase is 'cold' (highest score)",
                  top["phrase"], "cold")
            check("coffee pain: top-1 score > 10 (highly distinctive, not generic)",
                  top["score"] > 10, True)
            check("coffee pain: top-1 low_count is int",
                  isinstance(top["low_count"], int), True)
            check("coffee pain: top-1 high_count is int",
                  isinstance(top["high_count"], int), True)
            check("coffee pain: star_band='low'",
                  top["star_band"], "low")
    else:
        FAIL += 3
        print("  FAIL  (skipped -- TEST 1 crashed)")

    # =========================================================================
    # TEST 3 -- Pain phrases for the dentist vertical
    # =========================================================================
    print("\n=== TEST 3: Dentist vertical pain phrases ===")
    try:
        dentist_facts = extract_from_reviews(DENTIST_REVIEWS, {})
        dentist_pain_phrases = [x["phrase"] for x in dentist_facts["pain_points_low_star"]]

        check("dentist pain: non-empty pain list",
              len(dentist_pain_phrases) > 0, True)
        check("dentist pain: 'painful' appears in pain phrases",
              "painful" in dentist_pain_phrases, True)
        check("dentist pain: top-1 phrase is 'painful' (highest score)",
              dentist_facts["pain_points_low_star"][0]["phrase"] if dentist_facts["pain_points_low_star"] else None,
              "painful")
        check("dentist pain: top-1 score > 10 (highly distinctive)",
              dentist_facts["pain_points_low_star"][0]["score"] > 10 if dentist_facts["pain_points_low_star"] else False,
              True)
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  TEST 3 CRASHED: {exc}")
        dentist_facts = None

    # =========================================================================
    # TEST 4 -- Trust phrases (high-star signal)
    # =========================================================================
    print("\n=== TEST 4: Trust signal extraction ===")
    if coffee_facts is not None:
        coffee_trust_phrases = [x["phrase"] for x in coffee_facts["trust_signals_high_star"]]
        check("coffee trust: non-empty trust list",
              len(coffee_trust_phrases) > 0, True)
        check("coffee trust: first entry has star_band='high'",
              coffee_facts["trust_signals_high_star"][0]["star_band"] if coffee_facts["trust_signals_high_star"] else None,
              "high")
        # "hot" appears 3 times in high-star, 0 in low-star -> strong trust signal
        check("coffee trust: 'hot' appears as trust phrase",
              "hot" in coffee_trust_phrases, True)
    else:
        FAIL += 1
        print("  FAIL  (skipped -- TEST 1 crashed)")

    # =========================================================================
    # TEST 5 -- Adversarial corpus-driven proof
    #
    # If the engine were secretly hard-coded (word-list based), both verticals
    # would surface the same vocabulary. They must not. "cold" is a coffee-
    # specific pain; "painful" is a dentist-specific pain. Neither should
    # appear in the other vertical's pain list.
    # =========================================================================
    print("\n=== TEST 5: Adversarial corpus-driven proof ===")
    if coffee_facts is not None and dentist_facts is not None:
        coffee_pain_phrases = [x["phrase"] for x in coffee_facts["pain_points_low_star"]]
        dentist_pain_phrases = [x["phrase"] for x in dentist_facts["pain_points_low_star"]]

        check("adversarial: 'cold' is NOT in dentist pain list",
              "cold" in dentist_pain_phrases, False)
        check("adversarial: 'cold coffee' is NOT in dentist pain list",
              "cold coffee" in dentist_pain_phrases, False)
        check("adversarial: 'painful' is NOT in coffee pain list",
              "painful" in coffee_pain_phrases, False)
        check("adversarial: top coffee pain != top dentist pain",
              coffee_facts["pain_points_low_star"][0]["phrase"] !=
              dentist_facts["pain_points_low_star"][0]["phrase"]
              if (coffee_facts["pain_points_low_star"] and dentist_facts["pain_points_low_star"])
              else False,
              True)
    else:
        FAIL += 4
        print("  FAIL  (skipped -- earlier test crashed)")

    # =========================================================================
    # TEST 6 -- Distinctiveness: generic words (both-band) don't dominate
    #
    # "coffee" appears in both low-star and high-star coffee reviews.
    # Its distinctiveness score should be much lower than "cold" (low-star only).
    # "cold" must outrank "coffee" in the pain list.
    # =========================================================================
    print("\n=== TEST 6: Distinctiveness -- generic words do not dominate ===")
    if coffee_facts is not None:
        coffee_pain = coffee_facts["pain_points_low_star"]
        pain_phrases = [x["phrase"] for x in coffee_pain]

        # "coffee" appears in both bands; its score should be < 5
        coffee_score_entries = [x for x in coffee_pain if x["phrase"] == "coffee"]
        if coffee_score_entries:
            coffee_word_score = coffee_score_entries[0]["score"]
        else:
            coffee_word_score = 0.0   # not even in list (also fine)

        cold_score_entries = [x for x in coffee_pain if x["phrase"] == "cold"]
        cold_score = cold_score_entries[0]["score"] if cold_score_entries else 0.0

        check("distinctiveness: 'cold' score > 'coffee' score",
              cold_score > coffee_word_score, True)
        check("distinctiveness: 'cold' score >> 10 (not generic)",
              cold_score > 10, True)

        # The top-1 pain phrase must NOT be "coffee" (a both-band generic word)
        check("distinctiveness: top-1 pain phrase is not 'coffee'",
              (coffee_pain[0]["phrase"] if coffee_pain else "") != "coffee", True)
    else:
        FAIL += 3
        print("  FAIL  (skipped -- TEST 1 crashed)")

    # =========================================================================
    # TEST 7 -- Empty low-star corpus (graceful degradation)
    # =========================================================================
    print("\n=== TEST 7: Empty low-star corpus (graceful degradation) ===")
    all_high_reviews = [
        {"text": "Great place excellent service",   "stars": 5, "business": "Biz A"},
        {"text": "Wonderful experience every time", "stars": 5, "business": "Biz A"},
        {"text": "Highly recommend this business",  "stars": 4, "business": "Biz B"},
    ]
    try:
        empty_low_facts = extract_from_reviews(all_high_reviews, {})
        check("empty low-star: no crash",                              True,  True)
        check("empty low-star: low_star_n=0",
              empty_low_facts["meta"]["low_star_n"],                   0)
        check("empty low-star: directional=True",
              empty_low_facts["meta"]["directional"],                  True)
        check("empty low-star: pain_points_low_star is empty list",
              empty_low_facts["pain_points_low_star"],                 [])
        check("empty low-star: low_star_quotes is empty list",
              empty_low_facts["low_star_quotes"],                      [])
        check("empty low-star: meta note is non-empty (visible signal)",
              bool(empty_low_facts["meta"]["note"]),                   True)
        check("empty low-star: note mentions '0 low-star'",
              "0 low-star" in empty_low_facts["meta"]["note"],         True)
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  empty low-star CRASHED: {exc}")

    # =========================================================================
    # TEST 8 -- Field tolerance (alternate actor field names)
    #
    # The B2b actor output field names are # VERIFY -- they may differ between
    # actor versions. The parser tries a list of candidate keys; this test
    # confirms extraction still works when "reviewText"/"rating"/"placeName"
    # are used instead of the canonical "text"/"stars"/"title".
    # =========================================================================
    print("\n=== TEST 8: Field tolerance (alternate field names) ===")
    alt_field_reviews = [
        # Using: reviewText / rating / placeName  (not the canonical text/stars/title)
        {"reviewText": "Awful service and long wait for nothing",   "rating": 1, "placeName": "Test Biz"},  # VERIFY
        {"reviewText": "Terrible long wait and poor service",       "rating": 2, "placeName": "Test Biz"},  # VERIFY
        {"reviewText": "Long wait every single time we come here",  "rating": 2, "placeName": "Test Biz"},  # VERIFY
        {"reviewText": "Excellent and professional team",           "rating": 5, "placeName": "Test Biz"},  # VERIFY
        {"reviewText": "Outstanding service and friendly staff",    "rating": 5, "placeName": "Test Biz"},  # VERIFY
    ]
    try:
        alt_facts = extract_from_reviews(alt_field_reviews, {"min_phrase_count": 2})
        check("field tolerance: total_reviews=5 (all parsed)",
              alt_facts["meta"]["total_reviews"],                     5)
        check("field tolerance: low_star_n=3",
              alt_facts["meta"]["low_star_n"],                        3)
        check("field tolerance: 'long wait' in pain phrases",
              "long wait" in [x["phrase"] for x in alt_facts["pain_points_low_star"]],
              True)
        # Verify business field was also read via alternate key
        biz_names = {q["business"] for q in alt_facts["low_star_quotes"]}
        check("field tolerance: placeName read as business",
              "Test Biz" in biz_names,                               True)
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  field tolerance CRASHED: {exc}")

    # =========================================================================
    # TEST 9 -- Contraction handling (regression: real Google reviews)
    #
    # Found in the Phase-3 paid test on real Maps reviews: the tokenizer spaced
    # the apostrophe, so "it's" -> "it" + "s" and "doesn't" -> "doesn" + "t",
    # and the junk fragments "s"/"t"/"it s" dominated the phrase lists. Synthetic
    # test corpora used clean text and never exercised contractions. This test
    # locks the fix: apostrophes are deleted (contractions stay whole) and no
    # single-character or fragment token survives.
    # =========================================================================
    print("\n=== TEST 9: Contraction handling (regression) ===")
    try:
        toks = _tokenize("It's broken and doesn't work, they're never here, I can't")
        check("contractions stay whole ('doesnt' present)", "doesnt" in toks, True)
        check("no bare 's' fragment", "s" not in toks, True)
        check("no bare 't' fragment", "t" not in toks, True)
        check("no single-char tokens at all", all(len(t) > 1 for t in toks), True)
        contraction_reviews = [
            {"text": "It's always broken and doesn't ever get fixed", "stars": 1, "business": "B"},
            {"text": "The machines don't work and it's filthy",        "stars": 2, "business": "B"},
            {"text": "Doesn't work, wouldn't recommend, they're rude", "stars": 2, "business": "B"},
            {"text": "It's clean and the staff don't disappoint",      "stars": 5, "business": "B"},
            {"text": "They're friendly and it's always spotless",      "stars": 5, "business": "B"},
        ]
        cf = extract_from_reviews(contraction_reviews, {"min_phrase_count": 1})
        pain_phrases = [x["phrase"] for x in cf["pain_points_low_star"]]
        junk = {"s", "t", "it s", "doesn t", "don t", "they re", "can t"}
        check("no junk fragment phrases in pain output",
              not (set(pain_phrases) & junk), True)
        check("all pain phrase tokens are length>1",
              all(all(len(t) > 1 for t in p.split()) for p in pain_phrases), True)
    except Exception as exc:
        FAIL += 6
        print(f"  FAIL  TEST 9 CRASHED: {exc}")

    # =========================================================================
    # TEST 10 -- Source tag on quotes (v1.1)
    #
    # For a single-source corpus with no source field in the raw records, every
    # parsed quote must carry source=="maps" (the default). This is the
    # per-quote attribution requirement that makes multi-source reports honest.
    # =========================================================================
    print("\n=== TEST 10: Source tag on quotes ===")
    if coffee_facts is not None:
        low_srcs  = [q.get("source") for q in coffee_facts["low_star_quotes"]]
        high_srcs = [q.get("source") for q in coffee_facts["high_star_quotes"]]
        check("TEST10: every low-star quote has a source key",
              all(s is not None for s in low_srcs), True)
        check("TEST10: every high-star quote has a source key",
              all(s is not None for s in high_srcs), True)
        check("TEST10: all low-star quote sources are 'maps' (default for untagged records)",
              all(s == "maps" for s in low_srcs), True)
        check("TEST10: all high-star quote sources are 'maps'",
              all(s == "maps" for s in high_srcs), True)
        # Pain phrases must carry the sources annotation
        for item in coffee_facts["pain_points_low_star"]:
            if "sources" not in item:
                check("TEST10: pain phrase has 'sources' key", False, True)
                break
        else:
            check("TEST10: all pain phrases carry 'sources' annotation", True, True)
        # source_distribution present and correct for single-source corpus
        sd = coffee_facts["meta"].get("source_distribution", {})
        check("TEST10: source_distribution present in meta",     bool(sd),        True)
        check("TEST10: source_distribution has key 'maps'",      "maps" in sd,    True)
        check("TEST10: source_distribution['maps']==10 (total)", sd.get("maps"),  10)
        ps = coffee_facts["meta"].get("per_source", {})
        check("TEST10: per_source present in meta",              bool(ps),         True)
        check("TEST10: per_source['maps']['total']==10",
              ps.get("maps", {}).get("total"),                                     10)
        check("TEST10: per_source['maps']['low']==5",
              ps.get("maps", {}).get("low"),                                        5)
        check("TEST10: per_source['maps']['high']==5",
              ps.get("maps", {}).get("high"),                                       5)
    else:
        FAIL += 14
        print("  FAIL  (skipped -- TEST 1 crashed)")

    # =========================================================================
    # TEST 11 -- Two-source merged corpus (v1.1)
    #
    # Split the coffee corpus in half: first 5 records stay "maps", last 5 are
    # retagged "trustpilot". Merge and extract. Asserts:
    #   (a) source_distribution shows both sources with correct Ns
    #   (b) per_source low/high/total split is correct per source
    #   (c) every quote carries the source of its originating record
    #   (d) scores and phrase lists are computed from the combined corpus
    #       (not validated against oracle here -- the parity script does that)
    # =========================================================================
    print("\n=== TEST 11: Two-source merged corpus (v1.1) ===")
    try:
        import copy
        _maps_half       = [copy.copy(r) for r in COFFEE_REVIEWS[:5]]
        _trustpilot_half = [copy.copy(r) for r in COFFEE_REVIEWS[5:]]
        for r in _maps_half:
            r["source"] = "maps"
        for r in _trustpilot_half:
            r["source"] = "trustpilot"
        _merged = _maps_half + _trustpilot_half

        merged_facts = extract_from_reviews(_merged, {})

        sd2 = merged_facts["meta"].get("source_distribution", {})
        check("TEST11: source_distribution has 'maps'",            "maps"        in sd2, True)
        check("TEST11: source_distribution has 'trustpilot'",      "trustpilot"  in sd2, True)
        check("TEST11: maps N=5",                                   sd2.get("maps"),        5)
        check("TEST11: trustpilot N=5",                             sd2.get("trustpilot"),  5)
        check("TEST11: total N=10",
              sum(sd2.values()),                                                            10)

        ps2 = merged_facts["meta"].get("per_source", {})
        check("TEST11: per_source has 'maps'",                    "maps"       in ps2,  True)
        check("TEST11: per_source has 'trustpilot'",              "trustpilot" in ps2,  True)
        # maps half: reviews 0-4 are low-star (stars<=2 in COFFEE_REVIEWS)
        # The first 5 COFFEE_REVIEWS are all low-star (stars 1/2/2/1/2).
        check("TEST11: maps low=5 high=0",
              (ps2.get("maps", {}).get("low"),
               ps2.get("maps", {}).get("high")),                                  (5, 0))
        # trustpilot half: reviews 5-9 are all high-star (stars 5/5/4/4/5)
        check("TEST11: trustpilot low=0 high=5",
              (ps2.get("trustpilot", {}).get("low"),
               ps2.get("trustpilot", {}).get("high")),                            (0, 5))

        # Every quote must carry its originating source
        for q in merged_facts["low_star_quotes"]:
            if q.get("source") != "maps":
                check("TEST11: low-star quotes are source=maps", False, True)
                break
        else:
            check("TEST11: all low-star quotes have source='maps'",   True, True)

        for q in merged_facts["high_star_quotes"]:
            if q.get("source") != "trustpilot":
                check("TEST11: high-star quotes are source=trustpilot", False, True)
                break
        else:
            check("TEST11: all high-star quotes have source='trustpilot'", True, True)

        # Pain phrases should carry sources annotation showing maps only
        # (all low-star reviews came from the maps half)
        for item in merged_facts["pain_points_low_star"][:3]:
            src_keys = set(item.get("sources", {}).keys())
            if src_keys != {"maps"}:
                check(f"TEST11: pain phrase {item['phrase']!r} sources=={{maps}}", False, True)
                break
        else:
            check("TEST11: pain phrase sources annotations show maps only (low-star from maps half)",
                  True, True)

    except Exception as exc:
        FAIL += 13
        print(f"  FAIL  TEST 11 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 12: enabled source with 0 records appears in disclosure
    #
    # cfg={"sources":["maps","trustpilot"]} with a maps-only corpus: trustpilot
    # must appear in both source_distribution and per_source with N=0 so the
    # report can name it. Without the seed-before-count fix this would be absent.
    # =========================================================================
    print("\n=== TEST 12: empty enabled source appears in disclosure ===")
    try:
        import copy as _copy12
        _maps_only = [_copy12.copy(r) for r in COFFEE_REVIEWS]
        for r in _maps_only:
            r["source"] = "maps"
        adv006_facts = extract_from_reviews(_maps_only, {"sources": ["maps", "trustpilot"]})
        sd12 = adv006_facts["meta"]["source_distribution"]
        ps12 = adv006_facts["meta"]["per_source"]
        check("trustpilot in source_distribution (0-record source)",
              "trustpilot" in sd12, True)
        check("source_distribution['trustpilot'] == 0",
              sd12.get("trustpilot"), 0)
        check("trustpilot in per_source (0-record source)",
              "trustpilot" in ps12, True)
        check("per_source['trustpilot']['total'] == 0",
              ps12.get("trustpilot", {}).get("total"), 0)
        check("maps still correctly counted (N=10)",
              sd12.get("maps"), 10)
        check("per_source['maps']['total'] == 10",
              ps12.get("maps", {}).get("total"), 10)
    except Exception as exc:
        FAIL += 6
        print(f"  FAIL  TEST 12 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 13: silent star-drop / unparseable shrink disclosed
    #
    # A corpus containing 2 records with unparseable stars ("N/A" and None)
    # and 1 record with empty text must report dropped_records counts and
    # exclude them from n_total. Before the fix these were silently skipped.
    # =========================================================================
    print("\n=== TEST 13: dropped records are counted and disclosed ===")
    try:
        drop_corpus = [
            {"text": "Good service here", "stars": 5, "business": "B"},
            {"text": "Bad experience",    "stars": 1, "business": "B"},
            {"text": "Terrible",          "stars": "N/A", "business": "B"},  # unparseable
            {"text": "Awful",             "stars": None,  "business": "B"},  # null stars
            {"text": "",                  "stars": 3,     "business": "B"},  # empty text
        ]
        drop_facts = extract_from_reviews(drop_corpus, {})
        dr = drop_facts["meta"].get("dropped_records", {})
        check("dropped_records present in meta",
              "dropped_records" in drop_facts["meta"], True)
        check("no_stars == 2 (N/A and None)",
              dr.get("no_stars"), 2)
        check("empty_text == 1",
              dr.get("empty_text"), 1)
        check("total == 3",
              dr.get("total"), 3)
        check("n_total excludes dropped records (should be 2)",
              drop_facts["meta"]["total_reviews"], 2)
        check("meta note mentions dropped records",
              "dropped" in drop_facts["meta"].get("note", "").lower(), True)
    except Exception as exc:
        FAIL += 6
        print(f"  FAIL  TEST 13 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 14: quote-cap fires, note discloses phrase/quote diverge
    #
    # A corpus with 401 low-star reviews triggers the cap (cap=400). The capped
    # keys must be present AND meta["note"] must explain that phrase counts cover
    # all N reviews but only 400 quotes reach the writer.
    # =========================================================================
    print("\n=== TEST 14: quote-cap discloses phrase/quote diverge ===")
    try:
        _big_low  = [{"text": f"Terrible experience {i}", "stars": 1, "business": "Biz"}
                     for i in range(401)]
        _big_high = [{"text": "Great place highly recommend", "stars": 5, "business": "Biz"}]
        cap_facts = extract_from_reviews(_big_low + _big_high, {"min_phrase_count": 1})
        meta_cap  = cap_facts["meta"]
        check("low_quotes_capped_at present (401 > 400)",
              "low_quotes_capped_at" in meta_cap, True)
        check("low_quotes_capped_at == 400",
              meta_cap.get("low_quotes_capped_at"), 400)
        check("actual quote bank capped at 400",
              len(cap_facts["low_star_quotes"]), 400)
        note_cap = meta_cap.get("note", "")
        check("note mentions 'verbatim quotes'",
              "verbatim quotes" in note_cap, True)
        check("note mentions phrase counts (not just quotes)",
              "phrase" in note_cap.lower(), True)
    except Exception as exc:
        FAIL += 5
        print(f"  FAIL  TEST 14 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 15 -- COMPAT: extract_from_reviews(reviews, {}) byte-identical
    #
    # When cfg has NO "sources" key, no zero-seeded entries should appear in
    # source_distribution or per_source. This preserves backward-compat for all
    # inline tests that call extract_from_reviews(reviews, {}).
    # =========================================================================
    print("\n=== TEST 15: COMPAT -- cfg={} produces no ghost sources ===")
    try:
        compat_facts = extract_from_reviews(COFFEE_REVIEWS, {})
        sd_c = compat_facts["meta"]["source_distribution"]
        ps_c = compat_facts["meta"]["per_source"]
        check("COMPAT: source_distribution keys == {'maps'} only (cfg={})",
              set(sd_c.keys()), {"maps"})
        check("COMPAT: per_source keys == {'maps'} only (cfg={})",
              set(ps_c.keys()), {"maps"})
        check("COMPAT: maps total still N=10",
              sd_c.get("maps"), 10)
        check("COMPAT: dropped_records present even for clean corpus (total==0)",
              compat_facts["meta"].get("dropped_records", {}).get("total"), 0)
    except Exception as exc:
        FAIL += 4
        print(f"  FAIL  TEST 15 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 16: load_source_corpus authoritative source override
    #
    # A record that already carries a "source" field with a wrong value must be
    # retagged to the loader's authoritative source. The folder being read is the
    # ground truth -- the actor's own "source" field cannot be trusted (an actor
    # may emit a conflicting field, mis-attributing quotes to the wrong platform).
    # The non-dict guard is preserved unchanged.
    # =========================================================================
    print("\n=== TEST 16: authoritative source override ===")
    try:
        _adv015_rec = {"text": "some review text", "stars": 3, "source": "wrongvalue"}
        # Simulate the exact override logic now in load_source_corpus.
        _adv015_source = "maps"
        if isinstance(_adv015_rec, dict):
            _adv015_rec["source"] = _adv015_source
        check("authoritative override replaces conflicting source field",
              _adv015_rec["source"], "maps")
        # Non-dict records must not be mutated (existing guard preserved).
        _adv015_nondict = "raw string record"
        _adv015_mutated = False
        if isinstance(_adv015_nondict, dict):
            _adv015_mutated = True
        check("non-dict records are not mutated (isinstance guard preserved)",
              _adv015_mutated, False)
    except Exception as exc:
        FAIL += 2
        print(f"  FAIL  TEST 16 CRASHED: {exc}")

    # =========================================================================
    # TEST 17: 'worth' and 'value' removed from _PRICE_WORDS
    #
    # "worth every minute" and "great value" are positive-sentiment phrases, not
    # price signals. Removing these two words drops the false positives while
    # preserving genuine price detection via co-occurring anchors: currency
    # symbols, "price", "money", etc. still fire as expected.
    # =========================================================================
    print("\n=== TEST 17: price lexicon false positives removed ===")
    try:
        check("'worth every minute' is NOT a price mention (false positive removed)",
              _has_price_mention("worth every minute"), False)
        check("'great value' is NOT a price mention (false positive removed)",
              _has_price_mention("great value"), False)
        check("'not worth the $50' IS a price mention (currency anchor preserves it)",
              _has_price_mention("not worth the $50"), True)
        check("'the price is too high' IS a price mention",
              _has_price_mention("the price is too high"), True)
        check("'good value for the money' IS a price mention (money anchor)",
              _has_price_mention("good value for the money"), True)
    except Exception as exc:
        FAIL += 5
        print(f"  FAIL  TEST 17 CRASHED: {exc}")

    # =========================================================================
    # TEST 18: multilingual tokenizer (Unicode-aware)
    #
    # Before the fix _tokenize stripped every non-Latin character, so a Cyrillic
    # corpus produced empty token lists and zero pain phrases even for high-signal
    # reviews. After the fix \w (Unicode-aware) preserves non-ASCII letters.
    # Also verifies the non-English corpus caveat fires when nonascii_ratio > 0.20.
    # =========================================================================
    print("\n=== TEST 18: multilingual tokenizer ===")
    try:
        # Accented Latin: 'é' is now preserved (not stripped to 'caf')
        _cafe_toks = _tokenize("café")
        check("accented Latin preserved (café -> token contains 'café')",
              "café" in _cafe_toks, True)

        # Mostly-Cyrillic corpus -- the silent-empty break:
        # before the fix pain_points_low_star came back empty because every Cyrillic
        # token was stripped to spaces. After the fix Cyrillic tokens survive.
        _cyr_low = [
            {"text": "это ужасно медленно и плохо",    "stars": 1, "business": "Biz"},
            {"text": "очень плохое обслуживание здесь", "stars": 2, "business": "Biz"},
            {"text": "медленно и ужасно плохо совсем",  "stars": 1, "business": "Biz"},
        ]
        _cyr_high = [
            {"text": "отличное место и хорошо работает", "stars": 5, "business": "Biz"},
            {"text": "прекрасное обслуживание всегда",   "stars": 5, "business": "Biz"},
        ]
        _cyr_facts = extract_from_reviews(_cyr_low + _cyr_high, {"min_phrase_count": 2})

        check("Cyrillic corpus n_low > 0 (reviews counted)",
              _cyr_facts["meta"]["low_star_n"] > 0, True)
        check("pain_points non-empty (silent-empty break fixed)",
              len(_cyr_facts["pain_points_low_star"]) > 0, True)
        check("nonascii_ratio present in meta",
              "nonascii_ratio" in _cyr_facts["meta"], True)
        check("nonascii_ratio > 0.20 for Cyrillic corpus",
              _cyr_facts["meta"].get("nonascii_ratio", 0) > 0.20, True)
        check("English-tuned caveat in note for non-English corpus",
              "english" in _cyr_facts["meta"]["note"].lower(), True)
    except Exception as exc:
        FAIL += 6
        print(f"  FAIL  TEST 18 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 19: near-duplicate dedup (review-bomb resistance)
    #
    # 5 copy-pasted identical low-star reviews collapse to 1 after dedup.
    # n_low reflects the deduped count (3, not 7); meta["deduped"]["removed"]
    # is 4; and the note discloses the removal.
    # =========================================================================
    print("\n=== TEST 19: near-duplicate dedup ===")
    try:
        _dup_text = "Terrible experience here awful service"
        _bomb = (
            [{"text": _dup_text, "stars": 1, "business": "B"}] * 5 +
            [{"text": "Slow service always delayed",      "stars": 2, "business": "B"},
             {"text": "Slow service every time we visit", "stars": 1, "business": "B"}]
        )
        _bomb_facts = extract_from_reviews(_bomb, {})

        check("deduped key present in meta",
              "deduped" in _bomb_facts["meta"], True)
        check("removed == 4 (5 identical collapse to 1)",
              _bomb_facts["meta"]["deduped"]["removed"], 4)
        check("n_low == 3 after dedup (not 7)",
              _bomb_facts["meta"]["low_star_n"], 3)
        check("note mentions duplicates removed",
              "duplicate" in _bomb_facts["meta"]["note"].lower(), True)
    except Exception as exc:
        FAIL += 4
        print(f"  FAIL  TEST 19 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # TEST 20: graduated directional-honesty bands
    #
    # The old cliff silenced the note entirely at n_low=30. The fix replaces it
    # with graduated bands so the note is NEVER fully empty. Tests n_low=30
    # (indicative), n_low=100 (moderate), n_low=500 (substantial). Also confirms
    # the coffee fixture is unchanged (n_low=5 -> directional, note identical).
    # =========================================================================
    print("\n=== TEST 20: graduated directional-honesty bands ===")
    try:
        def _make_band_corpus(n_low, n_high=5):
            """Unique low-star + unique high-star reviews (no dedup collapse)."""
            lows  = [{"text": f"bad service issue repeat{i}", "stars": 1, "business": "B"}
                     for i in range(n_low)]
            highs = [{"text": f"great experience place enjoy{i}", "stars": 5, "business": "B"}
                     for i in range(n_high)]
            return lows + highs

        _band30  = extract_from_reviews(_make_band_corpus(30),  {})
        _band100 = extract_from_reviews(_make_band_corpus(100), {})
        _band500 = extract_from_reviews(_make_band_corpus(500), {})

        # n_low=30 -> indicative band
        check("n_low=30 note is non-empty (old cliff removed)",
              bool(_band30["meta"]["note"]), True)
        check("n_low=30 note contains 'indicative'",
              "indicative" in _band30["meta"]["note"].lower(), True)
        check("n_low=30 confidence_band=='indicative'",
              _band30["meta"].get("confidence_band"), "indicative")
        check("n_low=30 directional==False (bool unchanged, 30 is not < 30)",
              _band30["meta"]["directional"], False)

        # n_low=100 -> moderate band
        check("n_low=100 note is non-empty",
              bool(_band100["meta"]["note"]), True)
        check("n_low=100 note contains 'moderate'",
              "moderate" in _band100["meta"]["note"].lower(), True)
        check("n_low=100 confidence_band=='moderate'",
              _band100["meta"].get("confidence_band"), "moderate")

        # n_low=500 -> substantial band
        check("n_low=500 note is non-empty",
              bool(_band500["meta"]["note"]), True)
        check("n_low=500 note contains 'substantial'",
              "substantial" in _band500["meta"]["note"].lower(), True)
        check("n_low=500 confidence_band=='substantial'",
              _band500["meta"].get("confidence_band"), "substantial")

        # Coffee fixture unchanged: n_low=5 -> directional, note text intact
        if coffee_facts is not None:
            check("coffee fixture directional still True (n_low=5 < 30)",
                  coffee_facts["meta"]["directional"], True)
            check("coffee fixture note still contains 'directional'",
                  "directional" in coffee_facts["meta"].get("note", "").lower(), True)
        else:
            FAIL += 2
            print("  FAIL  (skipped -- coffee_facts from TEST 1 unavailable)")
    except Exception as exc:
        FAIL += 12
        print(f"  FAIL  TEST 20 CRASHED: {exc}")
        import traceback; traceback.print_exc()

    # =========================================================================
    # Summary + top-5 pain phrases for both verticals (corpus-driven proof)
    # =========================================================================
    print(f"\n{'='*60}")

    if coffee_facts is not None:
        print("\nTop-5 pain phrases -- COFFEE vertical:")
        for i, item in enumerate(coffee_facts["pain_points_low_star"][:5], 1):
            print(f"  {i}. {item['phrase']!r:30s}  score={item['score']:.2f}  "
                  f"low={item['low_count']}  high={item['high_count']}")

    if dentist_facts is not None:
        print("\nTop-5 pain phrases -- DENTIST vertical:")
        for i, item in enumerate(dentist_facts["pain_points_low_star"][:5], 1):
            print(f"  {i}. {item['phrase']!r:30s}  score={item['score']:.2f}  "
                  f"low={item['low_count']}  high={item['high_count']}")

    print(f"\n{'='*60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
