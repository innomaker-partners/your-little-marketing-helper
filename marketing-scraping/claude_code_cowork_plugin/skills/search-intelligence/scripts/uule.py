"""
UULE encoder for Google Search localisation.

A UULE string tells the Google Search actor which city/region to use when
building the SERP, so results reflect local pack rankings rather than a
country-level blend.

Usage:
    from scripts.uule import uule
    location_uule = uule("New York,New York,United States")

The canonical location name format expected by the actor:
    "<City>,<State/Province>,<Country>"
    e.g. "Budapest,Budapest,Hungary" or "München,Bavaria,Germany"

NOTE: Whether the live actor actually accepts this string as a valid UULE is
confirmed by a live paid test, not offline. If the actor rejects the encoded
string, the fallback is plain countryCode (no UULE). Do not interpret a
passing offline selftest as proof of Google-acceptance.
"""

import base64

# The key table used by Google's canonical UULE encoding.
# Index into this table using len(canonical_name) % 64 to pick the key byte.
_SECRET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def uule(canonical_name: str) -> str:
    """Encode a canonical location name into a Google UULE 'w+CAIQICI' string.

    The algorithm:
      1. Base64-encode the UTF-8 bytes of canonical_name.
      2. Pick a key character from _SECRET using len(canonical_name) % 64.
      3. Prepend the fixed prefix 'w+CAIQICI', then the key char, then the b64.

    Args:
        canonical_name: A string like "New York,New York,United States".
                        An empty string is accepted without crashing (the
                        encoding is well-defined but the actor may reject it).

    Returns:
        A UULE string starting with 'w+CAIQICI'.
    """
    b64 = base64.b64encode(canonical_name.encode("utf-8")).decode("ascii")
    key = _SECRET[len(canonical_name) % len(_SECRET)]
    return "w+CAIQICI" + key + b64


def _decode_uule(u: str) -> str:
    """Inverse of uule() — for round-trip testing only.

    Strips the 9-char prefix 'w+CAIQICI' plus the 1-char key, then base64-decodes.
    """
    return base64.b64decode(u[10:]).decode("utf-8")


# ---------------------------------------------------------------------------
# Selftest (run with: python3 scripts/uule.py)
# ---------------------------------------------------------------------------

def _selftest():
    checks_passed = 0

    # Case 1: output starts with the expected prefix.
    result = uule("New York,New York,United States")
    assert result.startswith("w+CAIQICI"), (
        f"Case 1: expected 'w+CAIQICI' prefix, got {result!r}"
    )
    checks_passed += 1

    # Case 2: round-trip — decode(encode(name)) == name — for several names.
    names = [
        "New York,New York,United States",
        "Budapest,Budapest,Hungary",
        "München,Bavaria,Germany",        # non-ASCII: ü
        "Los Angeles,California,United States",  # contains comma
        "London,England,United Kingdom",
    ]
    for name in names:
        encoded = uule(name)
        decoded = _decode_uule(encoded)
        assert decoded == name, (
            f"Case 2 round-trip failed for {name!r}: got {decoded!r}"
        )
        checks_passed += 1

    # Case 3: determinism — same input always produces same output.
    for name in names:
        assert uule(name) == uule(name), (
            f"Case 3: non-deterministic output for {name!r}"
        )
    checks_passed += 1

    # Case 4: empty string is handled without crashing (behavior is documented).
    # The encoding is well-defined; live actor acceptance is not guaranteed.
    empty_result = uule("")
    assert isinstance(empty_result, str), "Case 4: empty string must return a string"
    assert empty_result.startswith("w+CAIQICI"), (
        f"Case 4: empty string result has unexpected prefix: {empty_result!r}"
    )
    # Round-trip for empty string.
    assert _decode_uule(empty_result) == "", (
        f"Case 4: empty string round-trip failed: {_decode_uule(empty_result)!r}"
    )
    checks_passed += 1

    print(f"OK: {checks_passed} checks passed")
    print(f"  Example: uule('New York,New York,United States') = {uule('New York,New York,United States')!r}")
    print(f"  Round-trip proof: decode(encode('München,Bavaria,Germany')) = {_decode_uule(uule('München,Bavaria,Germany'))!r}")


if __name__ == "__main__":
    _selftest()
