#!/usr/bin/env python3
"""Run manifest for a content-at-scale run.

Why this exists: the orchestrator holds no run state
in its context. Everything a long run needs to know about itself lives in one
file on disk - groups, pieces, per-stage status, gate results, artifact
hashes, and the allocated search budget. The model does judgment; this file
does bookkeeping. That is what makes a run resumable after a failure or a
context reset rather than restartable from zero.

Python 3, standard library only.

Two things here are deliberate and should not be "simplified" away:

  * Every write is a locked read-modify-write followed by an atomic replace.
    Pieces run in parallel, so several agents update this file
    at once. An unserialized write silently loses an update and leaves a
    manifest that looks complete and is not - the same failure class the
    operating-context append avoids.

  * A piece carries BOTH the producing agent's stage results and a separate
    `verification` block written by the orchestrator. That is the
    producer/orchestrator split in data form: the producer self-runs the
    deterministic scripts so it can
    iterate in place, and the orchestrator independently recomputes on the
    final artifact and decides pass/fail. Collapsing the two into one status
    field would erase exactly the distinction that makes the check
    trustworthy - including the case of an agent that honestly ran the check,
    passed, then edited the text afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

# Schema 2 added the `anti_slop` piece stage between fact_check
# and de_slop. A schema-1 manifest has no such stage, so this script refuses to
# load one (load() checks the version) rather than operate on a shape it does
# not understand.
SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"
LOCK_NAME = "manifest.lock"

# Stage names. Piece stages are in pipeline order.
# `anti_slop` runs the deterministic checker on the verified text and writes the
# report de-slop consumes. It is a stage because
# everything the pipeline DOES is a stage; it is NOT a gate — it never fails on
# the checker's exit-3, it only records that the report was generated. It is not
# in VERIFY_CHECKS for the same reason fact_check and de_slop are not: there is
# no deterministic pass/fail to recompute, only "the report exists".
# spoken_to_written runs LAST, after the whole de-slop agent bracket
# (anti_slop -> de_slop -> claim_diff).
# claim_diff and anti_slop are tied to de_slop as ONE agent, the same way
# length and overlap are tied to produce; spoken is its own final agent that
# rewrites the de-slop bundle's output (final.md) into readable.md.  It gets NO
# gate of its own because its job is precisely to change the bytes claim_diff
# measured - a claim_diff after it would diff against a basis it deliberately
# broke - its whole job is to change the basis that diff was measured against.
# Its output integrity is held by its own anti-swap hash at
# delivery, not by a recomputable gate.
PIECE_STAGES = ("produce", "length", "overlap", "fact_check",
                "anti_slop", "de_slop", "claim_diff", "spoken_to_written")
GROUP_STAGES = ("research", "group_review")
RUN_STAGES = ("context_intake", "source_intake", "cross_group", "web_pass")

# Map from each PIECE_STAGE to the persistent output file(s) that stage
# produces under pieces/<piece_id>/.  Used by redo_stage to archive only
# the redone stage's own outputs (and those of downstream stages), never
# an upstream stage's files.  The filenames are the same ones hardcoded
# elsewhere in this module: deliver_pieces ships final.md,
# _check_deslop_prerequisites reads antislop.txt — the map is consistent
# with existing coupling, not new fragility.  Measurement-gate stages
# (length, overlap, claim_diff) produce no persistent output file.
STAGE_OUTPUTS: dict = {
    "produce":    ["draft.md"],      # draft.attempt*.md handled by glob in redo_stage
    "length":     [],
    "overlap":    [],
    "fact_check": ["verified.md"],
    "spoken_to_written": ["readable.md"],
    "anti_slop":  ["antislop.txt"],
    "de_slop":    ["final.md"],
    "claim_diff": [],
}

# Which piece stages a CLI caller may mark SKIPPED. SKIPPED records "this gate
# did not apply to this piece" -
# an honest statement for exactly one piece stage: `overlap`, when a piece was
# written from the intake alone and there is no source material to measure
# against (REFERENCE.md). For every other piece stage a skip is not "did not
# apply", it is the work being dodged while the record says done - the exact
# "said 12 stages, ran 8" failure this manifest exists to refuse.
#
# So the default is inverted: piece stages are NON-skippable, and only this
# whitelist may skip. Guarding stages one at a time leaves every unguarded
# stage open: a skippable-unless-guarded default makes each stage a silent
# door until someone remembers to guard it, so any stage added later is open
# by default. A whitelist closes the whole class at once and makes adding a
# legitimately-skippable stage a reviewed one-line change, not a silent door.
#
# Four piece stages are whitelisted, and they are skippable for two different
# honest reasons:
#
#   overlap  - the gate did not APPLY: a piece written from the intake alone has
#              no source material to measure against (unconditional).
#   spoken_to_written - the gate did not APPLY: this pass only runs when a
#              piece's content and voice come from the same "rambling" verbatim
#              source (a role=both transcript - the g2 case). For a piece whose
#              content is researched findings and whose voice is a separate
#              profile (the g1 case), there is no spoken grammar to repair, so
#              the stage is inapplicable and honestly skipped - the same "did not
#              apply" class as overlap. It is CONDITIONAL:
#              the condition is the source role, judged by the stage's own skill,
#              not a licence to skip a rambling-source piece. Because spoken now
#              runs LAST - after the whole de-slop bracket -
#              skipping it leaves no readable.md and the de-slop bundle's final.md
#              ships as-is; anti_slop and de_slop always read verified.md, never
#              readable.md, so a g1 skip changes nothing upstream.
#   de_slop  - the gate had NOTHING TO DO: a clean anti-slop report leaves
#              de-slop nothing to rewrite. CONDITIONAL - it may skip only once
#              anti_slop has passed, which its own precondition
#              (_check_deslop_prerequisites, fired on RUNNING and SKIPPED)
#              enforces. Membership here says "may be considered for a skip";
#              that precondition is the condition.
#   produce  - the PIECE WAS NOT MADE: skipping produce is the honest record of
#              a dropped piece (see test_voice
#              test_skipping_produce_needs_no_voice_reference). This is a
#              different meaning from the gate cases - it says the whole piece
#              was not written, not that one gate was inapplicable - but it is
#              equally honest, so produce stays skippable. The dodge to guard
#              against was never the skip itself; it was DELIVERING a non-produced
#              piece via a planted final.md. That is closed in deliver_pieces,
#              which delivers only pieces whose produce PASSED - so a
#              produce-skipped piece is correctly never handed over. Keeping
#              produce skippable here and blocking its delivery there preserves
#              the dropped-piece marker while closing the bypass at its root.
#
# Scope note: this whitelist governs PIECE stages only. Run stages
# (source_intake, context_intake) and group stages (research) are
# legitimately skippable and are not piece stages, so the
# guard - keyed on `stage in PIECE_STAGES` - never touches them. PIECE/GROUP/
# RUN stage names are disjoint, so the key is unambiguous.
SKIPPABLE_PIECE_STAGES = frozenset({"overlap", "spoken_to_written", "de_slop", "produce"})

# The orchestrator's independent recomputation. Only the
# deterministic checks can be recomputed - a model gate cannot be.
VERIFY_CHECKS = ("length", "overlap", "claim_diff")

# Run-level status. A field that accepts any string is not a status, and this
# one is how the orchestrator reports overall progress - so membership is
# checked. Deliberately NOT a transition graph: there is no fixed run-level
# lifecycle, and inventing one here would over-constrain a real run.
RUN_STATUSES = ("created", "running", "blocked", "complete", "abandoned")

# `barrier` exits 3 when the barrier is shut, matching the gate scripts: 0
# pass, 3 gate failed, 1 our own misuse, 2 left to argparse. One convention
# across every script an agent runs, so "3" never has to be looked up.
BARRIER_CLOSED = 3

# Hard cap on produce gate iterations, enforced at gate_runs reconciliation.
# A bound of 3 was established for analogous source-segmentation
# iteration; a documented failure mode (a writer that injects contrived
# vocabulary to beat the overlap gate loops to attempt4 rather than fixing
# the piece) extends that bound explicitly to the produce stage. A writer
# that grinds past 3 is not fixing a legitimate gap — it is gaming the gate.
# The orchestrator's reconciled gate_runs is the truthful count (from
# draft.attemptN.md snapshots on disk) and is the right enforcement point:
# attempts (orchestrator opens) is always 1 for a normal close and cannot
# catch iteration abuse.
PRODUCE_ATTEMPT_CAP = 3

PENDING = "pending"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
FLAGGED = "flagged"
SKIPPED = "skipped"

# Source polarity. `owned` means the user's own material; the overlap gate
# measures it as a FLOOR (high overlap is the goal, because sounding like
# the user is the purpose). `third_party` is someone else's material; the
# gate measures it as a CEILING (high overlap means the piece is lifted).
# The direction of the gate is opposite for each: a source with no declared
# polarity cannot be measured in any direction at all.
OWNED = "owned"
THIRD_PARTY = "third_party"
SOURCE_POLARITIES = (OWNED, THIRD_PARTY)

# Source role — independent of polarity. Polarity is the overlap gate's
# DIRECTION (owned floor / third_party ceiling); role is what the source is
# FOR:
#   content — routed into pieces as material (it appears in the segment map).
#             This is the historical default: every source registered before
#             roles existed was a content source, so add_source() defaults here
#             and old manifests read back as content with no migration.
#   voice   — feeds the voice anchor (voice.py); must NOT be routed into a
#             piece. context.py refuses a `sourced` entry that cites a voice
#             source.
#   both    — the transcript case: routed as content AND the voice reference.
# Making role explicit is what stops "every transcript is content" from
# creeping back: a voice-only source is declared and guarded, not left implicit
# in which mechanism an orchestrator happened to feed the file to.
VOICE_ROLE = "voice"
CONTENT_ROLE = "content"
BOTH_ROLE = "both"
SOURCE_ROLES = (VOICE_ROLE, CONTENT_ROLE, BOTH_ROLE)

# Allowed status transitions.
#
#   failed -> running   is the retry rule: a gate failure retries.
#   failed -> flagged   is the other half of the retry rule: repeated failure is
#                       read as an operating-context problem and goes to a
#                       human instead of looping forever.
#   flagged -> passed   is a human resolving it (the `ruled` case).
#   passed  -> nothing  because a passed stage is done. Re-running a check is
#                       not a transition, it is the orchestrator's separate
#                       verification block.
TRANSITIONS = {
    PENDING: {RUNNING, SKIPPED},
    RUNNING: {PASSED, FAILED, FLAGGED},
    FAILED: {RUNNING, FLAGGED},
    FLAGGED: {RUNNING, PASSED},
    PASSED: set(),
    SKIPPED: set(),
}


class ManifestError(Exception):
    """Anything wrong with a manifest or an operation on one."""


class InvalidTransition(ManifestError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_stage() -> dict:
    return {"status": PENDING, "attempts": 0, "gate_runs": None,
            "detail": None, "updated": None}


def _normalise_locked(value) -> dict:
    """Accept a list of locked-parameter keys or a pre-formed dict; return the
    dict form {key: {"at": <now>}}. None/empty -> {}. This is what lets intake
    write the natural `["length_bounds"]` while the manifest stores when each
    lock was taken."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    return {key: {"at": _now()} for key in value}


