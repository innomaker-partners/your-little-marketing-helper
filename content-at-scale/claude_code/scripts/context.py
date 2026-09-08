#!/usr/bin/env python3
"""The operating context for a content-at-scale run.

Why this is a script and not a skill: a skill cannot
enforce serialization. Concurrent subagents each follow instructions
independently, so two producers appending at the same moment silently lose one
update and leave a context that looks complete and is not. The DECISION that a
finding is worth recording is judgment and belongs to a model. The WRITE is
mechanical and belongs here.

Python 3, standard library only.

Three properties this file exists to guarantee:

  * APPEND-ONLY. An entry's text is never rewritten and never
    deleted. State moves forward - staged to promoted or dropped - and every
    move is recorded in the entry's own history. Nothing that was once true
    of the run becomes unfindable later. A late finding that could overwrite
    ground truth would invalidate content already produced and require a
    regeneration policy nobody has designed.

  * PROVENANCE ON EVERY ENTRY. Without labels, a finding the
    pipeline wrote at runtime is indistinguishable from user-asserted ground
    truth on the next read, and fact-check ends up validating content against
    material the pipeline itself produced - a self-confirming loop that grows
    more confident each pass while drifting further from any source.

  * CONTRADICTIONS GO TO A HUMAN. Never auto-resolved. A
    pipeline that quietly picks a winner between two conflicting facts is
    exactly the failure this tool exists to prevent.

WHAT THIS SCRIPT CANNOT DO, stated plainly rather than implied away:
it cannot DETECT that two statements contradict each other. That is semantic
judgment and there is no deterministic test for it. A caller declares a
contradiction with --contradicts; what this script guarantees is everything
that follows from the declaration - the flag reaches the run's single human
queue, and the contradicting entry cannot be promoted until a person rules on
it. Do not write this up as contradiction detection. It is contradiction
enforcement.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manifest as M  # noqa: E402

# Version 3 adds web-source provenance on `found` entries (source_class,
# source_date, verbatim_quote, volatility, corroborating, primary_attempted,
# audit). Version 2 adds `quotes` on every entry and a run-level `redactions`
# map. Older stores are upgraded in memory on load and written back on the next
# mutation; every upgrade is purely additive, so nothing recorded is lost.
SCHEMA_VERSION = 3
CONTEXT_NAME = "context.json"
LOCK_NAME = "context.lock"

# A quote span: a path, then a line or an inclusive line range. Both spellings are
# accepted because source-intake/SKILL.md section 6 has always documented the `#L`
# form to agents, and a mechanism that rejects the format its own instructions ask
# for would fail in the field for a reason nobody could see from the code.
#   sources/a.md:40-121   sources/a.md:538   sources/a.md#L40-L121   sources/a.md#L538
_SPAN_RE = re.compile(
    r"^(?P<path>.+?)(?::|#L)(?P<start>\d+)(?:-(?:L)?(?P<end>\d+))?$"
)

# The order is the trust order, most trusted first.
LABELS = ("given", "ruled", "sourced", "found", "inferred")

# Asserted by a person. An adversarial gate does not challenge these, and
# they cannot be filed as contradicting anything - they are how a
# contradiction gets resolved.
AUTHORITATIVE = ("given", "ruled")

# Everything that arrives through intake is live immediately. Staging applies
# to RUNTIME findings, not intake material: source-intake content has
# already been through segmentation, an adversarial challenge and the user's
# bulk confirmation before it ever gets here. Staging it would
# hide the run's own source material from every stage that needs it.
INTAKE_MADE = ("given", "ruled", "sourced")

# Produced by the pipeline at runtime. Staged on discovery, promoted only on
# confirmation - so an in-flight piece can read them knowing
# they are unconfirmed, while ground truth stays clean.
PIPELINE_MADE = ("found", "inferred")

# Web-source provenance (schema 3). A `found` entry must trace to a source with a
# class and a date, and carry the verbatim words that support it. Undated,
# secondary-sourced findings can look validated while resting on nothing, so
# provenance is enforced structurally at the CLI (the only path an agent uses),
# not left to a prompt the agent can talk itself out of.
SOURCE_CLASSES = ("primary", "secondary-corroborated")
VOLATILITY = ("durable", "volatile")

STAGED = "staged"
PROMOTED = "promoted"
DROPPED = "dropped"
RETRACTED = "retracted"

STATE_TRANSITIONS = {
    STAGED: {PROMOTED, DROPPED},
    # A promoted finding can be RETRACTED - pulled back when the auditor later
    # finds a clause in its text the source does not support. This
    # is append-only: the entry keeps its text and full history and simply stops
    # being visible to a writer, exactly like a drop. It is the recovery path for
    # embellishment found *after* promotion; the preferred path is still to catch
    # it while STAGED (staged -> dropped) so nothing unclean is ever promoted.
    PROMOTED: {RETRACTED},
    DROPPED: set(),
    RETRACTED: set(),
}


class ContextError(Exception):
    pass


def _norm(text: str) -> str:
    """Normalised form used only for duplicate detection."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def group_of(scope: str) -> str:
    """'g2-p4' -> 'g2'.  'g2' -> 'g2'.  'global' -> 'global'."""
    if scope == "global":
        return "global"
    return scope.split("-")[0]


