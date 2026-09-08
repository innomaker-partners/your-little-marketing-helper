"""
Quote-pasting tool for the pain & messaging report writer.

The report LLM runs as a subprocess starved of tools, so it cannot call an
external lookup at write time. Instead, each quote in the bank is labelled
with a short ID like [[Q7]]; the model writes that ID where it wants the
quote and this module replaces every [[Q<n>]] with the verbatim bank text
in straight double quotes, byte-for-byte. The model picks WHICH quote
(judgment); this code supplies the EXACT TEXT (determinism).

Design intent: writing [[Q7]] is far less effort than retyping a whole
sentence inside quotation marks, so the model's natural preference for the
lowest-effort path points toward the tool, not away from it.

Why not a live tool the model calls: the report writer runs as a subprocess
deliberately starved of MCP servers and tools (--strict-mcp-config, empty
--mcp-config, --tools ""). Re-enabling tools would reopen the artifact-publish
hole this starvation seals. This module is prompt-side reference syntax plus
post-processing substitution -- NOT an MCP or Bash tool. That is FROZEN.

Public API:
    paste_refs(text, quote_bank) -> (new_text, n_pasted, n_bad)
    count_inline_quotes(text) -> int
"""

import re
import sys

# Matches [[Q<n>]] -- case-insensitive, tolerant of inner whitespace.
# Examples: [[Q1]], [[q3]], [[ Q12 ]], [[ q 1 ]]
_REF_RE = re.compile(r'\[\[\s*[Qq]\s*(\d+)\s*\]\]')

# Matches quoted spans in both straight (U+0022) and curly (U+201C/U+201D)
# double quotes. Defined independently here to keep this module self-contained
# (report.py defines its own equivalent in _QUOTE_RE). Pattern uses the same
# hex-escape for U+0022 to prevent editors from auto-converting it.
_INLINE_QUOTE_RE = re.compile(
    '\x22([^\x22\n]+)\x22'           # straight double quotes (U+0022)
    '|“([^”\n]+)”'    # curly double quotes (U+201C / U+201D)
)


def _bank_text(entry) -> str:
    """Extract the quote text from a bank entry (dict or plain string)."""
    if isinstance(entry, dict):
        return entry.get("text", "")
    return str(entry)


def paste_refs(text: str, quote_bank: list) -> tuple:
    """Replace every [[Q<n>]] reference with the verbatim bank text.

    Each reference is replaced with the exact text of quote_bank[n-1]
    (1-based IDs), wrapped in straight double quotes. The model writes
    the ID; this code supplies the exact words -- copy-paste by determinism,
    not inference.

    Args:
        text:       Raw model output, possibly containing [[Q<n>]] references.
        quote_bank: Ordered list of quote dicts or plain strings. Index 0 is
                    Q1, index 1 is Q2, and so on. Order MUST match the labeled
                    list in the prompt, which paste_refs assumes without re-checking.

    Returns:
        (new_text, n_pasted, n_bad)
        n_pasted -- references resolved successfully
        n_bad    -- references that were out of range (or bank empty);
                    replaced with the literal marker [quote unavailable].
                    Never crashes, never pastes a wrong quote.
    """
    n_pasted = 0
    n_bad = 0

    def _replace(m):
        nonlocal n_pasted, n_bad
        idx = int(m.group(1)) - 1   # convert 1-based ID to 0-based index
        if not quote_bank or idx < 0 or idx >= len(quote_bank):
            n_bad += 1
            return "[quote unavailable]"
        verbatim = _bank_text(quote_bank[idx])
        n_pasted += 1
        return f'"{verbatim}"'

    new_text = _REF_RE.sub(_replace, text)
    return new_text, n_pasted, n_bad


def count_inline_quotes(text: str) -> int:
    """Count quoted spans the model typed itself instead of using [[Q]] refs.

    Scans for straight and curly double-quoted spans with >= 5 whitespace-
    separated words -- the same gate as _provenance_note in report.py so the
    two measures are comparable. Shorter spans (emphasis, product names) are
    ignored.

    Call this on the RAW model output BEFORE paste_refs runs. The count is
    the bypass rate: how often the model wrote customer words from memory
    instead of dropping in an ID. Zero is ideal; non-zero is a signal the
    prompt framing may need tuning.
    """
    count = 0
    for m in _INLINE_QUOTE_RE.finditer(text):
        span = m.group(1) or m.group(2)
        if span and len(span.split()) >= 5:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Self-test (run with: python3 scripts/quote_tool.py)
