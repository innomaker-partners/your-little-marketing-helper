"""
Report delivery for the linkedin-follower-tracker.

The run ends with a Claude app notification: in a
Claude-run context a push notification is the natural, zero-setup last step — no email,
no extra credential. Delivery is the invoking skill's job, NOT this script's. A plain
Python subprocess has no way to call a Claude tool, and the frozen architecture puts the
send in the "light skill" that invokes the pipeline ("...delivers the result as a Claude
app notification"). When the pipeline instead runs as a background task, the orchestrating
Claude session that launched it is the deliverer.

So this module does the one thing a subprocess CAN do: build a send-ready payload and
persist it. On completion the deliverer reads `notification.json` and sends its one-line
`message` as the Claude app notification. `notification.txt` keeps the fuller multi-line
report for a human reading the workdir directly. (This is why the send is not a call
inside `deliver`: the earlier "swap deliver for an actual notification" idea was
architecturally impossible — a subprocess can't reach the Claude app.)
"""

from __future__ import annotations

import json
from pathlib import Path


def build_report_text(summary: dict) -> str:
    """The full follower-change summary, multi-line, as written to notification.txt.

    The three headline lines and the dynamic per-label "Total <label>" lines are the
    original n8n report body (no hardcoded keys). The per-segment block sits between them:
    this run's NEW followers per in-target segment, then the active in-target total. In-target = every taxonomy label except the fallback, so
    it is emitted in taxonomy order and shows a 0 for a segment that gained nobody this
    run — stable for any user-defined taxonomy."""
    lines = [
        f"Overall connection number: {summary['new_connections_total']}",
        f"Net increase: {summary['net_increase_total']}",
        f"Lost connections: {summary['lost_connections']}",
    ]
    fold = summary.get("fallback_label", "").casefold()
    in_target_labels = [lab for lab in (summary.get("labels") or [])
                        if lab.casefold() != fold]
    new_counts = summary.get("new_label_counts", {})
    for lab in in_target_labels:
        lines.append(f"New {lab}: {new_counts.get(lab, 0)}")
    if in_target_labels:
        lines.append(f"Total in-target-segment followers: "
                     f"{summary.get('in_target_active_total', 0)}")
    for label, count in sorted(summary.get("label_counts", {}).items()):
        lines.append(f"Total {label}: {count}")
    return "\n".join(lines)


def build_notification_message(summary: dict) -> str:
    """The one-line push-notification message (kept well under the 200-char mobile
    limit). A push is a 'come look' nudge, so it leads with the run's actual change —
    net, new, lost — plus the current active total; the per-segment detail lives in
    notification.txt and the CSVs. Net is signed so a drop reads as a drop rather than a
    bare number.

    When the summary carries a `subject_label` (e.g. "ACME CEO"), it is prefixed to the
    message so the reader knows which subject this notification concerns. When absent,
    the generic "LinkedIn followers" prefix is used, preserving backward-compatible output.
    """
    net = summary["net_increase_total"]
    lost = summary["lost_connections"]
    new = net + lost  # net = new - lost, so `new` is recoverable without a new field
    active = summary["new_connections_total"]
    signed = f"+{net}" if net >= 0 else str(net)
    # In-target new = this run's new followers outside the fallback segment — the number
    # the owner most wants at a glance (49 target-segment gains reads very differently from
    # 213 raw). Sums new_label_counts over the non-fallback labels.
    fold = summary.get("fallback_label", "").casefold()
    in_target_new = sum(c for lab, c in summary.get("new_label_counts", {}).items()
                        if lab.casefold() != fold)
    body = (f"LinkedIn followers: {signed} net this run. "
            f"{new} new ({in_target_new} in-target), {lost} lost; {active} active total.")
    subject = (summary.get("subject_label") or "").strip()
    return f"{subject}, {body}" if subject else body


def deliver(summary: dict, workdir: Path) -> str:
    """Persist the send-ready notification payload and return the report text.

    Writes two artifacts: `notification.txt` (the multi-line report, for a human) and
    `notification.json` ({"message", "report"} — what the skill/orchestrator sends). The
    actual Claude app notification is sent by the CALLER (see module docstring): a
    subprocess cannot call a Claude tool. Returns the report text so the runner can echo
    it."""
    report = build_report_text(summary)
    message = build_notification_message(summary)
    Path(workdir, "notification.txt").write_text(report)
    Path(workdir, "notification.json").write_text(
        json.dumps({"message": message, "report": report}, indent=2))
    return report