def stack_for(scope: str) -> list:
    """The three-tier read stack for a scope.

    A piece sees global, then its group, then itself. Later tiers are more
    specific; none of them replaces an earlier one, because nothing here is
    ever overwritten.
    """
    if scope == "global":
        return ["global"]
    g = group_of(scope)
    return ["global", g] if g == scope else ["global", g, scope]


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


def create(run_dir: str) -> dict:
    run_dir = os.path.abspath(run_dir)
    path = os.path.join(run_dir, CONTEXT_NAME)
    if not os.path.exists(os.path.join(run_dir, M.MANIFEST_NAME)):
        raise ContextError(
            f"No manifest at {run_dir}. The run manifest defines the groups "
            "and pieces that context entries are scoped to, so it comes first."
        )
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_dir": run_dir,
        "created": M._now(),
        "updated": M._now(),
        "next_id": 1,
        "entries": [],
        "redactions": {},
    }
    with M.Lock(run_dir, LOCK_NAME):
        if os.path.exists(path):
            raise ContextError(f"A context already exists at {path}.")
        M.write_atomic(run_dir, CONTEXT_NAME, data)
    return data


def load(run_dir: str) -> dict:
    run_dir = os.path.abspath(run_dir)
    path = os.path.join(run_dir, CONTEXT_NAME)
    if not os.path.exists(path):
        raise ContextError(f"No operating context at {path}.")
    with open(path) as fh:
        data = json.load(fh)
    version = data.get("schema_version")
    if version not in (1, 2, SCHEMA_VERSION):
        raise ContextError(
            f"Context schema version {version}, "
            f"this script speaks {SCHEMA_VERSION}."
        )
    if version in (1, 2):
        # Purely additive upgrade, so code downstream never has to ask which
        # vintage it holds. v1 predates verbatim quotes and the redaction map;
        # v1 and v2 both predate web-source provenance.
        if version == 1:
            data.setdefault("redactions", {})
            for e in data["entries"]:
                e.setdefault("quotes", [])
        for e in data["entries"]:
            e.setdefault("source_class", None)
            e.setdefault("source_date", None)
            e.setdefault("verbatim_quote", None)
            e.setdefault("volatility", None)
            e.setdefault("corroborating", [])
            e.setdefault("primary_attempted", None)
            e.setdefault("audit",
                         {"recency_ok": None, "cross_checked": None, "by": None})
        data["schema_version"] = SCHEMA_VERSION
    return data


def _valid_scopes(run_dir: str) -> set:
    m = M.load(run_dir)
    scopes = {"global"}
    for g in m["groups"]:
        scopes.add(g["id"])
        for p in g["pieces"]:
            scopes.add(p["id"])
    return scopes


def by_id(data: dict, entry_id: str) -> dict:
    for e in data["entries"]:
        if e["id"] == entry_id:
            return e
    raise ContextError(f"No such entry: {entry_id}")


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------


def set_redaction(run_dir: str, frm: str, to: str) -> dict:
    """Register a replacement applied to every quote at store time.

    Store time rather than render time, deliberately. Redacting before any
    producing agent could see the material works because it is structural.
    A store holding raw speech and cleaning it on the way out is one forgotten flag away
    from a leak, and the leaked copy is the only copy.
    """
    if not frm or not frm.strip():
        raise ContextError("A redaction needs something to replace.")
    if not to or not to.strip():
        raise ContextError(
            "A redaction needs a replacement. Deleting a name silently would leave "
            "a sentence that reads as though nobody said it."
        )
    with M.Lock(run_dir, LOCK_NAME):
        data = load(run_dir)
        data["redactions"][frm] = to
        data["updated"] = M._now()
        M.write_atomic(run_dir, CONTEXT_NAME, data)
    return data["redactions"]


def _redact(text: str, redactions: dict) -> str:
    # Longest first, so a longer name is not half-replaced by a shorter one that
    # happens to be a substring of it.
    for frm in sorted(redactions, key=len, reverse=True):
        text = text.replace(frm, redactions[frm])
    return text


def _find_verbatim(region: str, want: str):
    """The substring of `region` matching `want`, tolerating only whitespace
    differences, or None if `want` is not a contiguous passage of `region`.

    Why this exists: a source line can be a single very long paragraph holding
    several distinct points. Storing the whole line (the only option before this)
    put the same mixed block into every piece that cited the line, so two pieces
    citing the same line got identical text - a distinctness failure.
    A caller can now name the exact stretch it wants; this locates it.

    Why whitespace-tolerant: the caller pastes a passage read from the source,
    and a stray double space or a line-wrap that differs from the stored file
    must not reject a genuine quote. Why it still returns `region`'s own bytes:
    the stored quote must be the source's real text, never the caller's reflow -
    the whole point of the field is that it is verbatim FROM the file.
    """
    # Exact match first: the common case, and unambiguous.
    i = region.find(want)
    if i != -1:
        return region[i:i + len(want)]
    # Whitespace-tolerant: collapse each run of whitespace to one space,
    # remembering where each normalised character came from so a match can be
    # mapped back to the region's original bytes.
    norm = []
    src = []
    prev_ws = False
    for k, ch in enumerate(region):
        if ch.isspace():
            if prev_ws:
                continue
            norm.append(" ")
            src.append(k)
            prev_ws = True
        else:
            norm.append(ch)
            src.append(k)
            prev_ws = False
    want_s = " ".join(want.split())
    if not want_s:
        return None
    j = "".join(norm).find(want_s)
    if j == -1:
        return None
    return region[src[j]:src[j + len(want_s) - 1] + 1]


