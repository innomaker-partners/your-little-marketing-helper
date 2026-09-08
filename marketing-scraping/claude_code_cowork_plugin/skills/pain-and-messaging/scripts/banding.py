#!/usr/bin/env python3
"""Component B -- sentiment banding for NO-STAR sources (Reddit now; others later).

A star-rated source (Maps, Trustpilot, App Store, Google Play) hands the extractor
a numeric `stars`, and the extractor splits pain (low) from trust (high) on that
number. Reddit has no stars. This module synthesizes the same signal so the
extractor and load_source_corpus stay COMPLETELY UNCHANGED (frozen spine).

Three outcomes per item, not two:
  - pain    -> synthetic stars = LOW_STARS  (enters the main low-star corpus)
  - praise  -> synthetic stars = HIGH_STARS (enters the main high-star corpus)
  - neutral -> NO stars; set aside into a separate list, never forced into the
               analysis. The majority of real Reddit discussion is neither a
               complaint nor praise (questions, topic chatter, jokes); forcing
               those into pain/praise would manufacture signal and violate the honesty rule that small-N sources must not manufacture signal. Neutral
               items are returned
               separately so the caller can store them (rigour/transparency) and
               optionally surface general topics & questions from them.

Why synthetic stars and not a new `band` field the extractor reads: the extractor
splits strictly on numeric `stars` and skips records whose stars is None
(extractor.py). Materializing the band as the field it already trusts keeps the
spine frozen; the honesty burden lives entirely in report.py's inferred-band
disclosure (Reddit N is reported as inferred-negative / inferred-positive, NEVER
as real 2- or 5-star reviews).

Polarity comes from LANGUAGE, never from score. On Reddit an upvote count is
crowd agreement, not sentiment: a complaint ("Is Spotify down?") can have 1977
upvotes. So the deterministic pass keys off a conservative complaint-vs-praise
lexicon with negation handling; score is carried through untouched as salience
context but is NOT used to decide polarity.

Two passes (deterministic-first: code assigns confident items; the LLM
only labels the ambiguous middle, one judgment per item, and counts nothing):
  1. Deterministic: a clear single-polarity item is banded in code.
  2. LLM adjudication: the ambiguous remainder is sent to a cheap claude-CLI
     subagent (haiku) that returns PAIN / PRAISE / NEUTRAL per item. The call is
     run without MCP servers loaded (via --strict-mcp-config), for a faster start
     with no side effects (see report.py::_run_claude for why: a plain `claude`
     subprocess boots the operator's whole MCP/browser stack).
     No timeout (non-deterministic step). On failure it degrades safely: the
     unadjudicated items fall to NEUTRAL, so a broken CLI can never manufacture
     pain -- it only shrinks the analyzed corpus.
"""

import json
import re
import subprocess
from pathlib import Path

# Synthetic star values. The extractor's default split is stars <= 3 -> low,
# stars >= 4 -> high. 2 is safely low without implying "worst possible"; 5 is
# clear praise. Configurable via cfg but these defaults match the thresholds.
LOW_STARS = 2
HIGH_STARS = 5