def _require_count(value, label: str) -> None:
    """A count must be a real non-negative integer.

    `bool` is a subclass of `int` in Python, so a stray True from a
    programmatically built spec would otherwise sail through as a search
    budget of 1 - a run that then dies at its first search with a message
    about exhausting its allocation rather than about a malformed spec.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{label} must be a non-negative integer, got {value!r}.")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# locking and atomic write
# --------------------------------------------------------------------------


class Lock:
    """Exclusive lock around a read-modify-write of a run file.

    Shared with the operating-context store (Phase 2) rather than duplicated
    there. Each store passes its own lock file name, so the manifest and the
    context do not contend. Anything that needs both must take the CONTEXT
    lock first and the manifest lock second - context.py is the only caller
    that nests them, and keeping that order is what stops the two from
    deadlocking against each other.

    Fails loudly on a platform without fcntl rather than degrading to no
    locking. A silently unlocked write is worse than a refusal: the run would
    appear to work and lose updates under concurrency, which is precisely the
    bug this class exists to prevent.
    """

    def __init__(self, run_dir: str, name: str = LOCK_NAME):
        if fcntl is None:
            raise ManifestError(
                "File locking is unavailable on this platform (no fcntl). "
                "content-at-scale runs pieces in parallel and cannot safely "
                "share a manifest without it. On Windows, run under WSL."
            )
        self.path = os.path.join(run_dir, name)
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
        return False


def write_atomic(run_dir: str, name: str, data: dict) -> None:
    """Write a run file so a reader never sees a half-written document."""
    path = os.path.join(run_dir, name)
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# create / load / update
# --------------------------------------------------------------------------


def _normalise_spec(spec: dict) -> list:
    """Turn a run spec into a list of groups.

    One code path, always at least one group. A run with no
    grouping is a run with a single group whose scope is global - not a
    second mode. Callers may pass either `groups` or a bare `pieces` list.
    """
    if "groups" in spec and "pieces" in spec:
        raise ManifestError(
            "Spec has both 'groups' and a top-level 'pieces'. Pass one: "
            "'pieces' for an ungrouped run, 'groups' for a grouped one."
        )
    if "groups" in spec:
        groups = spec["groups"]
        if not isinstance(groups, list) or not groups:
            raise ManifestError("'groups' must be a non-empty list.")
    else:
        pieces = spec.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise ManifestError("Spec needs 'groups' or a non-empty 'pieces'.")
        # An ungrouped run is one global group; it declares its voice choice
        # at the top level of the spec (there is no per-group block to carry it).
        groups = [{"name": "all", "scope": "global", "pieces": pieces,
                   "voice": spec.get("voice")}]

    out = []
    for gi, g in enumerate(groups, start=1):
        pieces = g.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise ManifestError(f"Group '{g.get('name', gi)}' has no pieces.")
        gid = f"g{gi}"
        for pi, p in enumerate(pieces, start=1):
            topic = p.get("topic") if isinstance(p, dict) else p
            if not isinstance(topic, str) or not topic.strip():
                raise ManifestError(
                    f"Piece {gid}-p{pi} has no usable topic ({topic!r}). Every "
                    "piece needs one - a producing agent given a null topic "
                    "writes something plausible about nothing, and the error "
                    "surfaces far downstream from the malformed spec."
                )
        out.append(
            {
                "id": gid,
                "name": g.get("name", gid),
                "scope": g.get("scope", "group"),
                "keyword_cluster": g.get("keyword_cluster"),
                # The group's DECLARED voice choice, carried from the spec and
                # checked at the produce gate against what voice.py actually
                # recorded. Optional here (a spec may omit it) but required to
                # START producing: the produce gate refuses a group that reaches
                # production with no declaration, because "a reference exists"
                # cannot tell a chosen anchor from an improvised one — the gap
                # that lets an improvised anchor pass for a chosen one. Shape,
                # when present:
                # {"kind": "profile", "name": "<profile>"} | {"kind": "routed"}
                # | {"kind": "user"}.
                "voice": g.get("voice"),
                "stages": {s: _new_stage() for s in GROUP_STAGES},
                "pieces": [
                    {
                        "id": f"{gid}-p{pi}",
                        "topic": (p.get("topic") if isinstance(p, dict) else p).strip(),
                        "brief": p.get("brief") if isinstance(p, dict) else None,
                        "artifact": None,
                        "stages": {s: _new_stage() for s in PIECE_STAGES},
                        "verification": {
                            "status": PENDING,
                            "checks": {c: None for c in VERIFY_CHECKS},
                            "updated": None,
                        },
                    }
                    for pi, p in enumerate(pieces, start=1)
                ],
            }
        )
    return out


def _build_budget(spec: dict, group_ids: list) -> dict:
    """Search budget as an allocated quantity, not an opportunistic spend.

    A session has a fixed WebSearch ceiling shared across
    the main conversation and every subagent. Without up-front allocation the
    research stages can consume nearly the whole ceiling before the optional
    end-of-run web pass runs at all - so the run would die at the final gate,
    after every piece was already produced. Allocating up front is what turns
    that into a refusal at intake instead.

    Known limit, stated rather than papered over: nothing exposes how many
    searches the session has ALREADY spent. This tracks our own spend against
    a ceiling we can read but cannot reconcile.
    """
    b = spec.get("search_budget") or {}
    ceiling = b.get("ceiling")
    if ceiling is None:
        raise ManifestError(
            "search_budget.ceiling is required. Read it from "
            "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION at intake; do not "
            "guess a default."
        )
    _require_count(ceiling, "search_budget.ceiling")

    reserved = dict(b.get("reserved") or {})
    valid = {"global", "web_pass", *group_ids}
    unknown = set(reserved) - valid
    if unknown:
        raise ManifestError(f"Unknown budget buckets: {sorted(unknown)}")
    for k, v in reserved.items():
        _require_count(v, f"Budget for '{k}'")

    total = sum(reserved.values())
    if total > ceiling:
        raise ManifestError(
            f"Allocated {total} searches against a ceiling of {ceiling}. "
            "Reduce the run, drop per-piece research, or raise "
            "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION."
        )
    return {
        "ceiling": ceiling,
        "reserved": reserved,
        "spent": {k: 0 for k in reserved},
        "unallocated": ceiling - total,
    }


def create(run_dir: str, spec: dict) -> dict:
    """Create a manifest. Refuses to overwrite an existing run.

    The existence check happens INSIDE the lock. It used to sit outside, which
    was a time-of-check/time-of-use race: two orchestrators starting against
    the same directory both saw no manifest, both passed the check, and the
    second silently overwrote the first. Sequential callers never reproduce
    it, which is why the original test missed it entirely.
    """
    os.makedirs(run_dir, exist_ok=True)
    run_dir = os.path.abspath(run_dir)
    path = os.path.join(run_dir, MANIFEST_NAME)

    # Validate before taking the lock - a bad spec should not block anyone.
    groups = _normalise_spec(spec)
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_id": spec.get("run_id") or _now().replace(":", "").replace("-", ""),
        "created": _now(),
        "updated": _now(),
        # The run records where it lives. The location is in the user's hands,
        # so there is no fixed place to look - a resumed run
        # is found by being told its directory, and this field is what lets us
        # notice a run that was copied or moved (see load()).
        "run_dir": run_dir,
        "status": "created",
        "parameters": spec.get("parameters") or {},
        "locked_parameters": _normalise_locked(spec.get("locked_parameters")),
        "param_overrides": [],
        "accepted_claims": [],
        "search_budget": _build_budget(spec, [g["id"] for g in groups]),
        "stages": {s: _new_stage() for s in RUN_STAGES},
        "groups": groups,
        "flags": [],
        "sources": [],
    }
    with Lock(run_dir):
        if os.path.exists(path):
            raise ManifestError(
                f"A manifest already exists at {path}. Resume it or choose "
                "another run directory; do not overwrite a run in flight."
            )
        write_atomic(run_dir, MANIFEST_NAME, data)
    return data


def load(run_dir: str, allow_moved: bool = False) -> dict:
    run_dir = os.path.abspath(run_dir)
    path = os.path.join(run_dir, MANIFEST_NAME)
    if not os.path.exists(path):
        raise ManifestError(f"No manifest at {path}.")
    with open(path) as fh:
        data = json.load(fh)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"Manifest schema version {data.get('schema_version')}, "
            f"this script speaks {SCHEMA_VERSION}."
        )
    if data.get("run_dir") != run_dir and not allow_moved:
        raise ManifestError(
            f"This manifest records its home as {data.get('run_dir')} but was "
            f"opened at {run_dir}. If the run was legitimately moved, pass "
            "--allow-moved to adopt the new path. If this is a COPY of a live "
            "run, stop: two orchestrators writing two copies will split the "
            "run's state and neither will be complete."
        )
    return data


def update(run_dir: str, fn, allow_moved: bool = False) -> dict:
    """Locked read-modify-write. `fn` mutates the manifest dict in place."""
    run_dir = os.path.abspath(run_dir)
    with Lock(run_dir):
        data = load(run_dir, allow_moved=allow_moved)
        # Reconcile the stored path to where the run actually is BEFORE running
        # the operation, not after. An operation may read data["run_dir"] itself
        # — redo_stage archives into it — and on a moved/copied run the stored
        # field is stale, so archiving would move files from the OLD location.
        # Fixing the path first makes every operation see the true current dir.
        if allow_moved:
            data["run_dir"] = run_dir
        fn(data)
        data["updated"] = _now()
        write_atomic(run_dir, MANIFEST_NAME, data)
    return data


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------


def find_target(data: dict, target: str):
    """Resolve 'run', a group id ('g2') or a piece id ('g2-p4')."""
    if target == "run":
        return data, RUN_STAGES
    for g in data["groups"]:
        if g["id"] == target:
            return g, GROUP_STAGES
        for p in g["pieces"]:
            if p["id"] == target:
                return p, PIECE_STAGES
    raise ManifestError(f"No such target: {target}")


def iter_pieces(data: dict):
    for g in data["groups"]:
        for p in g["pieces"]:
            yield g, p


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def set_stage(
    data: dict,
    target: str,
    stage: str,
    status: str,
    detail: str = None,
    artifact: str = None,
    gate_runs: int = None,
    _cli_call: bool = False,
) -> None:
    obj, allowed = find_target(data, target)
    if stage not in allowed:
        raise ManifestError(
            f"Stage '{stage}' is not valid for {target}. Valid: {list(allowed)}"
        )
    if status not in TRANSITIONS:
        raise ManifestError(f"Unknown status '{status}'.")

    cur = obj["stages"][stage]
    if status not in TRANSITIONS[cur["status"]]:
        raise InvalidTransition(
            f"{target}.{stage}: cannot go {cur['status']} -> {status}. "
            f"Allowed from {cur['status']}: {sorted(TRANSITIONS[cur['status']]) or 'none'}"
        )

    # Skip policy. Piece stages are
    # non-skippable by default; only SKIPPABLE_PIECE_STAGES may be marked SKIPPED
    # via the CLI. A skip on any other piece stage is the work being dodged while
    # the record says done - the founding failure. This one guard closes the
    # dodge on produce, length, fact_check and claim_diff at once
    # (see SKIPPABLE_PIECE_STAGES for the full rationale).
    #
    # CLI-gated like the other production guards below: the orchestrator drives
    # the run through the CLI, so the guard binds it; library callers (internal
    # code, and the verification-math tests that skip length/claim_diff directly
    # to exercise drop-from-verdict) stay exempt. Keyed on `stage in PIECE_STAGES`
    # so run/group stages that legitimately skip (source_intake, research) are
    # untouched - their names are disjoint from the piece stages.
    #
    # de_slop is in the whitelist but still conditional: its own precondition
    # (_check_deslop_prerequisites, below, fired on SKIPPED too) refuses the skip
    # until anti_slop has passed. This guard only says de_slop MAY be considered
    # for a skip; that precondition is the condition.
    # The dropped-piece exemption. A piece
    # whose produce is skipped was never made, so NONE of its gates apply -
    # there is no text to length-check, fact-check, anti-slop or claim-diff. The
    # whitelist alone would leave those gates pending with no honest terminal
    # state, deadlocking a run that legitimately drops a piece early. So a
    # dropped piece may skip any of its gates. This does not reopen the skip-
    # dodge: a produce-skipped piece is never delivered (deliver_pieces requires
    # produce PASSED), so skipping its gates ships nothing - the dodge is only
    # useful on a piece you can ship, and you cannot ship a dropped one.
    if (_cli_call and status == SKIPPED and stage in PIECE_STAGES
            and stage not in SKIPPABLE_PIECE_STAGES
            and not _piece_is_dropped(obj)):
        raise ManifestError(
            f"{stage} cannot be skipped: it applies to every piece, so a skip "
            "would just be a way to dodge the work while the record says it was "
            f"done. Run the {stage} stage and record its result. Only "
            f"{sorted(SKIPPABLE_PIECE_STAGES)} may be skipped (overlap when a "
            "piece has no source material; de_slop once anti_slop has passed). "
            "To drop the whole piece, skip its produce stage first - after that "
            "its remaining gates may be skipped as not-applicable."
        )

    # Prerequisites for opening the produce stage.
    # Checked after the transition is validated (so we know it would succeed)
    # but before any mutation, so a refusal leaves the stage unchanged.
    # Only enforced for CLI dispatches (_cli_call=True); library callers
    # (tests, internal code calling set_stage directly) are exempt so that
    # pre-existing library-level tests do not require a full production
    # directory layout to open a stage.
    if _cli_call and stage == "produce" and status == RUNNING:
        _check_produce_prerequisites(data, obj)

    # Closing anti_slop as passed asserts the report was generated. The stage is
    # only honest if the report is actually on disk, so - in the same spirit as
    # the produce brief check - refuse the close when antislop.txt is
    # missing or empty. A stage that can be marked passed without its artifact is
    # exactly the "said done, was not done" failure this manifest exists to stop.
    if _cli_call and stage == "anti_slop" and status == PASSED:
        _check_antislop_prerequisites(data, obj)

    # de-slop reads the anti-slop report, so it must not open until that report
    # exists. The driver already orders anti_slop before
    # de_slop, but ordering the driver advises is not ordering the manifest
    # enforces: an orchestrator that bypasses the driver could open de_slop with
    # no report on the bench. Require anti_slop PASSED - which, because passing
    # it requires antislop.txt on disk (above), means the report is present. A
    # FAILED anti_slop (report could not be generated) deliberately blocks
    # de_slop rather than letting it run blind; that stalls the piece for a
    # person, which is the right outcome when the report itself failed.
    #
    # The guard covers SKIPPED as well as RUNNING. pending -> skipped is a legal
    # transition and skipped
    # is terminal, so a de_slop marked skipped over a failed/absent anti_slop
    # would pass the completion gate (two terminal stages) with no report ever
    # written - the exact skip-dodge the lock exists to stop. de_slop leaving
    # pending in EITHER direction therefore requires anti_slop passed. Skipping
    # de_slop stays legal AFTER anti_slop passes: a clean report leaves de-slop
    # nothing to rewrite, and unlike anti_slop, de_slop does not apply to every
    # piece, so it is skip-gated rather than blanket non-skippable.
    if _cli_call and stage == "de_slop" and status in (RUNNING, SKIPPED):
        _check_deslop_prerequisites(data, obj)

    # Produce attempt cap: enforced at gate_runs reconciliation. Checked after
    # all transition validation but before any mutation, so a refusal leaves
    # the stage unchanged. CLI-gated: library callers (tests, internal code)
    # are exempt — the cap binds the orchestrator's CLI close path, not the
    # manifest library itself. gate_runs is the truthful count; attempts (always
    # 1 for a normal close) cannot detect grinding abuse. See PRODUCE_ATTEMPT_CAP.
    if (_cli_call and stage == "produce"
            and gate_runs is not None and gate_runs > PRODUCE_ATTEMPT_CAP):
        raise ManifestError(
            f"produce gate_runs {gate_runs} exceeds PRODUCE_ATTEMPT_CAP "
            f"({PRODUCE_ATTEMPT_CAP}). A writer grinding past {PRODUCE_ATTEMPT_CAP} "
            "attempts is gaming the gate; this can happen when vocabulary is being "
            "tuned to pass the overlap gate rather than the piece being fixed; reset "
            "the piece and review the source material. Flag the piece for "
            "orchestrator intervention rather than accepting the over-cap count."
        )

    if status == RUNNING:
        cur["attempts"] += 1
    cur["status"] = status
    cur["detail"] = detail
    cur["updated"] = _now()

    # gate_runs is the TRUTHFUL per-gate iteration count: how many times the
    # writer actually ran this deterministic gate (entry fails + final pass),
    # reconciled by the orchestrator against the draft.attemptN.md snapshots on
    # disk. It is distinct from 'attempts' (orchestrator opens of the stage) on
    # purpose - see skills/produce/SKILL.md "When you close a piece stage". Only
    # written when supplied, so opening/closing a stage never clobbers it.
    if gate_runs is not None:
        cur["gate_runs"] = gate_runs

    if artifact:
        if not os.path.exists(artifact):
            raise ManifestError(f"Artifact does not exist: {artifact}")
        obj["artifact"] = {
            "path": os.path.abspath(artifact),
            "sha256": sha256_file(artifact),
            "recorded": _now(),
        }

    # spoken_to_written runs LAST (after the de-slop bracket) and its output,
    # readable.md, is what a g2 piece SHIPS - but it deliberately rewrites the
    # bytes claim_diff measured, so no recomputable gate can vouch for it - its
    # whole job is to change the basis that diff was measured against. Record an
    # anti-swap hash of exactly the bytes it
    # produced, on the STAGE record itself. deliver_pieces re-checks it: this is
    # NOT a quality gate, it only proves the readable.md that ships is the
    # readable.md spoken wrote - the same "measured bytes = shipped bytes"
    # contract stale_verifications enforces for final.md, applied to the one
    # shipped file no verify-check covers. It lives on `cur` (the stage), not the
    # piece-level obj["artifact"] slot, so no other stage can clobber it and it
    # cannot drift off the shipped file. The CLI path (below, near the verify
    # binding) is what REQUIRES the artifact and binds it to readable.md; here we
    # only record whatever was supplied, keeping library callers/tests exempt.
    if stage == "spoken_to_written" and status == PASSED and artifact:
        cur["output"] = {
            "path": os.path.abspath(artifact),
            "sha256": sha256_file(artifact),
            "recorded": _now(),
        }


def reanchor_shipped(data: dict, piece_id: str, artifact: str,
                     reason: str = None) -> dict:
    """Re-record a voice piece's readable.md anti-swap hash after a small fix.

    readable.md is the shipped file for a spoken piece, and set_stage records its
    hash when spoken_to_written closes. A passed stage has no re-close transition,
    so once the orchestrator edits readable.md in place (the small cross-group fix
    path), the recorded hash is stale and delivery refuses the piece. This
    re-records the anti-swap hash against the current bytes WITHOUT reopening the
    stage: the deliberate, recorded escape hatch that lets a small in-place fix
    ship, distinct from redo (which would regenerate readable.md from final.md and
    discard the edit).

    Guards match the spoken close binding. The stage must be PASSED (there is a
    hash to re-anchor) and must already carry one. The `reanchored` flag and the
    reason are stamped on the record so a hand-edited readable.md is never a silent
    swap: the manifest shows the shipped bytes were re-measured after the stage
    ran, and why. The artifact->readable.md binding is enforced by the CLI, matching
    the spoken close and verify bindings.
    """
    piece, _ = find_target(data, piece_id)
    stg = piece["stages"].get("spoken_to_written")
    if not stg or stg["status"] != PASSED:
        raise ManifestError(
            f"Cannot re-anchor {piece_id}: spoken_to_written is "
            f"'{(stg or {}).get('status')}', not passed. Only a piece that ships a "
            "readable.md (passed spoken_to_written) carries an anti-swap hash to "
            "re-anchor. A non-voice piece re-anchors instead by re-recording its "
            "length/overlap/claim_diff verifications against the edited final.md."
        )
    if not stg.get("output"):
        raise ManifestError(
            f"Cannot re-anchor {piece_id}: spoken_to_written passed but recorded no "
            "anti-swap hash. Re-run its close with --artifact before re-anchoring."
        )
    if not os.path.exists(artifact):
        raise ManifestError(f"Artifact does not exist: {artifact}")
    stg["output"] = {
        "path": os.path.abspath(artifact),
        "sha256": sha256_file(artifact),
        "recorded": _now(),
        "reanchored": True,
        "reason": reason,
    }
    return stg["output"]


def set_verification(data: dict, piece_id: str, check: str, ok: bool,
                     detail: str = None, override: str = None,
                     artifact: str = None) -> None:
    """Record the ORCHESTRATOR's independent recomputation.

    Separate from the stage status on purpose. The stage status is what the
    producing agent reported after running the script itself so it could
    iterate; this is what the orchestrator got when it recomputed on the final
    artifact. Where they disagree, this one decides - and the disagreement
    itself is the signal worth keeping.

    `artifact` names the file that was actually measured and stores its hash.
    Without it a record cannot be told apart from one computed on a superseded
    file: a length verification can describe `draft.md` while claiming to
    describe `final.md`, with no verdict changed - which is the point: a control
    that happens to agree with the truth is not a control. `stale_verifications`
    is what reads this back.

    It is optional here and REQUIRED at the CLI, which is the path such records
    come through. A record without one is not treated as fine - it is
    reported by `stale_verifications` as uncheckable.
    """
    obj, stages = find_target(data, piece_id)
    if stages is not PIECE_STAGES:
        raise ManifestError("Verification is recorded on pieces, not on runs or groups.")
    if check not in VERIFY_CHECKS:
        raise ManifestError(
            f"'{check}' is not independently recomputable. Only {list(VERIFY_CHECKS)} "
            "are deterministic; a model gate cannot be re-run for the same answer."
        )
    v = obj["verification"]

    # A recorded failure can only be retired by a person, and the fact that a
    # person did it survives in the file. Same shape as a ruling retiring a
    # promoted context entry: the pipeline may not quietly reverse its
    # own verdict, and the reversal stays auditable afterwards.
    #
    # Without this, a second `verify --ok true` on a failed check overwrote the
    # failure and `show` read `verify=passed`, indistinguishable from a piece
    # that passed cleanly. That is the one mechanism by which this tool could
    # launder a failure into a pass, in a tool whose entire argument is that it
    # does not do that.
    previous = v["checks"].get(check)
    reversing = previous is not None and previous["ok"] is False and ok
    if reversing and not override:
        raise ManifestError(
            f"{piece_id}.{check} is recorded as FAILED. Recomputing it will not "
            "change that - if a person has decided to accept the piece anyway, "
            "record their decision with --override '<reason>'. The failure stays "
            "in the file either way."
        )
    if override and not reversing:
        raise ManifestError(
            f"--override only applies to a check already recorded as failed. "
            f"{piece_id}.{check} is "
            + ("not recorded yet." if previous is None else f"recorded ok={previous['ok']}.")
        )

    if artifact is not None and not os.path.exists(artifact):
        raise ManifestError(f"Artifact does not exist: {artifact}")

    entry = {
        "ok": bool(ok),
        "detail": detail,
        "at": _now(),
        "artifact": None if artifact is None else {
            "path": os.path.abspath(artifact),
            "sha256": sha256_file(artifact),
        },
    }
    if override:
        entry["override"] = {
            "reason": override,
            "previous_detail": previous.get("detail"),
            "at": _now(),
        }
    v["checks"][check] = entry

    # A check whose STAGE was skipped is not required for a verdict. Every
    # name in VERIFY_CHECKS is also a piece stage, so the stage's own status
    # already says whether the gate applied to this piece.
    #
    # Without this a whole legitimate class of run could never finish. A run
    # with no source material - content written from the intake interview
    # alone - has nothing for the overlap gate to measure against, and the
    # gate refuses to run without a source. So `overlap` could never be
    # recorded, verification stayed at "running" forever, and no piece could
    # ever reach passed.
    #
    # The alternative was recording overlap as ok on a check that never ran,
    # which is the exact dishonesty this tool exists to argue against. A
    # skipped stage says "did not apply" and stays visible in `show`.
    required = [c for c in VERIFY_CHECKS
                if obj["stages"][c]["status"] != SKIPPED]
    recorded = [v["checks"][c] for c in required if v["checks"][c] is not None]

    # A single failed recomputation is decisive - do not wait for the rest
    # before saying so. Leaving the piece at "running" when it is already
    # known to have failed would tell a resumed orchestrator to keep waiting
    # on a verdict that has in fact arrived. Checked across EVERY recorded
    # check, not just the required ones, so a failure cannot be retired by
    # skipping its stage afterwards.
    every = [v["checks"][c] for c in VERIFY_CHECKS if v["checks"][c] is not None]
    if any(not c["ok"] for c in every):
        v["status"] = FAILED
    elif required and len(recorded) == len(required):
        v["status"] = PASSED
    else:
        # `required` empty means every gate was skipped, which is not a pass.
        # A piece nothing was recomputed on has not been verified, and saying
        # so is the whole point of this block.
        v["status"] = RUNNING
    v["updated"] = _now()


def stale_verifications(data: dict) -> list:
    """Every recorded verification that can no longer be trusted, and why.

    Three reasons, deliberately kept apart because they need different fixes:

      "artifact changed since verification" - the file moved on after the
          record was written. This is F11: length was recomputed on `draft.md`
          and the piece then went through de-slop, so the record described a
          file two stages upstream of the one that shipped.
      "artifact missing" - the file named by the record is gone.
      "no artifact recorded" - the record never said what it measured, so
          nobody can confirm it later. Not the same failure as a stale record,
          and reporting it as fine would recreate the original defect.

    An empty list is a real statement: every verification on file names an
    artifact whose bytes still match.
    """
    out = []
    for group in data.get("groups", []):
        for piece in group.get("pieces", []):
            checks = piece.get("verification", {}).get("checks", {})
            for check, entry in checks.items():
                if entry is None:
                    continue
                art = entry.get("artifact")
                if art is None:
                    reason = "no artifact recorded"
                elif not os.path.exists(art["path"]):
                    reason = "artifact missing"
                elif sha256_file(art["path"]) != art["sha256"]:
                    reason = "artifact changed since verification"
                else:
                    continue
                out.append({
                    "piece": piece["id"],
                    "check": check,
                    "reason": reason,
                    "path": (art or {}).get("path"),
                    "recorded_at": entry.get("at"),
                })
    return out


def spend(data: dict, bucket: str, n: int) -> None:
    b = data["search_budget"]
    if bucket not in b["reserved"]:
        raise ManifestError(
            f"No search budget reserved for '{bucket}'. Buckets: {sorted(b['reserved'])}"
        )
    if n < 0:
        raise ManifestError("Cannot spend a negative number of searches.")
    if b["spent"][bucket] + n > b["reserved"][bucket]:
        raise ManifestError(
            f"'{bucket}' would spend {b['spent'][bucket] + n} of its reserved "
            f"{b['reserved'][bucket]} searches. Stop and re-plan the run rather "
            "than borrowing from another bucket - the end-of-run web pass is "
            "what gets starved."
        )
    b["spent"][bucket] += n


def set_run_status(data: dict, status: str) -> None:
    if status not in RUN_STATUSES:
        raise ManifestError(
            f"'{status}' is not a run status. Valid: {list(RUN_STATUSES)}"
        )
    if status == "complete":
        # A run cannot be called complete over stages that never ran. This check
        # lives HERE, in the core status transition,
        # not only in the CLI set-status path, so that no caller can reach
        # 'complete' around it - the guard is a property of the transition
        # itself, not of one entry point. _collect_stage_problems is defined
        # later in the module; Python resolves the name at call time, so the
        # forward reference is fine.
        problems = _collect_stage_problems(data)
        if problems:
            raise ManifestError(
                f"Cannot set status 'complete': {len(problems)} stage(s) are "
                "not resolved (every stage must be passed or skipped; a failed "
                "stage must be retried until it passes or flagged for a person "
                "to rule on):\n"
                + "\n".join(f"  {p}" for p in problems)
            )
        blocking = [f for f in data["flags"]
                    if not f["resolved"]
                    and f["kind"] in ("contradiction", "param_override",
                                      "claim_override")]
        if blocking:
            names = ", ".join(
                f["key"] or f["message"][:60] for f in blocking
            )
            param_b = [f for f in blocking if f["kind"] == "param_override"]
            claim_b = [f for f in blocking if f["kind"] == "claim_override"]
            hints = []
            if any(f["kind"] == "contradiction" for f in blocking):
                hints.append("a contradiction is resolved with "
                             "context.py --run-dir <run> rule ...")
            if param_b:
                hints.append("a param_override is accepted by a person with "
                             "manifest.py --run-dir <run> accept-override "
                             "--key <param> --by <who>")
            if claim_b:
                hints.append("a claim_override is accepted by a person with "
                             "manifest.py --run-dir <run> accept-claim "
                             "--piece <id> --by <who>")
            raise ManifestError(
                f"Cannot set status 'complete': {len(blocking)} unresolved "
                f"blocking flag(s) - {names}. "
                + "; ".join(hints) + "."
            )
        # A verification that no longer holds is worse than none: the run reads
        # as verified while the file that was measured has changed, vanished, or
        # was never named. `show` surfaced these but nothing refused completion
        # over them, so a run could be marked complete carrying a stale record
        # nobody read (a stale length record can sit in the file the whole
        # time). All three reasons block: a production
        # verification always names an artifact (REQUIRED at the CLI), so "no
        # artifact recorded" cannot come from an honest run.
        stale = stale_verifications(data)
        if stale:
            lines = "\n".join(
                f"  {s['piece']}.{s['check']}: {s['reason']}" for s in stale
            )
            raise ManifestError(
                f"Cannot set status 'complete': {len(stale)} verification(s) no "
                f"longer hold:\n{lines}\n"
                "Re-run the recompute on the shipped file and re-record it, or "
                "if the piece genuinely changed, re-verify it."
            )
        # A piece whose produce stage passed but carries no terminal verification
        # record (status PENDING or RUNNING) slips through the two guards above:
        # _collect_stage_problems checks stage statuses, and stale_verifications
        # only iterates records that EXIST — neither catches "produced content,
        # never verified." (FROZEN.) A piece whose produce
        # is skipped (a dropped, unmade piece) does not need verification — there
        # is no text to verify and it is never delivered. Terminal means PASSED or
        # FAILED; FAILED is allowed because a person may still override it, and
        # blocking on an existing human decision would be wrong.
        #   Invariant this guard relies on: verification.status == FAILED only
        # ever coexists with at least one recorded ok=False check, because the
        # only writers of that status (the `verify` CLI and set_verification)
        # always record a check entry. A hand-mutated FAILED+all-None dict would
        # slip past this guard, but that path is out of the trust model by the
        # same rule the skip/delivery guards use: the production
        # orchestrator drives the run through the CLI; a caller mutating the dict
        # directly is not defended against here.
        unverified = [
            p["id"]
            for _g, p in iter_pieces(data)
            if p["stages"]["produce"]["status"] == PASSED
            and p["verification"]["status"] not in (PASSED, FAILED)
        ]
        if unverified:
            raise ManifestError(
                f"Cannot set status 'complete': {len(unverified)} piece(s) whose "
                "produce stage passed carry no terminal verification record "
                f"({', '.join(unverified)}). Record the orchestrator's independent "
                "recomputation with:\n"
                "  manifest.py --run-dir <run> verify --piece <id> --check <check> "
                "--ok true/false --artifact <path>\n"
                "All three checks (length, overlap, claim_diff) must reach a "
                "terminal verdict (passed or failed) before the run can complete."
            )
    data["status"] = status


def redo_stage(data: dict, piece_id: str, stage: str, reason: str,
               triggered_by: str, keep_reviews: bool = False) -> dict:
    """Retire a piece's closed stage and all downstream work, then reset them.

    Every failure (FROZEN) — not just a
    last-moment one — routes back to the last agent that produced it. When
    that agent is gone (the shipped design hands each stage to a fresh
    subagent, so once a stage is closed its agent is gone), the whole step
    must be rerun, with the now-stale material and artifacts retired into
    archive — never deleted, because the on-disk ledger is the valuable part.

    This is a SEPARATE, EXPLICIT, LOGGED operation. It does NOT add any exit
    from PASSED to the general TRANSITIONS table (that table's invariant —
    'passed is terminal for set_stage; recompute first, close second' — is
    what makes the skip/lie dodge impossible). This function is the narrow,
    reviewed carve-out the redo policy requires; it would not exist
    without it.

    Four steps, in order:
      1. Archive: if the run directory is on disk, move the piece's current
         artifact file (and any draft.attemptN.md snapshots for 'produce')
         into <run>/archive/redo_<ts>_<piece>_<stage>/. Retired, not deleted.
      2. Reset: the target stage is reset to 'pending' (bypassing TRANSITIONS —
         this is the explicit carve-out; set_stage is NOT used here).
      3. Cascade: every downstream PIECE_STAGE (those with a higher index in
         PIECE_STAGES) that has already been closed is reset to 'pending'. The
         verification block is cleared back to its initial state (status=pending,
         all checks=None) because it was computed on text that no longer exists.
      4. Ledger: a record is appended to data["redos"] — what was redone, why,
         what was archived, when. Append-only; history is never rewritten.

    Returns the ledger record that was appended.

    Raises ManifestError when:
      - piece_id does not identify a piece (not a group or run target)
      - stage is not a valid PIECE_STAGE for the piece
    """
    # ---- validate --------------------------------------------------------
    obj, allowed = find_target(data, piece_id)
    if allowed is not PIECE_STAGES:
        raise ManifestError(
            f"redo_stage targets pieces only (e.g. 'g1-p1'). "
            f"'{piece_id}' resolved to a run or group target."
        )
    if stage not in PIECE_STAGES:
        raise ManifestError(
            f"'{stage}' is not a PIECE_STAGE. Valid: {list(PIECE_STAGES)}"
        )

    # A redo reopens work: it resets a stage and its downstream to pending, so
    # the run is no longer finished. Refuse on a complete run rather than leave
    # status 'complete' contradicting pending stages — that inconsistency let
    # `deliver` ship unverified content over a run that still read as done.
    # Reopen first, then redo.
    if data["status"] == "complete":
        raise ManifestError(
            "redo refused: the run is marked 'complete'. A redo reopens work and "
            "the run is no longer finished. Reopen it with "
            "`set-status --status running` first, then redo."
        )

    CLOSED = {PASSED, FAILED, FLAGGED, SKIPPED}
    # redo is the gone-agent recovery for a stage that already CLOSED. A pending
    # stage has no retired work to archive (redoing it only writes a misleading
    # ledger no-op); a running stage still has its agent, so the normal
    # failed -> running retry applies. Both are refused here.
    target_status = obj["stages"][stage]["status"]
    if target_status not in CLOSED:
        raise ManifestError(
            f"redo refused: {piece_id}.{stage} is '{target_status}', not a closed "
            "stage (passed/failed/flagged/skipped). A pending stage: open it with "
            "`stage --status running`. A running stage: use the normal "
            "failed -> running retry — its agent is still live."
        )

    ts = _now()
    stage_idx = PIECE_STAGES.index(stage)
    downstream = PIECE_STAGES[stage_idx + 1:]

    # ---- archive on-disk artifacts (best-effort; skipped when no run_dir) -
    archived_paths: list = []
    run_dir = data.get("run_dir", "")
    if run_dir:
        safe_ts = ts.replace(":", "-").replace("+", "")
        archive_dir = os.path.join(
            run_dir, "archive",
            f"redo_{safe_ts}_{piece_id}_{stage}",
        )
        os.makedirs(archive_dir, exist_ok=True)

        # Archive the persistent output files of the redone stage and every
        # downstream stage.  We iterate over PIECE_STAGES from the redone
        # stage's index onward so we never touch an upstream stage's files
        # (FROZEN: redo retires the unfinished material and all of its artifacts
        # that just became irrelevant; an upstream, still-passed stage's artifact
        # is NOT irrelevant — archiving it would be a bug).
        piece_dir = os.path.join(run_dir, "pieces", piece_id)
        for s in PIECE_STAGES[stage_idx:]:
            for fname in STAGE_OUTPUTS.get(s, []):
                fpath = os.path.join(piece_dir, fname)
                if os.path.exists(fpath):
                    dest = os.path.join(archive_dir, fname)
                    shutil.move(fpath, dest)
                    archived_paths.append(dest)
            # For 'produce', also archive draft.attempt*.md snapshots.
            # These are folded into STAGE_OUTPUTS["produce"]'s archival pass
            # rather than duplicating the glob-vs-list logic.
            if s == "produce" and os.path.isdir(piece_dir):
                for fname in os.listdir(piece_dir):
                    if fname.startswith("draft.attempt") and fname.endswith(".md"):
                        src = os.path.join(piece_dir, fname)
                        dest = os.path.join(archive_dir, fname)
                        shutil.move(src, dest)
                        archived_paths.append(dest)

    # ---- reset target stage (explicit bypass — this is the carve-out) ----
    target_stage = obj["stages"][stage]
    target_stage["status"] = PENDING
    target_stage["detail"] = None
    target_stage["updated"] = ts
    # spoken_to_written's anti-swap hash is measured on a now-retired readable.md;
    # drop it so a resumed run cannot deliver against a stale anchor.
    target_stage.pop("output", None)
    # Clear any stage-level artifact reference so a resumed orchestrator
    # does not find a path pointing to an archived (moved) file.
    obj["artifact"] = None

    # ---- cascade: reset every downstream stage that is already closed ----
    # Record what this redo actually retired AS IT RETIRES IT. Deriving the
    # list afterwards from "which downstream stages are now PENDING" would also
    # count stages that were already PENDING before the redo and that the
    # cascade correctly did not touch — an over-claiming ledger record, which
    # in a tool whose whole ethos is an honest on-disk history is a real defect,
    # not a cosmetic one.
    redone_downstream = []
    for ds in downstream:
        ds_stage = obj["stages"][ds]
        if ds_stage["status"] in CLOSED:
            ds_stage["status"] = PENDING
            ds_stage["detail"] = None
            ds_stage["updated"] = ts
            # If spoken_to_written is downstream of the redone stage, its
            # anti-swap hash is now stale too - clear it with the reset.
            ds_stage.pop("output", None)
            redone_downstream.append(ds)

    # ---- cascade: reset the stale corpus-level reviews --------------------
    # A piece redo changes the corpus that group_review (its group) and
    # cross_group (the run) already reviewed, so both are now stale — reset any
    # that had CLOSED, exactly as the downstream piece stages and the
    # verification block are reset above. If a review had not run yet (still
    # PENDING) this is a no-op. For a cross-group-driven redo the orchestrator
    # closes cross_group (flagged) before calling redo, so it is CLOSED here and
    # is reset for its second pass; a redo from any other cause resets it only if
    # it had already run. The re-run's group_review then runs in scoped mode
    # (only the re-run pieces, compared against the full group).
    #
    # keep_reviews=True skips this block: the redo is review-irrelevant (e.g. a
    # pure typo fix) and the orchestrator asserts the reviews remain valid.
    group_review_reset = None
    cross_group_reset = False
    if not keep_reviews:
        for g in data.get("groups", []):
            if any(p.get("id") == piece_id for p in g.get("pieces", [])):
                gr = g["stages"].get("group_review")
                if gr and gr["status"] in CLOSED:
                    gr["status"] = PENDING
                    gr["detail"] = None
                    gr["updated"] = ts
                    gr.pop("output", None)
                    group_review_reset = g.get("id")
                break
        cg = data.get("stages", {}).get("cross_group")
        if cg and cg["status"] in CLOSED:
            cg["status"] = PENDING
            cg["detail"] = None
            cg["updated"] = ts
            cg.pop("output", None)
            cross_group_reset = True

    # ---- clear verification block (computed on now-retired text) ----------
    old_verification = {
        "status": obj["verification"]["status"],
        "checks": dict(obj["verification"]["checks"]),
    }
    obj["verification"] = {
        "status": PENDING,
        "checks": {c: None for c in VERIFY_CHECKS},
        "updated": None,
    }

    # ---- ledger record (append-only) -------------------------------------
    record = {
        "at": ts,
        "piece": piece_id,
        "stage": stage,
        "reason": reason,
        "triggered_by": triggered_by,
        "downstream_reset": redone_downstream,
        "group_review_reset": group_review_reset,
        "cross_group_reset": cross_group_reset,
        "reviews_kept": keep_reviews,
        "verification_retired": old_verification,
        "archived_paths": archived_paths,
    }
    data.setdefault("redos", []).append(record)
    return record


def add_flag(data: dict, target: str, kind: str, message: str,
             key: str = None, refs: list = None) -> bool:
    """Something a human has to look at. Never auto-resolved.

    `key` makes filing idempotent: a second call with a key that already has
    an OPEN flag does nothing and returns False. This is what lets a caller
    that crashed between two writes simply retry - without it, the retry path
    ran into a duplicate check and the flag was never filed at all, leaving a
    real contradiction invisible in the human queue forever.

    `refs` are the entry ids the flag is about, stored so a later close can
    match on identity instead of searching for an id inside the message text.
    """
    if key:
        for f in data["flags"]:
            if f.get("key") == key and not f["resolved"]:
                return False
    data["flags"].append(
        {"target": target, "kind": kind, "message": message, "key": key,
         "refs": list(refs or []), "at": _now(), "resolved": False}
    )
    return True


def set_parameter(data: dict, key: str, value, override: bool = False,
                  reason: str = None, by: str = None) -> None:
    """Change a run parameter. A LOCKED parameter (recorded in
    locked_parameters at intake) can only change through an explicit, reasoned
    override, which is logged and raises a flag a person must accept before the
    run can complete. This is the parameter analogue of redo_stage: the one
    narrow, logged exit from an authoritative value. A piece can become stuck
    here if run parameters define no exit for this state — this override is
    the deliberate escape hatch."""
    locked = data.get("locked_parameters", {})
    if key in locked and not override:
        raise ManifestError(
            f"Parameter '{key}' is locked (locked at "
            f"{locked[key].get('at', 'intake')}). Changing it needs an explicit "
            "override with a reason: it is logged and raised as a flag a person "
            "must accept before the run can complete."
        )
    if key in locked and override:
        if not (reason or "").strip():
            raise ManifestError(
                f"Overriding locked parameter '{key}' requires a reason."
            )
        data.setdefault("param_overrides", []).append({
            "key": key,
            "from": data.get("parameters", {}).get(key),
            "to": value,
            "at": _now(),
            "reason": reason,
            "by": by or "unknown",
        })
        flag_key = f"param_override:{key}"
        msg = (f"Locked parameter '{key}' was overridden -> {value}. "
               f"Reason: {reason}. See the param_overrides log for the full "
               "history; a person must accept before completing.")
        existing = next((f for f in data["flags"]
                         if f.get("key") == flag_key and not f["resolved"]), None)
        if existing:
            existing["message"] = msg
        else:
            add_flag(data, target="run", kind="param_override", message=msg,
                     key=flag_key, refs=[f"param:{key}"])
    data.setdefault("parameters", {})[key] = value


def add_piece(data: dict, group_id: str, topic: str, brief: str = None) -> dict:
    """Append a new piece to an existing group after init.

    Mirrors the piece dict shape built in _normalise_spec lines 400–412 exactly.
    Returns the new piece record. The piece is also appended to data in place so
    the caller can pass the mutated dict to write_atomic / update().

    Raises ManifestError if the topic is null/blank (same spirit as lines
    376–381 in _normalise_spec: a null topic produces plausible-sounding content
    about nothing, and the error surfaces far downstream) or if group_id is not
    found in this manifest.
    """
    if not isinstance(topic, str) or not topic.strip():
        raise ManifestError(
            f"add_piece requires a non-blank topic ({topic!r}). A producing "
            "agent given a null topic writes something plausible about nothing, "
            "and the error surfaces far downstream from the malformed call."
        )
    group = next((g for g in data["groups"] if g["id"] == group_id), None)
    if group is None:
        raise ManifestError(
            f"No group with id '{group_id}' in this manifest. Known groups: "
            + ", ".join(g["id"] for g in data["groups"])
        )
    n = len(group["pieces"]) + 1
    piece = {
        "id": f"{group_id}-p{n}",
        "topic": topic.strip(),
        "brief": brief,
        "artifact": None,
        "stages": {s: _new_stage() for s in PIECE_STAGES},
        "verification": {
            "status": PENDING,
            "checks": {c: None for c in VERIFY_CHECKS},
            "updated": None,
        },
    }
    group["pieces"].append(piece)
    return piece


# Fields an agent may set after init through the set-field verb.  All other
# group fields are structural (fixed at init) — changing them post-init is the
# hand-edit hazard:  This whitelist is the
# complete list of safe post-init group fields; to add one, add it here AND
# write a test that verifies the refusal for the excluded structural fields.
SETTABLE_GROUP_FIELDS = ("voice", "keyword_cluster")


def set_group_field(data: dict, group_id: str, field: str, value) -> None:
    """Set a post-init group field through the locked read-modify-write.

    Only fields in SETTABLE_GROUP_FIELDS may be changed.  Structural fields
    (id, name, scope, stages, pieces) are fixed at init — changing them is
    the hand-edit hazard this verb exists to remove the motive for.  An agent
    that needs to change voice or keyword_cluster now has a sanctioned path;
    there is no sanctioned path for structural fields because none should exist.

    Raises ManifestError:
      - if field is not in SETTABLE_GROUP_FIELDS (forbidden structural field or
        simply unknown field — the whitelist is the contract, not a blacklist)
      - if group_id is not found in this manifest
    """
    if field not in SETTABLE_GROUP_FIELDS:
        raise ManifestError(
            f"'{field}' cannot be set through set-field. Only post-init group "
            f"fields may be changed this way: {', '.join(SETTABLE_GROUP_FIELDS)}. "
            "Structural fields (id, name, scope, stages, pieces) are fixed at "
            "init — changing them post-init is the hand-edit hazard this verb "
            "exists to remove the motive for.  If you need a different settable "
            "field, open an issue rather than editing manifest.json by hand."
        )
    group = next((g for g in data["groups"] if g["id"] == group_id), None)
    if group is None:
        raise ManifestError(
            f"No group with id '{group_id}' in this manifest. Known groups: "
            + ", ".join(g["id"] for g in data["groups"])
        )
    group[field] = value


def add_source(data: dict, path: str, polarity: str, piece: str = None,
               role: str = CONTENT_ROLE) -> bool:
    """Record a source file, its polarity for the overlap gate, and its role.

    `piece` scopes the entry to a single piece (e.g. 'g1-p1'). Absent
    means run-level/shared — the source is available to every piece. This
    is the pre-existing behaviour and what every stored manifest from before
    this change describes; old manifests remain valid because no `piece`
    field simply means run-level.

    Returns False when an entry with the same path, polarity, and piece scope
    is already recorded — re-recording is idempotent so source-intake can be
    re-run safely. Raises ManifestError when:

      - polarity is not 'owned' or 'third_party'
      - the file does not exist (a recorded path that resolves to nothing is
        worse than no record, because it reads as checked — same principle as
        `set_stage` refusing a nonexistent `--artifact`)
      - the same path was previously recorded with a different polarity,
        regardless of scope (polarity is a property of the file relative to
        the author, not the piece — recording it as owned for one piece and
        third_party for another would make gate results uninterpretable)
    """
    if polarity not in SOURCE_POLARITIES:
        raise ManifestError(
            f"'{polarity}' is not a valid source polarity. "
            f"Valid values: {list(SOURCE_POLARITIES)}"
        )
    if role not in SOURCE_ROLES:
        raise ManifestError(
            f"'{role}' is not a valid source role. "
            f"Valid values: {list(SOURCE_ROLES)}"
        )
    # Defect 2: use realpath so a symlink and its target resolve to the same
    # canonical path. A directory passes os.path.exists() but not
    # os.path.isfile(), so Defect 2 (directory accepted) is fixed by the same
    # line. Storing the realpath also makes the registry self-consistent: two
    # callers recording the same physical file via different paths get the same
    # stored entry rather than two separate ones that happen to have contradictory
    # polarities.
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        raise ManifestError(
            f"Source path is not a file: {path}. "
            "Directories and non-existent paths are refused — a recorded path "
            "that resolves to nothing reads as checked when it is not."
        )

    for entry in data.get("sources", []):
        if entry["path"] == path:
            if entry["polarity"] != polarity:
                # Polarity conflict is checked run-wide, not per scope: the
                # file's polarity is a fact about the file, not about which
                # piece uses it.  Owned-for-p1 + third_party-for-p2 would make
                # gate results on that file uninterpretable.
                raise ManifestError(
                    f"Source {path!r} was already recorded with polarity "
                    f"'{entry['polarity']}'; re-recording as '{polarity}' is refused. "
                    "Silently overwriting would change what every gate result on this "
                    "source meant for the whole run. If the change is deliberate, "
                    "edit the manifest JSON by hand and say so in the run log."
                )
            if entry.get("role", CONTENT_ROLE) != role:
                # Role, like polarity, is a fact about the file, not the piece.
                # Recording it as content for one piece and voice for another
                # would make routing and voice-anchoring disagree about the same
                # file — the exact implicit-role confusion the field removes.
                raise ManifestError(
                    f"Source {path!r} was already recorded with role "
                    f"'{entry.get('role', CONTENT_ROLE)}'; re-recording as '{role}' "
                    "is refused. A file's role (voice / content / both) governs "
                    "what routes it and what feeds the voice anchor; changing it "
                    "silently would split those. If deliberate, edit the manifest "
                    "JSON by hand and say so in the run log."
                )
            # Same path, same polarity and role: idempotent only when scope matches.
            if entry.get("piece") == piece:
                return False  # already recorded at this scope, nothing to do
            # Same path, same polarity, different scope → fall through to append
            # (e.g. a transcript that is both a run-level source and scoped to
            # a specific piece for a more targeted overlap check).

    entry = {
        "path": path,
        "polarity": polarity,
        "role": role,
        "recorded": _now(),
    }
    if piece is not None:
        entry["piece"] = piece
    data.setdefault("sources", []).append(entry)
    return True


def deliver_name(piece_id: str, topic: str) -> str:
    """Return a safe, distinct filename for handing a piece to its owner.

    Format: ``{piece_id}_{topic_slug}.md``

    The piece_id prefix (e.g. 'g1-p1') makes every name unique by
    construction, because piece ids are unique within a run. The topic slug
    adds human legibility so the owner can tell which piece they are holding
    without opening the file — which is the exact problem A9 describes.

    Slug rules: lowercase, any run of non-alphanumeric characters becomes a
    single hyphen, leading/trailing hyphens stripped, capped at 60 characters
    so the full filename stays short enough for any filesystem.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower().strip()).strip("-")
    slug = slug[:60].rstrip("-")
    if not slug:
        slug = "untitled"
    return f"{piece_id}_{slug}.md"


