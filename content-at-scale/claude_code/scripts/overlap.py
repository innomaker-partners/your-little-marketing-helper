#!/usr/bin/env python3
"""Overlap gate: how much of a piece is lifted from its source.

Two numbers, because they answer different questions and one without the
other misleads (both words AND phrases matter):

  WORD overlap - the share of the piece's word tokens that also occur in the
  source, counted as a multiset so repetition is not double-credited. This
  runs high by nature: any two texts in the same language share "the", "and",
  "we". A word overlap of 60% is unremarkable. What it catches is vocabulary
  that tracks the source unusually closely.

  PHRASE overlap - the share of the piece's words that sit inside a run of
  N or more consecutive words appearing verbatim in the source. This is the
  number that detects lifting. Natural prose about the same subject shares
  vocabulary constantly and long verbatim runs almost never.

NO DEFAULT THRESHOLD, for either. The caller supplies both, and
intake must explain what the numbers MEAN rather than just
prompting for them - a threshold chosen without that explanation is a
threshold chosen at random, which makes the gate decorative.

The phrase length N defaults to 4, and the threshold applies there.
Override it with --phrase-length if a run proves otherwise, and say so.

That default was 5 until it was measured. The argument for 5 was that
four-word runs collide by chance in ordinary prose ("at the end of") while
runs of five rarely do - reasoning that sounded right and was never checked.
Against a 30,318-token source, independently written prose scored 4.4% at
n=3 and 0.0% at n=4, n=5 and n=6, while the evasion below scored 38.6% at
n=4 and 17.5% at n=5. There was no false-positive cost to pay at n=4; the
evasion was invisible at n=5 specifically, which was the number the reasoning
had picked. Recorded here because the stale docstring outlived the fix once
already, and the lesson is the number was defended with an argument instead
of data.

EVASION, and why the profile exists. Inserting one non-source word after
every N-1 borrowed ones defeats any single fixed N: the runs never reach
length N and phrase overlap falls to zero while the piece stays borrowed.
Measured against a small source, a naturalistic version of that trick scored
71.9% word overlap and 17.5% at n=5 - clearing a 25% threshold comfortably -
while scoring 43.9% at n=3 and 38.6% at n=4.

That spread IS the signature. Honest prose about the same subject scores 0.0%
at every n from 4 up, so a piece that is high at n=3 and near zero at n=5 is
not a piece that merely shares a topic. Reporting one number hides this;
reporting the profile shows it.

A determined writer who knows the whole profile can still evade, by breaking
every second word. At that point the prose is destroyed and the trick costs
more than writing the thing. That is the honest limit of this gate: it makes
evasion expensive, not impossible.

Run by the PRODUCING agent, recomputed by the orchestrator on
the final artifact.

SOURCE POLARITY (FROZEN): this gate must distinguish the user's own
material from third-party material, because the correct direction is opposite
for each:

  OWNED SOURCES      -> FLOOR   : the piece should sound like the user; high
                                   overlap is the goal. Fail when overlap is
                                   BELOW the threshold.

  THIRD-PARTY SOURCES -> CEILING : lifted prose is the problem; high overlap
                                   means copying. Fail when overlap is ABOVE
                                   the threshold.

The pools must NEVER be merged. Averaging an owned pool with a third-party
pool produces a number that is meaningless in both directions.

--source IS REFUSED (exit 1). Callers must declare polarity with
--owned-source or --third-party-source. Defaulting silently would change what
every existing call means (if defaulting to owned) or hand the harmful ceiling
to the primary use case (if defaulting to third-party). Refusing makes the
caller declare. This follows the same convention: neither `length`
nor `overlap` has a default threshold; both refuse to run without one.

A voice anchor (any file inside this plugin's voice/ directory, or any file
named voice_ref.md) is NEVER a source. Emulating it is the intent; measuring
overlap against it would fail the piece for succeeding at its goal.

Exit codes, and why they are not 0/1/2:

  0  the gate passed
  3  the gate FAILED - the piece is outside its bounds
  1  the call was wrong (bad path, contradictory arguments)
  2  argparse's own code for a malformed command line

Gate failure is 3 rather than 2 because argparse already owns 2 and exits
with it before this file runs. If gate-failure shared that code, an agent
that misinvoked the script would read "you typed it wrong" as "the piece is
the wrong length" and start rewriting perfectly good content to fix a
command-line error. Found by a test that failed for the wrong reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

import textutil as T

# The working-file status banner stamped at the top of source documents:
#   > **WORKING FILE. NOT FINISHED, NOT APPROVED, NOT FOR PUBLICATION.**
#
# This is administrative boilerplate, not the writer's prose. Including it in
# the source token pool means a produced piece can be penalised for echoing
# words like "not", "approved", or "publication" that appeared in a status
# line, not in any session content the writer produced.
#
# Guard: the pattern requires (a) a blockquote marker at the start of the
# line, (b) the specific phrase "WORKING FILE" followed immediately by
# "NOT FINISHED". That combination is implausible in real marketing prose and
# cannot appear in a mid-sentence fragment. Text that merely contains "not
# finished" or "not approved" in a sentence body is never a blockquote-prefixed
# line beginning with "WORKING FILE" and will never be touched.
_BANNER_RE = re.compile(
    r"^[ \t]*>[ \t]*\**WORKING\s+FILE[.,]?\s+NOT\s+FINISHED[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_banner(text: str) -> str:
    """Remove the working-file status banner from source text.

    Conditional: only fires when the specific banner pattern is present.
    Any source document that does not carry the banner passes through unchanged.
    """
    return _BANNER_RE.sub("", text)

# The threshold applies at n=4. Measured, not reasoned: against a
# 30,318-token pooled source corpus (standing in for a run's research plus
# transcripts), independently written prose scored
# 0.0% at n=4 and 0.0% at n=5, while a naturalistic evasion scored 38.6% at
# n=4 and 17.5% at n=5. n=5 was the original choice and it was wrong - it was
# picked by argument rather than measurement, and the argument ("four-word
# runs collide by chance in ordinary prose") does not survive the data.
DEFAULT_PHRASE_LENGTH = 4

# Reported alongside it. The profile is the point: a piece assembled by
# breaking up borrowed runs scores HIGH at n=3 and near zero at n=5, a shape
# no honestly written piece produces. One number cannot show that.
PROFILE_LENGTHS = (3, 4, 5)

GATE_FAILED = 3


def word_overlap(content_tokens: list, source_tokens: list) -> float:
    """Multiset intersection over the piece's own length.

    Multiset, not set: if the piece says "framework" eight times and the
    source says it once, seven of those are the piece's own repetition and
    should not count as borrowed.
    """
    if not content_tokens:
        return 0.0
    shared = Counter(content_tokens) & Counter(source_tokens)
    return sum(shared.values()) / len(content_tokens)


def phrase_overlap(content_tokens: list, source_tokens: list, n: int) -> tuple:
    """Fraction of the piece's words inside a verbatim run of >= n words.

    Returns (fraction, longest_runs) where longest_runs are the matched
    stretches themselves, so a failure report can show what was lifted rather
    than only asserting that something was.
    """
    if not content_tokens or n < 1:
        return 0.0, []
    source_ngrams = set(T.ngrams(source_tokens, n))
    covered = [False] * len(content_tokens)
    for i, gram in enumerate(T.ngrams(content_tokens, n)):
        if gram in source_ngrams:
            for j in range(i, i + n):
                covered[j] = True

    runs, start = [], None
    for i, c in enumerate(covered):
        if c and start is None:
            start = i
        elif not c and start is not None:
            runs.append(" ".join(content_tokens[start:i]))
            start = None
    if start is not None:
        runs.append(" ".join(content_tokens[start:]))

    runs.sort(key=lambda r: -len(r.split()))
    return sum(covered) / len(covered), runs


class EmptySource(ValueError):
    """Refusing to measure overlap against nothing."""


def check(content: str, sources: list, min_word: float, max_phrase: float,
          n: int = DEFAULT_PHRASE_LENGTH) -> dict:
    """Third-party pool evaluator.

    Word overlap is a FLOOR (the piece must be grounded in the source
    vocabulary); phrase overlap is a CEILING (no verbatim
    lifting). longest_lifted_runs stays — lifting is still the phrase concern.

    Fail when word overlap is BELOW min_word, or phrase overlap is ABOVE
    max_phrase.
    """
    c = T.tokenize(content)
    s = []
    for text in sources:
        s.extend(T.tokenize(_strip_banner(text)))

    # This guard lives HERE and not only in main(). It was in the CLI first,
    # which meant any caller reaching check() from Python got a clean 0% and a
    # pass against no source at all - a gate reporting success on a check it
    # never performed. A guard in the argument parser protects the argument
    # parser; a guard in the function protects the function.
    if not s:
        raise EmptySource(
            "No source words to compare against. Overlap against an empty "
            "source is always 0% and clears any threshold, so the run would "
            "look checked when nothing was checked."
        )

    w = word_overlap(c, s)
    p, runs = phrase_overlap(c, s, n)
    profile = {str(k): round(phrase_overlap(c, s, k)[0], 4) for k in PROFILE_LENGTHS}
    problems = []
    if w < min_word:
        problems.append(f"word overlap {w:.1%} is below the {min_word:.1%} floor")
    if p > max_phrase:
        problems.append(f"phrase overlap {p:.1%} is over the {max_phrase:.1%} ceiling")
    return {
        "content_words": len(c),
        "source_words": len(s),
        "phrase_length": n,
        "word_overlap": round(w, 4),
        "phrase_overlap": round(p, 4),
        "min_word_overlap": min_word,
        "max_phrase_overlap": max_phrase,
        "phrase_overlap_profile": profile,
        "longest_lifted_runs": runs[:5],
        "ok": not problems,
        "problems": problems,
    }


def check_floor(content: str, sources: list, min_word: float, min_phrase: float,
                n: int = DEFAULT_PHRASE_LENGTH) -> dict:
    """Floor variant: ok when overlap is AT OR ABOVE the threshold.

    For the user's own material: sounding like them is the goal, so high
    overlap is a pass. Problems are populated when a value falls BELOW its
    threshold.

    Return dict has the same shape as check() except:
      - 'min_word_overlap' and 'min_phrase_overlap' instead of max_*
      - 'longest_matched_runs' instead of 'longest_lifted_runs' (the runs are
        what the piece currently draws from the source, not what it lifted)
    """
    c = T.tokenize(content)
    s = []
    for text in sources:
        s.extend(T.tokenize(_strip_banner(text)))

    if not s:
        raise EmptySource(
            "No source words to compare against. Overlap against an empty "
            "source is always 0% and clears any threshold, so the run would "
            "look checked when nothing was checked."
        )

    w = word_overlap(c, s)
    p, runs = phrase_overlap(c, s, n)
    profile = {str(k): round(phrase_overlap(c, s, k)[0], 4) for k in PROFILE_LENGTHS}
    problems = []
    if w < min_word:
        problems.append(f"word overlap {w:.1%} is below the {min_word:.1%} floor")
    if p < min_phrase:
        problems.append(f"phrase overlap {p:.1%} is below the {min_phrase:.1%} floor")
    return {
        "content_words": len(c),
        "source_words": len(s),
        "phrase_length": n,
        "word_overlap": round(w, 4),
        "phrase_overlap": round(p, 4),
        "min_word_overlap": min_word,
        "min_phrase_overlap": min_phrase,
        "phrase_overlap_profile": profile,
        "longest_matched_runs": runs[:5],
        "ok": not problems,
        "problems": problems,
    }


def check_both(content: str,
               owned_sources: list, third_party_sources: list,
               word_floor: float, owned_phrase_floor: float, tp_phrase_ceiling: float,
               n: int = DEFAULT_PHRASE_LENGTH) -> dict:
    """Combined check with polarity: owned gets both floors, third-party gets
    word floor + phrase ceiling.

    The single word_floor is applied to EACH present pool; pools are never
    merged (word overlap is a floor, not a merge). Pass [] to skip a
    class; at least one list must be non-empty. Thresholds for an omitted
    class are ignored.

    Returns:
        {
            "owned":        check_floor result  (only when owned_sources is non-empty)
            "third_party":  check result        (only when third_party_sources is non-empty)
            "ok":           True only when every present class passes its own direction
        }
    """
    if not owned_sources and not third_party_sources:
        raise ValueError(
            "at least one of owned_sources or third_party_sources must be non-empty")
    result = {"ok": True}
    if owned_sources:
        r = check_floor(content, owned_sources, word_floor, owned_phrase_floor, n)
        result["owned"] = r
        if not r["ok"]:
            result["ok"] = False
    if third_party_sources:
        r = check(content, third_party_sources, word_floor, tp_phrase_ceiling, n)
        result["third_party"] = r
        if not r["ok"]:
            result["ok"] = False
    return result


def _fraction(value: str, label: str) -> float:
    try:
        f = float(value.rstrip("%")) / (100 if value.endswith("%") else 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{label} must be a number like 0.25 or 25%")
    if not 0 <= f <= 1:
        raise argparse.ArgumentTypeError(f"{label} must be between 0 and 1 (or 0% and 100%)")
    return f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="overlap-with-source gate")
    ap.add_argument("content")
    # --source is retired. It is kept here (suppressed from help) so argparse
    # does not exit 2 on an unknown argument, giving main() the chance to
    # print the helpful refusal message with both replacement flags named.
    ap.add_argument("--source", action="append", help=argparse.SUPPRESS)
    ap.add_argument("--owned-source", action="append", dest="owned_source",
                    metavar="PATH",
                    help="your own material; overlap is a FLOOR (want it high)")
    ap.add_argument("--third-party-source", action="append", dest="third_party_source",
                    metavar="PATH",
                    help="someone else's material; overlap is a CEILING (want it low)")
    ap.add_argument("--min-word-overlap",
                    type=lambda v: _fraction(v, "--min-word-overlap"),
                    help="floor for word overlap against ALL sources (owned and third-party)")
    ap.add_argument("--min-phrase-overlap",
                    type=lambda v: _fraction(v, "--min-phrase-overlap"),
                    help="floor for phrase overlap against owned sources")
    # --max-word-overlap is retired. Kept as a suppressed arg so
    # argparse does not exit 2, giving main() the chance to print the helpful
    # refusal message naming --min-word-overlap as the replacement.
    ap.add_argument("--max-word-overlap", dest="max_word_overlap_retired",
                    help=argparse.SUPPRESS)
    ap.add_argument("--max-phrase-overlap",
                    type=lambda v: _fraction(v, "--max-phrase-overlap"),
                    help="ceiling for phrase overlap against third-party sources")
    ap.add_argument("--phrase-length", type=int, default=DEFAULT_PHRASE_LENGTH)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--producer", action="store_true",
                    help="output for the producing agent: the lifted/matched passages "
                         "but not the scores")
    ap.add_argument("--run-dir", dest="run_dir", metavar="PATH",
                    help="read sources and polarities from a run's manifest registry; "
                         "mutually exclusive with --owned-source / --third-party-source")
    a = ap.parse_args(argv)

    # Refuse the retired --source flag. The distinction between owned and
    # third-party is not cosmetic: the gate runs in opposite directions for
    # each, and a default would either silently change every existing call's
    # meaning or hand the harmful ceiling to the primary use case.
    if a.max_word_overlap_retired is not None:
        print(
            "error: --max-word-overlap is retired. Word overlap is now a FLOOR in "
            "every case: a piece must be grounded in its sources, and "
            "'too much shared vocabulary' is phrase overlap's concern, not word's. "
            "Use --min-word-overlap for the word floor and --max-phrase-overlap for "
            "the third-party phrase ceiling.",
            file=sys.stderr)
        return 1

    if a.source:
        print(
            "error: --source is no longer accepted. Use:\n"
            "  --owned-source PATH         your own material "
            "(overlap is a floor: want it high)\n"
            "  --third-party-source PATH   someone else's material "
            "(overlap is a ceiling: want it low)\n"
            "The distinction matters because the direction of the gate is "
            "opposite for each.",
            file=sys.stderr)
        return 1

    if a.phrase_length < 2:
        print("error: --phrase-length below 2 measures single words, which is "
              "what --min-word-overlap already measures.", file=sys.stderr)
        return 1

    # Voice anchor guard. Voice anchors define the style the piece should
    # emulate; measuring overlap against them would fail the piece for
    # succeeding at its own goal.
    #
    # Two classes of anchor, treated differently:
    #   1. Any file inside this plugin's voice/ directory — refused unconditionally.
    #      These are the shipped reference profiles.
    #   2. Any file named voice_ref.md that sits beside a manifest.json — refused
    #      because that combination identifies a run's own voice anchor, emitted by
    #      voice.py into the run directory. A voice_ref.md without a manifest beside
    #      it is a user's own source document that happens to carry that name; blocking
    #      it is a false positive and a defect, not caution.
    _voice_dir = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice"))

    def _is_voice_anchor(path: str) -> bool:
        resolved = os.path.realpath(path)
        if resolved.startswith(_voice_dir + os.sep):
            return True
        if os.path.basename(resolved) == "voice_ref.md":
            parent = os.path.dirname(resolved)
            return os.path.isfile(os.path.join(parent, "manifest.json"))
        return False

    try:
        content = T.read_text(a.content)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if a.run_dir:
        # DEFECT 1 FIX: registry-driven mode. The gate reads sources and
        # polarities from the run's manifest — so what the orchestrator recorded
        # at source-intake is exactly what the gate measures, with no opportunity
        # for path assembly to omit a source or pass it in the wrong direction.
        #
        # Explicit --owned-source / --third-party-source are refused alongside
        # --run-dir because two sources of truth that can silently disagree is
        # the defect this mode was built to prevent.
        if a.owned_source or a.third_party_source:
            print(
                "error: --run-dir reads sources from the manifest registry; "
                "--owned-source and --third-party-source are refused alongside it. "
                "Use --run-dir to have the registry drive the gate, or use explicit "
                "source flags for standalone use. Both together means two sources of "
                "truth that can silently disagree — the defect this mode was built "
                "to prevent.",
                file=sys.stderr)
            return 1

        # Lazy import: manifest.py is a sibling in the scripts directory.
        # Import here rather than at module level to avoid a circular dependency
        # if manifest ever imports overlap, and to keep standalone overlap.py use
        # free of the import cost.
        import manifest as _M

        try:
            _data = _M.load(a.run_dir)
        except _M.ManifestError as e:
            print(f"error loading manifest from {a.run_dir!r}: {e}", file=sys.stderr)
            return 1

        _sources = _data.get("sources", [])
        if not _sources:
            print(
                f"error: the run at {a.run_dir!r} has no recorded sources. "
                "Measuring overlap against nothing is always 0% and clears any "
                "threshold, so the run would look checked when nothing was checked. "
                "(This is the same principle as the EmptySource guard in check() — "
                "read its comment for the full argument.) "
                "Record sources with:\n"
                f"  manifest.py --run-dir {a.run_dir} source --path PATH "
                "--polarity owned|third_party --role content|voice|both",
                file=sys.stderr)
            return 1

        owned_paths = [s["path"] for s in _sources
                       if s["polarity"] == _M.OWNED
                       and s.get("role") != _M.VOICE_ROLE]
        tp_paths = [s["path"] for s in _sources
                    if s["polarity"] == _M.THIRD_PARTY
                    and s.get("role") != _M.VOICE_ROLE]
        has_owned = bool(owned_paths)
        has_tp = bool(tp_paths)

        # A measurable pool can be empty for two different reasons, and the
        # error must not conflate them (added after the role filter above made
        # the second reason reachable):
        #   * no source of that polarity was registered at all, or
        #   * sources of that polarity WERE registered but every one is
        #     role=voice, which the gate never measures (a voice sample is
        #     off-topic by design).
        # Saying "none recorded" in the second case is a lie the caller can
        # catch: they did register an owned source, it just happens to be a
        # voice one. Distinguish the two so the message names the real cause.
        owned_polarity_any = any(s["polarity"] == _M.OWNED for s in _sources)
        tp_polarity_any = any(s["polarity"] == _M.THIRD_PARTY for s in _sources)

        # M3 fix: read overlap thresholds from the manifest's locked_parameters
        # (or parameters) so the recompute uses what was recorded at init, not
        # an ad-hoc value passed by the caller. Key scheme (flat, JSON-
        # serializable): overlap.word_floor (run-level word floor),
        # overlap.phrase_floor (run-level owned-source phrase floor),
        # overlap.phrase_ceiling (run-level third-party phrase ceiling).
        # Per-group overrides as overlap.phrase.<gid>.
        #
        # If a locked value exists and an explicit flag disagrees, refuse —
        # two sources of truth that can silently disagree is the exact defect
        # this mode was built to prevent (mirrors the --owned-source + --run-dir
        # refusal above).
        _locked_p = _data.get("locked_parameters", {})
        _params_p = _data.get("parameters", {})

        def _manifest_overlap(key):
            """Return the manifest's recorded value for an overlap.* key, or None.

            Checks locked_parameters first (shape: {value: ..., by: ..., at: ...}),
            then parameters (shape: {key: value}).  locked_parameters wins so the
            recorded provenance is the authority; parameters is the fallback for
            runs that store thresholds there without locking them.
            """
            if key in _locked_p:
                entry = _locked_p[key]
                if isinstance(entry, dict) and "value" in entry:
                    return entry["value"]
            return _params_p.get(key)

        _m_word_floor = _manifest_overlap("overlap.word_floor")
        _m_phrase_floor = _manifest_overlap("overlap.phrase_floor")
        _m_phrase_ceiling = _manifest_overlap("overlap.phrase_ceiling")

        # Disagreement check: refuse an explicit flag that contradicts a locked value.
        for _flag_val, _mval, _mkey, _flagname in [
            (a.min_word_overlap, _m_word_floor, "overlap.word_floor",
             "--min-word-overlap"),
            (a.min_phrase_overlap, _m_phrase_floor, "overlap.phrase_floor",
             "--min-phrase-overlap"),
            (a.max_phrase_overlap, _m_phrase_ceiling, "overlap.phrase_ceiling",
             "--max-phrase-overlap"),
        ]:
            if (_flag_val is not None and _mval is not None
                    and _flag_val != _mval and _mkey in _locked_p):
                print(
                    f"error: {_flagname} {_flag_val} disagrees with locked "
                    f"parameter '{_mkey}' = {_mval}. Use --run-dir without the "
                    "threshold flag to let the manifest drive thresholds, or "
                    "change the locked parameter via:\n"
                    f"  manifest.py --run-dir <run> params --update "
                    f"--key {_mkey} --override --reason '...' --by you\n"
                    "(which blocks completion until a person accepts it).",
                    file=sys.stderr)
                return 1

        # Apply manifest values as defaults where no explicit flag was passed.
        if a.min_word_overlap is None and _m_word_floor is not None:
            a.min_word_overlap = _m_word_floor
        if a.min_phrase_overlap is None and _m_phrase_floor is not None:
            a.min_phrase_overlap = _m_phrase_floor
        if a.max_phrase_overlap is None and _m_phrase_ceiling is not None:
            a.max_phrase_overlap = _m_phrase_ceiling

        # Threshold flags must be consistent with the registry.
        # --min-word-overlap is the word floor for ALL pools, so
        # it is required whenever any measurable pool exists. --min-phrase-overlap
        # is owned-specific; --max-phrase-overlap is third-party-specific.
        #
        # Consistency checks: refuse a flag whose pool class is absent.
        if a.min_phrase_overlap is not None and not has_owned:
            if owned_polarity_any:
                print(
                    "error: --min-phrase-overlap measures the owned overlap floor, "
                    "but every owned source in this run is role=voice, which the "
                    "floor never measures (a voice sample is there for tone, not "
                    "subject matter). Register an owned source with --role content "
                    "(or both) to check the floor.",
                    file=sys.stderr)
            else:
                print(
                    "error: --min-phrase-overlap requires owned sources in the "
                    "registry, but the run has none recorded.",
                    file=sys.stderr)
            return 1
        if a.max_phrase_overlap is not None and not has_tp:
            if tp_polarity_any:
                print(
                    "error: --max-phrase-overlap measures the third-party phrase "
                    "ceiling, but every third-party source in this run is role=voice, "
                    "which the ceiling never measures (a voice sample is there for "
                    "tone, not subject matter). Register a third-party source with "
                    "--role content (or both) to check the ceiling.",
                    file=sys.stderr)
            else:
                print(
                    "error: --max-phrase-overlap requires third-party sources in the "
                    "registry, but the run has none recorded.",
                    file=sys.stderr)
            return 1
        # Required-flag checks.
        if (has_owned or has_tp) and a.min_word_overlap is None:
            print(
                "error: the run has measurable sources; --min-word-overlap is "
                "required (word overlap is a floor for all pools; "
                "no default threshold)",
                file=sys.stderr)
            return 1
        if has_owned and a.min_phrase_overlap is None:
            print(
                "error: the run has owned sources; --min-phrase-overlap is "
                "required (no default threshold)",
                file=sys.stderr)
            return 1
        if has_tp and a.max_phrase_overlap is None:
            print(
                "error: the run has third-party sources; --max-phrase-overlap is "
                "required (no default threshold)",
                file=sys.stderr)
            return 1

        # Voice anchor check on registry paths. Should never trigger in practice
        # (manifest.py does not record voice anchors as sources), but guard it so
        # a corrupted or hand-edited manifest does not silently produce a wrong result.
        for path in owned_paths + tp_paths:
            if _is_voice_anchor(path):
                print(
                    f"error: {path!r} is a voice anchor, not a source. "
                    "Voice anchors define the style the piece should emulate; "
                    "measuring overlap against one would fail the piece for succeeding "
                    "at its own goal.",
                    file=sys.stderr)
                return 1

        try:
            owned_texts = [T.read_text(p) for p in owned_paths]
            tp_texts = [T.read_text(p) for p in tp_paths]
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    else:
        # Explicit-flag mode: the caller declares polarity per source file.
        # This is the original and only mode; --run-dir is additive.
        has_owned = bool(a.owned_source)
        has_tp = bool(a.third_party_source)

        if not has_owned and not has_tp:
            print("error: at least one of --owned-source or --third-party-source is required",
                  file=sys.stderr)
            return 1

        # Consistency checks: refuse a flag whose pool class is absent.
        # --min-word-overlap applies to ALL pools — not refused
        # when only tp is present. --min-phrase-overlap is owned-specific;
        # --max-phrase-overlap is third-party-specific.
        if a.min_phrase_overlap is not None and not has_owned:
            print(
                "error: --min-phrase-overlap requires --owned-source "
                "(the phrase floor applies to owned material; passing it "
                "with no owned source is a mistake worth naming)",
                file=sys.stderr)
            return 1
        if a.max_phrase_overlap is not None and not has_tp:
            print(
                "error: --max-phrase-overlap requires --third-party-source "
                "(the phrase ceiling applies to third-party material; passing it "
                "with no third-party source is a mistake worth naming)",
                file=sys.stderr)
            return 1

        # Required-flag checks. --min-word-overlap is required whenever any
        # source class is present; --min-phrase-overlap required for owned;
        # --max-phrase-overlap required for third-party.
        if a.min_word_overlap is None:
            print(
                "error: --min-word-overlap is required (word overlap is a floor "
                "for all pools; no default threshold)",
                file=sys.stderr)
            return 1
        if has_owned and a.min_phrase_overlap is None:
            print(
                "error: --owned-source requires --min-word-overlap and "
                "--min-phrase-overlap",
                file=sys.stderr)
            return 1
        if has_tp and a.max_phrase_overlap is None:
            print(
                "error: --third-party-source requires --min-word-overlap and "
                "--max-phrase-overlap",
                file=sys.stderr)
            return 1

        for flag, paths in [("--owned-source", a.owned_source or []),
                            ("--third-party-source", a.third_party_source or [])]:
            for path in paths:
                if _is_voice_anchor(path):
                    print(
                        f"error: {path!r} is a voice anchor, not a source. "
                        f"Voice anchors define the style the piece should emulate; "
                        f"passing one as {flag} would fail the piece for succeeding "
                        f"at its own goal.",
                        file=sys.stderr)
                    return 1

        try:
            owned_texts = [T.read_text(p) for p in (a.owned_source or [])]
            tp_texts = [T.read_text(p) for p in (a.third_party_source or [])]
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    try:
        owned_result = (check_floor(content, owned_texts, a.min_word_overlap,
                                    a.min_phrase_overlap, a.phrase_length)
                        if has_owned else None)
        tp_result = (check(content, tp_texts, a.min_word_overlap,
                           a.max_phrase_overlap, a.phrase_length)
                     if has_tp else None)
    except EmptySource as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    overall_ok = (
        (owned_result is None or owned_result["ok"]) and
        (tp_result is None or tp_result["ok"])
    )

    # both_classes: when True, label each block so the direction is unambiguous
    both_classes = owned_result is not None and tp_result is not None

    if a.producer:
        # The producing agent must never receive a score
        # to optimise against, in either direction. An agent given a number and a
        # threshold converges on just-under (ceiling) or just-over (floor) — no
        # bad intent required, iteration alone does it.
        #
        # For the ceiling: it gets what it lifted, which is what it needs to fix.
        # For the floor: it gets what currently draws from its material, not how
        # close it came to a number.
        if owned_result is not None:
            prefix = "ok (owned)" if both_classes else "ok"
            fail_prefix = "FAILED (owned)" if both_classes else "FAILED"
            if owned_result["ok"]:
                print(f"{prefix}: your material is present in the piece")
            else:
                runs = owned_result.get("longest_matched_runs", [])
                if runs:
                    # The floor's direction, stated without a score:
                    # too little of the author's own wording is present. The runs
                    # shown are what it DOES draw; the instruction is to draw more,
                    # never to paraphrase what is there into something smoother.
                    print(f"{fail_prefix} - not enough of your own material is in "
                          "the piece. These are the passages it currently draws "
                          "from your material; weave in MORE of your actual "
                          "wording, do not paraphrase it away:")
                    for run in runs:
                        print(f"  \"{run}\"")
                else:
                    print(f"{fail_prefix} - the piece draws nothing verbatim from "
                          "your material. Rebuild it from your own actual wording "
                          "rather than writing around it.")
        if tp_result is not None:
            prefix = "ok (third-party)" if both_classes else "ok"
            fail_prefix = "FAILED (third-party)" if both_classes else "FAILED"
            if tp_result["ok"]:
                print(f"{prefix}: nothing substantial is lifted from the source")
            else:
                print(f"{fail_prefix} - these passages are lifted from the source. "
                      "Rewrite them in your own words; do not simply break them up:")
                for run in tp_result["longest_lifted_runs"]:
                    print(f"  \"{run}\"")

    elif a.json:
        out = {"ok": overall_ok}
        if owned_result is not None:
            out["owned"] = owned_result
        if tp_result is not None:
            out["third_party"] = tp_result
        print(json.dumps(out))

    else:
        # Human-readable orchestrator output. Each class is labelled with its
        # direction so a number like 82.6% cannot be misread as a failure when
        # it is a pass — the direction, not just the number, must be
        # visible.
        if owned_result is not None:
            r = owned_result
            w_status = "ok" if r["word_overlap"] >= r["min_word_overlap"] else "BELOW FLOOR"
            p_status = "ok" if r["phrase_overlap"] >= r["min_phrase_overlap"] else "BELOW FLOOR"
            print(f"owned floor: "
                  f"word {r['word_overlap']:.1%} (min {r['min_word_overlap']:.1%}) {w_status}  "
                  f"phrase {r['phrase_overlap']:.1%} "
                  f"(min {r['min_phrase_overlap']:.1%}) {p_status}")
            print("  profile: " + "  ".join(
                f"n={k} {v:.1%}" for k, v in r["phrase_overlap_profile"].items()))
            if not r["ok"]:
                for problem in r["problems"]:
                    print(f"  {problem}")
        if tp_result is not None:
            r = tp_result
            w_status = "ok" if r["word_overlap"] >= r["min_word_overlap"] else "BELOW FLOOR"
            p_status = "ok" if r["phrase_overlap"] <= r["max_phrase_overlap"] else "OVER CEILING"
            print(f"third-party (word floor, phrase ceiling): "
                  f"word {r['word_overlap']:.1%} (min {r['min_word_overlap']:.1%}) {w_status}  "
                  f"phrase {r['phrase_overlap']:.1%} "
                  f"(max {r['max_phrase_overlap']:.1%}) {p_status}")
            print("  profile: " + "  ".join(
                f"n={k} {v:.1%}" for k, v in r["phrase_overlap_profile"].items()))
            if not r["ok"]:
                for run in r["longest_lifted_runs"]:
                    print(f"  lifted: {run}")

    return 0 if overall_ok else GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