# Conservative lexicons -- ONLY unambiguous terms. The gray zone is the LLM's
# job; a loose lexicon would make confident-but-wrong deterministic calls that
# no later step can catch. Tuned for a low false-positive rate: only unambiguous sentiment terms are counted.
_COMPLAINT = {
    "broken", "crash", "crashes", "crashing", "crashed", "terrible", "awful",
    "horrible", "worst", "useless", "unusable", "refund", "refunds", "scam",
    "ripoff", "overpriced", "annoying", "frustrating", "frustrated", "buggy",
    "glitch", "glitchy", "glitches", "outage", "outages", "disappointed",
    "disappointing", "garbage", "trash", "sucks", "sucked", "complaint",
    "complain", "complaining", "laggy", "freezes", "freezing", "unreliable",
    "worse", "hate", "hated", "waste", "wasted", "error", "errors", "failing",
}
_PRAISE = {
    "love", "loved", "great", "excellent", "perfect", "amazing", "awesome",
    "fantastic", "best", "wonderful", "recommend", "flawless", "brilliant",
    "superb", "impressed", "reliable", "seamless", "favorite", "favourite",
    "underrated", "gem",
}
# Multi-word cues checked as substrings on the lowercased text.
_COMPLAINT_PHRASES = (
    "not working", "doesn't work", "does not work", "won't work", "wont work",
    "stopped working", "won't load", "wont load", "can't login", "cant login",
    "can't log in", "no longer use", "rip off", "rip-off", "waste of money",
)
_PRAISE_PHRASES = (
    "works great", "works perfectly", "worth it", "about time", "well done",
    "highly recommend", "so good", "love it", "love this",
)
_NEGATORS = {
    "not", "no", "never", "cant", "can't", "cannot", "dont", "don't", "doesnt",
    "doesn't", "didnt", "didn't", "wont", "won't", "isnt", "isn't", "hardly",
    "barely", "without",
}
_TOKEN = re.compile(r"[a-z']+")
# Clause boundary markers -- a negator cannot reach across one of these tokens
# into the next clause; used by _negation_affects_polarity to stop the scan.
_CLAUSE_BREAKERS = {"but", "however", "although", "though", "yet", "and", "or", "so", "because"}


def _negation_affects_polarity(low):
    """Return True when a negator sits within the same clause as a single-word
    polarity term (in _PRAISE or _COMPLAINT). Detects exactly where the
    deterministic lexicon is unreliable -- a negator adjacent to a polarity word
    within a clause -- so the item is deferred to the LLM rather than assigned
    the wrong band. Split on sentence/clause punctuation first so a negator in
    one sentence cannot reach a polarity word in the next (e.g. "I can't
    complain. Great product" -- the period breaks the clause before "great")."""
    for segment in re.split(r"[.,;:!?]", low):
        toks = _TOKEN.findall(segment)
        for i, tok in enumerate(toks):
            if tok in _NEGATORS:
                for w in toks[i + 1:i + 4]:
                    if w in _CLAUSE_BREAKERS:
                        break
                    if w in _PRAISE or w in _COMPLAINT:
                        return True
    return False


def _deterministic_polarity(text):
    """Return 'pain' | 'praise' | 'ambiguous' for a single item, from language
    only. A clear single-polarity item is banded; anything mixed, negated into
    ambiguity, or lexicon-silent falls to 'ambiguous' for the LLM."""
    low = str(text or "").lower()   # coerce: non-string sneaking in is harmless
    tokens = _TOKEN.findall(low)
    set_tokens = set(tokens)

    complaint = len(_COMPLAINT & set_tokens)
    praise = len(_PRAISE & set_tokens)
    complaint += sum(1 for p in _COMPLAINT_PHRASES if p in low)
    praise += sum(1 for p in _PRAISE_PHRASES if p in low)

    # Negation: when a negator sits next to a single-word polarity term within
    # the same clause, deterministic assignment is unsafe -- defer to the LLM
    # (return "ambiguous") rather than flipping the polarity. Multi-word phrases
    # in _COMPLAINT_PHRASES / _PRAISE_PHRASES are already negation-resolved and
    # commit their band even in the presence of negation.
    if _negation_affects_polarity(low):
        return "ambiguous"

    if complaint > 0 and praise == 0:
        return "pain"
    if praise > 0 and complaint == 0:
        return "praise"
    # both>0 (mixed) or both==0 (lexicon-silent) -> let the LLM decide.
    return "ambiguous"


def _band_text(rec):
    """The text banding judges -- posts already carry 'title. body' from the
    adapter, so the record's 'text' is authoritative."""
    t = rec.get("text")
    return t if isinstance(t, str) else ""   # guard: non-string text (e.g. int) -> ""