def deliver_pieces(run_dir: str, out_dir: str) -> list:
    """Copy each piece's shipped file to a distinctly named file in out_dir.

    The shipped file is readable.md when spoken_to_written passed (a g2 piece,
    where spoken rewrote final.md into readable.md as the last stage), else
    final.md (a g1 piece, or any piece spoken did not run for).  Naming follows
    ``deliver_name(piece_id, topic)``.  The collision guard runs before any file
    is written: if two pieces would produce the same filename the whole
    operation is refused — partial delivery would be more confusing than none.

    Returns a list of (piece_id, dest_path) pairs for every file written.
    Raises ManifestError when:

      - two pieces produce the same filename (the guard fires)
      - a piece's shipped file does not exist (only completed pieces may be
        delivered; handing over a non-existent file would look like silence)
      - a spoken piece's readable.md is missing, unrecorded, or its hash no
        longer matches the anti-swap anchor spoken recorded (shipped bytes must
        be the measured bytes)
    """
    data = load(run_dir)
    # Delivery is the handover boundary: it must not be a way around the
    # completion gate. `deliver` used to check only that each final.md existed,
    # so a run could hand finished files to the owner with stages never run
    # (anti_slop skipped, a verification stale) as long as final.md was present -
    # a second path to "said done, was not done", separate from set-status. So
    # delivery requires a completed run, which is the single checkpoint that
    # enforces every stage terminal (including anti_slop), no stale verification,
    # and no open contradiction.
    if data["status"] != "complete":
        raise ManifestError(
            f"Cannot deliver: run status is '{data['status']}', not 'complete'. "
            "Delivery hands finished pieces to the owner, so it requires a "
            "completed run. Run `set-status --status complete` first; if that is "
            "refused, the run is not actually finished and must not be delivered."
        )
    # Re-check the verifications AT DELIVERY, not only at completion. The
    # completion gate confirmed every recorded verification matched its artifact
    # at the moment `complete` was set - but delivery is a later moment, and the
    # file can change in between the two calls (complete a run with verifications
    # matching the real final.md, swap final.md, then deliver the swap). The
    # shipped bytes must be
    # the bytes that were measured, so the hash is re-checked here at the point
    # of handover. Same check the completion gate runs; delivery is a fresh point
    # of use that must not trust the earlier snapshot.
    stale = stale_verifications(data)
    if stale:
        lines = "\n".join(
            f"  {s['piece']}.{s['check']}: {s['reason']}" for s in stale
        )
        raise ManifestError(
            f"Cannot deliver: {len(stale)} verification(s) no longer hold - the "
            f"file that ships is not the file that was measured:\n{lines}\n"
            "Re-run the recompute on the current file and re-record it, or if the "
            "piece genuinely changed, re-verify it before delivering."
        )
    if os.path.isfile(out_dir):
        raise ManifestError(
            f"Delivery output directory {out_dir!r} already exists as a regular "
            "file, not a directory. Remove or rename the file before running deliver."
        )
    os.makedirs(out_dir, exist_ok=True)

    # A piece is a real deliverable only if it was actually produced. Delivery
    # keys on final.md existing, but final.md is only a file on disk - an
    # orchestrator that skipped produce (the honest "piece not made" marker for
    # a dropped piece) could plant a final.md and hand over content the pipeline
    # never wrote. So only pieces
    # whose produce stage PASSED are delivered; any other produce status excludes
    # the piece. This is the root fix for that bypass: it keeps produce honestly
    # skippable (a dropped piece stays markable) while making a dropped piece
    # undeliverable, so a planted final.md reaches no one.
    #
    # Exclusion is reported, never silent (Rule 12, fail loud): a piece the owner
    # might have expected but did not receive must be visible, not quietly
    # dropped from the handover. A produce-skipped piece is the intended,
    # legitimate exclusion; any other non-passed produce status is surfaced the
    # same way so it cannot hide.
    #
    # Plan: build (piece_id, filename, src_path) for every DELIVERABLE piece
    # first, then check for collisions, then write.  Never write a partial set.
    planned = []
    excluded = []
    for _g, p in iter_pieces(data):
        produce_status = p["stages"]["produce"]["status"]
        if produce_status != PASSED:
            excluded.append((p["id"], produce_status))
            continue
        # Defence in depth: a produce-passed piece must have every stage resolved.
        # The completion gate guarantees this at `complete`, but delivery is a
        # later, independent point of use and must not trust the run flag alone. A
        # reopen + redo could reset a downstream stage while status still read
        # complete, and the redo also clears the verification records — so the
        # stale-check above would pass trivially (nothing left to compare) and the
        # piece would ship unverified. Refuse
        # rather than hand over a piece the run only claims is finished.
        unresolved = [s for s in PIECE_STAGES
                      if p["stages"][s]["status"] not in (PASSED, SKIPPED)]
        if unresolved:
            raise ManifestError(
                f"Cannot deliver: piece {p['id']} is marked deliverable (produce "
                f"passed) but has unresolved stage(s): {', '.join(unresolved)}. The "
                "run's 'complete' status is inconsistent with its stages — reopen, "
                "finish the piece, and re-complete before delivering."
            )
        name = deliver_name(p["id"], p["topic"])
        # Which file ships: spoken_to_written runs LAST and, when it runs (a g2
        # piece), rewrites final.md into readable.md - so readable.md is the
        # deliverable. When spoken is skipped (g1) or never produced one,
        # final.md ships unchanged. (Zero STAGE_OUTPUTS change - spoken keeps
        # writing readable.md, deliver just prefers it when the stage passed.)
        spoken_status = p["stages"]["spoken_to_written"]["status"]
        if spoken_status == PASSED:
            # readable.md is the shipped file and no verify-check covers it
            # (spoken breaks claim_diff's basis by design). Re-hash it against
            # the anti-swap hash spoken recorded when it passed - the same
            # measured-vs-shipped re-check stale_verifications does for final.md,
            # applied here to the one shipped file the verification block cannot.
            # A passed spoken with no recorded output, no readable.md on disk, or
            # a changed hash is refused: the bytes that would ship are then not
            # the bytes spoken measured.
            readable = os.path.join(run_dir, "pieces", p["id"], "readable.md")
            recorded = p["stages"]["spoken_to_written"].get("output")
            if not os.path.isfile(readable):
                raise ManifestError(
                    f"Cannot deliver: piece {p['id']} passed spoken_to_written "
                    f"but has no readable.md at {readable!r}. The stage that "
                    "produces the shipped file left nothing on disk."
                )
            if not recorded:
                raise ManifestError(
                    f"Cannot deliver: piece {p['id']} passed spoken_to_written "
                    "without recording its output hash. readable.md is the "
                    "shipped file and this hash is its only anti-swap anchor - "
                    "re-run the stage close with --artifact pointing at readable.md."
                )
            if sha256_file(readable) != recorded["sha256"]:
                raise ManifestError(
                    f"Cannot deliver: piece {p['id']}'s readable.md has changed "
                    "since spoken_to_written measured it - the file that would "
                    "ship is not the file spoken produced. Re-run spoken and "
                    "re-record, or restore the measured bytes."
                )
            src = readable
        else:
            src = os.path.join(run_dir, "pieces", p["id"], "final.md")
        planned.append((p["id"], name, src))

    for pid, produce_status in excluded:
        print(
            f"warning: piece {pid} not delivered: its produce stage is "
            f"'{produce_status}', not passed - the piece was not produced, so "
            "any final.md on disk for it is not a pipeline output.",
            file=sys.stderr,
        )

    # Collision guard: piece_id prefix makes this structurally impossible in a
    # valid manifest, but the guard is explicit so the invariant is enforced
    # rather than assumed.
    seen: dict = {}
    for pid, name, _ in planned:
        if name in seen:
            raise ManifestError(
                f"Two pieces would produce the same delivery filename {name!r}: "
                f"'{seen[name]}' and '{pid}'. Delivery refused — "
                "deliverables must never share a filename."
            )
        seen[name] = pid

    # Existence check before writing anything.
    for pid, _name, src in planned:
        if not os.path.isfile(src):
            raise ManifestError(
                f"Piece '{pid}' has no shipped file at {src!r}. "
                "Only pieces with a completed shipped file may be delivered."
            )

    written = []
    for pid, name, src in planned:
        dest = os.path.join(out_dir, name)
        shutil.copy2(src, dest)
        written.append((pid, dest))
    return written