def _read_span(spec: str, redactions: dict, want: str = None) -> dict:
    """Resolve `path:start[-end]` to its verbatim text, redacted.

    The text is stored, not the pointer. The pointer is kept beside it for audit. This
    is the whole mechanism: a pipeline that records pointers of exactly this shape
    but never dereferences one leaves the material never reaching the writer.
    A pointer is an attribution. Only text is material.

    `want` (optional): the exact passage to store from within the cited span,
    instead of the whole line(s). It must be a contiguous stretch of the span's
    real text (whitespace differences tolerated) or the read is refused - the
    anti-fabrication guard, keyed to the cited place. This is how one long source
    line holding several points routes a DIFFERENT stretch to each piece.
    """
    m = _SPAN_RE.match(spec)
    if not m:
        raise ContextError(
            f"'{spec}' is not a source span. Write `path:line` or `path:start-end` - "
            "a bare path cannot say which part of the file you mean, and the point of "
            "a quote is that it is a specific passage."
        )
    path = m.group("path")
    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1:
        raise ContextError(f"'{spec}': line numbers start at 1.")
    if end < start:
        raise ContextError(f"'{spec}': the span ends before it starts.")
    if not os.path.exists(path):
        raise ContextError(
            f"'{spec}': no file at {path}. A quote whose source cannot be opened is "
            "the defect this field exists to remove, so it is refused at write time "
            "rather than discovered at read time."
        )
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    if end > len(lines):
        raise ContextError(
            f"'{spec}': the file has {len(lines)} lines."
        )
    region = "\n".join(lines[start - 1:end])
    if want is not None:
        matched = _find_verbatim(region, want)
        if matched is None:
            raise ContextError(
                f"'{spec}': the passage you asked to quote is not present verbatim in "
                "the cited line(s). It must be a contiguous stretch of the real text at "
                "this span (whitespace differences are tolerated). This refusal is the "
                "anti-fabrication guard: only words actually present at the cited place "
                "may be stored - a paraphrase or a passage from elsewhere is refused here "
                "rather than discovered downstream."
            )
        text = matched.strip()
    else:
        text = region.strip()
    return {"citation": spec, "text": _redact(text, redactions)}


def _check_shape(provenance, citation, found_by, contradicts, quote_from=None):
    if quote_from and provenance == "inferred":
        raise ContextError(
            "An 'inferred' entry must not carry a quote. Inferred means model-derived "
            "with no source, and a verbatim quote is a source. This mirrors the guard "
            "on citation below and exists for the same reason: 'inferred' is the label "
            "a gate attacks first, so it must not be able to borrow authority."
        )

    if provenance not in LABELS:
        raise ContextError(f"'{provenance}' is not a provenance label. Use one of {list(LABELS)}.")

    if provenance == "sourced" and not citation:
        raise ContextError(
            "A 'sourced' entry needs a citation. The whole point of the label "
            "is that a gate challenges the citation rather than the fact - "
            "with nothing to point at there is nothing to challenge."
        )
    if provenance == "found":
        if not citation:
            raise ContextError("A 'found' entry needs a citation.")
        if not found_by:
            raise ContextError(
                "A 'found' entry needs --found-by naming the stage that found "
                "it, so a later reader can tell what kind of look produced it."
            )
    if provenance == "inferred" and citation:
        raise ContextError(
            "An 'inferred' entry must not carry a citation - inferred means "
            "model-derived with no source. If there is a source, it is "
            "'found' or 'sourced'. This is the label a gate attacks first, so "
            "letting it borrow a citation would disarm the gate."
        )
    if provenance in AUTHORITATIVE and contradicts:
        raise ContextError(
            f"A '{provenance}' entry cannot be filed as contradicting "
            "something. Authoritative entries are how a contradiction gets "
            "RESOLVED - use the `rule` command."
        )


def _span_path(spec: str):
    """Return the file path portion of a `path:start-end` / `path#L..` span, or None."""
    m = _SPAN_RE.match(spec or "")
    return m.group("path") if m else None


def _voice_role_paths(run_dir: str) -> set:
    """Realpaths of sources the manifest registers with role 'voice'.

    A voice source feeds the voice anchor and must never be routed into a
    piece. This is read from the manifest source registry so the guard sits at
    the routing act (append) rather than depending on the orchestrator to
    remember which files were voice-only.
    """
    try:
        mdata = M.load(run_dir)
    except Exception:
        return set()
    return {s.get("path") for s in mdata.get("sources", [])
            if s.get("role") == "voice"}


