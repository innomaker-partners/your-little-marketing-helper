#!/usr/bin/env python3
"""Driver — control inversion for content-at-scale runs.

The reliability failure this addresses: a model is told to run twelve stages,
runs eight, and reports twelve.  Nothing today distinguishes those two runs.

Philosophy:
  Stop asking the model to remember the sequence.  Let the sequence ask the
  model.

A `next` command reads the manifest and returns the single next action the
run requires, in imperative form, with the exact command to run afterward.
The script owns the order; the model owns the work.  A stage cannot be
forgotten because the agent is never asked to remember it.

Limitation stated rather than papered over: the Driver misses a model that
ignores `next` and does something else.  It removes the need for diligence
rather than the possibility of its absence.

This `next` command is available both here and on manifest.py:
  manifest.py --run-dir <run> next
  driver.py  --run-dir <run> next
They are the same command; driver.py is a thin standalone entry point.

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manifest as M


# Statuses that count as "cleanly done" for dependency purposes.
# `failed` is deliberately absent: downstream stages should not proceed past a
# failure without a retry or an explicit decision to skip the gate.  This
# mirrors the barrier() function in manifest.py, which treats PASSED and
# SKIPPED as terminal and treats FAILED as not yet resolved.
CLEAN_TERMINAL = frozenset({M.PASSED, M.SKIPPED})

# Statuses that need human or orchestrator attention before the run can move.
NEEDS_ATTENTION = frozenset({M.RUNNING, M.FLAGGED, M.FAILED})


def _is_clean(stage: dict) -> bool:
    """Stage is cleanly done: passed or skipped."""
    return stage["status"] in CLEAN_TERMINAL


def _is_pending(stage: dict) -> bool:
    """Stage has not been started."""
    return stage["status"] == M.PENDING


def _is_attention(stage: dict) -> bool:
    """Stage is in a state that blocks the run without being actionable."""
    return stage["status"] in NEEDS_ATTENTION


def _manifest_cmd(run_dir: str, rest: str) -> str:
    """Full manifest.py command string, including the run-dir prefix."""
    return f"manifest.py --run-dir {run_dir} {rest}"


# ---------------------------------------------------------------------------
# Action builders — one per stage type.  Each returns a dict with:
#   kind       "action" | "wait" | "human" | "retry" | "complete"
#   target     the manifest target string (run / g1 / g1-p1)
#   stage      the stage name
#   imperative plain-language instruction for what to do
#   command    the exact manifest.py command to run after completing the action
# ---------------------------------------------------------------------------


def _run_pre_action(run_dir: str, stage_name: str) -> dict:
    texts = {
        "context_intake": (
            "context_intake has not run.  Conduct the context intake interview "
            "with the owner using skills/context-intake/SKILL.md (or the replay "
            "harness for recorded sessions).  Capture run parameters, source "
            "material references, and any prohibitions from the owner."
        ),
        "source_intake": (
            "source_intake has not run.  Segment and route the raw source material "
            "using skills/source-intake/SKILL.md.  Record each source file with "
            "its polarity (owned / third_party) so the overlap gate knows the "
            "direction to measure."
        ),
    }
    return {
        "kind": "action",
        "target": "run",
        "stage": stage_name,
        "imperative": texts[stage_name],
        "command": _manifest_cmd(
            run_dir, f"stage --target run --stage {stage_name} --status passed"
        ),
    }


def _group_research_action(run_dir: str, gid: str) -> dict:
    return {
        "kind": "action",
        "target": gid,
        "stage": "research",
        "imperative": (
            f"{gid}'s research has not run.  Dispatch the research phase for "
            f"group {gid} (see produce/SKILL.md section 3).  Gather relevant "
            "source material and record any WebSearch spend with: "
            f"{_manifest_cmd(run_dir, f'spend --bucket {gid} -n <count>')}"
        ),
        "command": _manifest_cmd(
            run_dir, f"stage --target {gid} --stage research --status passed"
        ),
    }


def _piece_produce_action(run_dir: str, pid: str, topic: str) -> dict:
    return {
        "kind": "action",
        "target": pid,
        "stage": "produce",
        "imperative": (
            f"{pid} has not been produced.  Write the producing brief at "
            f"pieces/{pid}/producing_brief.md first, verify the barrier is open "
            f"({_manifest_cmd(run_dir, 'barrier')}), then dispatch the producing "
            f"agent for piece {pid} (topic: \"{topic}\") using "
            "skills/produce/SKILL.md."
        ),
        "command": _manifest_cmd(
            run_dir, f"stage --target {pid} --stage produce --status passed"
        ),
    }


def _group_review_action(run_dir: str, gid: str, pieces: list) -> dict:
    piece_ids = ", ".join(p["id"] for p in pieces)
    return {
        "kind": "action",
        "target": gid,
        "stage": "group_review",
        "imperative": (
            f"{gid}'s group review has not run.  Dispatch the group-review stage "
            f"over {piece_ids}.  Run "
            f"scripts/groupreview.py --run-dir {run_dir} --group {gid} to surface "
            "candidate repetitions and format findings, judge them, and record the "
            "report.  If findings stand, set the stage flagged; once every finding "
            "is worked (forward edits to the offending pieces) and the check "
            "re-runs clean, set it passed."
        ),
        "command": _manifest_cmd(
            run_dir, f"stage --target {gid} --stage group_review --status passed"
        ),
    }


def _piece_gate_action(run_dir: str, pid: str, gate: str) -> dict:
    texts = {
        "length": (
            f"Run the length gate on {pid}.  Verify the artifact is within the "
            "configured word-count bounds using scripts/length.py.  The script "
            "exits 3 on failure."
        ),
        "overlap": (
            f"Run the overlap gate on {pid}.  Verify source overlap meets the "
            "floor (owned sources) or ceiling (third-party sources) threshold "
            "using scripts/overlap.py with --run-dir."
        ),
        "fact_check": (
            f"Run fact-check on {pid} using skills/fact-check/SKILL.md.  Verify "
            "every claim against the sourced context entries, including the "
            "composition pass: does the source support the connection, or only "
            "the parts?"
        ),
        "spoken_to_written": (
            f"Run the spoken-to-written pass on {pid} using "
            "skills/spoken-to-written/SKILL.md.  This is the LAST piece stage and "
            "is CONDITIONAL: it runs only when the piece's content and voice come "
            "from the same rambling verbatim source (a role=both transcript). If "
            "the piece's content is researched findings with a separate voice "
            "profile, the skill marks the stage skipped and stops, and "
            f"pieces/{pid}/final.md ships unchanged.  When it runs, it reads "
            f"pieces/{pid}/final.md — the finished de-slop output, already "
            "fact-checked, claim-diffed and de-slopped — and makes the owner's "
            "verbatim spoken words readable WITHOUT paraphrasing: only cutting "
            "obstructing filler, reordering fragments verbatim, and adding "
            f"conjunctions.  It writes pieces/{pid}/readable.md, which is the file "
            "this piece SHIPS.  There is no gate after it and it carries no "
            "claim-freeze — its job is precisely to change the sentences "
            "claim_diff measured — so its integrity is a hash instead: you MUST "
            f"close it with --artifact pointing at pieces/{pid}/readable.md, whose "
            "hash is the anti-swap anchor delivery re-checks."
        ),
        "anti_slop": (
            f"Generate the anti-slop report for {pid}.  Run antislop.py bare on "
            f"pieces/{pid}/verified.md (it takes no profile; a voice anchor never "
            f"relaxes the checker) and save it: "
            f"antislop.py pieces/{pid}/verified.md > pieces/{pid}/antislop.txt.  "
            "This report is de-slop's input, not a gate — it never fails the "
            "piece on the checker's exit code; it records that the report exists "
            "for the next stage to read."
        ),
        "de_slop": (
            f"Run de-slop on {pid} using skills/de-slop/SKILL.md.  Remove "
            f"AI-writing markers, reading pieces/{pid}/antislop.txt (the "
            "anti-slop report generated in the previous stage) as a second pair "
            "of eyes — its flags are candidates for your judgment, never a "
            "checklist to zero out, and it never overrides a verified claim."
        ),
        "claim_diff": (
            f"Run claim-diff on {pid} using scripts/claimdiff.py.  Verify no "
            "claims were introduced or removed between draft and final."
        ),
    }
    # spoken_to_written must be closed with its output artifact: readable.md is
    # what a g2 piece ships and no verify-check covers it, so the hash recorded
    # at close is its only anti-swap anchor at delivery (the manifest CLI refuses
    # a passed spoken with no --artifact).  Every other gate closes without one.
    artifact_suffix = ""
    if gate == "spoken_to_written":
        artifact_suffix = f" --artifact {run_dir}/pieces/{pid}/readable.md"
    return {
        "kind": "action",
        "target": pid,
        "stage": gate,
        "imperative": texts[gate],
        "command": _manifest_cmd(
            run_dir,
            f"stage --target {pid} --stage {gate} --status passed{artifact_suffix}",
        ),
    }


def _run_post_action(run_dir: str, stage_name: str) -> dict:
    texts = {
        "cross_group": (
            "cross_group pass has not run.  Dispatch the cross-group comparison "
            "pass across all groups to identify inter-group contradictions and "
            "redundancies.  Record any contradictions as flags."
        ),
        "web_pass": (
            "web_pass has not run.  Run the optional web verification pass to "
            "confirm key claims against external sources.  Record WebSearch spend "
            f"as it happens with: {_manifest_cmd(run_dir, 'spend --bucket web_pass -n <count>')}"
        ),
    }
    return {
        "kind": "action",
        "target": "run",
        "stage": stage_name,
        "imperative": texts[stage_name],
        "command": _manifest_cmd(
            run_dir, f"stage --target run --stage {stage_name} --status passed"
        ),
    }


def _attention(run_dir: str, target: str, stage: str, stage_dict: dict) -> dict:
    status = stage_dict["status"]
    attempts = stage_dict.get("attempts", "?")
    if status == M.RUNNING:
        return {
            "kind": "wait",
            "target": target,
            "stage": stage,
            "imperative": (
                f"Stage {stage} on {target} is currently in progress "
                f"(status: running, attempts so far: {attempts}).  Wait for the "
                "dispatched agent to complete and record its result."
            ),
            "command": _manifest_cmd(run_dir, "show"),
        }
    if status == M.FLAGGED:
        return {
            "kind": "human",
            "target": target,
            "stage": stage,
            "imperative": (
                f"Stage {stage} on {target} is flagged and requires a human "
                "ruling.  Review the open flags and resolve or accept the piece."
            ),
            "command": _manifest_cmd(run_dir, "show"),
        }
    # status == M.FAILED
    return {
        "kind": "retry",
        "target": target,
        "stage": stage,
        "imperative": (
            f"Stage {stage} on {target} has failed "
            f"(attempts: {attempts}).  Review the failure detail, address the "
            "underlying issue, then retry."
        ),
        "command": _manifest_cmd(
            run_dir, f"stage --target {target} --stage {stage} --status running"
        ),
    }


# ---------------------------------------------------------------------------
# Core driver logic
# ---------------------------------------------------------------------------


def next_action(data: dict) -> dict:
    """Return the single next action the run requires.

    Ordering model
    --------------
    1. Run pre-stages (context_intake, source_intake) — always actionable.

    2. Per group (in manifest order):
       a. group.research — actionable once run pre-stages are clean.
       b. Per piece (in order): piece.produce — actionable once research clean.
       c. Per piece (in order): quality gates (length, overlap, fact_check,
          anti_slop, de_slop, claim_diff, spoken_to_written), each actionable
          once the previous gate in the same piece is clean.
          anti_slop sits before de_slop because it generates the report de_slop
          consumes; claim_diff closes the de-slop bracket.  spoken_to_written
          runs LAST, after the whole de-slop bracket:
          claim_diff and anti_slop are tied to de_slop as one agent, the way
          length and overlap are tied to produce, so the spoken pass belongs
          after all of them.  It is a conditional pass that makes a verbatim
          transcript's spoken grammar readable; it deliberately changes the
          sentences claim_diff just measured (so it can carry no gate of its own)
          and its output, readable.md, is what the piece ships.  It is skipped
          for pieces whose content and voice differ (the g1 case), and a skip
          counts as clean, so final.md ships unchanged and the chain completes.

       d. group.group_review — actionable only when EVERY piece has EVERY stage
          clean (produce AND all quality gates).  Placed LAST among group stages
          because it compares finished pieces — fact-checked, de-slopped, claim-
          diffed — not raw drafts.  A finding from group_review is worked in place
          (forward edits + re-run), not a gate-back to regenerate.

    3. Run post-stages (cross_group, web_pass) — actionable once all group reviews clean.

    Why this catches the E05 failure
    ---------------------------------
    E05 had status=complete with g1.group_review=pending and all three pieces
    showing length=pending, overlap=pending.  In this ordering:
      - context_intake=skipped (clean) → skip
      - source_intake=skipped (clean) → skip
      - g1.research=skipped (clean) → skip
      - 2b: all three produces=passed (clean) → no pending produce found
      - 2c: per-piece quality gates pending → RETURN FIRST QUALITY GATE
    The Driver surfaces the pending gate despite the manifest's status=complete claim.
    """
    run_dir = data["run_dir"]
    run_stages = data["stages"]

    # ------------------------------------------------------------------
    # 1. Run pre-stages
    # ------------------------------------------------------------------
    for stage_name in ("context_intake", "source_intake"):
        s = run_stages[stage_name]
        if _is_pending(s):
            return _run_pre_action(run_dir, stage_name)
        if _is_attention(s):
            return _attention(run_dir, "run", stage_name, s)

    # ------------------------------------------------------------------
    # 2. Per group
    # ------------------------------------------------------------------
    for g in data["groups"]:
        gid = g["id"]
        g_stages = g["stages"]
        pieces = g["pieces"]

        # 2a. Group research
        s = g_stages["research"]
        if _is_pending(s):
            return _group_research_action(run_dir, gid)
        if _is_attention(s):
            return _attention(run_dir, gid, "research", s)

        # 2b. Per-piece produce (only enters when research is clean; see above
        # for how the clean check on research gates entry to this section).
        if _is_clean(g_stages["research"]):
            for p in pieces:
                pid = p["id"]
                s = p["stages"]["produce"]
                if _is_pending(s):
                    return _piece_produce_action(run_dir, pid, p["topic"])
                if _is_attention(s):
                    return _attention(run_dir, pid, "produce", s)

        # 2c. Group review — actionable only when every piece has EVERY stage clean:
        # produce AND every per-piece quality gate.  This ensures group_review runs
        # over fully-finished pieces, after all per-piece work is done.  Gating on
        # produce alone was the bug: it released group_review before any quality gate
        # had run, so group_review could read un-fact-checked, un-de-slopped text.
        _gate_seq_for_review = ("length", "overlap", "fact_check", "anti_slop",
                                "de_slop", "claim_diff", "spoken_to_written")
        all_pieces_finished = all(
            _is_clean(p["stages"]["produce"])
            and all(_is_clean(p["stages"][gate]) for gate in _gate_seq_for_review)
            for p in pieces
        )
        if all_pieces_finished:
            s = g_stages["group_review"]
            if _is_pending(s):
                return _group_review_action(run_dir, gid, pieces)
            if _is_attention(s):
                return _attention(run_dir, gid, "group_review", s)

        # 2c (quality gates). Per-piece quality gates. Each gate is actionable
        # only when the previous gate in the same piece is clean.  These run
        # BEFORE group_review (step 2d above), so group_review sees finished
        # pieces, not raw drafts.  The `break` is defensive: if a gate is not
        # clean and not pending/attention, do not try to run subsequent gates.
        if _is_clean(g_stages["research"]):
            gate_seq = ("length", "overlap", "fact_check", "anti_slop",
                        "de_slop", "claim_diff", "spoken_to_written")
            for p in pieces:
                pid = p["id"]
                prev_name = "produce"
                for gate in gate_seq:
                    prev_stage = p["stages"][prev_name]
                    if not _is_clean(prev_stage):
                        break  # cannot proceed past a non-clean gate
                    cur_stage = p["stages"][gate]
                    if _is_pending(cur_stage):
                        return _piece_gate_action(run_dir, pid, gate)
                    if _is_attention(cur_stage):
                        return _attention(run_dir, pid, gate, cur_stage)
                    prev_name = gate

    # ------------------------------------------------------------------
    # 3. Run post-stages (actionable only when all group reviews are clean)
    # ------------------------------------------------------------------
    all_group_reviews_clean = all(
        _is_clean(g["stages"]["group_review"]) for g in data["groups"]
    )
    if all_group_reviews_clean:
        for stage_name in ("cross_group", "web_pass"):
            s = run_stages[stage_name]
            if _is_pending(s):
                return _run_post_action(run_dir, stage_name)
            if _is_attention(s):
                return _attention(run_dir, "run", stage_name, s)

    # ------------------------------------------------------------------
    # All stages are terminal (or pending but not yet actionable)
    # ------------------------------------------------------------------
    return {
        "kind": "complete",
        "target": None,
        "stage": None,
        "imperative": (
            "All pipeline stages are terminal.  The run has completed its "
            "pipeline.  Review the manifest and, if every stage closed "
            "correctly, record completion."
        ),
        "command": _manifest_cmd(run_dir, "set-status --status complete"),
    }


def format_action(action: dict) -> str:
    """Format an action dict as the human/agent-readable next-action string.

    Output form matches the design document's worked example:
      <imperative>.  Then: <command>
    separated so the command is visually distinct and easy to copy.
    """
    return f"{action['imperative']}\n\nThen: {action['command']}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Driver: reads a run manifest and returns the single next action "
            "the run requires.  The script owns the order; the model owns the work."
        )
    )
    ap.add_argument(
        "--run-dir", required=True,
        help="path to the run directory (must contain manifest.json)"
    )
    ap.add_argument(
        "--allow-moved", action="store_true",
        help="allow the manifest's recorded run_dir to differ from --run-dir"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "next",
        help=(
            "print the single next action this run requires, with the exact "
            "command to run after completing it"
        ),
    )

    a = ap.parse_args(argv)

    try:
        data = M.load(a.run_dir, allow_moved=a.allow_moved)
    except M.ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if a.cmd == "next":
        action = next_action(data)
        print(format_action(action))
        return 0

    return 0  # argparse makes unknown subcommands unreachable here


if __name__ == "__main__":
    sys.exit(main())