def close_flags(data: dict, entry_ids: list, resolved_by: str) -> int:
    """Close open flags that refer to any of `entry_ids`.

    Matching is on the stored `refs`, never on substrings of the message. A
    substring match would close the wrong flag as soon as ids stopped being
    the same width - 'e0001' is a substring of 'e10001'.
    """
    wanted, closed = set(entry_ids), 0
    for f in data["flags"]:
        if not f["resolved"] and wanted & set(f.get("refs") or []):
            f["resolved"] = True
            f["resolved_by"] = resolved_by
            closed += 1
    return closed


def accept_override(data: dict, key: str, by: str, note: str = None) -> None:
    """A person accepts a locked-parameter override, clearing the flag that
    blocks completion. Deliberately NOT reachable through `resolve` (which an
    agent hits reflexively when blocked) - accepting an override is a distinct,
    logged act, the same shape as a person ruling on a contradiction. Who
    accepted, and when, survive in the param_overrides record."""
    flag_key = f"param_override:{key}"
    open_flags = [f for f in data["flags"]
                  if f.get("key") == flag_key and not f["resolved"]]
    if not open_flags:
        raise ManifestError(
            f"No open param_override flag for '{key}'. Nothing to accept."
        )
    for f in open_flags:
        f["resolved"] = True
        f["resolved_by"] = by
    stamped_at = _now()
    for rec in data.get("param_overrides", []):
        if rec["key"] == key and "accepted_by" not in rec:
            rec["accepted_by"] = by
            rec["accepted_at"] = stamped_at
            if note:
                rec["note"] = note