def append(
    run_dir: str,
    text: str,
    provenance: str,
    scope: str,
    citation: str = None,
    found_by: str = None,
    contradicts: list = None,
    quote_from: list = None,
    quote_text: list = None,
    source_class: str = None,
    source_date: str = None,
    verbatim_quote: str = None,
    volatility: str = None,
    corroborating: list = None,
    primary_attempted: str = None,
) -> dict:
    """Add an entry. Returns the entry (or the existing one, if duplicate).

    `quote_text` (optional): pairs by position with `quote_from`. `quote_text[i]`
    is the exact passage to store from `quote_from[i]`'s span instead of the whole
    line; a missing or None entry stores the whole line (the prior behaviour, so
    existing callers are unchanged).
    """
    run_dir = os.path.abspath(run_dir)
    if not text or not text.strip():
        raise ContextError("An entry needs text.")
    contradicts = list(contradicts or [])
    quote_from = list(quote_from or [])
    quote_text = list(quote_text or [])
    corroborating = list(corroborating or [])
    _check_shape(provenance, citation, found_by, contradicts, quote_from)

    # Read the manifest BEFORE taking the context lock - see the lock-ordering
    # note on manifest.Lock.
    valid = _valid_scopes(run_dir)
    if scope not in valid:
        raise ContextError(
            f"Scope '{scope}' is not part of this run. Valid scopes are "
            "'global', a group id, or a piece id — scope is an attribute of "
            "every item, but it has to name something real."
        )

    # Role guard: a source declared voice-only must never be routed into a piece.
    # A `sourced` entry cites a source file; if the manifest registers that file
    # with role 'voice', routing it as content is the exact collapse the role
    # field exists to prevent — a voice source silently becoming content.
    if provenance == "sourced":
        voice_paths = _voice_role_paths(run_dir)
        if voice_paths:
            for spec in [s for s in ([citation] + quote_from) if s]:
                p = _span_path(spec)
                if p and os.path.realpath(p) in voice_paths:
                    raise ContextError(
                        f"Source {p!r} is registered with role 'voice' and cannot be "
                        "routed into a piece. A voice source feeds the voice anchor "
                        "(voice.py record), not the segment map. If this file is also "
                        "a content source, register it with role 'both'."
                    )

    result = {}

    def _apply(data):
        for e in data["entries"]:
            if e["scope"] == scope and _norm(e["text"]) == _norm(text):
                # Fifteen pieces running in parallel will rediscover the same
                # thing. Returning the existing entry keeps the context from
                # bloating without discarding anything.
                result.update(e)
                result["_duplicate_of"] = e["id"]
                return
        # Resolved here rather than before the lock because the redaction map is
        # part of the context, and a quote must never be built from a stale one.
        quotes = [
            _read_span(s, data.get("redactions", {}),
                       want=(quote_text[i] if i < len(quote_text) else None))
            for i, s in enumerate(quote_from)
        ]
        for cid in contradicts:
            by_id(data, cid)  # raises if unknown

        state = PROMOTED if provenance in INTAKE_MADE else STAGED
        entry = {
            "id": f"e{data['next_id']:04d}",
            "text": text.strip(),
            "provenance": provenance,
            "scope": scope,
            "discovered_scope": scope,
            "state": state,
            "citation": citation,
            "quotes": quotes,
            "source_class": source_class,
            "source_date": source_date,
            # Redacted at store time, like span quotes, so a price or a
            # never-to-be-said detail cannot enter through a web quote.
            "verbatim_quote": (_redact(verbatim_quote, data.get("redactions", {}))
                               if verbatim_quote else None),
            "volatility": volatility,
            "corroborating": list(corroborating),
            "primary_attempted": primary_attempted,
            "audit": {"recency_ok": None, "cross_checked": None, "by": None},
            "found_by": found_by,
            "contradicts": contradicts,
            "resolves": [],
            "created": M._now(),
            "history": [{"state": state, "at": M._now(), "detail": "created"}],
        }
        data["next_id"] += 1
        data["entries"].append(entry)
        result.update(entry)

    with M.Lock(run_dir, LOCK_NAME):
        data = load(run_dir)
        _apply(data)
        data["updated"] = M._now()
        M.write_atomic(run_dir, CONTEXT_NAME, data)

        # Still inside the context lock, take the manifest lock to file the
        # flag. One human queue for the whole run - a contradiction that only
        # existed in this file would be a second place to look, and the one
        # nobody checks.
        #
        # This runs on the duplicate path too, and the filing is keyed and
        # idempotent. The two writes cannot be made atomic across two files,
        # so the recovery story is that a retry heals: if the process died
        # after the context write and before this one, calling append again
        # with the same text files the missing flag instead of short-circuiting
        # on the duplicate and leaving a real contradiction invisible.
        _ensure_flag(run_dir, result)
    return result


def _ensure_flag(run_dir: str, entry: dict) -> None:
    """File the human-queue flag for an entry's contradictions, if missing."""
    if not entry.get("contradicts"):
        return
    M.update(
        run_dir,
        lambda m: M.add_flag(
            m,
            entry["scope"],
            "contradiction",
            f"{entry['id']} contradicts {', '.join(entry['contradicts'])}: {entry['text']}",
            key=f"contradiction:{entry['id']}",
            refs=[entry["id"], *entry["contradicts"]],
        ),
    )


