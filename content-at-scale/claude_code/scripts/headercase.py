#!/usr/bin/env python3
"""Deterministic header sentence-caser for produced markdown.

Rewrites every ATX heading line ("# ...", "## ...") of a markdown file to
sentence case using textutil.sentence_case_header - the first letter of the
header plus abbreviations only. It exists because SEO pieces shipped with
lowercase keyword H2s ("## ai seo tools 2026") and title-cased H1s ("# How to
Use AI in Marketing"), and the fix must be a determinism, not a
maintained abbreviation list or a model's judgement.

PRECONDITION, and it is load-bearing: the abbreviations in a header must already
be cased correctly ("AI", "SEO", "ChatGPT"). That casing is fixed once, at
intake, from the human-confirmed keyword display forms; this tool cannot invent
the capital in "AI" from a lowercase "ai". Feeding it un-cased keyword text
produces "Ai seo tools", which is why the keyword display casing is carried into
the brief upstream (see textutil.sentence_case_header for that boundary).

Run by the orchestrator on final.md at finalization - after de-slop, before the
final claim-diff recompute - so the delivered text is what claim-diff measures.
A heading is not a claim, so casing it changes nothing claim-diff guards; running
it on the draft instead would leave a later de-slop edit free to reintroduce a
title-cased heading the reader then sees.

Headings inside fenced code blocks are left alone: a "# comment" line in a
```python block is code, not a heading, and rewriting its casing would corrupt
a snippet. Setext headings (underlined with === or ---) are NOT handled - they
are rare in produced pieces and need two-line lookahead this does not do; a
known limit, stated rather than papered over.

Exit codes follow the gate convention (see length.py):
  0  the rewrite was printed/written, or --check found nothing to change
  3  --check found headings not in sentence case (the "gate failed" code)
  1  the call was wrong (bad path)
  2  argparse's own malformed-command-line code
"""
from __future__ import annotations

import argparse
import re
import sys

import textutil as T

CHANGES_FOUND = 3

# An ATX heading: up to three leading spaces, 1-6 '#', at least one space, then
# the heading text and any trailing whitespace. Kept in the same shape as
# textutil._HEADING_RE so the two agree on what a heading is.
_HEADING_LINE_RE = re.compile(r"^([ \t]{0,3}#{1,6}[ \t]+)(.*?)([ \t]*)$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def recase_headers(text: str) -> str:
    """Return `text` with every ATX heading outside a code fence sentence-cased.

    Whitespace, body text, blank lines and code fences are preserved exactly; a
    trailing newline round-trips. Only the heading's visible text is touched, and
    only the casing of that text.
    """
    out = []
    in_fence = False
    for line in (text or "").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEADING_LINE_RE.match(line)
            if m and m.group(2):
                line = m.group(1) + T.sentence_case_header(m.group(2)) + m.group(3)
        out.append(line)
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="deterministic markdown header sentence-caser")
    ap.add_argument("path")
    ap.add_argument("--in-place", action="store_true",
                    help="rewrite the file in place instead of printing to stdout")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 3 if any heading is not sentence case")
    a = ap.parse_args(argv)

    if a.in_place and a.check:
        print("error: --in-place and --check are mutually exclusive", file=sys.stderr)
        return 1

    try:
        original = T.read_text(a.path)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    recased = recase_headers(original)

    if a.check:
        if recased != original:
            print(f"{a.path}: headings are not in sentence case", file=sys.stderr)
            return CHANGES_FOUND
        return 0

    if a.in_place:
        if recased != original:
            with open(a.path, "w", encoding="utf-8") as f:
                f.write(recased)
        return 0

    sys.stdout.write(recased)
    return 0


if __name__ == "__main__":
    sys.exit(main())
