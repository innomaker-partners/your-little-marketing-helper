#!/usr/bin/env python3
"""Fact-token gate: deterministic backstop before a de-slop superseded stamp.

Before de-slop may stamp a claim entry superseded (§5b of de-slop/SKILL.md),
it runs this script on superseded.original vs the new sentence. If the
fact-bearing tokens differ, the stamp is forbidden — the case escalates to
the orchestrator, which re-enters the normal claim-diff ladder (§5a). It does
NOT raise claim_override; that flag is the orchestrator's to raise when its
own retries exhaust.

What "fact-bearing tokens" means — see textutil.fact_tokens for the full
rationale and known limits:
  * number tokens — any digit-bearing run in the sentence, optionally ending
    in a percent sign ("35%", "3.5", "1,000"). A prose paraphrase that
    replaces a concrete figure with a vague phrase (or vice versa) is caught.
  * name tokens — Title-cased or _is_abbrev words (case-preserved), with the
    sentence-initial word excluded from the Title-case rule. A swap of a
    proper noun or acronym is caught.

The check is a DIFFERENCE DETECTOR whose false positives are SAFE: a false
"differ" routes to the orchestrator (one extra ladder trip), never to a silent
stamp. When in doubt, the result is "differ". The prose guards in §5b still
apply on top when the check passes. (FROZEN)

Exit codes:
  0  fact tokens match — stamp allowed (prose guards in §5b still apply)
  3  fact tokens DIFFER — do not stamp; escalate to the orchestrator
  1  bad call (missing or empty argument, or unreadable argument)
  2  argparse's own code for a malformed command line

Gate failure is 3 rather than 2 because argparse already owns 2. An agent
that misinvoked the script would read "you typed it wrong" as "stamp
forbidden" — both direct it not to stamp, so the safe direction is preserved,
but the cause would be misidentified. (Found by reviewing length.py's note on
the same design choice.)
"""

from __future__ import annotations

import argparse
import json
import sys

import textutil as T

GATE_FAILED = 3


def check(original: str, new_sentence: str) -> dict:
    """Pure-Python entry point for callers that do not want subprocess."""
    orig = (original or "").strip()
    new = (new_sentence or "").strip()
    if not orig or not new:
        raise ValueError("original and new_sentence must both be non-empty")
    match, diff = T.fact_tokens_match(orig, new)
    return {"match": match, "diff": diff}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="fact-token backstop before a de-slop superseded stamp")
    ap.add_argument(
        "--original", required=True,
        help="the claim sentence from verified.md / superseded.original")
    ap.add_argument(
        "--new", required=True, dest="new_sentence",
        help="the enclosing sentence as it now appears in final.md")
    ap.add_argument(
        "--json", action="store_true",
        help="emit result as JSON instead of human-readable text")
    a = ap.parse_args(argv)

    orig = a.original.strip()
    new = a.new_sentence.strip()
    if not orig or not new:
        print("error: --original and --new must not be empty.", file=sys.stderr)
        return 1

    match, diff = T.fact_tokens_match(orig, new)

    if a.json:
        print(json.dumps({"match": match, "diff": diff}))
    elif match:
        print("ok: fact tokens match — stamp allowed")
    else:
        print("DIFFER: fact tokens changed — do not stamp; escalate to orchestrator")
        if "numbers" in diff:
            orig_n = diff["numbers"]["original"]
            new_n = diff["numbers"]["new"]
            print(f"  numbers differ: original={orig_n!r} new={new_n!r}")
        if "names" in diff:
            orig_nm = diff["names"]["original"]
            new_nm = diff["names"]["new"]
            print(f"  names differ:   original={orig_nm!r} new={new_nm!r}")

    return 0 if match else GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