def reconcile(run_dir: str) -> dict:
    """Repair a context and manifest that disagree, and report what it did.

    The context and the manifest are two files. A write to both cannot be one
    atomic act, so a process that dies between them leaves them disagreeing.
    Both directions are handled, and neither one makes a judgment a human has
    not already made:

      * an unresolved contradiction with no open flag  -> file the flag
      * an open flag whose contradiction has since been ruled on -> close it

    The second is not auto-resolution. It mirrors a ruling a
    person already made and that is already recorded in the context; the flag
    is just stale bookkeeping about it.

    A resuming orchestrator should run this before reading anything.
    """
    run_dir = os.path.abspath(run_dir)
    data = load(run_dir)
    report = {"flags_filed": [], "flags_closed": []}

    for e in data["entries"]:
        if e["contradicts"] and e["state"] == STAGED:
            before = len([f for f in M.load(run_dir)["flags"] if not f["resolved"]])
            _ensure_flag(run_dir, e)
            if len([f for f in M.load(run_dir)["flags"] if not f["resolved"]]) > before:
                report["flags_filed"].append(e["id"])

    settled = {e["id"] for e in data["entries"] if e["state"] == DROPPED}
    rulings = {r for e in data["entries"] for r in (e["resolves"] or [])}
    for ruling in [e for e in data["entries"] if e["resolves"]]:
        done = [r for r in ruling["resolves"] if r in settled]
        if done:
            def _close(m, d=done, by=ruling["id"]):
                if M.close_flags(m, d, by):
                    report["flags_closed"].append(by)
            M.update(run_dir, _close)
    del rulings
    return report


# --------------------------------------------------------------------------
# state changes
# --------------------------------------------------------------------------


def _set_state(data: dict, entry: dict, state: str, detail: str,
               by_ruling: bool = False) -> None:
    # A person ruling on a contradiction may retire a PROMOTED entry, which no
    # automatic path can do. Without it, a run where the ground truth turns
    # out to be the wrong side of a contradiction has nowhere to go: the
    # ruling gets added, the superseded assertion stays live, and the rendered
    # context shows two authoritative statements that disagree with no
    # indication which one won. That is worse than either fact alone.
    #
    # This is not the pipeline picking a winner - it is recording
    # that a human did, and the retired entry keeps its text and its full
    # history.
    if by_ruling and entry["state"] == PROMOTED and state == DROPPED:
        pass
    elif state not in STATE_TRANSITIONS[entry["state"]]:
        raise ContextError(
            f"{entry['id']}: cannot go {entry['state']} -> {state}. "
            f"Allowed: {sorted(STATE_TRANSITIONS[entry['state']]) or 'none'}"
        )
    entry["state"] = state
    entry["history"].append({"state": state, "at": M._now(), "detail": detail})


def promote(run_dir: str, entry_id: str, detail: str = "confirmed") -> dict:
    """Promote a confirmed finding.

    The destination tier is an intake parameter, not a rule of the design
    - it is read from the manifest, never chosen here. The trade
    the user made at intake: promoting to global means every group benefits
    but all pieces become coupled through it; staying at group level preserves
    independence but lets a later group re-research what an earlier one
    already established.
    """
    run_dir = os.path.abspath(run_dir)
    tier = (M.load(run_dir).get("parameters") or {}).get("promotion_tier")
    if tier not in ("global", "group"):
        raise ContextError(
            "The manifest has no usable parameters.promotion_tier "
            f"({tier!r}). It is asked at intake; do not guess a "
            "default here."
        )
    out = {}

    def _apply(data):
        e = by_id(data, entry_id)
        if e["provenance"] not in PIPELINE_MADE:
            raise ContextError(
                f"{entry_id} is '{e['provenance']}' and is already "
                "authoritative. Promotion is for pipeline-made findings."
            )
        unresolved = [c for c in e["contradicts"] if by_id(data, c)["state"] != DROPPED]
        if unresolved:
            raise ContextError(
                f"{entry_id} contradicts {unresolved} and cannot be promoted "
                "while that is open. A person rules on it; the "
                "pipeline does not pick a winner."
            )
        # A volatile finding - a version, a price, a product status, any
        # "current X" - cannot promote until the auditor has recorded a positive
        # recency-and-cross-check verdict. A stale "current X" claim can be
        # promoted when nothing checks whether the source is still current;
        # this is that check, and it is a gate rather than a prompt for
        # the same reason the rest of provenance is.
        if e.get("volatility") == "volatile":
            au = e.get("audit") or {}
            if not (au.get("recency_ok") and au.get("cross_checked")):
                raise ContextError(
                    f"{entry_id} is a volatile finding and cannot be promoted "
                    "until the auditor records a positive recency verdict: "
                    "context.py audit --id "
                    f"{entry_id} --recency-ok true --cross-checked true --by <stage>. "
                    "Volatile facts go stale; this gate is why a stale one cannot "
                    "reach the writer as ground truth."
                )
        _set_state(data, e, PROMOTED, detail)
        # Scope may widen on promotion. discovered_scope is kept so the trail
        # back to where it was found is never lost.
        e["scope"] = "global" if tier == "global" else group_of(e["discovered_scope"])
        out.update(e)

    _update(run_dir, _apply)
    return out


def drop(run_dir: str, entry_id: str, detail: str = "not confirmed") -> dict:
    out = {}

    def _apply(data):
        _set_state(data, by_id(data, entry_id), DROPPED, detail)
        out.update(by_id(data, entry_id))

    _update(run_dir, _apply)
    return out


