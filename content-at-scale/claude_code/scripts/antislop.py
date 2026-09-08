#!/usr/bin/env python3
"""Deterministic anti-slop checker.

Given a piece of text, reports:
  - Lexical hits: Part 2 banned words present (whole-word, case-insensitive,
    entry counted once even when a slash-bundled entry bundles several forms).
  - Structural counts: 3.1 negation-correction antithesis; 3.4 neat parallel
    triplet.
  - Formatting counts: 4.1 em-dash and en-dash occurrences.
  - Verdict per rule: count, cap currently in force, whether over cap.

Caps are taken verbatim from ANTI_SLOP_RULESET.md. No threshold is invented
here. A later item measures false-negative rates on known-bad material and
false-positive rates on known-good material before these caps are adopted in
production.

CLI usage:
    python3 antislop.py <path-to-piece>

API usage:
    import antislop
    result = antislop.check(text)

Exit codes, and why they are not 0/1/2:

  0  the piece passed every check
  3  the gate FAILED — a banned word or structural cap was exceeded
  1  the call was wrong (file unreadable)
  2  argparse's own code for a malformed command line

Gate failure is 3 rather than 2 because argparse already owns 2. Without
this separation, an agent that misinvoked the script — a wrong path — would
read an invocation error as a gate failure and start rewriting content that
was never sloppy. The one exit-1 path is an unreadable file, an invocation
problem. Gate failure is a different outcome and must be unambiguously 3.

The checker takes no voice profile: a secondary voice anchor never relaxes a
rule. It always runs bare.
"""

import json
import os
import re
import sys

GATE_FAILED = 3


# banned.json lives beside this script's parent directory under voice/.
# The path is resolved relative to this file so the plugin is
# self-contained and behaves identically on any machine — no reference to
# private paths, ~/.claude, or any development-only tree outside the plugin.
_VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice")
_BANNED_JSON = os.path.join(_VOICE_DIR, "banned.json")