# Pattern matches extractor.py and report.py exactly.
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

    # Shared fixture
    _BANK = [
        {"text": "Equipment is always broken and nobody fixes it",
         "stars": 2, "business": "FitZone"},
        {"text": "Staff never available when you need help",
         "stars": 1, "business": "FitZone"},
    ]
    _T1 = _BANK[0]["text"]
    _T2 = _BANK[1]["text"]

    # -------------------------------------------------------------------------
    # TEST 1 -- basic substitution: two refs, both resolved
    # -------------------------------------------------------------------------
    print("=== TEST 1: basic two-ref substitution ===")
    _raw = "see [[Q1]] and [[Q2]] here"
    _out, _np, _nb = paste_refs(_raw, _BANK)
    check("1: n_pasted == 2",   _np, 2)
    check("1: n_bad == 0",      _nb, 0)
    check("1: Q1 text in output", f'"{_T1}"' in _out, True)
    check("1: Q2 text in output", f'"{_T2}"' in _out, True)
    check("1: no [[Q]] ref remains", "[[" not in _out, True)

    # -------------------------------------------------------------------------
    # TEST 2 -- verbatim byte-for-byte check
    # -------------------------------------------------------------------------
    print("\n=== TEST 2: verbatim byte-for-byte ===")
    _v_out, _, _ = paste_refs("Claim: [[Q1]].", _BANK)
    check("2: pasted text is exactly the bank text (no mutation)",
          _v_out, f'Claim: "{_T1}".')

    # -------------------------------------------------------------------------
    # TEST 3 -- case/space tolerance
    # -------------------------------------------------------------------------
    print("\n=== TEST 3: case and space tolerance ===")
    _o_lower, _np3a, _ = paste_refs("[[q1]]", _BANK)
    check("3: [[q1]] resolves",        _np3a, 1)
    check("3: [[q1]] text correct",    f'"{_T1}"' in _o_lower, True)
    _o_space, _np3b, _ = paste_refs("[[ Q1 ]]", _BANK)
    check("3: [[ Q1 ]] resolves",      _np3b, 1)
    check("3: [[ Q1 ]] text correct",  f'"{_T1}"' in _o_space, True)

    # -------------------------------------------------------------------------
    # TEST 4 -- out-of-range ref
    # -------------------------------------------------------------------------
    print("\n=== TEST 4: out-of-range ref ===")
    _o_bad, _np4, _nb4 = paste_refs("See [[Q99]] for details.", _BANK)
    check("4: n_pasted == 0",          _np4, 0)
    check("4: n_bad == 1",             _nb4, 1)
    check("4: marker inserted",        "[quote unavailable]" in _o_bad, True)
    check("4: no crash on empty bank", paste_refs("[[Q1]]", [])[2], 1)

    # -------------------------------------------------------------------------
    # TEST 5 -- plain-string bank entries
    # -------------------------------------------------------------------------
    print("\n=== TEST 5: plain-string bank entries ===")
    _plain_bank = ["Friendly staff and clean facilities every time"]
    _o_plain, _np5, _nb5 = paste_refs("Top trust signal: [[Q1]].", _plain_bank)
    check("5: n_pasted == 1",          _np5, 1)
    check("5: n_bad == 0",             _nb5, 0)
    check("5: plain text resolved",
          '"Friendly staff and clean facilities every time"' in _o_plain, True)

    # -------------------------------------------------------------------------
    # TEST 6 -- count_inline_quotes
    # -------------------------------------------------------------------------
    print("\n=== TEST 6: count_inline_quotes ===")
    # One long typed quote (8 words) + one short (2 words) -- counts 1
    _mixed = ('Finding: "Equipment is always broken and nobody fixes it" was top. '
              'Also a minor "sync" note.')
    check("6: one long + one short -> counts 1",   count_inline_quotes(_mixed), 1)
    # Text using only [[Q]] refs (no literal quotes at all) -- counts 0
    _refs_only = "See [[Q1]] for the main complaint and [[Q2]] for the second."
    check("6: [[Q]]-only text -> counts 0",        count_inline_quotes(_refs_only), 0)
    # Two long typed quotes
    _two_long = ('"Equipment is always broken and nobody fixes it" was one. '
                 '"Staff never available when you need help here" was another.')
    check("6: two long quotes -> counts 2",        count_inline_quotes(_two_long), 2)
    # Exactly 4-word span -- below the gate, not counted
    _four_word = 'Issue: "really bad locker rooms" noted.'
    check("6: 4-word span -> counts 0",            count_inline_quotes(_four_word), 0)
    # Curly quotes also counted
    _curly = '“Equipment is always broken and nobody fixes it” was cited.'
    check("6: curly-quoted long span -> counts 1", count_inline_quotes(_curly), 1)

    print(f"\n{'='*60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