def retract(run_dir: str, entry_id: str, detail: str = "retracted",
            superseded_by: str = None) -> dict:
    """Pull back a PROMOTED finding whose text the source does not fully support.
    Only a promoted entry can be retracted - to reject something
    before promotion, drop it while it is still staged. Append-only: the entry
    keeps its text and history and disappears from `read`, like a drop. An
    optional `superseded_by` records the clean re-stage that replaces it.
    """
    out = {}

    def _apply(data):
        e = by_id(data, entry_id)
        if e["state"] != PROMOTED:
            raise ContextError(
                f"{entry_id}: only a promoted finding can be retracted "
                f"(state is {e['state']}). To reject a staged finding, drop it."
            )
        if superseded_by is not None:
            by_id(data, superseded_by)  # raises if the replacement id is unknown
            e["superseded_by"] = superseded_by
        _set_state(data, e, RETRACTED, detail)
        out.update(e)

    _update(run_dir, _apply)
    return out


def audit(run_dir: str, entry_id: str, recency_ok: bool, cross_checked: bool,
          by: str) -> dict:
    """Record the provenance auditor's recency / cross-check verdict on a finding.

    Kept separate from promote because the verdict is a distinct act the auditor
    performs, and promote() reads it back as a gate - a volatile finding cannot
    promote without a positive verdict here. Only a pipeline-made finding can be
    audited; there is nothing to audit on intake-authored material.
    """
    out = {}

    def _apply(data):
        e = by_id(data, entry_id)
        if e["provenance"] not in PIPELINE_MADE:
            raise ContextError(
                f"{entry_id} is '{e['provenance']}', not a pipeline finding; "
                "there is nothing for the auditor to record a verdict on."
            )
        e["audit"] = {"recency_ok": bool(recency_ok),
                      "cross_checked": bool(cross_checked), "by": by}
        e.setdefault("history", []).append({
            "state": e["state"], "at": M._now(),
            "detail": f"audited by {by}: recency_ok={bool(recency_ok)}, "
                      f"cross_checked={bool(cross_checked)}"})
        out.update(e)

    _update(run_dir, _apply)
    return out


def rule(run_dir: str, text: str, scope: str, resolves: list) -> dict:
    """Record a person's ruling on a flagged contradiction.

    `ruled` stays a separate label from `given` so it remains auditable which
    calls the pipeline could not make for itself.

    This creates a NEW entry. It does not edit either side of the
    contradiction, because nothing here is ever rewritten - the losing entry
    is dropped, which leaves it readable with its whole history intact.
    """
    run_dir = os.path.abspath(run_dir)
    if not resolves:
        raise ContextError("A ruling has to say which entries it resolves.")

    # `resolves` names the entries this ruling RETIRES - the side that lost.
    # Validate before writing anything, because the failure this replaces was
    # silent: naming the wrong side used to close the flag while leaving the
    # finding staged and unpromotable, with no error and no way back.
    existing = load(run_dir)
    for rid in resolves:
        by_id(existing, rid)  # raises on an unknown id

    entry = append(run_dir, text, "ruled", scope)

    def _apply(data):
        e = by_id(data, entry["id"])
        e["resolves"] = list(resolves)
        for rid in resolves:
            if rid == e["id"]:
                raise ContextError("A ruling cannot resolve itself.")
            target = by_id(data, rid)
            if target["state"] == DROPPED:
                continue  # already settled; re-running a ruling is harmless
            _set_state(data, target, DROPPED,
                       f"superseded by ruling {e['id']}", by_ruling=True)

    _update(run_dir, _apply)

    # Close the human queue item, now that a human has actually closed it.
    # Matching is on the flag's stored refs - a flag is about both sides of a
    # contradiction, so retiring either side settles it.
    M.update(run_dir, lambda m: M.close_flags(m, resolves, entry["id"]))
    return by_id(load(run_dir), entry["id"])


def _update(run_dir: str, fn) -> dict:
    with M.Lock(run_dir, LOCK_NAME):
        data = load(run_dir)
        fn(data)
        data["updated"] = M._now()
        M.write_atomic(run_dir, CONTEXT_NAME, data)
    return data


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


def read(data: dict, scope: str, include_staged: bool = False) -> list:
    tiers = stack_for(scope)
    out = []
    for tier in tiers:
        for e in data["entries"]:
            if e["scope"] != tier:
                continue
            if e["state"] == PROMOTED or (include_staged and e["state"] == STAGED):
                out.append(e)
    return out


