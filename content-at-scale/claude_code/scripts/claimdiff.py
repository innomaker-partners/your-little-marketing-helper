#!/usr/bin/env python3
"""Claim-diff gate: did de-slop change a sentence that fact-check verified?

The problem it exists for (an accepted cost, stated rather
than hidden): gates run fact-check BEFORE de-slop, so the shipped text is not
the text that was verified. De-slop is strongly prompted to leave claim
sentences verbatim and rewrite only the phrasing around them,
but a prompt is an instruction, not a guarantee. This script is the
deterministic backstop for that instruction.

IT FLAGS, IT DOES NOT BLOCK. Exit status is 0 even when claims
changed, because a changed claim is not automatically wrong - it may have
been a legitimate tightening. The judgment belongs to a person or a later
gate; this script's job is to make sure nobody has to notice on their own.
Read the JSON, not the exit code. Exit 1 still means the call was wrong.

Run by the DE-SLOP agent, recomputed by the orchestrator on the
final artifact.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys

import textutil as T

# Below this similarity a "closest match" is not a rewrite of the claim, it is
# a different sentence that happens to share some words. Reporting it as the
# rewrite would send a reviewer to the wrong line. It is a
# reporting nicety, not a pass/fail threshold, and nothing depends on it.
CLOSEST_MATCH_FLOOR = 0.55


def _sentences(text: str) -> list:
    """Split on sentence-ending punctuation followed by whitespace.

    Crude, and it DOES take part in the verdict - see `_enclosing`. That is a
    change from the first version, which compared the claim as a bare
    substring and so could not see a sentence being EXTENDED. A claim marked
    without its trailing period matched happily inside "...year on year,
    driven by enterprise deals", and a de-slop pass that appended qualifying
    clauses to verified sentences went completely undetected.

    The trade is deliberate: a mis-split makes this gate flag something that
    was fine, which costs a person one look, and this gate
    flags rather than blocks. The other error ships an altered claim.
    """
    import re
    parts = re.split(r"(?<=[.!?])\s+", T.normalize_ws(text))
    return [p for p in parts if p]


def compare(before: str, after: str, claims: list) -> dict:
    after_norm = T.normalize_ws(after)
    after_sents = _sentences(after)
    before_norm = T.normalize_ws(before)

    before_sents = _sentences(before)

    def _enclosing(c):
        """The shortest verified sentence containing the claim.

        Comparing that, rather than the claim alone, is what makes an
        extension visible: the claim survives inside the longer sentence, but
        the sentence it was verified in does not.
        """
        holders = [s for s in before_sents if c in s]
        return min(holders, key=len) if holders else c

    kept, changed, missing_from_before, empty = [], [], [], []
    for claim in claims:
        c = T.normalize_ws(claim)
        if not c:
            empty.append(claim)
            continue
        if c not in before_norm:
            # The claim was not in the text that was verified either. That is
            # a broken hand-off between fact-check and de-slop, not a de-slop
            # failure, and calling it one would send the fix to the wrong
            # place.
            missing_from_before.append(claim)
            continue
        target = _enclosing(c)
        if target in after_norm:
            kept.append(claim)
            continue
        match = difflib.get_close_matches(target, after_sents, n=1,
                                          cutoff=CLOSEST_MATCH_FLOOR)
        changed.append({
            "claim": claim,
            "closest_after": match[0] if match else None,
            "verified_sentence": target,
            "similarity": round(difflib.SequenceMatcher(None, target, match[0]).ratio(), 3)
            if match else 0.0,
        })

    return {
        # Everything handed to the gate. This is the headline denominator, and
        # it is NOT `claims_checked` - see below.
        "claims_total": len(claims),
        # Claims actually comparable between the two texts. Unchanged meaning:
        # a claim absent from the verified text cannot be checked against the
        # final one, and calling it "checked" would be false.
        "claims_checked": len(kept) + len(changed),
        "kept_verbatim": len(kept),
        "changed": changed,
        "claims_not_found_in_before": missing_from_before,
        # Blank entries are reported rather than dropped, so that
        # total == kept + changed + not_found + empty always closes. An
        # unaccounted item is how the original defect stayed invisible.
        "claims_empty": empty,
        "ok": not changed and not missing_from_before,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="flag claim sentences altered after verification")
    ap.add_argument("--before", required=True, help="the text fact-check verified")
    ap.add_argument("--after", required=True, help="the text de-slop produced")
    ap.add_argument("--claims", required=True,
                    help='JSON file: {"claims": ["sentence", ...]} or a bare list')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        before = T.read_text(a.before)
        after = T.read_text(a.after)
        raw = json.loads(T.read_text(a.claims))
    except OSError as e:
        print(
            f"error: {e}\n"
            "  --before expects the fact-check-verified text; "
            "--after expects the de-slop output; "
            "--claims expects the claims JSON produced by the fact_check stage — "
            "re-run fact-check to regenerate it.",
            file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"error: --claims is not valid JSON: {e}. "
              "This file is produced by the fact_check stage — "
              "re-run fact-check to regenerate it.", file=sys.stderr)
        return 1

    claims = raw.get("claims") if isinstance(raw, dict) else raw
    if not isinstance(claims, list) or not all(isinstance(c, str) for c in claims):
        print('error: --claims must hold a list of sentence strings, either as '
              '{"claims": [...]} or a bare list. '
              'This file is produced by the fact_check stage — '
              're-run fact-check to regenerate it.', file=sys.stderr)
        return 1
    if not claims:
        print("error: no claims to check. A piece with zero claim sentences either "
              "asserts nothing or was not marked up. "
              "Re-run the fact_check stage to re-mark, then re-run claim-diff.",
              file=sys.stderr)
        return 1

    result = compare(before, after, claims)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        # The denominator is every claim in the file, not every claim that
        # survived to be comparable. Counting only the comparable ones lets a
        # piece whose claim had VANISHED print a perfect score (e.g. 6/6 against
        # 7 claims), which is exactly the case the summary line most needs to
        # surface.
        print(f"{result['kept_verbatim']}/{result['claims_total']} claims kept verbatim")
        for c in result["changed"]:
            print(f"  CHANGED: {c['claim']}")
            print(f"       ->: {c['closest_after'] or '(no close match in the new text)'}")
        for c in result["claims_not_found_in_before"]:
            print(f"  NOT IN THE VERIFIED TEXT: {c}")
        if result["claims_empty"]:
            print(f"  BLANK ENTRIES IN THE CLAIMS FILE: {len(result['claims_empty'])}")
    # Always 0: this gate flags, it does not block.
    return 0


if __name__ == "__main__":
    sys.exit(main())
