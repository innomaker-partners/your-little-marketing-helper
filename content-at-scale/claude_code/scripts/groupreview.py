#!/usr/bin/env python3
"""Group review: surface candidate repeats and format divergences for agent judgment.

Step 12 of the content-at-scale pipeline. Runs after every piece in a group is
finished (produced, fact-checked, de-slopped, made readable). Reads the whole
finished group together and surfaces three classes of candidates:

  1. Intra-piece repeated spans — text that occurs twice or more within a single
     piece. Catches both exact repeats and near-exact ones (a repeat where a couple
     of words differ).

  2. Cross-piece shared distinctive content — a named example, statistic, proper
     noun cluster, or specific number appearing in multiple pieces, even when the
     phrasing differs. Combines exact n-gram matching with a distinctive-token
     signal so paraphrased duplicates are not missed.

  3. Format profile and conformance — structural facts (heading outline, word
     count, H1, CTA presence) per piece; cross-piece consistency check; declared
     spec parameter conformance.

THIS SCRIPT PROPOSES; IT NEVER DECIDES. The judgment ("is this a legitimate
bookend or lazy duplication?") belongs to the reviewing agent. That division is
deliberate and FROZEN: a deterministic, testable floor under the judgment-heavy
stage, per the Step 12 design.

Tunable constants are marked MOVABLE below with notes on calibration direction.
Defaults are recall-tuned: a false positive (surfaced candidate the agent keeps)
costs one cheap judgment; a false negative (missed real repeat) ships.

CLI:
  python3 groupreview.py --run-dir <run> --group <gid>
  python3 groupreview.py --pieces p1.md p2.md p3.md --group g1
  python3 groupreview.py --help

Outputs a JSON object to stdout. Exit code is always 0 (this script surfaces,
it does not gate; there is no pass/fail here).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

import textutil as T

# ---------------------------------------------------------------------------
# Tunable constants (principled defaults, never fitted to any corpus)
# ---------------------------------------------------------------------------

# N-gram length for exact phrase detection (cross-piece and intra-piece).
# Matches overlap.py's DEFAULT_PHRASE_LENGTH=4 so thresholds are comparable.
DEFAULT_PHRASE_LENGTH = 4

# Maximum token-position substitutions for the near-repeat detector.
# 0 = exact only; 1 = one token may differ (catches light paraphrases).
# MOVABLE: raise to 2 for looser near-matching (more candidates, more noise).
NEAR_MAX_SUBS = 1

# Tokens appearing in more than this fraction of pieces are common vocabulary,
# not distinctive content. A distinctive token must also appear in >= 2 pieces.
# MOVABLE: lower if the group shares unusually heavy terminology.
DISTINCTIVE_MAX_PIECE_FRACTION = 0.75

# Characters of surrounding text to include as context for each occurrence.
# MOVABLE: raise for more context, lower to reduce output size.
CONTEXT_RADIUS = 60

# Characters from end of piece to scan for a CTA or trailing link.
# MOVABLE: raise for longer pieces.
CTA_WINDOW = 500

# Minimum non-stopword tokens a repeated span must contain to be surfaced.
# A span of pure function words ("you don't need to") carries no distinctive
# repetition signal. Requires at least this many content tokens.
# MOVABLE: raise to 3 for stricter filtering.
MIN_CONTENT_TOKENS = 2

# A verbatim span with this many content tokens is inherently distinctive,
# regardless of vocabulary. Long verbatim spans are suspicious even when they
# consist of common words — the probability of accidental co-occurrence at this
# length is negligible. MOVABLE: lower to 5 to catch shorter verbatim repeats.
LONG_SPAN_TOKENS = 6

# Fraction of pieces a content token may appear in and still be considered
# "rare across the group." 0.34 means must appear in ≤1 of 3 pieces; 0.51
# means ≤1 of 2 or ≤1 of 3. MOVABLE: raise to allow tokens in 2-of-3 to qualify.
RARE_PIECE_FRACTION = 0.34

# Minimum character length a content token must have to qualify via the rarity
# condition. Short common words ("add", "end", "kind") can appear in only one
# piece by chance; requiring 5+ chars filters most of them.
# MOVABLE: lower to 4 if the group shares unusually long vocabulary.
MIN_RARE_TOKEN_LENGTH = 5

# English function words that carry no distinctive repetition signal.
# A repeated span is surfaced only if it contains >= MIN_CONTENT_TOKENS tokens
# NOT in this set. Focused on core function words; does not include content
# words even when common (e.g. "work", "need", "think").
_STOPWORDS = frozenset({
    # articles, prepositions, conjunctions
    "a", "an", "the", "and", "or", "but", "nor", "so", "yet", "both",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "up",
    "down", "out", "off", "as", "into", "about", "through", "before",
    "after", "while", "although", "because", "unless", "until", "whether",
    # pronouns
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "their", "our", "its", "this", "that",
    "these", "those", "who", "what", "which", "it",
    # auxiliaries (forms)
    "be", "been", "being", "am", "are", "is", "was", "were",
    "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "must", "shall", "can",
    # contractions (tokenised forms)
    "don't", "doesn't", "didn't", "isn't", "aren't", "wasn't", "weren't",
    "haven't", "hasn't", "hadn't", "won't", "wouldn't", "couldn't",
    "shouldn't", "it's", "that's", "let's", "they're", "we're", "you're",
    "i'm", "can't", "n't",
    # common adverbs / quantifiers that add nothing distinctive
    "all", "each", "every", "some", "any", "also", "just", "only", "even",
    "still", "now", "then", "than", "when", "where", "how", "if",
    # determiners / correlatives
    "either", "neither",
    # filler
    "etc",
})


# ---------------------------------------------------------------------------
# Token distinctiveness
# ---------------------------------------------------------------------------

def _is_distinctive_token(tok: str) -> bool:
    """True if a token is likely a proper noun, abbreviation, or statistic.

    Three shapes qualify, and only these, because only these can be told apart
    from ordinary prose tokens by surface form alone:

      * Digit-bearing: statistics, years, prices, model names. "4,000", "2026",
        "GPT-4", "n8n", "90-day" all qualify.
      * All-caps (>= 2 alphabetic chars): "SEO", "AI", "ROI", "CRM".
      * Interior capital: "ChatGPT", "iPhone", "HubSpot", "YouTube", "LinkedIn".

    A plain capitalized word at sentence-start ("This", "The", "Marketing") does
    NOT qualify — deliberate trade-off to avoid enormous sentence-start noise.
    Same logic as textutil._is_abbrev().
    """
    if any(c.isdigit() for c in tok):
        return True
    letters = [c for c in tok if c.isalpha()]
    if not letters:
        return False
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return True
    if any(c.isupper() for c in letters[1:]):
        return True
    return False


def _distinctive_tokens_in(text: str) -> frozenset:
    """Extract distinctive tokens preserving original casing for detection.

    Works on strip_markdown output BEFORE lowercasing, so "YouTube" keeps
    its interior capital. Returns a frozenset of lowercased tokens (for
    cross-piece comparison).
    """
    stripped = T.strip_markdown(text)
    result = set()
    for m in T.WORD_RE.finditer(stripped):
        tok = m.group(0)
        if _is_distinctive_token(tok):
            result.add(tok.lower())
    return frozenset(result)


# ---------------------------------------------------------------------------
# Working-file banner stripping
# ---------------------------------------------------------------------------

# The working-file status banner stamped at the top of source documents:
#   > **WORKING FILE. NOT FINISHED, NOT APPROVED, NOT FOR PUBLICATION.**
#
# Must be removed before any n-gram or token analysis; otherwise the boilerplate
# leaks into shared n-grams between pieces that all carry the same banner.
#
# Guard: requires a blockquote marker at line start PLUS "WORKING FILE" followed
# immediately by "NOT FINISHED". That combination is implausible in real
# marketing prose and cannot appear in a mid-sentence fragment.
_BANNER_RE = re.compile(
    r"^[ \t]*>[ \t]*\**WORKING\s+FILE[.,]?\s+NOT\s+FINISHED[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_banner(text: str) -> str:
    """Remove the working-file banner from piece text (conditional, safe to apply always)."""
    return _BANNER_RE.sub("", text)


# ---------------------------------------------------------------------------
# Distinctiveness gate
# ---------------------------------------------------------------------------

def _is_gate_distinctive(tok: str) -> bool:
    """True if tok qualifies as distinctive for the candidacy gate.

    Compound-digit tokens (containing both digit and non-digit chars, like
    "90-day", "3,500", "gpt-4") qualify. Bare integers like "2026", "37"
    do NOT — they are too common as years, counts, and round numbers.

    All-caps abbreviations (SEO, CRM) and interior-cap proper nouns (YouTube,
    HubSpot) also qualify. Works correctly on ORIGINAL-CASE tokens extracted
    from the piece text; lowercased copies of such tokens will not fire this
    function (use doc_freq rarity for lowercased content instead).
    """
    letters = [c for c in tok if c.isalpha()]
    # Compound digit: has at least one digit AND at least one non-digit char
    # (ratio, price, model name, hyphenated compound like "90-day")
    if any(c.isdigit() for c in tok) and any(not c.isdigit() for c in tok):
        return True
    if not letters:
        return False
    # All-caps abbreviation
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return True
    # Interior capital (camelCase / PascalCase brand names)
    if len(letters) >= 2 and any(c.isupper() for c in letters[1:]):
        return True
    return False


def _compute_doc_freq(piece_texts: dict) -> dict:
    """Return {lowercased_token: count_of_pieces_containing_it} for non-None pieces."""
    freq: dict = {}
    for text in piece_texts.values():
        if text is None:
            continue
        cleaned = T.strip_markdown(_strip_banner(text))
        toks_in_piece = {
            m.group(0).lower()
            for m in T.WORD_RE.finditer(cleaned)
        }
        for tok in toks_in_piece:
            freq[tok] = freq.get(tok, 0) + 1
    return freq


def _span_is_gate_ok(span_text: str, original_text: str,
                     doc_freq: dict, n_pieces: int) -> bool:
    """True if a repeated/shared span passes the distinctiveness gate.

    A span qualifies if it meets at least one of:
    (a) Contains a compound-digit / all-caps / interior-cap token in the
        original-case piece text (proper nouns, statistics, model names).
    (b) Contains a content token that is RARE across the group — appears in
        fewer than ALL pieces (i.e., it is not common vocabulary that every
        piece in the group shares).
    (c) Is a LONG verbatim span: >= LONG_SPAN_TOKENS non-stopword tokens.

    Rationale (FROZEN): generic common-word repetition carries no signal.
    The gate enforces that only distinctive or long spans are surfaced. A
    false negative (missed distinctive repeat) is worse than a false positive
    here, so conditions are generous — any one of the three suffices.
    """
    toks = span_text.split()
    content_toks = [t for t in toks if t not in _STOPWORDS]

    # (c) Long span — check first, cheapest exit
    if len(content_toks) >= LONG_SPAN_TOKENS:
        return True

    # (a) Compound-digit / proper-noun token in original text
    # Find the span in the original (case-preserving) text and scan tokens
    pos = original_text.lower().find(span_text.lower())
    if pos != -1:
        orig_span = original_text[pos: pos + len(span_text)]
        for m in T.WORD_RE.finditer(orig_span):
            if _is_gate_distinctive(m.group(0)):
                return True

    # (b) Strictly rare content token: only meaningful when comparing across
    # multiple pieces (n_pieces >= 2). With a single piece every token has
    # freq=1 by construction, making the rarity test degenerate.
    if n_pieces >= 2:
        max_rare = max(1, round(n_pieces * RARE_PIECE_FRACTION))
        for tok in content_toks:
            freq = doc_freq.get(tok, 0)
            if 0 < freq <= max_rare and len(tok) >= MIN_RARE_TOKEN_LENGTH:
                return True

    return False


# ---------------------------------------------------------------------------
# Line number and context helpers
# ---------------------------------------------------------------------------

def _build_line_starts(text: str) -> list:
    """Character offsets where each line begins (0-based lines)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset_to_line(offset: int, line_starts: list) -> int:
    """1-based line number for a character offset (binary search)."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _context_at(offset: int, text: str) -> str:
    """Short snippet of text centred on offset."""
    start = max(0, offset - CONTEXT_RADIUS)
    end = min(len(text), offset + CONTEXT_RADIUS)
    return text[start:end].replace("\n", " ").strip()


def _find_span_line(stripped: str, span_text: str) -> int:
    """1-based line of the first occurrence of span_text in stripped."""
    pos = stripped.lower().find(span_text.lower())
    if pos == -1:
        return -1
    return stripped[:pos].count("\n") + 1


def _find_any_token_line(stripped: str, tokens: list) -> int:
    """1-based line of the first occurrence of any token in stripped."""
    for tok in tokens:
        pos = stripped.lower().find(tok.lower())
        if pos != -1:
            return stripped[:pos].count("\n") + 1
    return -1


# ---------------------------------------------------------------------------
# Detector 1: intra-piece repeated spans
# ---------------------------------------------------------------------------

def _maximal_multi_span(tokens: list, positions: list, n: int) -> tuple:
    """Find the maximal shared span at all occurrence positions simultaneously.

    Extends the initial n-gram both FORWARD and BACKWARD as long as all
    positions continue to match. This ensures every overlapping n-gram within
    the same repeated passage maps to the same canonical start + length:

      A passage of K tokens yields K-n+1 repeated n-grams, each starting one
      token later. The gram at position p+1 backward-extends by 1 token (tokens
      at p match in both occurrences), recovering the same (start, length) as
      the gram at position p. One passage -> one key -> one candidate.

    Returns (new_start_positions, total_span_length).
    new_start_positions[i] is the canonical start of occurrence i, shifted
    backward from positions[i] by the common backward extension.
    """
    n_tok = len(tokens)

    # Extend forward: how many additional tokens match beyond the initial n-gram
    forward = n
    while True:
        if any(pos + forward >= n_tok for pos in positions):
            break
        next_toks = {tokens[pos + forward] for pos in positions}
        if len(next_toks) != 1:
            break
        forward += 1

    # Extend backward: how far before the gram start tokens still match
    backward = 0
    while True:
        if any(pos - backward <= 0 for pos in positions):
            break
        prev_toks = {tokens[pos - backward - 1] for pos in positions}
        if len(prev_toks) != 1:
            break
        backward += 1

    new_starts = [pos - backward for pos in positions]
    return new_starts, forward + backward


def _span_has_content(span_text: str) -> bool:
    """True if span contains >= MIN_CONTENT_TOKENS non-stopword tokens.

    Filter rationale: a span of pure function words ("you don't need to") is not
    a meaningful repetition signal — the same string recurs in any prose. Requiring
    content tokens ensures only distinctive repeats are surfaced.
    """
    toks = span_text.split()
    return sum(1 for t in toks if t not in _STOPWORDS) >= MIN_CONTENT_TOKENS


def _dedup_intra(candidates: list,
                 phrase_length: int = DEFAULT_PHRASE_LENGTH) -> list:
    """Post-process intra-piece candidates: filter noise and remove sub-span fragments.

    Applied after the per-passage key deduplication in intra_piece():

    (b) Same-line filter: drop candidates whose occurrences all fall on the same
        line. These are intra-sentence repetitions, not cross-distance redundancy.

    (c) Stopword filter: drop candidates with < MIN_CONTENT_TOKENS non-stopword
        tokens. Pure function-word spans carry no meaningful repetition signal.

    (a) Sub-span deduplication: when a candidate is a fragment of a longer
        already-kept candidate AND they share at least one occurrence line, drop
        the shorter. "Fragment" means EITHER:
          - its span text is a substring of the kept span (exact fragment), OR
          - its span shares >= phrase_length-1 tokens with the kept span
            (token-overlap fragment — catches near-repeat sub-grams whose span
            starts one position before or after the exact-repeat span, so the
            substring check misses them).

    Step (a) cleans up residual near-repeat fragments: a passage like "define
    clearly what needs to be done" generates both an exact-repeat for "clearly
    what needs to be done..." (long span) and near-repeat fragments like "define
    clearly what needs" (4-token, not a substring of the long span but sharing
    3/4 tokens). The token-overlap check catches these.
    """
    # (b) Same-line filter
    candidates = [
        c for c in candidates
        if len({occ["line"] for occ in c["occurrences"]}) >= 2
    ]

    # (c) Stopword/content filter
    candidates = [c for c in candidates if _span_has_content(c["span"])]

    # (a) Sub-span deduplication: longest span first; drop fragments.
    candidates.sort(key=lambda c: -len(c["span"]))
    kept: list = []
    min_shared = phrase_length - 1  # 3 for default phrase_length=4
    for cand in candidates:
        cand_lines = {occ["line"] for occ in cand["occurrences"]}
        cand_toks = set(cand["span"].split())
        is_fragment = False
        for k in kept:
            k_lines = {occ["line"] for occ in k["occurrences"]}
            if not (cand_lines & k_lines):
                continue  # different occurrence lines — distinct passages
            # Exact substring OR sufficient token overlap → fragment
            if cand["span"] in k["span"]:
                is_fragment = True
                break
            k_toks = set(k["span"].split())
            if len(cand_toks & k_toks) >= min_shared:
                is_fragment = True
                break
        if not is_fragment:
            kept.append(cand)
    return kept


def intra_piece(piece_id: str, text: str,
                phrase_length: int = DEFAULT_PHRASE_LENGTH) -> list:
    """Detector 1: surface repeated spans within a single piece.

    Returns a list of:
        {"piece": str, "span": str,
         "occurrences": [{"line": int, "context": str}, ...]}

    For exact repeats: uses bidirectional span extension (_maximal_multi_span)
    so that all overlapping n-grams within the same repeated passage collapse to
    one canonical (start, length) key — one candidate per passage, not one per
    overlapping sub-gram.

    For near-repeats: uses the mask technique (NEAR_MAX_SUBS substitutions).

    Post-processed by _dedup_intra to filter same-line occurrences, stopword-only
    spans, and any residual sub-span fragments.
    """
    text = _strip_banner(text)
    tokens = T.tokenize(text, markdown=True)
    if len(tokens) < phrase_length * 2:
        return []

    stripped = T.strip_markdown(text)

    token_offsets: list = [m.start() for m in T.WORD_RE.finditer(stripped)]
    line_starts = _build_line_starts(stripped)

    def _occ(pos: int, length: int = 1) -> dict:
        # `text` is the ACTUAL token slice at this position, not the candidate's
        # representative span. For exact repeats every occurrence holds the same
        # span, so text == span. For near-repeats each occurrence holds a
        # one-token variant of the representative gram; reporting the actual text
        # per occurrence prevents the phantom-occurrence bug where a line is
        # labelled with a span it does not literally contain.
        if pos >= len(token_offsets):
            return {"line": -1, "context": "", "text": ""}
        off = token_offsets[pos]
        return {
            "line": _offset_to_line(off, line_starts),
            "context": _context_at(off, stripped),
            "text": " ".join(tokens[pos:pos + length]),
        }

    grams = T.ngrams(tokens, phrase_length)
    gram_positions: dict = defaultdict(list)
    for i, gram in enumerate(grams):
        gram_positions[gram].append(i)

    raw_candidates: list = []
    # Track canonical passage keys (frozenset of new_starts after bidirectional
    # extension) to emit exactly one candidate per unique repeated passage.
    passage_keys: set = set()

    # --- Exact repeats: one candidate per maximal passage ---
    for gram, positions in gram_positions.items():
        if len(positions) < 2:
            continue
        new_starts, total_len = _maximal_multi_span(tokens, positions, phrase_length)
        key = frozenset(new_starts)
        if key in passage_keys:
            continue
        passage_keys.add(key)

        span_text = " ".join(tokens[new_starts[0]:new_starts[0] + total_len])
        raw_candidates.append({
            "piece": piece_id,
            "span": span_text,
            "occurrences": [_occ(s, total_len) for s in new_starts],
        })

    # --- Near-repeats (NEAR_MAX_SUBS substitutions via mask technique) ---
    # For each n-gram, generate masks with one position set to None.
    # Grams sharing a mask differ in exactly that one position -> near-match.
    if NEAR_MAX_SUBS >= 1 and grams:
        near_keys: set = set()
        mask_map: dict = defaultdict(lambda: defaultdict(list))
        for i, gram in enumerate(grams):
            for k in range(len(gram)):
                mask = gram[:k] + (None,) + gram[k + 1:]
                mask_map[mask][gram].append(i)

        for mask, gram_groups in mask_map.items():
            unique_grams = list(gram_groups.keys())
            if len(unique_grams) < 2:
                continue

            all_positions = sorted({
                pos
                for positions in gram_groups.values()
                for pos in positions
            })
            if len(all_positions) < 2:
                continue

            key = frozenset(all_positions)
            if key in near_keys or key in passage_keys:
                continue
            near_keys.add(key)

            # Representative span: first gram's text (near-repeat spans are
            # phrase_length tokens; _dedup_intra removes sub-span fragments).
            rep_gram = unique_grams[0]
            span_text = " ".join(rep_gram)
            raw_candidates.append({
                "piece": piece_id,
                "span": span_text,
                "occurrences": [_occ(p, phrase_length) for p in all_positions],
            })

    # Post-process: filter noise and collapse any residual sub-span fragments
    return _dedup_intra(raw_candidates, phrase_length=phrase_length)


# ---------------------------------------------------------------------------
# Detector 2: cross-piece shared distinctive content
# ---------------------------------------------------------------------------

def cross_piece(pieces: dict, phrase_length: int = DEFAULT_PHRASE_LENGTH) -> list:
    """Detector 2: cross-piece repetition candidates and shared-fact locators.

    Cross-piece repetition is the SAME job as intra-piece repetition, at group
    scope: a shared fact, statistic, or example is reusable supporting material
    and is NOT a defect — pieces are expected to draw on the same evidence, and
    for the user's own data doubly so. The defect is the same argument delivered
    the same WAY: near-verbatim phrasing carrying the same point in two pieces.

    So the two signals mean DIFFERENT things, and are labelled accordingly:
      (a) shared_phrasing — near-verbatim wording shared across pieces. This is
          the repetition candidate: the reviewer checks whether it is the same
          argument the same way (defect) or incidental shared wording (fine).
      (b) shared_fact — the same distinctive fact/example (proper noun, statistic,
          number) appearing in multiple pieces even when the phrasing differs.
          This is NOT a defect candidate. It is a LOCATOR: it tells the reviewer
          where a shared fact lives so they can check whether its DELIVERY is also
          duplicated. Sharing the fact is legitimate; only same-delivery is not.

    Returns a list of:
        {"kind": "shared_phrasing"|"shared_fact",
         "shared": str,
         "pieces": [{"piece": str, "line": int}, ...]}
    """
    piece_ids = sorted(pieces.keys())
    results = []
    reported_shared: set = set()

    # Strip the working-file banner before any analysis — it creates spurious
    # shared n-grams between pieces that all carry the same boilerplate header.
    pieces = {pid: _strip_banner(t) for pid, t in pieces.items()}

    # --- Signal (a): exact shared n-grams ---
    piece_ngrams: dict = {}
    piece_stripped: dict = {}
    for pid, text in pieces.items():
        tokens = T.tokenize(text, markdown=True)
        piece_ngrams[pid] = set(T.ngrams(tokens, phrase_length))
        piece_stripped[pid] = T.strip_markdown(text)

    for i, pid1 in enumerate(piece_ids):
        for pid2 in piece_ids[i + 1:]:
            shared = piece_ngrams[pid1] & piece_ngrams[pid2]
            if not shared:
                continue
            for gram in sorted(shared):
                span_text = " ".join(gram)
                key = ("ngram", span_text, pid1, pid2)
                if key in reported_shared:
                    continue
                reported_shared.add(key)
                results.append({
                    "kind": "shared_phrasing",
                    "shared": span_text,
                    "pieces": [
                        {"piece": pid1,
                         "line": _find_span_line(piece_stripped[pid1], span_text)},
                        {"piece": pid2,
                         "line": _find_span_line(piece_stripped[pid2], span_text)},
                    ],
                })

    # --- Signal (b): shared distinctive tokens ---
    piece_distinctive: dict = {}
    for pid, text in pieces.items():
        piece_distinctive[pid] = _distinctive_tokens_in(text)

    token_doc_freq: Counter = Counter()
    for pid, dt in piece_distinctive.items():
        for tok in dt:
            token_doc_freq[tok] += 1

    n_pieces = max(1, len(piece_ids))
    max_freq = max(1, round(n_pieces * DISTINCTIVE_MAX_PIECE_FRACTION))

    # Tokens in 2..max_freq pieces are distinctive-AND-shared
    group_distinctive = {
        tok for tok, freq in token_doc_freq.items()
        if 2 <= freq <= max_freq
    }

    for i, pid1 in enumerate(piece_ids):
        for pid2 in piece_ids[i + 1:]:
            shared_toks = sorted(
                (piece_distinctive[pid1] & piece_distinctive[pid2]) & group_distinctive
            )
            if not shared_toks:
                continue
            key = ("distinctive", frozenset(shared_toks), pid1, pid2)
            if key in reported_shared:
                continue
            reported_shared.add(key)
            results.append({
                "kind": "shared_fact",
                "shared": ", ".join(shared_toks),
                "pieces": [
                    {"piece": pid1,
                     "line": _find_any_token_line(piece_stripped[pid1], shared_toks)},
                    {"piece": pid2,
                     "line": _find_any_token_line(piece_stripped[pid2], shared_toks)},
                ],
            })

    return results


# ---------------------------------------------------------------------------
# Detector 3: format profile and conformance
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_TRAILING_LINK_RE = re.compile(r"\[.+?\]\(https?://", re.IGNORECASE)
_FREE_TOOL_RE = re.compile(r"\bfree\s+tool\b", re.IGNORECASE)


def format_profile(pieces: dict, spec: dict, group_id: str) -> dict:
    """Detector 3: format profile per piece, cross-piece divergences, and
    conformance against declared spec parameters.

    Returns:
        {
            "per_piece": [...],
            "cross_piece_divergences": [...],
            "declared_param_status": [...],
        }

    Purely descriptive: divergences are candidates for the agent, not rulings.
    """
    per_piece = []
    for pid in sorted(pieces.keys()):
        text = pieces[pid]
        if text is None:
            per_piece.append({
                "piece": pid,
                "words": None,
                "h1": None,
                "headings": [],
                "has_cta": None,
                "note": "file missing",
            })
            continue

        headings = [
            [len(m.group(1)), m.group(2).strip()]
            for m in _HEADING_RE.finditer(text)
        ]
        h1 = next((h[1] for h in headings if h[0] == 1), None)
        words = len(T.tokenize(text, markdown=True))
        tail = text[-CTA_WINDOW:] if len(text) > CTA_WINDOW else text
        has_cta = bool(_TRAILING_LINK_RE.search(tail) or _FREE_TOOL_RE.search(tail))

        per_piece.append({
            "piece": pid,
            "words": words,
            "h1": h1,
            "headings": headings,
            "has_cta": has_cta,
        })

    # --- Cross-piece consistency ---
    divergences = []
    present = [p for p in per_piece if p.get("words") is not None]

    missing_h1 = [p["piece"] for p in present if p["h1"] is None]
    if missing_h1:
        divergences.append(f"missing H1: {', '.join(missing_h1)}")

    heading_counts = [len(p["headings"]) for p in present]
    if len(heading_counts) > 1:
        median_count = sorted(heading_counts)[len(heading_counts) // 2]
        if median_count > 0:
            outliers = [
                p["piece"] for p in present
                if abs(len(p["headings"]) - median_count) > median_count * 0.5
            ]
            if outliers:
                divergences.append(
                    f"heading count diverges from median {median_count}: "
                    + ", ".join(outliers)
                )

    cta_vals = [p["has_cta"] for p in present if p.get("has_cta") is not None]
    if cta_vals and any(cta_vals) and not all(cta_vals):
        missing_cta = [p["piece"] for p in present if not p["has_cta"]]
        divergences.append(f"CTA absent from: {', '.join(missing_cta)}")

    # --- Declared-parameter conformance ---
    param_status = []
    length_bounds = spec.get("length_bounds", {})
    group_bounds = length_bounds.get(group_id, {})
    if group_bounds:
        lb_min = group_bounds.get("min")
        lb_max = group_bounds.get("max")
        for p in present:
            words = p["words"]
            if lb_min is not None and lb_max is not None:
                ok = lb_min <= words <= lb_max
                detail = f"{words} in [{lb_min},{lb_max}]"
            elif lb_min is not None:
                ok = words >= lb_min
                detail = f"{words} >= {lb_min}"
            elif lb_max is not None:
                ok = words <= lb_max
                detail = f"{words} <= {lb_max}"
            else:
                continue
            param_status.append({
                "piece": p["piece"],
                "param": "length_bounds",
                "ok": ok,
                "detail": detail,
            })

    return {
        "per_piece": per_piece,
        "cross_piece_divergences": divergences,
        "declared_param_status": param_status,
    }


# ---------------------------------------------------------------------------
# Top-level analysis
# ---------------------------------------------------------------------------

def _dedup_cross_ngrams(cross_results: list,
                        phrase_length: int = DEFAULT_PHRASE_LENGTH) -> list:
    """Deduplicate shared_phrasing cross-piece candidates within each piece pair.

    Multiple overlapping n-grams from the same piece pair (e.g. "ai platforms
    like google", "platforms like google ai", "like google ai overviews" — all
    from the same shared passage) are fragments of one repeated passage. Keep
    only the longest non-overlapping representative per piece pair, using the
    same token-overlap suppression as _dedup_intra.

    shared_fact candidates are passed through unchanged.
    """
    from collections import defaultdict
    pair_groups: dict = defaultdict(list)
    other: list = []
    for c in cross_results:
        if c["kind"] == "shared_phrasing":
            pair = frozenset(p["piece"] for p in c["pieces"])
            pair_groups[pair].append(c)
        else:
            other.append(c)

    # Cross-piece passages are shorter and more varied than intra-piece passages;
    # 2-token overlap is sufficient to identify co-located n-gram fragments from
    # the same shared passage within one piece pair.
    min_shared = max(2, phrase_length - 2)
    result = list(other)
    for pair, candidates in pair_groups.items():
        candidates.sort(key=lambda c: -len(c["shared"]))
        kept: list = []
        for cand in candidates:
            cand_toks = set(cand["shared"].split())
            is_fragment = any(
                len(cand_toks & set(k["shared"].split())) >= min_shared
                for k in kept
            )
            if not is_fragment:
                kept.append(cand)
        result.extend(kept)
    return result


def _merge_colocated(candidates: list) -> list:
    """Collapse intra-piece candidates that point at overlapping passage regions.

    Two passes:

    Pass 1 — equal occurrence line-sets: multiple short repeated spans whose
    occurrence lines are IDENTICAL (e.g. three 4-gram fragments of the same
    bookend passage at lines [3,29]) are fragments of one repeated passage.
    Keep only the longest representative for each (piece, frozenset_of_lines).

    Pass 2 — subset occurrence line-sets: if one candidate's occurrence lines
    are a strict SUBSET of another same-piece candidate's lines, the smaller
    is subsumed by the larger passage. Drop the subset candidate (the larger
    one already flags the same content at more sites).

    Applied AFTER _dedup_intra so the token-overlap fragment filter runs first.
    """
    # Pass 1: identical line-sets
    groups: dict = {}
    for c in candidates:
        key = (c["piece"], frozenset(occ["line"] for occ in c["occurrences"]))
        if key not in groups or len(c["span"]) > len(groups[key]["span"]):
            groups[key] = c
    after_equal = list(groups.values())

    # Pass 2: subset line-sets (same piece, lines A ⊂ lines B → drop A)
    result = []
    for cand in after_equal:
        cand_piece = cand["piece"]
        cand_lines = frozenset(occ["line"] for occ in cand["occurrences"])
        subsumed = any(
            k["piece"] == cand_piece
            and frozenset(occ["line"] for occ in k["occurrences"]) > cand_lines
            for k in after_equal
            if k is not cand
        )
        if not subsumed:
            result.append(cand)
    return result


def analyze(group_id: str, pieces: dict, spec: dict,
            phrase_length: int = DEFAULT_PHRASE_LENGTH) -> dict:
    """Run all three detectors and return the combined JSON output.

    pieces: {piece_id: text_or_None}
    spec: the run spec dict (may be empty; detectors degrade gracefully)

    Post-processing applied here (after per-detector calls):
    1. Co-located merge: collapse intra candidates with identical occurrence
       line-sets to the single longest representative.
    2. Distinctiveness gate: drop any remaining intra or cross shared_phrasing
       candidates that carry no distinctive signal — no compound-digit or
       proper-noun token, no group-rare content token, and shorter than
       LONG_SPAN_TOKENS content tokens.
    """
    live_pieces = {pid: text for pid, text in pieces.items() if text is not None}
    n_pieces = max(1, len(live_pieces))

    # Pre-compute document frequency across the group for the gate's rarity test
    doc_freq = _compute_doc_freq(live_pieces)

    # Intra-piece detection
    raw_intra: list = []
    for pid, text in sorted(live_pieces.items()):
        raw_intra.extend(intra_piece(pid, text, phrase_length))

    # Co-located merge: one repeated passage → one candidate
    merged_intra = _merge_colocated(raw_intra)

    # Distinctiveness gate: keep only spans with distinctive/rare/long signal
    intra = [
        c for c in merged_intra
        if _span_is_gate_ok(c["span"], live_pieces[c["piece"]],
                            doc_freq, n_pieces)
    ]

    # Cross-piece detection
    raw_cross = cross_piece(live_pieces, phrase_length)

    # Distinctiveness gate for shared_phrasing candidates only;
    # shared_fact are already distinctive by construction.
    gated_cross = []
    for c in raw_cross:
        if c["kind"] == "shared_fact":
            gated_cross.append(c)
        elif c["kind"] == "shared_phrasing":
            # Use the first piece's text as the original for case-checking
            first_pid = c["pieces"][0]["piece"]
            orig = live_pieces.get(first_pid, c["shared"])
            if _span_is_gate_ok(c["shared"], orig, doc_freq, n_pieces):
                gated_cross.append(c)

    # Cross-piece shared_phrasing dedup: when multiple overlapping n-grams from the
    # same piece-pair all passed the gate, they are fragments of the same shared
    # passage. For each piece pair, keep only the longest non-overlapping spans
    # (same token-overlap test as _dedup_intra, applied per piece pair).
    cross = _dedup_cross_ngrams(gated_cross, phrase_length)

    fmt = format_profile(pieces, spec, group_id)

    return {
        "group": group_id,
        "phrase_length": phrase_length,
        "intra_piece": intra,
        "cross_piece": cross,
        "format": fmt,
    }


# ---------------------------------------------------------------------------
# Run-directory piece discovery
# ---------------------------------------------------------------------------

def _find_pieces_in_run(run_dir: str, group_id: str) -> dict:
    """Return {piece_id: text_or_None} for all pieces in a group.

    Looks under {run_dir}/pieces/ for subdirectories named {group_id}-p*.
    File preference order (most to least finished):
      readable.md > final.md > verified.md > draft.md

    readable.md is the human-readable polished version; final.md is the
    content-at-scale final output; verified.md is claim-verified but not yet
    human-readable; draft.md is the raw initial draft. This order ensures we
    always analyse the most finished available artifact.

    Records None for a piece with no recognised file rather than crashing,
    so a partially-finished group is still surfaced (format profile notes it).
    """
    pieces_dir = os.path.join(run_dir, "pieces")
    if not os.path.isdir(pieces_dir):
        raise FileNotFoundError(
            f"No pieces/ directory found in {run_dir!r}. "
            "Is this a valid run directory?"
        )

    pieces: dict = {}
    prefix = group_id + "-p"
    for entry in sorted(os.listdir(pieces_dir)):
        if not entry.startswith(prefix):
            continue
        piece_dir = os.path.join(pieces_dir, entry)
        if not os.path.isdir(piece_dir):
            continue
        text = None
        for fname in ("readable.md", "final.md", "verified.md", "draft.md"):
            fpath = os.path.join(piece_dir, fname)
            if os.path.isfile(fpath):
                text = T.read_text(fpath)
                break
        pieces[entry] = text

    return pieces


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Group review: surface candidate repeats and format divergences. "
            "Outputs JSON to stdout. Proposes candidates; never decides."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 groupreview.py --run-dir /path/to/run --group g2
  python3 groupreview.py --pieces p1.md p2.md p3.md --group g1
  python3 groupreview.py --run-dir /path/to/run --group g1 --phrase-length 5
""",
    )
    ap.add_argument(
        "--run-dir", dest="run_dir", metavar="PATH",
        help="path to a run directory; discovers pieces automatically",
    )
    ap.add_argument(
        "--group", required=True, metavar="GID",
        help="group ID, e.g. g1 or g2",
    )
    ap.add_argument(
        "--pieces", nargs="+", metavar="PATH",
        help="explicit piece file paths (alternative to --run-dir; group ID labels output only)",
    )
    ap.add_argument(
        "--phrase-length", type=int, default=DEFAULT_PHRASE_LENGTH, dest="phrase_length",
        help=f"n-gram length for phrase matching (default: {DEFAULT_PHRASE_LENGTH})",
    )
    a = ap.parse_args(argv)

    if a.run_dir and a.pieces:
        print("error: --run-dir and --pieces are mutually exclusive", file=sys.stderr)
        return 1
    if not a.run_dir and not a.pieces:
        print("error: one of --run-dir or --pieces is required", file=sys.stderr)
        return 1
    if a.phrase_length < 2:
        print("error: --phrase-length must be at least 2", file=sys.stderr)
        return 1

    spec: dict = {}

    if a.run_dir:
        run_dir = os.path.abspath(a.run_dir)
        try:
            pieces = _find_pieces_in_run(run_dir, a.group)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        if not pieces:
            print(
                f"error: no pieces found for group {a.group!r} in {run_dir!r}. "
                "Check that the group ID matches directories under pieces/.",
                file=sys.stderr,
            )
            return 1

        # Read spec from manifest (lazy import, same pattern as overlap.py)
        try:
            import manifest as _M
            data = _M.load(run_dir)
            spec = data.get("spec", {})
        except Exception:
            spec = {}

    else:
        pieces = {}
        for path in a.pieces:
            pid = os.path.splitext(os.path.basename(path))[0]
            try:
                pieces[pid] = T.read_text(path)
            except OSError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

    result = analyze(a.group, pieces, spec, phrase_length=a.phrase_length)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