def accept_claim(data: dict, piece: str, by: str, note: str = None) -> None:
    """A person accepts a claim_override flag, clearing the block that prevents
    run completion. Deliberately NOT reachable through `resolve` — accepting a
    claim override is a distinct, logged act that records who decided and why.
    The orchestrator may not retire its own verdict on verified content;
    this gate enforces that by requiring a named human sign-off."""
    flag_key = f"claim_override:{piece}"
    open_flags = [f for f in data["flags"]
                  if f.get("key") == flag_key and not f["resolved"]]
    if not open_flags:
        raise ManifestError(
            f"No open claim_override flag for piece '{piece}'. Nothing to accept."
        )
    for f in open_flags:
        f["resolved"] = True
        f["resolved_by"] = by
    stamped_at = _now()
    rec = {
        "piece": piece,
        "accepted_by": by,
        "accepted_at": stamped_at,
    }
    if note:
        rec["note"] = note
    data.setdefault("accepted_claims", []).append(rec)


def barrier(data: dict, group: str = None) -> list:
    """Reasons no piece may start yet. An empty list means production may begin.

    A hard barrier stands before any piece starts: context intake, source
    intake and any research complete first. An unresolved contradiction holds
    the same barrier shut.

    This is a function and not a paragraph in a skill on purpose. Whether the
    barrier is crossable is a mechanical fact - stages have statuses, flags
    are resolved or not - and mechanical facts belong in scripts. An
    orchestrator asked to *remember* to check is an orchestrator
    that eventually does not, and the failure is silent: the run produces
    fifteen pieces against a context with a known conflict in it and reports
    that everything was checked.

    Only `contradiction` flags block. Other open flags are reported and do not
    hold the barrier - a piece
    flagged for repeated gate failure is a flag raised *after* production
    started, and blocking on it would deadlock the run that raised it.

    A sourced entry that carries a citation but no verbatim text holds
    the barrier shut. The whole point of the `sourced` label is that a gate
    challenges the citation - with nothing quoted there is nothing to challenge.
    A general observation becomes undetectably first-person; a frame invented
    around true details survives unchallenged. This check cannot import
    context.py (circular - context.py imports manifest.py), so it reads
    context.json directly.
    """
    reasons = []
    for name in ("context_intake", "source_intake"):
        status = data["stages"][name]["status"]
        if status not in (PASSED, SKIPPED):
            reasons.append(
                f"intake barrier: run stage {name} is '{status}', not passed or skipped"
            )
    for f in data["flags"]:
        if not f["resolved"] and f["kind"] == "contradiction":
            reasons.append(f"unresolved contradiction - {f['message']}")
    if group is not None:
        g, _ = find_target(data, group)
        if "pieces" not in g:
            raise ManifestError(f"{group} is not a group")
        status = g["stages"]["research"]["status"]
        if status not in (PASSED, SKIPPED):
            reasons.append(
                f"research barrier: {group} stage research is '{status}', "
                "not passed or skipped"
            )

    # Sourced-quote check: check context.json for sourced entries with no verbatim text.
    # Skipped gracefully when no context file exists (run may predate context
    # intake, or a test is operating on a manifest-only temp dir).
    run_dir = data.get("run_dir")
    if run_dir:
        ctx_path = os.path.join(run_dir, "context.json")
        if os.path.exists(ctx_path):
            try:
                with open(ctx_path) as _fh:
                    _ctx = json.load(_fh)
                # `quotes` is absent in v1 stores (treated identically to []).
                unquoted = [
                    e["id"]
                    for e in _ctx.get("entries", [])
                    if e.get("provenance") == "sourced"
                    and e.get("citation")
                    and not e.get("quotes")
                ]
                if unquoted:
                    reasons.append(
                        f"sourced-quote barrier: {len(unquoted)} sourced "
                        f"{'entry' if len(unquoted) == 1 else 'entries'} "
                        f"carry a citation but no verbatim text; "
                        f"add --quote-from for: {', '.join(unquoted)}"
                    )
            except (json.JSONDecodeError, KeyError):
                pass  # Corrupt context is not a barrier problem; other stages catch it.

    return reasons