# ---------------------------------------------------------------------------
# LLM adjudication (a local claude CLI call, run without MCP servers loaded to
# prevent browser server startup -- see report.py::_run_claude for the full rationale)
# ---------------------------------------------------------------------------

_ADJ_MODEL = "claude-haiku-4-5-20251001"   # cheap: per-item labels, not synthesis
_BATCH = 40                                 # items per LLM call (default)
# Adaptive degradation guard. A healthy haiku batch on this trivial 3-way task
# returns an explicit label for ~every item. If a batch comes back with explicit
# labels for materially FEWER items than were sent, the model likely degraded on
# too-wide a prompt (misnumbered, dropped lines) -- and the missing items would
# silently default to neutral, quietly shrinking the corpus. So we re-run that
# batch split in half, down to _MIN_BATCH, giving each item more attention. This
# keeps the cheap wide batch for the common case and only pays for smaller calls
# on the rare bad batch. A TOTAL failure (empty output = CLI missing/errored) is
# NOT a degradation -- splitting can't fix a down CLI -- so it short-circuits to
# all-neutral without a retry storm.
_MIN_MATCH_RATE = 0.8   # retry a batch if < this fraction got an explicit label
_MIN_BATCH = 8          # do not split below this; accept whatever came back

_LABEL_PROMPT = (
    "You are labeling Reddit posts/comments about a product or service "
    "for a customer-pain analysis. For EACH numbered item, output its "
    "number and exactly one label:\n"
    "  PAIN    = expresses a complaint, problem, frustration, or negative "
    "experience with the product/service\n"
    "  PRAISE  = expresses satisfaction, a positive experience, or a "
    "recommendation\n"
    "  NEUTRAL = neither -- a question, a topic discussion, a joke, news, "
    "or anything not clearly a complaint or praise\n\n"
    "Be strict: if it is not clearly PAIN or PRAISE, it is NEUTRAL. Do "
    "not force items into PAIN or PRAISE.\n\n"
    "Output one line per item as `N: LABEL`, nothing else.\n\n"
    "Items:\n{items}\n"
)


def _adjudicate(items_text, model=None, batch=None, min_match=None, min_batch=None):
    """Label each ambiguous item PAIN / PRAISE / NEUTRAL via a cheap claude-CLI
    subagent, in batches of `batch` (default _BATCH). Each batch self-heals via
    _label_chunk's adaptive split-retry. Returns a list of labels aligned to
    items_text. Degrades to 'neutral' on any failure -- never manufactures pain.
    """
    model = model or _ADJ_MODEL
    batch = _BATCH if batch is None else max(1, int(batch))
    min_match = _MIN_MATCH_RATE if min_match is None else float(min_match)
    min_batch = _MIN_BATCH if min_batch is None else max(1, int(min_batch))

    labels = ["neutral"] * len(items_text)
    if not items_text:
        return labels

    for start in range(0, len(items_text), batch):
        chunk = items_text[start:start + batch]
        chunk_labels = _label_chunk(chunk, model, min_match, min_batch)
        for j, lab in enumerate(chunk_labels):
            labels[start + j] = lab
    return labels


def _label_chunk(texts, model, min_match, min_batch):
    """Label one chunk, with the adaptive degradation guard. Sends the chunk; if
    the LLM returned explicit labels for < min_match of the items AND the chunk is
    still bigger than min_batch AND the output was non-empty (a partial, not a
    total CLI failure), split in half and retry each half. Otherwise accept what
    came back (unmatched items -> neutral)."""
    n = len(texts)
    if n == 0:
        return []
    numbered = "\n".join(f"{i + 1}. {t.strip()[:500]}" for i, t in enumerate(texts))
    out = _run_claude_labels(_LABEL_PROMPT.format(items=numbered), model)
    parsed, matched = _parse_labels_counted(out, n)

    # Total failure (empty output): splitting won't help a down CLI -- accept the
    # all-neutral result rather than fan out into many identical failing calls.
    if not (out or "").strip():
        return parsed
    # Healthy enough, or too small to be worth splitting: accept.
    if n <= min_batch or matched >= min_match * n:
        return parsed
    # Partial degradation on a wide batch: split and retry smaller.
    print(f"[banding] batch of {n} got explicit labels for only {matched}/{n} "
          f"({matched / n:.0%} < {min_match:.0%}) -- splitting and retrying smaller")
    mid = n // 2
    return (_label_chunk(texts[:mid], model, min_match, min_batch)
            + _label_chunk(texts[mid:], model, min_match, min_batch))


