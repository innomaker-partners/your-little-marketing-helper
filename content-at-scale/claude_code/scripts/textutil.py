#!/usr/bin/env python3
"""Shared text handling for the three deterministic gates.

One module on purpose. If `length` and `overlap` tokenised differently, their
numbers would not be comparable and a user reading both would be quietly
misled - a 900-word piece by one count and 840 by another, with no way to
tell which the threshold was set against.

Python 3, standard library only.

Everything here is mechanical. No judgment lives in this file, and none
should ever be added to it.
"""

from __future__ import annotations

import collections
import re

# A word is a run of letters and digits, allowing internal apostrophes and
# hyphens, so "don't" and "mid-market" each count once rather than two or
# three. Unicode letters count: this tool is used on non-English copy.
#
# The decimal alternative comes FIRST so "3.5" stays one token instead of
# being split at the point. Letters and digits mix in the second alternative
# so a product name like "n8n" or "GPT-4" is one word - the team writes about
# those constantly, and splitting "n8n" into n / 8 / n inflated every count
# that touched them.
#
# KNOWN LIMIT, not papered over: languages written without spaces (Chinese,
# Japanese) collapse into one token per run of characters. Segmenting them
# needs a dictionary, which is not in the standard library and not in this
# tool's scope. If content-at-scale is ever pointed at CJK copy, the length
# gate will be wrong and this is where to fix it.
WORD_RE = re.compile(r"\d+(?:[.,]\d+)+|[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Requires a letter, slash or bang after "<" so prose like "a < b" survives.
_HTML_TAG_RE = re.compile(r"<[/!]?[a-zA-Z][^>]*>")
_FOOTNOTE_REF_RE = re.compile(r"\[\^[^\]]+\]")
# A link definition line carries no text a reader sees - only a label and a
# target. A FOOTNOTE definition is different and is handled above by removing
# just its label: the body is prose someone reads, so it keeps counting.
_LINK_DEF_RE = re.compile(r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+", re.MULTILINE)
# Asterisks and tildes are always markup. Underscores are only markup at a
# word boundary: "snake_case", "n8n_workflow" and Make scenario names keep
# theirs, while "_emphasis_" loses them. Stripping every underscore mangled
# exactly the product names this team writes about.
_EMPHASIS_RE = re.compile(
    r"\*{1,3}|~~|(?<![^\W_])_{1,3}|_{1,3}(?![^\W_])")


def strip_markdown(text: str) -> str:
    """Reduce markdown to the prose a reader actually reads.

    What is removed, and why each one:

      * YAML frontmatter - metadata, never read as content
      * fenced code blocks and inline code - a code sample is not prose, and
        counting it would let a piece hit its length target on snippets
      * HTML comments, and HTML tags - a reader sees "important", not
        "b important b"
      * link and image targets, inline and reference style, and bare URLs -
        the visible LINK TEXT is kept because a reader reads it
      * link definition lines - a label and a target, no reader-visible text
      * footnote markers like [^1] - the marker is machinery
      * heading, blockquote, list and emphasis markers - syntax, not words

    What is deliberately NOT removed: heading text, list item text,
    blockquote text, and FOOTNOTE BODIES. All four are read by someone. The
    footnote body is the arguable one - it is read at the bottom of the page
    rather than in the flow - but dropping it would silently shorten a piece
    that does its citing properly, and this tool is not in the business of
    penalising that.

    Four-space indented code blocks are NOT stripped, and that is a known
    gap rather than a decision: telling them apart from an indented list
    continuation needs a real markdown parser.
    """
    text = _FRONTMATTER_RE.sub("", text or "")
    text = _FENCE_RE.sub(" ", text)
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _FOOTNOTE_REF_RE.sub(" ", text)
    text = _LINK_DEF_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub(" ", text)
    text = _HEADING_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    return text


def tokenize(text: str, markdown: bool = True) -> list:
    """Lowercased word tokens. The one definition of 'a word' in this tool."""
    if markdown:
        text = strip_markdown(text)
    return [m.group(0).lower() for m in WORD_RE.finditer(text)]


def ngrams(tokens: list, n: int) -> list:
    if n < 1:
        raise ValueError("n-gram length must be at least 1")
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def normalize_ws(text: str) -> str:
    """Collapse whitespace. Used when comparing sentences for identity."""
    return re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip()


# A header word for casing: letters (Unicode) with internal apostrophes or
# hyphens, so "e-commerce" and "don't" are one token and "GPT-4" survives as
# "GPT" + "-4" joined. Digits are allowed inside so "n8n" stays whole.
_HEADER_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def _is_abbrev(tok: str) -> bool:
    """True if a token's own casing marks it an abbreviation or brand.

    Two shapes qualify, and only these two, because only these two can be told
    apart from an ordinary Title-cased word by casing alone:

      * all-caps, length >= 2 - "AI", "SEO", "ROI", "GPT-4"
      * an interior capital - "ChatGPT", "iPhone", "YouTube"

    A single leading capital ("Tools", "Google", "Small") is NOT enough: an
    ordinary word title-cased by a writer and a proper noun look identical this
    way. That collision is the known limit documented on sentence_case_header.
    """
    cased = [c for c in tok if c.isalpha()]
    if len(cased) >= 2 and tok.isupper():
        return True
    return any(c.isupper() for c in tok[1:])


def sentence_case_header(text: str) -> str:
    """Sentence-case a header whose abbreviations are ALREADY correctly cased.

    The contract is the precondition: the input must arrive with its
    abbreviations and brand names capitalised as they should read ("AI",
    "SEO", "ChatGPT"). That casing is fixed once, at intake, from
    human-confirmed keyword display forms - never guessed here. Given that,
    this function is pure mechanism:

      * an ordinary word - all-lowercase, or Title-case with only its first
        letter capital - is lowercased: "Tools" -> "tools", "Use" -> "use"
      * a token carrying an interior capital or standing all-caps is an
        abbreviation or brand and is preserved verbatim: "SEO", "AI",
        "ChatGPT", "iPhone" all survive untouched (see _is_abbrev)
      * the header's first letter is uppercased, UNLESS the first word is an
        abbreviation/brand whose own casing must stand: "marketing automation"
        -> "Marketing automation", but "AI SEO tools" and "iPhone tips" are
        left to open as they are

    Worked: "How to Use AI in Marketing" -> "How to use AI in marketing";
    "AI SEO Tools for Small Business" -> "AI SEO tools for small business";
    "## ai seo tools 2026" (never intake-cased, so the precondition is broken)
    -> "Ai seo tools 2026" - WRONG, and wrong on purpose: this function cannot
    invent the capital in "AI" from a lowercase source, which is exactly why
    the casing is fixed upstream at intake and not here.

    Why not title-case or a lookup table: title-case is the defect this
    replaces; a lookup table of abbreviations is a list to maintain that breaks
    silently on the first brand it has not seen. This rule needs no list.

    KNOWN LIMIT, not papered over: a proper noun with a single leading capital
    and no all-caps or interior capital ("Google", "Hungary") is
    indistinguishable by casing from an ordinary Title-cased word and will be
    lowercased. The fix, if a run needs it, is the same one that cases the rest:
    carry the proper noun's display form from intake and, where it must survive
    sentence-casing, write it in a shape _is_abbrev recognises or use it as the
    verbatim keyword header (which is not passed through this function).
    """
    text = text or ""
    first = _HEADER_WORD_RE.search(text)
    first_is_abbrev = bool(first) and _is_abbrev(first.group(0))

    def repl(m: "re.Match") -> str:
        tok = m.group(0)
        return tok if _is_abbrev(tok) else tok.lower()

    result = _HEADER_WORD_RE.sub(repl, text)
    if first_is_abbrev:
        return result
    for i, ch in enumerate(result):
        if ch.isalpha():
            return result[:i] + ch.upper() + result[i + 1:]
    return result


# Number token: a digit-bearing run, allowing internal commas and decimal
# points, with an optional trailing percent sign. Applied to strip_markdown
# output so markdown syntax does not create phantom numbers. Used only in
# fact_tokens; the WORD_RE family handles everything else.
_NUMTOKEN_RE = re.compile(r"\d[\d.,]*%?")


def fact_tokens(text: str) -> tuple[collections.Counter, collections.Counter]:
    """Extract fact-bearing tokens from a sentence as (number_Counter, name_Counter).

    WHY this shape, and its limits — documented here rather than papered over,
    in the same spirit as WORD_RE's CJK note and _is_abbrev's single-capital note:

    Numbers and proper names are the fact-bearing surface that changes most
    dangerously in a de-slop reword, and that casing and digits make
    mechanically detectable without any model judgment. Qualifiers such as
    "up to" or "roughly", and whole-assertion reversals, are NOT mechanically
    detectable this way — the prose guard in de-slop/SKILL.md §5b stays in
    force for those. This function is defence-in-depth for the mechanical cases;
    it is not a replacement for the prose guard. (FROZEN)

    The check is a DIFFERENCE DETECTOR whose false positives are SAFE: a false
    "differ" escalates to the orchestrator (one extra ladder trip), never
    allows a wrong stamp. So the design is liberal — when in doubt, a change
    is treated as a difference. This is the same "doubt routes to the safe
    side" principle §5b already states. (FROZEN)

    KNOWN LIMIT, not papered over: a proper noun that is the sentence-initial
    word in one version but mid-sentence in the other will produce different
    name-token sets, because the first-word exclusion applies in only one
    version. The result is a DIFFER (a safe false positive — it escalates to
    the orchestrator rather than allowing a silent stamp). The reverse case —
    two DIFFERENT proper nouns both at sentence-initial position in their
    respective versions — is undetectable by casing alone (both are excluded),
    and is the documented evasion. The number-token channel is unaffected by
    this limit.

    Number tokens: every digit-bearing run in strip_markdown(text), including
    a trailing percent sign when present ("35%", "3.5", "1,000").

    Name tokens (case-preserved, from WORD_RE over strip_markdown output):
    - A token is a name token if it is Title-cased (first letter uppercase)
      OR _is_abbrev() returns True.
    - The sentence-initial token is excluded from the Title-case rule, because
      the first word of any sentence is capitalised for position, not because
      it is a proper noun (excluding it prevents "We"→"Our" from firing).
    - _is_abbrev tokens are kept even at position 0: an all-caps or interior-
      capital opener (e.g. "AI", "ChatGPT") really is a brand or abbreviation.

    Comparison is multiset (order-independent): a reword may move a name
    without changing which names are present.
    """
    clean = strip_markdown(text or "")
    nums = collections.Counter(_NUMTOKEN_RE.findall(clean))
    words = list(WORD_RE.finditer(clean))
    names = []
    for i, m in enumerate(words):
        tok = m.group(0)
        if _is_abbrev(tok):
            names.append(tok)
        elif i > 0 and tok[0].isalpha() and tok[0].isupper():
            names.append(tok)
    return nums, collections.Counter(names)


def fact_tokens_match(original: str, new: str) -> tuple[bool, dict]:
    """Compare fact-bearing tokens of two sentences.

    Returns (match: bool, diff: dict).

    match is True  → fact tokens are identical; stamp allowed (subject to the
                     prose guards in de-slop/SKILL.md §5b, which still apply).
    match is False → fact tokens differ; escalate to the orchestrator.
                     Do not stamp. Do not self-authorize.

    diff is empty when match is True. When False it carries one or both keys:
      "numbers": {"original": {...}, "new": {...}}
      "names":   {"original": {...}, "new": {...}}
    These let the de-slop report quote exactly what differed.
    """
    orig_nums, orig_names = fact_tokens(original)
    new_nums, new_names = fact_tokens(new)
    nums_ok = orig_nums == new_nums
    names_ok = orig_names == new_names
    diff: dict = {}
    if not nums_ok:
        diff["numbers"] = {
            "original": dict(orig_nums),
            "new": dict(new_nums),
        }
    if not names_ok:
        diff["names"] = {
            "original": dict(orig_names),
            "new": dict(new_names),
        }
    return nums_ok and names_ok, diff


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()