def _collect_stage_problems(data: dict) -> list:
    """Every stage that is not RESOLVED, blocking completion.

    Used by `set-status complete` to ensure the record is honest before
    completion is declared (a run cannot be called
    complete over stages that never ran). Returns a list of human-readable
    problem descriptions; an empty list means every stage is resolved.

    A stage is RESOLVED only when it is `passed` or `skipped` - the two states
    with nothing left to do (in TRANSITIONS they are the sinks, no outgoing
    move). Every other status blocks completion, each because work remains:
    `pending`/`running` never finished; `flagged` is waiting on a person; and
    `failed` is unresolved - it must be retried until it passes or flagged for a
    person (the retry rule). Accepting `failed` here would be an inconsistency:
    the rest of the system
    already treats FAILED as not-done - the driver's CLEAN_TERMINAL excludes it
    and barrier() does too - and a run marked complete over a failed gate is the
    founding "said done, was not done" failure, delivering content a gate
    rejected. So FAILED blocks completion, aligned with FLAGGED.

    Deliberate: this is a pure function of the data dict so it can be called
    inside the locked update() callback (has the current state) or outside
    it (preview). Both callers need the same logic.
    """
    RESOLVED = {PASSED, SKIPPED}
    problems = []

    for name, stage in data.get("stages", {}).items():
        st = stage["status"]
        if st not in RESOLVED:
            problems.append(f"run stage {name} is '{st}' (not resolved)")

    for g in data.get("groups", []):
        for name, stage in g.get("stages", {}).items():
            st = stage["status"]
            if st not in RESOLVED:
                problems.append(
                    f"group {g['id']} stage {name} is '{st}' (not resolved)"
                )

    for g in data.get("groups", []):
        for p in g.get("pieces", []):
            for name, stage in p.get("stages", {}).items():
                st = stage["status"]
                if st not in RESOLVED:
                    problems.append(
                        f"piece {p['id']} stage {name} is '{st}' (not resolved)"
                    )

    return problems


def _check_antislop_prerequisites(data: dict, piece: dict) -> None:
    """The report must exist before anti_slop can be marked passed.

    Called from set_stage() only when _cli_call=True, so library callers (tests,
    internal code) are unaffected, matching _check_produce_prerequisites.

    The anti_slop stage's whole meaning is "the checker's report was generated
    on the verified text and is on disk for de-slop to read". Marking it passed
    with no antislop.txt would be the manifest
    recording work that did not happen - the same failure class the produce
    brief check closes. anti_slop is non-skippable (it applies to every
    piece), so there is no "did not apply" escape: the only honest closes are
    passed (report present) or failed (report could not be generated). What is
    refused here is a *passed* with nothing behind it.
    """
    run_dir = data.get("run_dir", "")
    piece_id = piece.get("id", "")
    if not run_dir or not piece_id:
        return
    report_path = os.path.join(run_dir, "pieces", piece_id, "antislop.txt")
    if not os.path.isfile(report_path) or os.path.getsize(report_path) == 0:
        raise ManifestError(
            f"Cannot pass {piece_id}.anti_slop: "
            f"pieces/{piece_id}/antislop.txt must exist and be non-empty first. "
            "Run the anti-slop checker on verified.md and save its report there "
            "(it is de-slop's input); then close the stage passed. anti_slop "
            "cannot be skipped - it applies to every piece."
        )


def _piece_is_dropped(piece: dict) -> bool:
    """True when this piece was dropped: its produce stage is skipped.

    A dropped piece was never written, so none of its downstream gates apply -
    there is no text to length-check, fact-check, anti-slop or claim-diff. This
    is what lets those otherwise-non-skippable stages be skipped for a dropped
    piece without reopening the skip-dodge:
    a produce-skipped piece is never delivered (deliver_pieces requires produce
    PASSED), so skipping its gates ships nothing.

    Guarded with .get() so it is safe to call on any target; only a piece has a
    produce stage, and the callers only reach it for piece targets.
    """
    return piece.get("stages", {}).get("produce", {}).get("status") == SKIPPED


def _check_deslop_prerequisites(data: dict, piece: dict) -> None:
    """anti_slop must be passed before de_slop can open.

    Called from set_stage() only when _cli_call=True, matching the other
    pre-dispatch checks; library callers are exempt.

    de-slop consumes the anti-slop report, so opening it before anti_slop has
    passed would let de-slop run with no report to read - the out-of-order path
    the driver advises against but could not, before this, prevent. Requiring
    PASSED (not merely terminal) is deliberate: passing anti_slop requires
    antislop.txt on disk, so PASSED is the state in which the report is
    guaranteed present. anti_slop is non-skippable, so the only other terminal
    state is FAILED, which means the report could not be generated - and in that
    case de-slop must NOT proceed; the piece stalls for a person instead.

    Two checks, because the recorded pass and the file can disagree. The
    manifest status is checked first,
    then antislop.txt is re-read from disk: the pass was recorded at close time,
    but a crash or an inter-agent race in an unattended run can delete the file
    afterwards, and de-slop about to READ the report must find it actually
    there - not merely trust that it once was. This mirrors the produce brief
    check, which also reads disk at open time rather than trusting a flag.

    Exempt for a dropped piece: if produce
    is skipped, the piece was never made, so de_slop - like its other gates -
    did not apply and may be skipped even though anti_slop is skipped rather
    than passed. Without this exemption a dropped piece could skip every gate
    except de_slop, which its normal precondition (anti_slop must be PASSED)
    would still block, re-creating the deadlock one stage later.
    """
    if _piece_is_dropped(piece):
        return
    piece_id = piece.get("id", "")
    if not piece_id:
        return
    anti = piece["stages"]["anti_slop"]["status"]
    if anti != PASSED:
        raise ManifestError(
            f"Cannot open {piece_id}.de_slop: anti_slop is '{anti}', not passed. "
            "de-slop reads the anti-slop report, so anti_slop must run first. "
            f"Generate pieces/{piece_id}/antislop.txt and close the anti_slop "
            "stage passed, then open de_slop."
        )
    run_dir = data.get("run_dir", "")
    if run_dir:
        report_path = os.path.join(run_dir, "pieces", piece_id, "antislop.txt")
        if not os.path.isfile(report_path) or os.path.getsize(report_path) == 0:
            raise ManifestError(
                f"Cannot open {piece_id}.de_slop: anti_slop is recorded passed "
                f"but pieces/{piece_id}/antislop.txt is missing or empty. The "
                "report de-slop reads is gone (deleted or lost after the pass "
                "was recorded). Regenerate it before opening de_slop."
            )


def _check_produce_prerequisites(data: dict, piece: dict) -> None:
    """Pre-dispatch checks when opening the produce stage.

    Called from set_stage() only when _cli_call=True, so library callers
    (tests, internal code that call set_stage() directly) are unaffected.
    All CLI dispatches of produce->running go through this check, regardless
    of whether a pieces directory has been created — the directory check that
    existed previously failed open when the directory was absent.

    The brief check: producing_brief.md must exist and be non-empty.
    An uninspectable dispatch cannot be audited afterwards; nobody can tell
    what the writer was actually asked for - a dispatch with no brief on disk
    leaves no record of what it was asked to write.

    The barrier check: the barrier must be open. barrier() is correct when
    called; the risk is that consulting it requires an agent to remember, and
    every requirement that depends on an agent remembering fails under load.
    """
    run_dir = data.get("run_dir", "")
    piece_id = piece.get("id", "")
    if not run_dir or not piece_id:
        return

    # Brief check: producing brief must exist and be non-empty
    brief_path = os.path.join(run_dir, "pieces", piece_id, "producing_brief.md")
    if not os.path.isfile(brief_path) or os.path.getsize(brief_path) == 0:
        raise ManifestError(
            f"Cannot open {piece_id}.produce: "
            f"pieces/{piece_id}/producing_brief.md must exist and be non-empty "
            "before dispatching. "
            "Write the producing brief first, then open the stage."
        )

    # Barrier check: barrier must be open
    gid = piece_id.split("-")[0]
    try:
        reasons = barrier(data, group=gid)
    except ManifestError as exc:
        reasons = [str(exc)]
    if reasons:
        raise ManifestError(
            f"Cannot open {piece_id}.produce: the barrier is shut.\n"
            + "\n".join(f"  {r}" for r in reasons)
            + f"\nTo resolve a contradiction: "
            f"context.py --run-dir {run_dir} rule "
            "--text '<your ruling>' --scope '<scope>' --resolves <entry_id>"
        )


def summary(data: dict) -> str:
    lines = []
    b = data["search_budget"]
    lines.append(f"run {data['run_id']}  status={data['status']}  schema v{data['schema_version']}")
    lines.append(f"  dir: {data['run_dir']}")
    # "unallocated" means not reserved by any bucket. It is NOT headroom:
    # spend() takes a bucket name and there is no unallocated bucket, so this
    # number cannot be drawn on without re-planning the run. Spelled out
    # because the reader here is a model deciding whether it has room.
    lines.append(
        "  searches: "
        + f"{sum(b['spent'].values())} spent / {sum(b['reserved'].values())} reserved "
        + f"/ {b['ceiling']} ceiling "
        + f"({b['unallocated']} unreserved - not spendable without re-planning)"
    )
    # Per bucket, not only the total. A bucket cannot borrow from another, so
    # the total says nothing about whether the group about to fan out has room.
    if b["reserved"]:
        lines.append("    " + "  ".join(
            f"{name}:{b['spent'].get(name, 0)}/{res}"
            + ("(EXHAUSTED)" if b["spent"].get(name, 0) >= res else "")
            for name, res in sorted(b["reserved"].items())
        ))
    lines.append("  run stages: " + ", ".join(f"{k}={v['status']}" for k, v in data["stages"].items()))
    sources = data.get("sources", [])
    if sources:
        lines.append(f"  sources ({len(sources)}):")
        for s in sources:
            lines.append(f"     [{s['polarity']}/{s.get('role', CONTENT_ROLE)}] {s['path']}")
    else:
        lines.append("  sources: none recorded")
    for g in data["groups"]:
        gs = ", ".join(f"{k}={v['status']}" for k, v in g["stages"].items())
        lines.append(f"  [{g['id']}] {g['name']}  ({gs})")
        for p in g["pieces"]:
            stages = " ".join(
                f"{s}:{p['stages'][s]['status'][:4]}" for s in PIECE_STAGES
            )
            # An overridden check is marked here and not only in the JSON. A
            # piece a person waved through must not look like one that passed.
            overrides = [c for c in VERIFY_CHECKS
                         if (p["verification"]["checks"].get(c) or {}).get("override")]
            mark = "*" if overrides else " "
            status = p["verification"]["status"] + mark
            lines.append(f"     {p['id']}  verify={status:8s} {stages}")
            for c in overrides:
                o = p["verification"]["checks"][c]["override"]
                lines.append(
                    f"        * {c} FAILED and was accepted by a person: {o['reason']}"
                )
    # A verification whose artifact has moved on is worse than no verification,
    # because the run reads as verified. Surfaced here rather than only in the
    # JSON: a stale length record can sit in the file the whole time with
    # nothing ever saying so.
    stale = stale_verifications(data)
    if stale:
        lines.append(f"  {len(stale)} VERIFICATION(S) THAT NO LONGER HOLD:")
        for s in stale:
            lines.append(f"     {s['piece']}.{s['check']}: {s['reason']}")
    open_flags = [f for f in data["flags"] if not f["resolved"]]
    if open_flags:
        lines.append(f"  {len(open_flags)} OPEN FLAG(S) needing a human:")
        for f in open_flags:
            lines.append(f"     [{f['kind']}] {f['target']}: {f['message']}")
    return "\n".join(lines)


