#!/usr/bin/env python3
"""Voice reference for a content-at-scale run.

A run can select a voice anchor at intake and then lose it: the producing
dispatch never receives it. This module closes that gap. It records
the full text of the active voice reference once, at the start of a run, and
makes it available to every producing dispatch without re-reading anything
outside the run directory.

Two kinds of reference are supported:

  * user material  — text the user supplied (their own writing, a company
                     style guide, a sample piece). Stored exactly as given.
  * shipped profile — one of the voice profiles the plugin ships. Resolved by
                      short name to the full markdown document and stored in
                      its entirety — not summarised, not reduced to adjectives.

Why full text, not a path: a path that is never read is indistinguishable in
every record the run keeps from one that was. The emitted brief must contain
the voice text, not a pointer to it.

Python 3, standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

# Voice profile markdown files live under voice/ beside the scripts directory.
# Resolved relative to this file so the plugin is self-contained and works
# identically on any machine — no reference to ~/.claude or any path outside
# the plugin tree.
_VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice")

# The voice reference is stored as a plain file in the run directory so the
# full text is recoverable from the run without reading anything outside it.
VOICE_REF_NAME = "voice_ref.md"

# Sidecar recording which anchor was used and what kind it was. Written beside
# VOICE_REF_NAME so the anti-slop checker can be given the right --profile
# without re-reading anything outside the run directory.
VOICE_SIDECAR_NAME = "voice_ref_anchor.json"

# The canonical anchor for a group whose voice IS its routed content — a source
# registered with role 'both' (a transcript that is voice and content at once).
# There is no separate document to emulate: the group's own verbatim words are
# already in each brief as routed material, and the voice instruction is to
# preserve them. Recording this as a named kind ('routed') stops each run's
# orchestrator from improvising the same instruction in slightly different words
# — the thin-instruction workaround made first-class.
ROUTED_ANCHOR_TEXT = (
    "The passages routed into this brief are the user's own verbatim words — "
    "their recorded speech, segmented to this piece. Write in that voice: "
    "preserve the sentence shape, the hedges, the idiom and the rhythm of those "
    "passages. Never smooth them into neutral business English — the phrasing is "
    "the asset. The voice for this piece is the routed material itself, not a "
    "separate reference you emulate at a distance."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VoiceError(Exception):
    pass


# Per-group voice references. A run can have more than one group (g1, g2, ...),
# and voice is a group-scoped attribute: a formal case-study voice and a loose
# LinkedIn voice in one run are two group-scoped entries, not one global
# compromise. A single shared voice_ref.md could not honour that — a two-group
# run either shared one anchor or lost one when the second was recorded over
# the first.
#
# A scope is a group id like 'g1' (or a piece id like 'g1-p2'). It goes into a
# filename, so it is validated to the same character set the rest of the run
# uses — never a path fragment.
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_scope(scope):
    if scope is not None and not _SCOPE_RE.match(scope):
        raise VoiceError(
            f"Invalid voice scope {scope!r}. A scope is a group or piece id "
            "like 'g1' or 'g1-p2' — letters, digits, '-' or '_' only."
        )


def _ref_name(scope):
    return VOICE_REF_NAME if scope is None else f"voice_ref_{scope}.md"


def _sidecar_name(scope):
    return VOICE_SIDECAR_NAME if scope is None else f"voice_ref_anchor_{scope}.json"


def _read_paths(run_dir, scope):
    """Resolve (ref_path, sidecar_path) for READING, honouring scope with a
    fallback to the run-level (unscoped) files.

    A scoped reference wins when it exists; otherwise the run-level files apply.
    This is what lets a run set one anchor for every group (record with no
    scope) OR override per group (record with --scope), and it keeps every run
    recorded before per-group existed working unchanged: emit(scope='g1') on
    such a run finds no voice_ref_g1.md and reads the run-level voice_ref.md.

    The pair is resolved at one level together — a scoped ref is returned with
    its scoped sidecar, the run-level ref with the run-level sidecar — so the
    sidecar and its reference text are always the pair written by one record()
    call.
    """
    if scope is not None:
        scoped_ref = os.path.join(run_dir, _ref_name(scope))
        if os.path.isfile(scoped_ref):
            return scoped_ref, os.path.join(run_dir, _sidecar_name(scope))
    return (os.path.join(run_dir, VOICE_REF_NAME),
            os.path.join(run_dir, VOICE_SIDECAR_NAME))


def _resolve_shipped(short_name: str) -> str:
    """Load a shipped voice profile by short name and return its full text.

    Short names correspond to markdown files shipped inside the plugin's own
    voice/ directory:
        'james_clear'  -> voice/voice_james_clear.md
        'ann_handley'  -> voice/voice_ann_handley.md

    Raises VoiceError if the name is not recognised.
    """
    candidate = os.path.join(_VOICE_DIR, f"voice_{short_name}.md")
    if not os.path.isfile(candidate):
        raise VoiceError(
            f"Unknown shipped profile {short_name!r}. Expected a file at "
            f"{candidate}. Known profiles: james_clear, ann_handley."
        )
    with open(candidate, encoding="utf-8") as f:
        return f.read()


def record(run_dir: str, kind: str, text: str, scope: str = None) -> None:
    """Record the active voice reference for a run, or for one group of it.

    Args:
        run_dir: The run directory (must exist).
        kind:    'user' for user-supplied material; 'profile' for a shipped
                 profile identified by short name.
        text:    For kind='user', the voice text itself. For kind='profile',
                 the short name (e.g. 'james_clear').
        scope:   None records the run-level reference (voice_ref.md). A group
                 id like 'g1' records that group's own reference
                 (voice_ref_g1.md), which a producing dispatch for that group
                 reads in preference to the run-level one. A run with two
                 groups that read differently records one per group; recording
                 g1 does not touch g2's reference.

    The reference is written to the scope's ref file inside run_dir. Calling
    record a second time for the same scope overwrites that scope's reference —
    each scope has one active voice reference, chosen once at intake.
    """
    run_dir = os.path.abspath(run_dir)
    _check_scope(scope)
    if kind == "user":
        content = text
    elif kind == "profile":
        content = _resolve_shipped(text)
    elif kind == "routed":
        # The voice is the routed content itself; the anchor is the canonical
        # preserve-the-phrasing instruction. `text` is ignored.
        content = ROUTED_ANCHOR_TEXT
    else:
        raise VoiceError(
            f"Unknown kind {kind!r}. Use 'user' for user-supplied material, "
            "'profile' for a shipped profile short name, or 'routed' for a group "
            "whose voice is its own routed content (a role='both' source)."
        )
    if not content or not content.strip():
        raise VoiceError("Voice reference text is empty.")
    ref_path = os.path.join(run_dir, _ref_name(scope))
    with open(ref_path, "w", encoding="utf-8") as f:
        f.write(content)
    # Sidecar records the anchor identity so the anti-slop checker can be
    # invoked with --profile without re-reading anything outside the run.
    # The digest fingerprints the reference text at record time so a later
    # change to it can be detected. NOTE: nothing reads text_sha256 today; it is
    # still written pending a decision on whether to drop it. recorded_anchor, the
    # remaining sidecar reader, does not use it.
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if kind in ("user", "routed"):
        # 'routed' and 'user' carry no shipped-profile name — the sidecar
        # records only kind, timestamp, and the text digest.
        sidecar: dict = {"kind": kind, "recorded": _now(), "text_sha256": digest}
    else:
        # text is the short name (e.g. 'james_clear') for kind='profile'.
        sidecar = {"kind": "profile", "name": text, "recorded": _now(),
                   "text_sha256": digest}
    sidecar_path = os.path.join(run_dir, _sidecar_name(scope))
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)


def emit(run_dir: str, scope: str = None) -> str:
    """Return the voice reference text for embedding into a producing brief.

    scope selects a group's own reference (e.g. 'g1'), falling back to the
    run-level reference when the group has none of its own — see _read_paths.

    Raises VoiceError if no reference has been recorded for this scope (and no
    run-level one to fall back to). The error is intentionally loud: a missing
    voice reference that returns empty text looks like "no voice needed" and is
    precisely the failure mode this module exists to prevent.
    """
    run_dir = os.path.abspath(run_dir)
    _check_scope(scope)
    ref_path, _ = _read_paths(run_dir, scope)
    if not os.path.isfile(ref_path):
        if scope is not None:
            expected = (
                f"expected {os.path.join(run_dir, _ref_name(scope))} "
                f"or the run-level {os.path.join(run_dir, VOICE_REF_NAME)}"
            )
        else:
            expected = f"expected {ref_path}"
        raise VoiceError(
            f"No voice reference recorded for scope {scope!r} ({expected}). "
            "Call 'record' at intake before dispatching any producing agent."
        )
    with open(ref_path, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        raise VoiceError(
            f"Voice reference file exists but is empty or whitespace-only ({ref_path}). "
            "The file was recorded but contains no text — possible causes: a truncated "
            "write, an interrupted run, or an external tool that overwrote the file. "
            "Re-record the voice reference before dispatching any producing agent."
        )
    return content


def recorded_anchor(run_dir: str, scope: str = None) -> "dict | None":
    """Return the recorded anchor's declared identity, or None if none exists.

    This returns the raw recorded identity — kind and, for a profile, its short
    name — so a caller can check the reference actually on disk against a
    SEPARATELY declared choice. That check is the whole point: collapsing a user
    sample or routed content to a bare 'none' cannot distinguish "a user sample
    was chosen" from "the chosen profile was lost and a user sample improvised
    in its place". recorded_anchor keeps the
    distinction by reporting exactly what was recorded.

    Returns:
        {"kind": "profile", "name": "<author>"} for a profile anchor,
        {"kind": "user"} or {"kind": "routed"} for those, or
        None when no sidecar exists for this scope (honouring the run-level
        fallback via _read_paths) or it cannot be read as a dict with a kind.

    This reads only the sidecar's recorded intent; it does not re-verify the
    reference text against its recorded digest.
    """
    run_dir = os.path.abspath(run_dir)
    _check_scope(scope)
    _, sidecar_path = _read_paths(run_dir, scope)
    if not os.path.isfile(sidecar_path):
        return None
    try:
        with open(sidecar_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("kind"):
        return None
    out = {"kind": data["kind"]}
    name = data.get("name")
    if name:
        out["name"] = name
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="content-at-scale voice reference")
    ap.add_argument("--run-dir", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # --scope names a group (e.g. 'g1') to record/read that group's own voice;
    # omit it for the run-level voice. Shared across the three subcommands.
    _scope_help = ("group id like 'g1' for a per-group voice; omit for the "
                   "run-level voice")

    p = sub.add_parser("record", help="record the active voice reference for a run")
    p.add_argument("--kind", required=True, choices=["user", "profile", "routed"])
    # For kind=user: the voice text itself. For kind=profile: the short name.
    p.add_argument("--text", help="voice text (--kind user)")
    p.add_argument("--name", help="shipped profile short name (--kind profile)")
    p.add_argument("--scope", help=_scope_help)

    pe = sub.add_parser("emit", help="print the voice reference text for a run")
    pe.add_argument("--scope", help=_scope_help)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "record":
            if a.kind == "user":
                if not a.text:
                    ap.error("--text is required for --kind user")
                record(a.run_dir, "user", a.text, scope=a.scope)
            elif a.kind == "routed":
                # No --text or --name: the anchor text is canonical.
                record(a.run_dir, "routed", "", scope=a.scope)
            else:
                if not a.name:
                    ap.error("--name is required for --kind profile")
                record(a.run_dir, "profile", a.name, scope=a.scope)
            where = f"scope {a.scope}" if a.scope else "run-level"
            print(f"voice reference recorded ({a.kind}, {where})")
            return 0
        if a.cmd == "emit":
            print(emit(a.run_dir, scope=a.scope), end="")
            return 0
    except VoiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