def _load_banned():
    with open(_BANNED_JSON, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 3.1 Negation-correction antithesis
#
# Four clear-cut forms:
#   "X is not Y. It is Z."
#   "It's not about X. It's about Y."
#   "You don't need X. You need Y."      (and the "do not" variant)
#   Comma form: "You're not X, you're Y."
#
# The full soft form ("You don't need the maths anymore. You need curiosity."
# — the see-saw structure even when softened) is NOT implemented. Detecting
# semantic contrast without an explicit negation marker requires natural
# language understanding beyond what regex can reliably provide.
#
# It was approximated once, by a pattern matching the same capitalised word opening
# two consecutive sentences. That approximation lives separately as rule 3.1p, a
# style constraint — see the block below the pattern list. It is deliberately not a
# member of this family, because this family is a calibrated slop detector and 3.1p
# is not calibrated against anything.
#
# So what these patterns detect is deliberately narrower: the MECHANICAL form, where
# the contrast is spelled out with an explicit negation. That is what separates the
# tiers.
# ---------------------------------------------------------------------------

_ANTITHESIS_PATTERNS = [
    # "X is not Y. It is Z."
    re.compile(
        r'\w[\w\s,\']*\bis\s+not\b[\w\s,\']*\.\s+(?:It|This|That|They)\s+is\b',
        re.IGNORECASE | re.DOTALL,
    ),
    # "It's not about X. It's about Y."
    re.compile(
        r"\bIt'?s\s+not\b[\w\s,\']*\.\s+It'?s\s+\w",
        re.IGNORECASE | re.DOTALL,
    ),
    # "You don't need X. You need Y." / "You do not X. You Y."
    re.compile(
        r"\bYou\s+do(?:n'?t|\s+not)\s+\w[\w\s,\']*\.\s+You\s+\w",
        re.IGNORECASE | re.DOTALL,
    ),
    # Comma form: "You're not X, you're Y."
    re.compile(
        r"\bYou'?re\s+not\s+[\w\s,\']+,\s+you'?re\s+\w",
        re.IGNORECASE,
    ),
    # "The work does not disappear. It transforms."
    # Third person with a lexical verb. Pattern 1 covers third person only with the
    # copula and pattern 3 covers lexical verbs only for "You", so this cell of the
    # person x verb-type grid was empty - an instance of it can slip through all five
    # of the other shipped patterns.
    # The subject is any noun phrase, matching pattern 1's shape rather than a fixed
    # determiner list. Measured both ways before choosing: a narrow version restricted to
    # (The|This|That|It|They|A|An) scored the same 33.3% sensitivity and 0.0% false
    # positives at document level but caught only 12 occurrences in known_bad against this
    # version's 20, with both at zero in known_good. Wider was free here; it was checked
    # rather than assumed, because "surely a bit broader is fine" is how the pattern this
    # replaces came to fire on every published article in the corpus.
    re.compile(
        r"\w[\w\s,']*\bdo(?:es)?\s+not\s+\w[\w\s,']*\.\s+(?:It|This|That|They)\s+\w",
        re.IGNORECASE | re.DOTALL,
    ),
    # "These are not suggestions. They are requirements."
    re.compile(
        r"\w[\w\s,']*\bare\s+not\b[\w\s,']*\.\s+(?:They|These|Those)\s+are\b",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# 3.1p Same-first-word parallelism — A STYLE CONSTRAINT, NOT A SLOP DETECTOR.
#
# This rule was once removed and then restored under a different classification.
# Both were correct, and the reason they can both be correct is the taxonomy this
# file was missing.
#
# WHY IT WAS REMOVED AS A DETECTOR. Controlled experiments retired it. At MATCHED
# DOCUMENT LENGTH it fires on 7 of 7 professionally published articles: a 100%
# false-positive rate against 88.9% sensitivity. Measuring the intended move directly
# showed the signal runs the other way — genuine tight parallelism is MORE frequent in
# good longform than in this pipeline's output. As a slop DETECTOR it was worthless,
# and no threshold could have saved it. That finding stands. Nothing below contradicts it.
#
# WHY IT IS BACK. Every one of those measurements asks the same question: "does
# third-party published prose contain this?" That is the wrong question. The
# construction is genuine craft, and the rule is that the pipeline must not attempt
# it. Under that reading a 100% hit rate on human writers is not a false-positive rate
# at all — it is a measurement of humans, and we never judge humans with this file. On
# GENERATED output every hit is a hit on a machine attempting a move it cannot land.
#
# This is exactly the em-dash rule's status (see 4.1): a style constraint enforced by
# fiat on generated output, where it works perfectly, and meaningless when pointed at
# third-party prose.
#
# WHY IT IS NOT BACK INSIDE _ANTITHESIS_PATTERNS. That family is calibrated: it owes a
# sensitivity and a false-positive rate, and _CAP_31 = 1 was derived on length-matched
# data. Folding an uncalibrated fiat rule into a calibrated family produces a
# misleading whole-ruleset figure, because it makes one pass/fail verdict out of two
# incomparable things. 3.1p is counted separately and reported separately so the split
# is visible in the output.
#
# WHY THE CAP IS 0 AND NOT A MEASURED NUMBER. "Do not let it try" is a zero. There is no
# corpus that could move this number, because no corpus of human writing is evidence
# about what the pipeline should be permitted to emit. Recorded explicitly so nobody
# later reads an underived threshold as an unchecked one — this one is underived on
# purpose, which is a different thing. (FROZEN.)
#
# NOT YIELDABLE TO A VOICE ANCHOR. NO rule yields to any anchor — a secondary voice
# anchor is a writer-side sample and never relaxes the checker. 3.1p was the strictest
# case even under the old yield mechanism (a profile claiming it would have been
# claiming a licence to attempt exactly the move this rule forbids); it is now simply
# one of the rules that always enforce. Do not reintroduce a per-rule yield.
#
# Two further negation-correction cells were tested and deliberately NOT added to the
# family above, because symmetry is not evidence: first person + copula ("We are not X.
# We are Y.") fires on neither tier, so it is untested rather than calibrated; first
# person + lexical verb scores 0% sensitivity against 14.3% false positives.
# ---------------------------------------------------------------------------

# The opening word must be a CONTENT word. This restriction was added when the pattern
# was restored, because restoring the pattern verbatim proved immediately unshippable,
# and the measurement is worth keeping:
#
#   Restored verbatim, cap 0, measured over a set of finished pipeline pieces: 38 hits,
#   11 of 12 pieces failing. Of the 38, the opening word was "The" 18 times, "You" 6,
#   "It" 5, "We" 2 — 31 of 38 on four function words. It also failed the suite's own
#   clean-prose fixture, "The project is proceeding well. The team is aligned on the
#   goal.", which is not parallelism at all; it is two sentences that both begin with
#   "The".
#
#   A gate that fires on 11 of 12 documents carries no information. That is the
#   grain of truth in the 100%-false-positive result that retired the pattern: most
#   of what it was detecting was the definite article.
#
#   With the stopword exclusion below, same data: 4 hits across 12 pieces, 8 pieces
#   clean, and the clean-prose fixture passes. It still fires on the canonical craft
#   instance the original source comment cited — "Compete externally and you compare.
#   Compete internally and you improve." — which is the case the rule exists for.
#
# The cap of 0 is FROZEN; the stopword list is MOVABLE. Cap 0 is a fiat ("do not let
# it try"), not a measured number; the word list is a tunable part that can be revised
# against data.
#
# What CANNOT be reported: the narrow version's rate on published human longform. The
# length-matched articles that produced the 7-of-7 figure are not retained. Under the
# style-constraint classification that measurement would not govern anything anyway,
# but the gap is stated rather than papered over.
_PARALLELISM_STOPWORDS = (
    "The|This|That|It|They|A|An|We|You|I|He|She|There|These|Those|"
    "And|But|Or|So|If|When|Then|Now|In|On|At|For|As|Of|To|"
    "What|Why|How|One|Some|Most|Every|All|No|Not|"
    "Its|Their|Our|My|His|Her|Your"
)

_PARALLELISM_PATTERN = re.compile(
    r'\b(?!(?:%s)\b)([A-Z][a-z]+)\s+[\w\s,\']+[.!?]\s+\1\s+[\w\s,\']+[.!?]'
    % _PARALLELISM_STOPWORDS
)

_CAP_31P = 0


def _count_parallelism(text):
    """Return the number of same-first-word sentence pairs (rule 3.1p).

    Overlapping spans are merged for the same reason _count_antitheses merges
    them: three consecutive sentences opening on the same word are one stylistic
    event, not two.
    """
    spans = [(m.start(), m.end()) for m in _PARALLELISM_PATTERN.finditer(text)]
    return _merge_span_count(spans)

# Cap: a single antithesis per piece is acceptable when it marks an actual
# turning point in the argument.
#
# Measured against 18 known_bad and the 7 known_good documents of comparable
# length (the short-post tier cannot inform a cap on longform):
#
#     cap   sensitivity   false positives
#      0       66.7%          28.6%
#      1       50.0%           0.0%      <- shipped
#      2       50.0%           0.0%
#      3       16.7%           0.0%
#
# The number did not need to change. With the removed pattern still in the set this same
# cap of 1 failed 7 of 7 published articles, because that corpus averages four hits per
# article on a pattern that fires on ordinary prose. The cap was never the defect; what it
# was counting was. Recorded because a threshold that survives calibration unchanged looks
# indistinguishable from one nobody ever checked.
_CAP_31 = 1

# Cap: rule 3.4 carries no exception clause, so this was set to 0 - a threshold derived
# from the rule's silence rather than from any measurement.
#
# Measured against 18 known_bad and the 7 known_good documents of comparable
# length. The triplet is a normal device in published longform:
#
#     known_bad  counts: 1 x13, 2 x5      median 1
#     known_good counts: 0,0,0,1,1,1,1    median 1
#
#     cap 0:  sensitivity 100.0%   false positives 57.1%   <- was shipped
#     cap 1:  sensitivity  27.8%   false positives  0.0%   <- now
#
# Raised to 1 by the owner's ruling. Taken to the owner rather than changed on the
# numbers alone, because unlike the removed antithesis pattern - a proxy invented in
# code - this cap is a house constraint and could legitimately be stricter than
# published practice. A calibration can show a threshold is costly. It cannot decide
# whether the owner wants to pay.
_CAP_34 = 1


def _merge_span_count(spans):
    """Count match spans after merging overlaps.

    Extracted so rule 3.1p can reuse it. The behaviour is unchanged
    from the inline version that lived in _count_antitheses.
    """
    if not spans:
        return 0

    spans = sorted(spans)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return len(merged)


def _count_antitheses(text):
    """Return the number of negation-correction antitheses detected.

    Merges overlapping match spans across all patterns so the same sentence
    pair is not counted twice when it matches more than one pattern.
    """
    spans = []
    for pattern in _ANTITHESIS_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    return _merge_span_count(spans)


def _count_triplets(text):
    """Return the number of neat parallel triplets detected (rule 3.4).

    Detects three consecutive sentences that all open with the same word,
    matching the ruleset's example: "You optimise... You renew... You
    capture..."

    Triplets are counted non-overlapping: after a triplet is found at
    positions i, i+1, i+2, the next candidate starts at i+3.
    """
    units = re.split(r'[.!?]\s+', text)
    first_words = []
    for unit in units:
        stripped = unit.strip()
        if not stripped:
            continue
        m = re.match(r'\b(\w+)', stripped)
        if m:
            first_words.append(m.group(1).lower())

    count = 0
    i = 0
    while i <= len(first_words) - 3:
        if first_words[i] == first_words[i + 1] == first_words[i + 2]:
            count += 1
            i += 3
        else:
            i += 1
    return count
def _find_lexical_hits(text, banned_data):
    """Find banned-word entries present in text.

    Matching is whole-word and case-insensitive. An entry is counted ONCE
    even if multiple forms of a slash-bundled entry appear in the text.

    There is NO profile-based excuse. A secondary voice anchor never relaxes a
    lexical ban: a banned word is banned regardless of any anchor's published prose.
    The former per-form yield against a profile's verified_quoted_text is gone —
    it was what let a banned word through whenever some cited author had once
    published it.

    Returns a list of dicts: {entry, forms_found, count}.
    """
    hits = []
    for entry in banned_data["banned_words"]:
        entry_count = 0
        forms_found = []
        for form in entry["forms"]:
            pattern = re.compile(r'\b' + re.escape(form) + r'\b', re.IGNORECASE)
            matches = pattern.findall(text)
            if matches:
                entry_count += len(matches)
                forms_found.append(form)
        if forms_found:
            hits.append({
                "entry": entry["original"],
                "forms_found": forms_found,
                "count": entry_count,
            })
    return hits


def _make_structural_entry(rule_id, count, cap, label):
    """Build the structural result dict for one rule.

    A secondary voice anchor never yields a structural cap, so there is no
    profile-yield state and no deferred "pending calibration" verdict: the rule is simply
    enforced. over_cap is count > cap.
    """
    return {
        "count": count,
        "cap": cap,
        "over_cap": count > cap,
        "label": label,
    }


def check(text, banned_data=None):
    """Check text against the anti-slop ruleset.

    Args:
        text: The piece text as a string.
        banned_data: Pre-loaded dict from banned.json, or None to load from
            disk. Pass a pre-loaded dict in tests to avoid repeated I/O.

    A voice profile is NEVER consulted. A secondary anchor is a writer-side
    voice sample only and never relaxes the checker. The checker always runs bare.

    Returns a dict:
        {
          "lexical_hits": [...],          # Part 2 violations
          "structural": {
            "3.1":  {"count": N, "cap": 1, "over_cap": bool, "label": str},
            "3.1p": {"count": N, "cap": 0, "over_cap": bool, "label": str},
            "3.4":  {"count": N, "cap": 1, "over_cap": bool, "label": str},
          },
          "formatting": {
            "4.1": {"em_dash_count": N, "en_dash_count": N, "total": N,
                    "label": str},
          },
          "ok": bool,   # True only when no lexical hits and no cap exceeded
        }
    """
    if banned_data is None:
        banned_data = _load_banned()

    lexical_hits = _find_lexical_hits(text, banned_data)
    antithesis_count = _count_antitheses(text)
    parallelism_count = _count_parallelism(text)
    triplet_count = _count_triplets(text)

    dash_chars = banned_data["dash_chars"]
    em_dash_count = text.count(dash_chars[0])   # em-dash U+2014
    en_dash_count = text.count(dash_chars[1])   # en-dash U+2013

    result = {
        "lexical_hits": lexical_hits,
        "structural": {
            "3.1": _make_structural_entry(
                "3.1", antithesis_count, _CAP_31,
                "negation-correction antithesis",
            ),
            "3.1p": _make_structural_entry(
                "3.1p", parallelism_count, _CAP_31P,
                "same-first-word parallelism [style constraint]",
            ),
            "3.4": _make_structural_entry(
                "3.4", triplet_count, _CAP_34,
                "neat parallel triplet",
            ),
        },
        "formatting": {
            "4.1": {
                "em_dash_count": em_dash_count,
                "en_dash_count": en_dash_count,
                "total": em_dash_count + en_dash_count,
                "label": "em-dash and en-dash",
            },
        },
    }
    result["ok"] = (
        len(lexical_hits) == 0
        and not any(r["over_cap"] for r in result["structural"].values())
    )
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Anti-slop checker")
    parser.add_argument("file", help="Path to the piece file to check")
    args = parser.parse_args()

    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    result = check(text)

    print("=== Anti-slop report ===\n")

    if result["lexical_hits"]:
        print(f"Lexical hits ({len(result['lexical_hits'])} banned entries):")
        for h in result["lexical_hits"]:
            print(f"  [{h['entry']}]  forms: {h['forms_found']}  "
                  f"({h['count']} occurrence(s))")
    else:
        print("Lexical hits: none")

    print()
    for rule_id, r in result["structural"].items():
        verdict = "OVER CAP" if r["over_cap"] else "ok"
        print(f"  {rule_id} {r['label']}: {r['count']} "
              f"(cap {r['cap']}) - {verdict}")

    print()
    d = result["formatting"]["4.1"]
    print(f"  4.1 em-dash: {d['em_dash_count']}, "
          f"en-dash: {d['en_dash_count']}, total: {d['total']}")

    print()
    print(f"Overall: {'PASS' if result['ok'] else 'FAIL'}")
    sys.exit(0 if result["ok"] else GATE_FAILED)