def _run_claude_labels(prompt, model):
    """Run the claude CLI without MCP servers loaded (via --strict-mcp-config), for a
    faster start with no side effects (no browser servers boot).
    Returns stdout text, or "" on any failure. No timeout (non-deterministic step)."""
    cmd = [
        "claude", "--print", "--output-format", "text",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "",
        "--model", model,
    ]
    try:
        result = subprocess.run(cmd, input=prompt, capture_output=True, text=True)
    except FileNotFoundError:
        print("[banding] `claude` CLI not found -- ambiguous items default to neutral.")
        return ""
    except Exception as exc:  # noqa: BLE001
        print(f"[banding] adjudication error: {exc} -- ambiguous default to neutral.")
        return ""
    if result.returncode != 0:
        print(f"[banding] adjudication non-zero exit ({result.returncode}) -- "
              "ambiguous default to neutral.")
        return ""
    return result.stdout or ""


_LABEL_LINE = re.compile(r"^\s*(\d+)\s*[:.\)]\s*(PAIN|PRAISE|NEUTRAL)\b", re.IGNORECASE)
_LABEL_MAP = {"PAIN": "pain", "PRAISE": "praise", "NEUTRAL": "neutral"}


def _parse_labels_counted(text, n):
    """Parse `N: LABEL` lines into (labels, n_matched). labels is length n with
    missing/garbled items defaulted to 'neutral'; n_matched is how many DISTINCT
    item indices actually received an explicit label from the output -- the signal
    the degradation guard keys on. Duplicate lines for the same index count once.

    Misnumbering guard: line-numbers must be strictly increasing in the
    order they appear. Legitimate omissions keep the sequence increasing (e.g.
    1,3,4); a permutation (1,3,2) or a repeat within distinct indices is not. A
    non-monotonic response means the LLM's numbering is untrustworthy: return
    all-neutral with matched=0 so the degradation guard in _label_chunk splits and
    retries smaller. A single-item chunk is trivially monotonic and always accepted.
    Duplicate lines for the SAME index (model hedging) do not trigger this check --
    only the first occurrence of each index is tracked in the sequence."""
    labels = ["neutral"] * n
    matched = set()
    parsed_order = []   # line-numbers in appearance order, first occurrence per index
    for line in (text or "").splitlines():
        m = _LABEL_LINE.match(line)
        if not m:
            continue
        num = int(m.group(1))
        idx = num - 1
        if 0 <= idx < n:
            labels[idx] = _LABEL_MAP[m.group(2).upper()]
            if idx not in matched:
                parsed_order.append(num)  # track first occurrence only
            matched.add(idx)
    if len(parsed_order) > 1:
        if any(parsed_order[i] >= parsed_order[i + 1] for i in range(len(parsed_order) - 1)):
            return ["neutral"] * n, 0
    return labels, len(matched)