def render(data: dict, scope: str, include_staged: bool = False) -> str:
    """Markdown for a skill to read. Provenance is never omitted."""
    lines = [f"# Operating context for `{scope}`", ""]
    if include_staged:
        lines += [
            "> Entries marked UNCONFIRMED are staged findings: discovered "
            "during this run and not yet confirmed. Do not treat them as "
            "ground truth.",
            "",
        ]
    entries = read(data, scope, include_staged)
    if not entries:
        lines.append("_No entries._")
        return "\n".join(lines)

    for tier in stack_for(scope):
        tier_entries = [e for e in entries if e["scope"] == tier]
        if not tier_entries:
            continue
        lines += [f"## {tier}", ""]
        for e in tier_entries:
            tag = e["provenance"].upper()
            if e["state"] == STAGED:
                tag += " · UNCONFIRMED"
            lines.append(f"- **[{tag}]** {e['text']}")
            # The assertion is a judgment about the source; the quote is the source.
            # Both are rendered because they do different jobs - the assertion carries
            # the routing decision and any prohibition attached to it, the quote carries
            # the phrasing. Rendering only the assertion is what produced six pieces in
            # which 256 tool mentions became zero.
            for q in e.get("quotes", []):
                lines.append(f"  - VERBATIM from `{q['citation']}`:")
                for line in q["text"].split("\n"):
                    lines.append(f"    > {line}")
            if e["citation"]:
                lines.append(f"  - source: {e['citation']}")
            # Schema-3 web provenance. Captured and gated at research time, it was
            # not reaching the writer: render only emitted the assertion, the
            # file-span quotes and the citation, so source class, date, the
            # supporting quote and volatility - the whole point of the
            # provenance-first research stage - died at this handoff. Surface them
            # so a producing agent can attribute a load-bearing fact and knows not
            # to state a volatile figure more precisely than its source. Guarded on
            # presence: entries without these fields (given/inferred, pre-schema-3)
            # render exactly as before.
            if e.get("source_class"):
                lines.append(f"  - source class: {e['source_class']}")
            if e.get("source_date"):
                lines.append(f"  - source date: {e['source_date']}")
            if e.get("verbatim_quote"):
                lines.append("  - supporting quote from source:")
                for line in str(e["verbatim_quote"]).split("\n"):
                    lines.append(f"    > {line}")
            if e.get("volatility"):
                note = " - verify freshness before publishing" if e["volatility"] == "volatile" else ""
                lines.append(f"  - volatility: {e['volatility']}{note}")
            if e["found_by"]:
                lines.append(f"  - found by: {e['found_by']}")
            if e["contradicts"]:
                lines.append(f"  - CONTRADICTS {', '.join(e['contradicts'])} - awaiting a ruling")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="content-at-scale operating context")
    ap.add_argument("--run-dir", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create an empty context for an existing run")

    p = sub.add_parser("append", help="add an entry")
    p.add_argument("--text", required=True)
    p.add_argument("--provenance", required=True, choices=LABELS)
    p.add_argument("--scope", required=True)
    p.add_argument("--citation")
    p.add_argument("--quote-from", nargs="*", default=[], metavar="PATH:START-END",
                   help="source span(s) to store verbatim beside the assertion, e.g. "
                        "sources/a.md:40-121. Repeatable. The text is copied into the "
                        "entry, not referenced - a pointer is an attribution, only text "
                        "is material. By default the whole cited line(s) is stored; pair "
                        "with --quote-text to store only a chosen stretch.")
    p.add_argument("--quote-text", action="append", default=[], metavar="PASSAGE",
                   help="the exact passage to store from the paired --quote-from span, "
                        "instead of the whole line. The Nth --quote-text pairs with the "
                        "Nth --quote-from. Whitespace differences are tolerated, but the "
                        "passage must be a contiguous stretch of the cited line(s) or the "
                        "append is refused. Use this when one source line holds more than "
                        "one point, so each piece gets its own stretch rather than the "
                        "whole mixed paragraph.")
    p.add_argument("--found-by")
    p.add_argument("--contradicts", nargs="*", default=[])
    p.add_argument("--source-class", choices=SOURCE_CLASSES,
                   help="provenance class for a 'found' entry (required for found)")
    p.add_argument("--source-date",
                   help="the source's publication date or page_age (required for found)")
    p.add_argument("--quote", dest="verbatim_quote",
                   help="verbatim sentence(s) from the source supporting a 'found' claim "
                        "(required for found)")
    p.add_argument("--volatility", choices=VOLATILITY,
                   help="durable, or volatile (versions/prices/status/'current X') "
                        "(required for found)")
    p.add_argument("--corroborating", action="append", default=[], metavar="URL|DATE",
                   help="an independent dated secondary 'url|date'; repeat; "
                        ">=2 required when source-class is secondary-corroborated")
    p.add_argument("--primary-attempted", dest="primary_attempted",
                   help="what happened reaching the primary "
                        "(required when source-class is secondary-corroborated)")

    p = sub.add_parser("redact", help="register a replacement applied to every quote")
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", required=True)

    p = sub.add_parser("promote", help="promote a confirmed finding")
    p.add_argument("--id", required=True)
    p.add_argument("--detail", default="confirmed")

    p = sub.add_parser("drop", help="drop an unconfirmed finding")
    p.add_argument("--id", required=True)
    p.add_argument("--detail", default="not confirmed")

    p = sub.add_parser("retract",
                       help="pull back a promoted finding the source does not fully support")
    p.add_argument("--id", required=True)
    p.add_argument("--detail", default="retracted")
    p.add_argument("--superseded-by", dest="superseded_by", default=None,
                   help="id of the clean re-stage that replaces this finding")

    p = sub.add_parser("audit",
                       help="record the provenance auditor's recency/cross-check verdict")
    p.add_argument("--id", required=True)
    p.add_argument("--recency-ok", dest="recency_ok", required=True,
                   choices=["true", "false"])
    p.add_argument("--cross-checked", dest="cross_checked", required=True,
                   choices=["true", "false"])
    p.add_argument("--by", required=True, help="the stage recording the verdict")

    p = sub.add_parser("rule", help="record a person's ruling on a contradiction")
    p.add_argument("--text", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--resolves", nargs="+", required=True)

    sub.add_parser("reconcile", help="repair context/manifest disagreement after a crash")

    p = sub.add_parser("read", help="render the context for a scope")
    p.add_argument("--scope", required=True)
    p.add_argument("--include-staged", action="store_true")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "init":
            create(a.run_dir)
            print(f"context created at {a.run_dir}")
            return 0
        if a.cmd == "append":
            # A sourced entry with a citation must carry verbatim text so
            # fact-check has something concrete to challenge. Without it a general
            # observation can become an undetectable first-person claim. Enforced
            # here rather than in _check_shape so the Python API can still be called
            # without quote_from in existing code paths (e.g. tests that confirm
            # citation presence independently of verbatim text).
            if a.provenance == "sourced" and a.citation and not a.quote_from:
                print(
                    "error: a 'sourced' entry with a citation requires --quote-from.\n"
                    "Provide at least one source span so the claim can be traced to "
                    "specific words in the source. Example:\n"
                    "  --quote-from sources/transcript.md:40-121",
                    file=sys.stderr,
                )
                return 1
            if len(a.quote_text) > len(a.quote_from):
                print(
                    "error: more --quote-text passages than --quote-from spans. Each "
                    "--quote-text pairs with the --quote-from in the same position; a span "
                    "with no paired passage stores its whole line.",
                    file=sys.stderr,
                )
                return 1
            # A 'found' entry needs full web-source provenance. Enforced here at
            # the CLI (the only path an agent uses) rather than in _check_shape,
            # mirroring the sourced/quote-from guard above, so the Python API and
            # existing tests stay callable while an agent structurally cannot
            # stage an undated, secondary-sourced finding.
            if a.provenance == "found":
                missing = [flag for flag, val in (
                    ("--source-class", a.source_class),
                    ("--source-date", a.source_date),
                    ("--quote", a.verbatim_quote),
                    ("--volatility", a.volatility),
                ) if not val]
                if missing:
                    print(
                        "error: a 'found' entry needs full provenance; missing "
                        + ", ".join(missing) + ".\n"
                        "A finding must trace to a dated source and carry the exact "
                        "words that support it. Undated, secondary-sourced findings "
                        "can look validated while resting on nothing; this gate "
                        "is why that cannot happen.",
                        file=sys.stderr,
                    )
                    return 1
                if a.source_class == "secondary-corroborated":
                    if len(a.corroborating) < 2:
                        print(
                            "error: source-class 'secondary-corroborated' needs at least "
                            "two independent --corroborating 'url|date' sources (the "
                            "primary was unreachable, so two dated secondaries must "
                            "agree). A lone secondary is a HOLD, not a finding.",
                            file=sys.stderr,
                        )
                        return 1
                    if not a.primary_attempted:
                        print(
                            "error: 'secondary-corroborated' needs --primary-attempted "
                            "recording what happened reaching the primary.",
                            file=sys.stderr,
                        )
                        return 1
            e = append(a.run_dir, a.text, a.provenance, a.scope,
                       a.citation, a.found_by, a.contradicts, a.quote_from,
                       quote_text=a.quote_text,
                       source_class=a.source_class, source_date=a.source_date,
                       verbatim_quote=a.verbatim_quote, volatility=a.volatility,
                       corroborating=a.corroborating,
                       primary_attempted=a.primary_attempted)
            if e.get("_duplicate_of"):
                print(f"{e['id']} (already present, not duplicated)")
            else:
                q = f", {len(e['quotes'])} quote(s)" if e["quotes"] else ""
                print(f"{e['id']} {e['state']}{q}")
            return 0
        if a.cmd == "redact":
            r = set_redaction(a.run_dir, a.frm, a.to)
            print(f"redaction registered; {len(r)} in force")
            return 0
        if a.cmd == "promote":
            print(f"{promote(a.run_dir, a.id, a.detail)['id']} promoted")
            return 0
        if a.cmd == "drop":
            print(f"{drop(a.run_dir, a.id, a.detail)['id']} dropped")
            return 0
        if a.cmd == "retract":
            r = retract(a.run_dir, a.id, a.detail, a.superseded_by)
            sup = f" (superseded by {r['superseded_by']})" if r.get("superseded_by") else ""
            print(f"{r['id']} retracted{sup}")
            return 0
        if a.cmd == "audit":
            r = audit(a.run_dir, a.id, a.recency_ok == "true",
                      a.cross_checked == "true", a.by)
            print(f"{r['id']} audited: recency_ok={r['audit']['recency_ok']}, "
                  f"cross_checked={r['audit']['cross_checked']}")
            return 0
        if a.cmd == "rule":
            print(f"{rule(a.run_dir, a.text, a.scope, a.resolves)['id']} ruled")
            return 0
        if a.cmd == "reconcile":
            r = reconcile(a.run_dir)
            print(f"flags filed: {r['flags_filed'] or 'none'}; "
                  f"flags closed by rulings: {r['flags_closed'] or 'none'}")
            return 0
        if a.cmd == "read":
            data = load(a.run_dir)
            if a.json:
                print(json.dumps(read(data, a.scope, a.include_staged), indent=2))
            else:
                print(render(data, a.scope, a.include_staged))
            return 0
    except (ContextError, M.ManifestError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
