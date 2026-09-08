"""
Seed resolution for the search-intelligence tool.

Turns the user's config into the initial seed-keyword list that stage A1
will expand via Google autocomplete.

Two supported input modes:

  Mode 1 -- seed_terms:
    cfg["seed_terms"] is a non-empty list. The user already knows their
    keywords. Use them directly (normalized, deduped, whitespace-trimmed).

  Mode 2 -- vertical:
    cfg["seed_terms"] is empty, but cfg["vertical"] is set (a string OR a
    list of strings). The vertical term(s) become the initial seeds.
    A1 (a later phase) does the actual autocomplete expansion; this module
    only normalizes the vertical value(s) into the same list shape as mode 1.
    No modifier variants ("free", "software", etc.) are added here -- those
    are speculative and belong in the expansion layer, not in seed resolution.

  Neither set -> SystemExit with a clear message for the user.

Mode 3 (competitor domain -> keywords) is deliberately deferred to v1.1.
It needs a Semrush actor call (paid, non-deterministic), so it is not a
pure config-resolution step. Do not add it here.

Stdlib only -- no pip install needed.
"""

from __future__ import annotations


def resolve_seeds(cfg: dict) -> tuple[list[str], str]:
    """
    Return (seeds, mode).

    seeds -- the deduplicated, normalized seed-keyword list.
    mode  -- "seed_terms" (mode 1) or "vertical" (mode 2).

    Normalization applied to both modes:
      - strip leading/trailing whitespace from each term
      - drop empty strings after stripping
      - deduplicate while preserving order; case-insensitive comparison
        keeps the FIRST spelling seen

    Pure function: reads only cfg, performs no file or network I/O.
    """
    # --- Mode 1: seed_terms (list, or a bare string forgiven as one seed) --
    normalized = _normalize(_as_list(cfg.get("seed_terms")))
    if normalized:
        return normalized, "seed_terms"

    # --- Mode 2: vertical (string or list) --------------------------------
    normalized = _normalize(_as_list(cfg.get("vertical")))
    if normalized:
        return normalized, "vertical"

    # --- Neither set ------------------------------------------------------
    raise SystemExit(
        "No seeds configured.\n"
        "  Option A: set 'seed_terms' to a list of keywords you already know.\n"
        "  Option B: set 'vertical' to a market label (e.g. 'project management\n"
        "            software') and this tool will expand it via autocomplete.\n"
        "Copy SEARCH_CONFIG.example.json to SEARCH_CONFIG.json and fill in one\n"
        "of those two fields."
    )


def _as_list(value) -> list[str]:
    """Coerce a config value into a list of strings.

    A bare string becomes a one-element list -- this forgives the common
    misconfiguration `"seed_terms": "crm software"` (a string) written instead
    of `["crm software"]` (a list), which would otherwise be iterated
    character-by-character into garbage seeds. A list is passed through with
    each item stringified; None / anything else becomes empty. `vertical` and
    `seed_terms` go through the same coercion so both behave identically.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _normalize(terms: list[str]) -> list[str]:
    """Strip, drop empties, deduplicate (case-insensitive, keep first seen)."""
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        s = t.strip()
        if not s:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Self-test -- run with `python3 seeds.py`
# ---------------------------------------------------------------------------

def _selftest() -> None:
    checks = 0

    # Case 1: mode-1 passthrough -- two clean keywords.
    seeds, mode = resolve_seeds({"seed_terms": ["project management software", "team task tracker"], "vertical": ""})
    assert mode == "seed_terms", f"case 1: mode={mode!r}"
    assert seeds == ["project management software", "team task tracker"], f"case 1: seeds={seeds!r}"
    checks += 1

    # Case 2: mode-1 with duplicates and whitespace to normalize.
    # "  task tracker  " strips to "task tracker"; "Task Tracker" is a case-dup
    # of "task tracker" (second occurrence dropped); "" and "  " are dropped.
    seeds, mode = resolve_seeds({
        "seed_terms": ["crm software", "  task tracker  ", "", "Task Tracker", "crm software", "  "],
        "vertical": ""
    })
    assert mode == "seed_terms", f"case 2: mode={mode!r}"
    assert seeds == ["crm software", "task tracker"], f"case 2: seeds={seeds!r}"
    checks += 1

    # Case 3: mode-2 from a string vertical.
    seeds, mode = resolve_seeds({"seed_terms": [], "vertical": "project management software"})
    assert mode == "vertical", f"case 3: mode={mode!r}"
    assert seeds == ["project management software"], f"case 3: seeds={seeds!r}"
    checks += 1

    # Case 4: mode-2 from a list vertical.
    seeds, mode = resolve_seeds({"seed_terms": [], "vertical": ["crm", "sales automation"]})
    assert mode == "vertical", f"case 4: mode={mode!r}"
    assert seeds == ["crm", "sales automation"], f"case 4: seeds={seeds!r}"
    checks += 1

    # Case 5: neither set -> SystemExit.
    raised = False
    try:
        resolve_seeds({"seed_terms": [], "vertical": ""})
    except SystemExit:
        raised = True
    assert raised, "case 5: expected SystemExit when neither field is set"
    checks += 1

    # Case 6 (regression): seed_terms given as a bare STRING (misconfiguration)
    # must be forgiven as a single seed, NOT iterated character-by-character.
    seeds, mode = resolve_seeds({"seed_terms": "crm software", "vertical": ""})
    assert mode == "seed_terms", f"case 6: mode={mode!r}"
    assert seeds == ["crm software"], f"case 6: seeds={seeds!r}"
    checks += 1

    # Case 7 (regression): all-empty seed_terms falls THROUGH to vertical.
    seeds, mode = resolve_seeds({"seed_terms": ["", "  "], "vertical": "fallback market"})
    assert mode == "vertical", f"case 7: mode={mode!r}"
    assert seeds == ["fallback market"], f"case 7: seeds={seeds!r}"
    checks += 1

    print(f"OK: {checks} checks passed")


if __name__ == "__main__":
    _selftest()
