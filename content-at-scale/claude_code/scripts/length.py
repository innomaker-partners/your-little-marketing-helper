#!/usr/bin/env python3
"""Content length gate.

Counts the words a reader actually reads and compares them to bounds the
caller supplies. There is no default length and there never will be: the
target belongs to the piece brief, and a script that invented one would be
setting editorial policy from inside a word counter.

Run by the PRODUCING agent so it can iterate in place when the
piece comes up short, and recomputed by the orchestrator on the final
artifact because that is what decides pass or fail.

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
import sys

import textutil as T

GATE_FAILED = 3


def measure(text: str, markdown: bool = True) -> int:
    return len(T.tokenize(text, markdown=markdown))


def check(text: str, minimum: int = None, maximum: int = None, markdown: bool = True) -> dict:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            f"floor {minimum} is above ceiling {maximum} - no piece can pass. "
            "Checked here rather than only in the CLI so a Python caller "
            "cannot get a confident failure report from an impossible bound."
        )
    n = measure(text, markdown)
    problems = []
    if minimum is not None and n < minimum:
        problems.append(f"{n} words, {minimum - n} short of the {minimum} floor")
    if maximum is not None and n > maximum:
        problems.append(f"{n} words, {n - maximum} over the {maximum} ceiling")
    return {"words": n, "min": minimum, "max": maximum,
            "ok": not problems, "problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="content length gate")
    ap.add_argument("path")
    ap.add_argument("--min", type=int, help="floor, in words")
    ap.add_argument("--max", type=int, help="ceiling, in words")
    ap.add_argument("--raw", action="store_true",
                    help="count the file as-is instead of stripping markdown")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.min is None and a.max is None:
        print("error: give --min, --max, or both. This gate does not have a "
              "default length - the target comes from the piece brief.",
              file=sys.stderr)
        return 1
    if a.min is not None and a.max is not None and a.min > a.max:
        print(f"error: --min {a.min} is above --max {a.max}.", file=sys.stderr)
        return 1

    try:
        text = T.read_text(a.path)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    result = check(text, a.min, a.max, markdown=not a.raw)
    if a.json:
        print(json.dumps(result))
    elif result["ok"]:
        print(f"ok: {result['words']} words")
    else:
        print("; ".join(result["problems"]))
    return 0 if result["ok"] else GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