def context_divergence(run_dir: str, manifest_data: dict) -> list:
    """Find contradictions in context.json that have no open manifest flag.

    Returns a list of human-readable strings, one per divergence.  An empty
    list means the context and manifest agree on this dimension.

    The state this detects: a context entry has `contradicts` set and
    is still staged (not yet resolved), but no open manifest flag with
    kind='contradiction' names that entry's id in its refs.  The barrier
    checks manifest flags only, so this contradiction is invisible to the
    barrier — the run looks clean while holding a known conflict.

    This cannot import context.py (circular: context.py imports manifest.py),
    so it reads context.json directly.  Same pattern as barrier()'s
    sourced-quote check.

    Two cases are deliberately distinguished:

      * context.json is absent — returns [], silently.  A run need not use
        context at all; absence is legitimate.  LOCKED by existing tests.

      * context.json is present but unreadable, unparseable, or structurally
        not what we expect — returns a warning message so `show` prints it.
        A crash can leave context.json half-written.  Going quiet on damaged
        input fails at exactly the moment this check was built for; the
        warning tells the operator to inspect the file directly.
    """
    ctx_path = os.path.join(run_dir, "context.json")
    if not os.path.exists(ctx_path):
        return []  # context not yet created — not a divergence

    # Present but damaged: report a warning rather than silencing.  The caller
    # (`show`) prints whatever strings are in this list; a non-empty return
    # from this path means "could not check", not "contradiction found".
    _UNREADABLE = (
        "context.json is present but could not be checked for contradictions "
        "(unexpected format or partial write). "
        "Inspect context.json directly before trusting this run's barrier status."
    )

    try:
        with open(ctx_path) as _fh:
            ctx = json.load(_fh)
    except (OSError, json.JSONDecodeError):
        return [_UNREADABLE]

    if not isinstance(ctx, dict):
        return [_UNREADABLE]

    entries = ctx.get("entries", [])
    if not isinstance(entries, list):
        return [_UNREADABLE]

    # Build the set of entry ids that already have an open contradiction flag
    # in the manifest.  A flag's refs name the entry it was filed for.
    flagged_ids: set = set()
    for f in manifest_data.get("flags", []):
        if f.get("kind") == "contradiction" and not f.get("resolved"):
            for ref in (f.get("refs") or []):
                flagged_ids.add(ref)

    divergences = []
    try:
        for e in entries:
            if not isinstance(e, dict):
                return [_UNREADABLE]
            entry_id = e.get("id")
            if entry_id is None:
                return [_UNREADABLE]
            if e.get("contradicts") and e.get("state") == "staged":
                # This entry's contradiction was declared in the context…
                if entry_id not in flagged_ids:
                    # …but nothing in the manifest flags names it.
                    # The barrier checks manifest flags and will not fire.
                    divergences.append(
                        f"context entry {entry_id!r} (scope={e.get('scope')!r}) "
                        f"contradicts {e.get('contradicts')!r} but has no open manifest flag — "
                        f"the barrier will not fire on this conflict: "
                        f"{e.get('text', '')[:80]!r}"
                    )
    except Exception:
        return [_UNREADABLE]

    return divergences


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="content-at-scale run manifest")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--allow-moved", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a manifest from a run spec")
    p.add_argument("--spec", required=True, help="path to spec JSON, or - for stdin")

    sub.add_parser("show", help="human-readable status")
    sub.add_parser("json", help="dump the manifest")

    p = sub.add_parser("stage", help="set a stage status")
    p.add_argument("--target", required=True, help="run | g1 | g1-p2")
    p.add_argument("--stage", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--detail")
    p.add_argument("--artifact")
    p.add_argument("--gate-runs", type=int, dest="gate_runs",
                   help="the TRUTHFUL number of times the writer ran this "
                        "deterministic gate (entry fails + final pass), "
                        "reconciled against the draft.attemptN.md snapshots on "
                        "disk. Distinct from 'attempts', which counts the "
                        "orchestrator's opens of the stage.")

    p = sub.add_parser("verify", help="record the orchestrator's recomputation")
    p.add_argument("--piece", required=True)
    p.add_argument("--check", required=True, choices=VERIFY_CHECKS)
    p.add_argument("--ok", required=True, choices=("true", "false"))
    p.add_argument("--detail")
    p.add_argument("--artifact", required=True,
                   help="the file you actually measured. Required: a record "
                        "that does not name its artifact cannot be told apart "
                        "from one computed on a superseded file")
    p.add_argument("--override", metavar="REASON",
                   help="a person accepted this piece despite a recorded failure")

    p = sub.add_parser("spend", help="record WebSearch calls against a bucket")
    p.add_argument("--bucket", required=True)
    p.add_argument("-n", type=int, required=True)

    p = sub.add_parser("flag", help="raise something for a human")
    p.add_argument("--target", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--refs", nargs="*", metavar="ENTRY_ID", default=[],
                   help="context entry ids this flag is about. A flag filed "
                        "without refs can never be closed by `resolve`.")
    p.add_argument("--key",
                   help="idempotency key: a second call with the same key "
                        "and an open flag does nothing and returns False")

    p = sub.add_parser("resolve", help="close open flags that refer to given entry ids")
    p.add_argument("--refs", nargs="+", required=True, metavar="ENTRY_ID",
                   help="entry ids whose flags should be closed")
    p.add_argument("--by", required=True, metavar="WHO",
                   help="who or what resolved this")

    p = sub.add_parser("accept-override",
                       help="a person accepts a locked-parameter override, "
                            "clearing the flag that blocks completion")
    p.add_argument("--key", required=True, help="the parameter key, e.g. length_bounds")
    p.add_argument("--by", required=True, help="who accepted")
    p.add_argument("--note", help="optional note recorded with the acceptance")

    p = sub.add_parser("accept-claim",
                       help="a person accepts an unrestorable claim alteration, "
                            "clearing the flag that blocks completion")
    p.add_argument("--piece", required=True,
                   help="the piece id whose claim_override flag to clear, e.g. g1-p1")
    p.add_argument("--by", required=True, help="who accepted")
    p.add_argument("--note", help="optional note recorded with the acceptance")

    p = sub.add_parser("set-status", help="set the run-level status")
    p.add_argument("--status", required=True, choices=RUN_STATUSES)

    p = sub.add_parser(
        "redo",
        help=(
            "retire a closed piece stage and all downstream work, then reset "
            "them for a fresh dispatch. "
            "Use when a failure is detected after a stage is already passed — "
            "e.g. claim_diff fails and implicates de_slop. Archives on-disk "
            "artifacts, cascades resets forward, clears verification, appends "
            "a ledger record. Does NOT modify the TRANSITIONS table."
        ),
    )
    p.add_argument("--piece", required=True, metavar="PIECE_ID",
                   help="piece to redo (e.g. g1-p1)")
    p.add_argument("--stage", required=True, choices=PIECE_STAGES,
                   help="stage to rerun from scratch")
    p.add_argument("--reason", required=True,
                   help="why this redo is needed (goes into the ledger)")
    p.add_argument("--triggered-by", required=True, dest="triggered_by",
                   help="what failure triggered this (e.g. 'claim_diff verification')")
    p.add_argument("--keep-reviews", action="store_true", dest="keep_reviews",
                   help="skip the group_review + cross_group reset for a "
                        "review-irrelevant change (e.g. a pure typo fix). "
                        "The ledger records reviews_kept: true with the reason.")

    p = sub.add_parser("barrier", help="may any piece start? exits 3 if not")
    p.add_argument("--group", help="also require this group's research stage")

    p = sub.add_parser("source", help="record a source file's polarity and role")
    p.add_argument("--path", required=True, help="path to the source file")
    p.add_argument("--polarity", required=True, choices=SOURCE_POLARITIES,
                   help="'owned' -> floor (want high); 'third_party' -> ceiling (want low)")
    p.add_argument("--role", required=True, choices=SOURCE_ROLES,
                   help="'content' -> routed into pieces; 'voice' -> feeds the voice "
                        "anchor, never routed; 'both' -> routed AND the voice reference")
    p.add_argument("--piece", default=None, metavar="PIECE_ID",
                   help="scope this source to one piece (e.g. g1-p1). "
                        "Absent means run-level/shared.")

    p = sub.add_parser("sources", help="list recorded source paths, one per line")
    p.add_argument("--polarity", choices=SOURCE_POLARITIES,
                   help="filter to one polarity class")
    p.add_argument("--role", choices=SOURCE_ROLES,
                   help="filter to one role class (content|voice|both)")
    p.add_argument("--piece", default=None, metavar="PIECE_ID",
                   help="filter to sources scoped to this piece only "
                        "(run-level/shared entries are not included in piece-filtered output)")

    p = sub.add_parser("deliver",
                       help="copy each piece's final.md to a distinctly named file")
    p.add_argument("--out-dir", required=True, metavar="DIR",
                   help="directory to write delivery files into")

    p = sub.add_parser(
        "reanchor",
        help=("re-record a voice piece's readable.md anti-swap hash after a small "
              "in-place fix, without reopening spoken_to_written"))
    p.add_argument("--piece", required=True, metavar="PIECE_ID")
    p.add_argument("--artifact", required=True, metavar="PATH",
                   help="the piece's readable.md (the shipped file being re-measured)")
    p.add_argument("--reason", default=None,
                   help="why the shipped bytes were re-anchored (recorded on the stage)")

    sub.add_parser(
        "next",
        help=(
            "print the single next action this run requires, with the exact "
            "command to run after completing it (delegates to driver.py)"
        ),
    )

    p = sub.add_parser("params", help="change a run parameter (the only write path)")
    p.add_argument("--update", action="store_true", required=True,
                   help="change a parameter value")
    p.add_argument("--key", required=True)
    p.add_argument("--value", required=True,
                   help="new value, parsed as JSON (e.g. '{\"min\":80}' or '\"skipped\"')")
    p.add_argument("--override", action="store_true",
                   help="required to change a LOCKED parameter; logs + raises a flag")
    p.add_argument("--reason", help="why (required with --override)")
    p.add_argument("--by", help="who authorised the override")

    p = sub.add_parser(
        "add-piece",
        help=(
            "append a newly discovered content piece to an existing group after "
            "init — enables the intake→research→ideate→init→add-piece flow where "
            "the full piece list is not known at init time"
        ),
    )
    p.add_argument("--group", required=True, help="group id to append to (e.g. g1)")
    p.add_argument("--topic", required=True, help="topic string for the new piece")
    p.add_argument("--brief", default=None, help="optional brief for the new piece")

    p = sub.add_parser(
        "set-field",
        help=(
            "set a post-init group field (voice, keyword_cluster) through the "
            "sanctioned locked read-modify-write — removes any reason for an "
            "agent to hand-edit manifest.json"
        ),
    )
    p.add_argument("--group", required=True, help="group id to update (e.g. g1)")
    p.add_argument("--field", required=True,
                   help=f"field to set; allowed: {', '.join(SETTABLE_GROUP_FIELDS)}")
    p.add_argument("--value", required=True,
                   help="new value, parsed as JSON (e.g. '{\"kind\":\"profile\","
                        "\"name\":\"ann_handley\"}' or '\"my-cluster\"')")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "init":
            raw = sys.stdin.read() if a.spec == "-" else open(a.spec).read()
            create(a.run_dir, json.loads(raw))
            print(summary(load(a.run_dir)))
            return 0

        if a.cmd == "show":
            manifest_data = load(a.run_dir, a.allow_moved)
            divergences = context_divergence(a.run_dir, manifest_data)
            if divergences:
                print(
                    f"WARNING: context/manifest divergence — "
                    f"{len(divergences)} contradiction(s) in context.json "
                    f"have no open manifest flag:"
                )
                for d in divergences:
                    print(f"  {d}")
                print()
            print(summary(manifest_data))
            return 0

        if a.cmd == "json":
            print(json.dumps(load(a.run_dir, a.allow_moved), indent=2))
            return 0

        if a.cmd == "sources":
            data = load(a.run_dir, a.allow_moved)
            entries = data.get("sources", [])
            if a.polarity:
                entries = [e for e in entries if e["polarity"] == a.polarity]
            if a.role:
                entries = [e for e in entries if e.get("role") == a.role]
            if a.piece is not None:
                entries = [e for e in entries if e.get("piece") == a.piece]
            for e in entries:
                print(e["path"])
            return 0

        if a.cmd == "deliver":
            written = deliver_pieces(a.run_dir, a.out_dir)
            for pid, dest in written:
                print(f"{pid} -> {dest}")
            return 0

        if a.cmd == "barrier":
            reasons = barrier(load(a.run_dir, a.allow_moved), a.group)
            if not reasons:
                print("BARRIER OPEN - pieces may start")
                return 0
            print("BARRIER CLOSED - no piece may start:", file=sys.stderr)
            for r in reasons:
                print(f"  {r}", file=sys.stderr)
            return BARRIER_CLOSED

        if a.cmd == "next":
            # Lazy import to avoid circular import: driver.py does
            # `import manifest as M` at module level; a top-level import here
            # would create a cycle.  Importing inside the branch is safe and
            # is the same pattern used by the `stage produce` branch above for
            # `import voice as V`.
            import driver as D  # noqa: PLC0415
            action = D.next_action(load(a.run_dir, a.allow_moved))
            print(D.format_action(action))
            return 0

        if a.cmd == "resolve":
            closed = [0]

            def _resolve(d):
                # A contradiction is never closed from here (FROZEN).
                # close_flags itself has no kind filter and must not grow one -
                # context.py's rule() and reconcile() are legitimate callers that
                # DO close contradictions. The guard belongs at the only surface an
                # agent can reach, which is this one.
                #
                # Without it, an agent blocked by the completion check could call
                # `resolve --refs <id> --by itself`, close the contradiction, and
                # complete the run - dismissing a conflict no person ever ruled on.
                wanted = set(a.refs)
                blocked = [f for f in d["flags"]
                           if not f["resolved"]
                           and f["kind"] in ("contradiction", "param_override",
                                             "claim_override")
                           and wanted & set(f.get("refs") or [])]
                if blocked:
                    kinds = "/".join(sorted({f["kind"] for f in blocked}))
                    raise ManifestError(
                        f"{len(blocked)} of the matching flags are {kinds} "
                        "flags, which `resolve` does not close. A contradiction "
                        "is resolved by a person ruling on it (context.py "
                        "--run-dir <run> rule ...); a param_override is accepted "
                        "by a person (manifest.py --run-dir <run> accept-override "
                        "--key <param> --by <who>); a claim_override is accepted "
                        "by a person (manifest.py --run-dir <run> accept-claim "
                        "--piece <id> --by <who>). All record who decided; this "
                        "would only hide the flag. Nothing was closed."
                    )
                closed[0] = close_flags(d, a.refs, a.by)

            update(a.run_dir, _resolve, allow_moved=a.allow_moved)
            print(f"closed {closed[0]} flag(s)")
            return 0

        if a.cmd == "accept-override":
            update(a.run_dir, lambda d: accept_override(
                d, a.key, a.by, a.note), allow_moved=a.allow_moved)
            print(f"accepted override of '{a.key}' by {a.by}")
            return 0

        if a.cmd == "accept-claim":
            update(a.run_dir, lambda d: accept_claim(
                d, a.piece, a.by, a.note), allow_moved=a.allow_moved)
            print(f"accepted claim_override for piece '{a.piece}' by {a.by}")
            return 0

        if a.cmd == "params":
            try:
                value = json.loads(a.value)
            except json.JSONDecodeError as e:
                ap.error(f"--value must be JSON: {e}")
            update(a.run_dir, lambda d: set_parameter(
                d, a.key, value, override=a.override, reason=a.reason, by=a.by),
                allow_moved=a.allow_moved)
            print(f"parameter '{a.key}' set")
            return 0

        if a.cmd == "add-piece":
            new_id = [None]

            def _add_piece(d):
                p = add_piece(d, a.group, a.topic, brief=a.brief)
                new_id[0] = p["id"]

            update(a.run_dir, _add_piece, allow_moved=a.allow_moved)
            print(new_id[0])
            return 0

        if a.cmd == "set-field":
            try:
                value = json.loads(a.value)
            except json.JSONDecodeError as e:
                ap.error(f"--value must be JSON: {e}")
            update(a.run_dir, lambda d: set_group_field(d, a.group, a.field, value),
                   allow_moved=a.allow_moved)
            print(f"group '{a.group}' field '{a.field}' set")
            return 0

        if a.cmd == "flag":
            # Two ways to file a contradiction that looks raised and is not.
            # Both are cheap to make and impossible to notice afterwards, and
            # both got worse when `complete` started matching on this string:
            # a near-miss used to only fail to shut the barrier, and now it
            # also fails to hold the run open.
            if a.kind != "contradiction" and a.kind.strip().lower() == "contradiction":
                raise ManifestError(
                    f"'{a.kind}' is not the kind that blocks the run. The literal "
                    "string 'contradiction' is what barrier() and `set-status "
                    "complete` match on, exactly. A near-miss files a flag that "
                    "shows up in the queue and blocks nothing."
                )
            # A contradiction with no refs cannot be closed by anything:
            # close_flags matches on refs, and it is the only closing path.
            # That is a permanent block on completion created by one typo.
            if a.kind == "contradiction" and not a.refs:
                raise ManifestError(
                    "A contradiction flag needs --refs: the entry ids it is "
                    "about. Flags are closed by matching refs, so one filed "
                    "without them can never be closed and would block "
                    "`set-status complete` for the life of the run. Note that "
                    "context.py files contradictions itself, with refs, "
                    "whenever an entry is appended with --contradicts."
                )

        if a.cmd == "stage" and a.stage == "produce" and a.status == RUNNING:
            import voice as V
            # A producing dispatch is for one piece, which belongs to one group.
            # Check that group's own voice anchor (voice.emit falls back to the
            # run-level anchor when the group has none of its own, so runs with a
            # single shared anchor are unaffected). Before per-group anchors this
            # checked one run-level reference, which meant a two-group run could
            # dispatch g1 against g2's voice, or lose g1's anchor when g2's was
            # recorded over it — the collapse this scope closes.
            gid = a.target.split("-")[0]
            try:
                V.emit(a.run_dir, scope=gid)
            except V.VoiceError as e:
                raise ManifestError(
                    f"No usable voice reference for {a.target} (voice scope '{gid}'): {e}\n"
                    "The producing dispatch requires a voice reference for this group. "
                    "Record one with:\n"
                    f"  voice.py --run-dir {a.run_dir} record --kind profile --name <author_profile> --scope {gid}\n"
                    f"  voice.py --run-dir {a.run_dir} record --kind user --text \"<their writing>\" --scope {gid}"
                )

            # A reference existing is not enough. A chosen anchor can be lost and
            # the user's own transcript improvised in its place, with emit() above
            # satisfied because *a* reference sat on disk. This gate makes the
            # recorded reference prove it is the DECLARED choice, so a lost or
            # scavenged anchor can no longer pass.
            declared = None
            for _g in load(a.run_dir, a.allow_moved).get("groups", []):
                if _g.get("id") == gid:
                    declared = _g.get("voice")
                    break
            if not declared:
                raise ManifestError(
                    f"Group '{gid}' declares no voice choice, so {a.target} may not "
                    "start producing. A voice reference exists on disk, but nothing "
                    "records that it was CHOSEN rather than improvised — the exact gap "
                    "this gate closes. Declare the group's voice in "
                    'the run spec, e.g. "voice": {"kind": "profile", "name": '
                    '"<author>"}, or {"kind": "routed"} for a group whose voice is '
                    'its own routed content, or {"kind": "user"} for a real user sample.'
                )
            recorded = V.recorded_anchor(a.run_dir, scope=gid)
            if recorded is None:
                raise ManifestError(
                    f"Group '{gid}' declares voice {declared} but no anchor sidecar "
                    f"was recorded for {a.target}. Record the declared choice with "
                    f"voice.py before dispatching production."
                )
            declared_kind = declared.get("kind")
            mismatch = declared_kind != recorded.get("kind") or (
                declared_kind == "profile"
                and declared.get("name") != recorded.get("name")
            )
            if mismatch:
                raise ManifestError(
                    f"Group '{gid}' declares voice {declared} but the recorded "
                    f"reference is {recorded}. {a.target} is refused: the anchor on "
                    "disk is not the declared choice. Either re-record the chosen "
                    "anchor with voice.py, or correct the spec's declaration if the "
                    "choice genuinely changed. This is the check that catches an "
                    "improvised transcript standing in for the chosen profile."
                )

        # spoken_to_written passed must be recorded with --artifact pointing at
        # the piece's readable.md.
        # readable.md is the file a g2 piece SHIPS and no recomputable gate
        # covers it - spoken breaks claim_diff's basis by design - so the hash
        # recorded at close (set_stage, cur["output"]) is its only anti-swap
        # anchor at delivery. Without this binding an orchestrator could close
        # spoken passed with no artifact, or one pointing at a side file, and
        # ship an unmeasured readable.md whose absent/side hash never catches the
        # swap - the same measured-vs-shipped hole the verify --artifact binding
        # closes for final.md. CLI-only, matching the _cli_call trust model of
        # the other production guards (library callers and tests may close spoken
        # without laying down a real readable.md). normcase folds case on
        # macOS/Windows and is a no-op on Linux; two paths that are the same file
        # compare equal, nothing else does.
        if a.cmd == "stage" and a.stage == "spoken_to_written" and a.status == PASSED:
            if not a.artifact:
                raise ManifestError(
                    "spoken_to_written passed must be recorded with --artifact "
                    "pointing at the piece's readable.md. It is the file this "
                    "piece ships and no recomputable gate covers it, so the hash "
                    "recorded now is its only anti-swap anchor at delivery."
                )
            expected_artifact = os.path.normcase(os.path.abspath(
                os.path.join(a.run_dir, "pieces", a.target, "readable.md")
            ))
            given_artifact = os.path.normcase(os.path.abspath(a.artifact))
            if given_artifact != expected_artifact:
                raise ManifestError(
                    f"spoken_to_written --artifact must point at the piece's "
                    f"shipped file (pieces/{a.target}/readable.md). The hash must "
                    "be of the exact bytes deliver will hand to the owner - "
                    "otherwise a later swap of readable.md ships unmeasured "
                    "content while the recorded hash refers to a side file.\n"
                    f"  expected: {expected_artifact}\n"
                    f"  given:    {given_artifact}"
                )

        # Artifact binding: the verify guard is scoped to cmd==verify
        # so that a.piece is always defined (it is not a "stage" arg).
        if a.cmd == "verify":
            # Artifact binding: the CLI
            # verify path must enforce that --artifact resolves to
            # pieces/<piece>/final.md - the output of the de-slop bracket, which
            # is exactly what length/overlap/claim_diff measure.  The stale-check
            # re-hashes the named artifact — so if an orchestrator records verify
            # pointing at a side measurement-file and then swaps final.md,
            # deliver ships tampered, unmeasured content whose side-file hash
            # still passes.  Binding verify to final.md closes this.
            #
            # Because spoken_to_written runs last, final.md is not always the
            # SHIPPED file: for a g2 piece it rewrites final.md into readable.md,
            # which ships instead.  That does not
            # loosen this binding - the recomputable checks still measure the
            # de-slop bracket's final.md and must anchor to it.  readable.md is a
            # SEPARATE contract: it carries spoken's own anti-swap hash
            # (set_stage records cur["output"], deliver_pieces re-checks it),
            # because a recomputable gate cannot vouch for a file whose whole
            # purpose is to break claim_diff's basis.  Two shipped-file contracts,
            # one per producing stage; each shipped file is hash-anchored to the
            # bytes its stage recorded.  FROZEN (the rule is structural and
            # derives from the shipped-file contract; the exact wording is MOVABLE).
            # Library callers and existing tests that call set_verification()
            # directly are exempt — this guard is CLI-only, matching the same
            # _cli_call trust model used by the other production guards (skip,
            # de_slop prereq, voice anchor).
            # normcase on both sides so the comparison matches the filesystem's
            # own case semantics: a no-op on case-sensitive Linux, a lowercase
            # fold on case-insensitive macOS/Windows. Without it, an orchestrator
            # naming `.../final.MD` on macOS — the same file the pipeline shipped —
            # is spuriously rejected. It never
            # loosens the guard: two paths that are the same file compare equal,
            # nothing else does.
            expected_artifact = os.path.normcase(os.path.abspath(
                os.path.join(a.run_dir, "pieces", a.piece, "final.md")
            ))
            given_artifact = os.path.normcase(os.path.abspath(a.artifact))
            if given_artifact != expected_artifact:
                raise ManifestError(
                    f"verify --artifact must point at the piece's shipped file "
                    f"(pieces/{a.piece}/final.md).  The recomputation must be "
                    "on the exact bytes that deliver will hand to the owner — "
                    "otherwise a later swap of final.md ships unmeasured content "
                    "while the verification record refers to a side file whose "
                    "hash still passes.\n"
                    f"  expected: {expected_artifact}\n"
                    f"  given:    {given_artifact}"
                )

        # verify --ok true is refused when the stage has not yet run.
        # The two subsystems (stage status and verification record) reconcile at
        # write time, preventing the manifest from simultaneously asserting a gate
        # never ran and that it passed.
        def _verify_op(d):
            if a.ok == "true":
                piece_obj, _ = find_target(d, a.piece)
                stage_st = piece_obj["stages"].get(a.check, {}).get("status", PENDING)
                if stage_st not in (PASSED, FAILED, SKIPPED):
                    raise ManifestError(
                        f"Cannot verify {a.piece}.{a.check} as passed: "
                        f"stage '{a.check}' is '{stage_st}', not terminal "
                        "(passed/failed/skipped). "
                        "Open and close the stage before recording its "
                        "verification."
                    )
            set_verification(d, a.piece, a.check, a.ok == "true",
                             a.detail, a.override, a.artifact)

        # Completion leg: all stages must reach a terminal status before
        # the run can be marked complete. A run with pending stages cannot honestly
        # claim to have finished.
        def _set_status_op(d):
            # The completion guard now lives in set_run_status itself, so every
            # caller is covered - not just this CLI path. Nothing to re-check here.
            set_run_status(d, a.status)

        def _reanchor_op(d):
            expected = os.path.normcase(os.path.abspath(
                os.path.join(a.run_dir, "pieces", a.piece, "readable.md")))
            given = os.path.normcase(os.path.abspath(a.artifact))
            if given != expected:
                raise ManifestError(
                    "reanchor --artifact must point at the piece's shipped file "
                    f"(pieces/{a.piece}/readable.md) - the anti-swap hash must "
                    "measure the exact bytes delivery will hand over.\n"
                    f"  expected: {expected}\n  given:    {given}"
                )
            reanchor_shipped(d, a.piece, a.artifact, reason=a.reason)

        def _redo_op(d):
            rec = redo_stage(d, a.piece, a.stage, a.reason, a.triggered_by,
                             keep_reviews=a.keep_reviews)
            print(
                f"redo recorded: {a.piece}.{a.stage} reset to pending. "
                f"Downstream reset: {rec['downstream_reset'] or 'none'}. "
                f"Verification cleared. "
                f"Archived paths: {len(rec['archived_paths'])}."
            )

        # Reconcile gate_runs from disk at a produce close so
        # the PRODUCE_ATTEMPT_CAP check at ~706 fires on disk-truth even when
        # --gate-runs is omitted.  Placement: CLI boundary (here, where --run-dir
        # is available), not inside set_stage, so set_stage stays a pure library
        # function usable without a directory (dir-less unit tests are unaffected).
        #
        # Semantics (from skills/orchestrate/SKILL.md): gate_runs = number of
        # draft.attempt*.md snapshots + 1 (each snapshot is a failed attempt; the
        # final passing attempt writes draft.md, no snapshot).  A clean
        # single-attempt produce has 0 snapshots → disk_gate_runs = 1 (≤ cap).
        #
        # If --gate-runs was explicitly passed AND the disk count disagrees, refuse.
        # Two sources of truth that contradict cannot be reconciled silently —
        # mirroring the overlap.py --run-dir + --owned-source refusal.
        # gate_runs only exists on the 'stage' subcommand's Namespace; other
        # subcommands (verify, spend, flag, …) do not define it.
        _reconciled_gate_runs = getattr(a, "gate_runs", None)
        if a.cmd == "stage" and a.stage == "produce":
            piece_dir = os.path.join(a.run_dir, "pieces", a.target)
            if os.path.isdir(piece_dir):
                _snapshots = [
                    f for f in os.listdir(piece_dir)
                    if f.startswith("draft.attempt") and f.endswith(".md")
                ]
                _disk_gate_runs = len(_snapshots) + 1
                if a.gate_runs is not None and a.gate_runs != _disk_gate_runs:
                    raise ManifestError(
                        f"produce --gate-runs {a.gate_runs} disagrees with the "
                        f"live draft.attempt*.md snapshot count (disk says "
                        f"{_disk_gate_runs}: {len(_snapshots)} snapshot(s) + 1 "
                        "for the final pass). Pass --gate-runs only when it "
                        "matches disk-truth, or omit it to use the disk count. "
                        "Two contradicting sources of truth cannot be reconciled "
                        "silently."
                    )
                _reconciled_gate_runs = _disk_gate_runs

        ops = {
            "stage": lambda d: set_stage(d, a.target, a.stage, a.status,
                                          a.detail, a.artifact,
                                          gate_runs=_reconciled_gate_runs,
                                          _cli_call=True),
            "verify": _verify_op,
            "spend": lambda d: spend(d, a.bucket, a.n),
            "flag": lambda d: add_flag(d, a.target, a.kind, a.message,
                                       key=a.key, refs=a.refs),
            "set-status": _set_status_op,
            "source": lambda d: add_source(d, a.path, a.polarity, a.piece, role=a.role),
            "redo": _redo_op,
            "reanchor": _reanchor_op,
        }
        update(a.run_dir, ops[a.cmd], allow_moved=a.allow_moved)
        if a.cmd == "reanchor":
            print(f"re-anchored readable.md for {a.piece}: shipped bytes re-measured.")
        if a.cmd == "set-status":
            # Speak on success. A silent exit 0 gave the caller no way to tell a
            # granted completion from a no-op; the produce skill instructs the
            # orchestrator to require this line before treating the run as done.
            if a.status == "complete":
                print("run marked complete: every stage is resolved (passed or skipped).")
            else:
                print(f"run status set to '{a.status}'.")
        return 0

    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
