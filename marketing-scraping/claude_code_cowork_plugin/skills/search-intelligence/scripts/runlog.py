"""
Auto-logging: append a structured entry to a stage's RUN_LOG.md and commands.sh
after every actor run. Fixes the pilot's "logs written after the fact" gap.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def append_run_log(stage_dir, stage, run, item_count, note="", est_usd=None):
    """Append one entry to <stage_dir>/RUN_LOG.md."""
    p = Path(stage_dir) / "RUN_LOG.md"
    if not p.exists():
        p.write_text(f"# Run log: {stage}\n\n> Append-only. One entry per actor run.\n", encoding="utf-8")
    reported = run.get("usageTotalUsd")
    entry = [
        f"\n### {_now()}  ·  {stage}",
        f"- **Run ID:** `{run.get('id','?')}`  ·  **Dataset:** `{run.get('defaultDatasetId','-')}`  ·  **KV store:** `{run.get('defaultKeyValueStoreId','-')}`",
        f"- **Status:** {run.get('status','?')}  ·  **Items:** {item_count}",
        f"- **Cost:** reported ${reported if reported is not None else '?'} "
        f"(⚠ underreports; verify on dashboard)"
        + (f"  ·  **Pre-run estimate:** ~${est_usd:.2f}" if est_usd is not None else ""),
    ]
    if note:
        entry.append(f"- **Note:** {note}")
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(entry) + "\n")


def append_command(stage_dir, actor, actor_input, run):
    """Append a reproducible curl command to <stage_dir>/commands.sh."""
    p = Path(stage_dir) / "commands.sh"
    if not p.exists():
        p.write_text("#!/bin/bash\n# Reproducible Apify commands (auto-generated).\n"
                     "# APIFY_TOKEN must be set in the environment.\n", encoding="utf-8")
    path_actor = actor.replace("/", "~")
    inline = json.dumps(actor_input, ensure_ascii=False)
    block = [
        f"\n# {_now()}  ·  {actor}  (run {run.get('id','?')}, dataset {run.get('defaultDatasetId','-')})",
        f"curl -s -X POST \"https://api.apify.com/v2/acts/{path_actor}/runs?token=$APIFY_TOKEN&memory=4096&timeout=1800\" \\",
        f"  -H 'Content-Type: application/json' \\",
        f"  -d '{inline}'",
    ]
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")
