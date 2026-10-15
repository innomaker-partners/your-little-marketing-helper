"""
Auto-logging: append a structured entry to a stage's RUN_LOG.md and commands.sh
after every actor run.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _cost_line(run):
    """The RUN_LOG cost line: the run's charge as the bill counts it.

    WHY: this line used to print the raw usageTotalUsd
    read at the moment the run ended, with "underreports; verify on dashboard",
    because Apify fills a finished run's ledger late. apify_client now reads
    the charge only once it is final (_settle_charge), and on all 70 real runs read back from
    Apify actual_charge_usd of the settled record
    equalled what Apify charged (usageTotalUsd). So the line gives that figure,
    the same one the bill adds up. A run whose charges were still being
    recorded when the wait ran out (_charge_incomplete) keeps the warning."""
    import contextlib
    import io
    try:
        import apify_client as _A
        # Its warning for an unpriceable ledger was already printed when the
        # step was charged; do not print it a second time from here.
        with contextlib.redirect_stderr(io.StringIO()):
            charged = _A.actual_charge_usd(run)
    except Exception:  # noqa: BLE001 -- the log must never stop a run
        charged = None
    if charged is None:
        reported = run.get("usageTotalUsd")
        return (f"- **Cost:** reported ${reported if reported is not None else '?'} "
                f"(⚠ may be incomplete; verify on the Apify console)")
    if run.get("_charge_incomplete"):
        return (f"- **Cost:** ${charged:.6f} counted so far (⚠ Apify was still recording "
                f"this run's charges; verify on the Apify console)")
    return f"- **Cost:** ${charged:.6f} charged by Apify"


def append_run_log(stage_dir, stage, run, item_count, note="", est_usd=None):
    """Append one entry to <stage_dir>/RUN_LOG.md."""
    p = Path(stage_dir) / "RUN_LOG.md"
    if not p.exists():
        p.write_text(f"# Run log: {stage}\n\n> Append-only. One entry per actor run.\n", encoding="utf-8")
    entry = [
        f"\n### {_now()}  ·  {stage}",
        f"- **Run ID:** `{run.get('id','?')}`  ·  **Dataset:** `{run.get('defaultDatasetId','-')}`  ·  **KV store:** `{run.get('defaultKeyValueStoreId','-')}`",
        f"- **Status:** {run.get('status','?')}  ·  **Items:** {item_count}",
        _cost_line(run)
        + (f"  ·  **Pre-run estimate:** ~${est_usd:.2f}" if est_usd is not None else ""),
    ]
    if note:
        entry.append(f"- **Note:** {note}")
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(entry) + "\n")


def append_command(stage_dir, actor, actor_input, run, memory_mb=None):
    """Append a reproducible curl command to <stage_dir>/commands.sh."""
    p = Path(stage_dir) / "commands.sh"
    if not p.exists():
        p.write_text("#!/bin/bash\n# Reproducible Apify commands (auto-generated).\n"
                     "# APIFY_TOKEN must be set in the environment.\n", encoding="utf-8")
    path_actor = actor.replace("/", "~")
    # The input is written inside single
    # quotes, so a single quote in it (a business name like "Joe's Pizza")
    # ended the quote: the line was invalid shell, and a crafted name could
    # run a command when the file is replayed. Each ' is written as '\'' (close
    # the quote, an escaped quote, reopen), which replays as the same JSON.
    # run_phase._parse_commands_sh undoes it for lines in this form.
    inline = json.dumps(actor_input, ensure_ascii=False).replace("'", "'\\''")
    # The replay asks for the memory the call really used (the caller passes
    # run_phase._mem_for's value). A line that always said 4096 made a replay of a
    # 512 MB or 1024 MB call bill more start events than the run did. Without a
    # value the memory is left out, so Apify uses the actor's own default; a
    # figure the tool does not know is never written.
    query = (f"memory={int(memory_mb)}&" if memory_mb else "") + "timeout=1800"
    block = [
        f"\n# {_now()}  ·  {actor}  (run {run.get('id','?')}, dataset {run.get('defaultDatasetId','-')})",
        # The token goes in a header, never the address: a copied
        # line with ?token= put it in the address, where server and proxy logs
        # keep it. The variable stays unexpanded in the file.
        f"curl -s -X POST \"https://api.apify.com/v2/acts/{path_actor}/runs?{query}\" \\",
        f"  -H \"Authorization: Bearer $APIFY_TOKEN\" \\",
        f"  -H 'Content-Type: application/json' \\",
        f"  -d '{inline}'",
    ]
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")