def _parse_labels(text, n):
    """Parse `N: LABEL` lines into a list of n labels. Missing/garbled lines
    default to 'neutral' (conservative)."""
    return _parse_labels_counted(text, n)[0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def band_records(records, cfg=None, adjudicate=True):
    """Band a no-star corpus. Returns (main, neutral):

      main    -- records that are pain or praise, each with a synthetic `stars`
                 (LOW_STARS / HIGH_STARS), `inferred_band` True, and
                 `band_method` in {"lexicon","llm"}. These feed the unchanged
                 extractor.
      neutral -- records that are neither, each with `band` = "neutral" and NO
                 stars. Set aside for the caller to store and optionally mine for
                 general topics/questions. Never enter the pain/trust analysis.

    Deterministic-first: confident single-polarity items are banded in code; the
    ambiguous remainder is LLM-adjudicated in one or more batches. `adjudicate`
    False skips the LLM (all ambiguous -> neutral) -- used by deterministic tests.
    """
    cfg = cfg or {}
    low_stars = int(cfg.get("band_low_stars", LOW_STARS))
    high_stars = int(cfg.get("band_high_stars", HIGH_STARS))

    confident = []          # (record, "pain"/"praise")
    ambiguous = []          # records needing the LLM
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pol = _deterministic_polarity(_band_text(rec))
        if pol == "ambiguous":
            ambiguous.append(rec)
        else:
            confident.append((rec, pol))

    # LLM pass on the ambiguous middle. Batch size and the degradation guard's
    # thresholds are config-overridable (defaults: 40 / 0.8 / 8).
    if ambiguous and adjudicate:
        labels = _adjudicate(
            [_band_text(r) for r in ambiguous],
            batch=cfg.get("band_batch_size"),
            min_match=cfg.get("band_min_match_rate"),
            min_batch=cfg.get("band_min_batch"),
        )
    else:
        labels = ["neutral"] * len(ambiguous)

    main, neutral = [], []

    def _emit_main(rec, pol, method):
        out = dict(rec)
        out["stars"] = low_stars if pol == "pain" else high_stars
        out["inferred_band"] = True
        out["band"] = "pain" if pol == "pain" else "praise"
        out["band_method"] = method
        main.append(out)

    def _emit_neutral(rec, method):
        out = dict(rec)
        out.pop("stars", None)          # ensure no stray star sneaks in
        out["inferred_band"] = True
        out["band"] = "neutral"
        out["band_method"] = method
        neutral.append(out)

    for rec, pol in confident:
        _emit_main(rec, pol, "lexicon")
    for rec, lab in zip(ambiguous, labels):
        if lab == "pain":
            _emit_main(rec, "pain", "llm")
        elif lab == "praise":
            _emit_main(rec, "praise", "llm")
        else:
            _emit_neutral(rec, "llm")

    return main, neutral


# ===========================================================================
# Inline verification tests:  python3 banding.py
# ===========================================================================
if __name__ == "__main__":
    PASS = FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")
            FAIL += 1

    # ---- Part A: deterministic polarity (no LLM) --------------------------
    print("=== TEST A: deterministic polarity ===")
    check("A1 clear complaint -> pain",
          _deterministic_polarity("The app crashes constantly and it's useless"), "pain")
    check("A2 clear praise -> praise",
          _deterministic_polarity("I love this, works perfectly, highly recommend"), "praise")
    check("A3 phrase complaint -> pain",
          _deterministic_polarity("Spotify appears to be down, not working on my phone"), "pain")
    check("A4 negated praise -> ambiguous (deferred, not flipped to pain fix)",
          _deterministic_polarity("honestly not great, isn't the best"), "ambiguous")
    check("A5 mixed -> ambiguous",
          _deterministic_polarity("great sound but it crashes all the time"), "ambiguous")
    check("A6 lexicon-silent question -> ambiguous",
          _deterministic_polarity("Is there a way to report a song within the app?"), "ambiguous")
    # A subtle double-negative ("no complaints") is deliberately NOT force-banded
    # by the conservative lexicon -- it defers to the LLM as ambiguous rather than
    # risk a wrong confident call. The only hard guarantee: it is not mislabeled pain.
    check("A7 double-negative defers to LLM (not pain)",
          _deterministic_polarity("no complaints here, it just works"), "ambiguous")
    check("A8 empty -> ambiguous",
          _deterministic_polarity(""), "ambiguous")

    # ---- Part A2: band_records structure (adjudicate OFF -> ambiguous=neutral)
    print("\n=== TEST A2: band_records shape (no LLM) ===")
    recs = [
        {"text": "the app crashes and is useless", "score": 5, "source": "reddit"},
        {"text": "love this, works perfectly", "score": 9, "source": "reddit"},
        {"text": "Is Spotify down right now?", "score": 1977, "source": "reddit"},  # ambiguous->neutral
    ]
    main, neutral = band_records(recs, {}, adjudicate=False)
    check("A2a one pain + one praise in main", len(main), 2)
    check("A2b one neutral set aside", len(neutral), 1)
    pain = [m for m in main if m["band"] == "pain"][0]
    praise = [m for m in main if m["band"] == "praise"][0]
    check("A2c pain gets LOW_STARS", pain["stars"], LOW_STARS)
    check("A2d praise gets HIGH_STARS", praise["stars"], HIGH_STARS)
    check("A2e pain method lexicon", pain["band_method"], "lexicon")
    check("A2f neutral has no stars", "stars" in neutral[0], False)
    check("A2g neutral flagged inferred_band", neutral[0]["inferred_band"], True)
    check("A2h score carried through untouched", pain["score"], 5)
    check("A2i source preserved", pain["source"], "reddit")

    # ---- Part A3: config override of synthetic star values ----------------
    print("\n=== TEST A3: configurable band star values ===")
    m2, _ = band_records([{"text": "terrible, broken, refund"}],
                         {"band_low_stars": 1}, adjudicate=False)
    check("A3a custom low star honored", m2[0]["stars"], 1)

    # ---- Part A4: label parser -------------------------------------------
    print("\n=== TEST A4: _parse_labels ===")
    check("A4a parses mixed lines",
          _parse_labels("1: PAIN\n2: NEUTRAL\n3: PRAISE", 3), ["pain", "neutral", "praise"])
    check("A4b missing line -> neutral default",
          _parse_labels("1: PAIN\n3: PRAISE", 3), ["pain", "neutral", "praise"])
    check("A4c garbled -> all neutral",
          _parse_labels("i cannot help with that", 2), ["neutral", "neutral"])

    # ---- Part A5: _parse_labels_counted reports the explicit-match count ---
    print("\n=== TEST A5: _parse_labels_counted (match count) ===")
    check("A5a all matched -> count == n",
          _parse_labels_counted("1: PAIN\n2: NEUTRAL\n3: PRAISE", 3), (["pain", "neutral", "praise"], 3))
    check("A5b one missing -> count 2, gap defaulted",
          _parse_labels_counted("1: PAIN\n3: PRAISE", 3), (["pain", "neutral", "praise"], 2))
    check("A5c garbled -> count 0",
          _parse_labels_counted("nope", 3)[1], 0)
    check("A5d duplicate index counts once (last wins)",
          _parse_labels_counted("1: PAIN\n1: PRAISE", 1), (["praise"], 1))

    # ---- Part A6: adaptive degradation guard (deterministic, stubbed CLI) --
    # The guard splits a wide batch that came back under-labeled, and recovers
    # full labeling; a healthy batch is not split; a total CLI failure does NOT
    # fan out into a retry storm. Stub CLI counts calls and simulates each case.
    print("\n=== TEST A6: adaptive degradation guard ===")
    _saved_run = _run_claude_labels
    try:
        def _count_items(prompt):
            return len(re.findall(r"(?m)^(\d+)\. ", prompt))

        # -- A6a healthy: full labels, one call, no split --
        calls_h = []
        def _stub_healthy(prompt, model):
            k = _count_items(prompt); calls_h.append(k)
            return "\n".join(f"{i}: PRAISE" for i in range(1, k + 1))
        globals()["_run_claude_labels"] = _stub_healthy
        out_h = _adjudicate([f"item {i}" for i in range(40)], batch=40)
        check("A6a healthy: all explicitly labeled praise", all(l == "praise" for l in out_h), True)
        check("A6a healthy: exactly ONE call (no split)", len(calls_h), 1)

        # -- A6b degraded wide batch recovers via split --
        # Big chunk (>8) returns only item 1; small chunk (<=8) returns full PAIN.
        # If the guard works, splitting down to <=8 relabels everything PAIN;
        # without it, only item 1 would be pain and the rest silent-neutral.
        calls_d = []
        def _stub_degraded(prompt, model):
            k = _count_items(prompt); calls_d.append(k)
            if k <= 8:
                return "\n".join(f"{i}: PAIN" for i in range(1, k + 1))
            return "1: PAIN"   # degraded: only one line for a wide batch
        globals()["_run_claude_labels"] = _stub_degraded
        out_d = _adjudicate([f"item {i}" for i in range(40)], batch=40)
        check("A6b degraded batch fully recovered (all pain, none silent-neutral)",
              all(l == "pain" for l in out_d), True)
        check("A6b did split (more than one call happened)", len(calls_d) > 1, True)
        check("A6b terminated at small chunks (a <=8 call occurred)",
              any(k <= 8 for k in calls_d), True)

        # -- A6c total CLI failure: empty output, all neutral, NO retry storm --
        calls_e = []
        def _stub_empty(prompt, model):
            calls_e.append(_count_items(prompt)); return ""
        globals()["_run_claude_labels"] = _stub_empty
        out_e = _adjudicate([f"item {i}" for i in range(40)], batch=40)
        check("A6c CLI failure -> all neutral", all(l == "neutral" for l in out_e), True)
        check("A6c CLI failure -> exactly ONE call (no fan-out storm)", len(calls_e), 1)

        # -- A6d config passthrough: cfg batch/thresholds reach _adjudicate --
        calls_c = []
        def _stub_count(prompt, model):
            calls_c.append(_count_items(prompt))
            k = _count_items(prompt)
            return "\n".join(f"{i}: NEUTRAL" for i in range(1, k + 1))
        globals()["_run_claude_labels"] = _stub_count
        band_records([{"text": f"discussion item {i}"} for i in range(10)],
                     {"band_batch_size": 5}, adjudicate=True)
        check("A6d cfg band_batch_size honored (10 items / batch 5 -> 2 calls)",
              len(calls_c), 2)
    finally:
        globals()["_run_claude_labels"] = _saved_run

    # ---- Part A7: non-string text coercion --------------------------
    print("\n=== TEST A7: non-string text coercion ===")
    check("A7a non-string int -> empty string at boundary",
          _band_text({"text": 42}), "")
    check("A7b non-string int -> ambiguous (no lexicon hit, no crash)",
          _deterministic_polarity(42), "ambiguous")
    check("A7c normal string text unchanged (no regression)",
          _band_text({"text": "real complaint text"}), "real complaint text")

    # ---- Part A8: misnumbered LLM labels ----------------------------
    print("\n=== TEST A8: misnumbered LLM labels ===")
    check("A8a well-formed monotonic -> correct labels, matched==3 (regression guard)",
          _parse_labels_counted("1. PAIN\n2. NEUTRAL\n3. PRAISE", 3),
          (["pain", "neutral", "praise"], 3))
    check("A8b permuted response -> all-neutral, matched==0 (rejected)",
          _parse_labels_counted("1. PAIN\n3. PRAISE\n2. NEUTRAL", 3),
          (["neutral", "neutral", "neutral"], 0))
    check("A8c monotonic-with-gap -> trusted (pain@0, neutral@1, praise@2, matched==2)",
          _parse_labels_counted("1. PAIN\n3. PRAISE", 3),
          (["pain", "neutral", "praise"], 2))

    # ---- Part A9: negation-fix regression suite --------------------
    print("\n=== TEST A9: negation-defer fix ===")
    # 1. Headline bug: "can't recommend it enough" must NO LONGER be "pain".
    check("A9a can't recommend it enough -> ambiguous (was pain, headline bug)",
          _deterministic_polarity("can't recommend it enough"), "ambiguous")
    # 2. Clean complaint unchanged.
    check("A9b the service is terrible -> pain (clean, no negation)",
          _deterministic_polarity("the service is terrible"), "pain")
    # 3. Clean praise unchanged.
    check("A9c absolutely love this app -> praise (clean, no negation)",
          _deterministic_polarity("absolutely love this app"), "praise")
    # 4. Curated complaint phrase still commits even though "doesn't" is a negator;
    #    the phrase fires on the substring and "work" alone is not in _COMPLAINT,
    #    so _negation_affects_polarity returns False.
    check("A9d doesn't work at all -> pain (phrase fires, no single-word polarity hit)",
          _deterministic_polarity("doesn't work at all"), "pain")
    # 5. Sentence-boundary case: "can't complain" is negator+_COMPLAINT in the
    #    same clause (before the period), so _negation_affects_polarity returns True
    #    and the whole item defers to "ambiguous". The period prevents "great" from
    #    being the reason, but "complain" is close enough on its own.
    check("A9e _negation_affects_polarity: can't complain. great product -> True "
          "(negator adjacent to 'complain' in same clause before period)",
          _negation_affects_polarity("i can't complain. great product"), True)
    check("A9f deterministic: i can't complain. great product -> ambiguous "
          "(deferred because negator+complaint-word in same clause)",
          _deterministic_polarity("i can't complain. great product"), "ambiguous")
    # 6. Clause-breaker stops the scan: "not great but fast" -- "not" adjacent
    #    to "great" (in _PRAISE) before reaching the "but" breaker -> True.
    check("A9g _negation_affects_polarity: not great but fast -> True "
          "(negator reaches 'great' before 'but' clause-breaker)",
          _negation_affects_polarity("not great but fast"), True)

    print(f"\n{'='*60}\nPART A: {PASS} passed, {FAIL} failed")

    # ---- Part B: live LLM adjudication on real ambiguous items ------------
    import shutil
    print(f"\n{'='*60}\n=== PART B: live LLM adjudication ===\n")
    if not shutil.which("claude"):
        print("[banding] PART B SKIPPED: claude CLI not found.")
        print(f"\nResults: {PASS} passed, {FAIL} failed  (Part B skipped)")
        raise SystemExit(1 if FAIL else 0)

    b_pass = b_fail = 0
    def bcheck(label, got, expected):
        global b_pass, b_fail
        if got == expected:
            print(f"  PASS  {label}"); b_pass += 1
        else:
            print(f"  FAIL  {label}\n        got: {got!r} expected: {expected!r}"); b_fail += 1

    # Items the deterministic pass leaves ambiguous but a human reads clearly.
    amb = [
        {"text": "I switched to Apple Music last month and honestly regret nothing, "
                 "Spotify kept logging me out"},                      # pain
        {"text": "been using it for years, the recommendations just get me every time"},  # praise
        {"text": "Does anyone know when the 2024 Wrapped comes out?"},                     # neutral
    ]
    main_b, neutral_b = band_records(amb, {}, adjudicate=True)
    bands = {m["text"][:20]: m["band"] for m in main_b}
    bands.update({n["text"][:20]: n["band"] for n in neutral_b})
    print("  live labels:", {k: v for k, v in bands.items()})
    bcheck("B1 all three adjudicated (nothing lost)", len(main_b) + len(neutral_b), 3)
    bcheck("B2 the question is neutral",
           [n for n in neutral_b if "Wrapped" in n["text"]][0]["band"], "neutral")
    bcheck("B3 llm-banded items carry band_method=llm",
           all(x["band_method"] == "llm" for x in main_b + neutral_b), True)

    print(f"\n{'='*60}\nResults: {PASS + b_pass} passed, {FAIL + b_fail} failed")
    raise SystemExit(1 if (FAIL or b_fail) else 0)
