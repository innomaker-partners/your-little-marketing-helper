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


# Default push elements. Net and lost are LOG-only — they inflate in
# multi-run histories and diverge from the active-count change when reactivations occur.
# The user's mental model is ACTIVE followers; the push leads with that.
DEFAULT_NOTIFICATION_ELEMENTS: list[str] = [
    "active_total", "new_total", "new_in_target", "in_target_active_total"
]

# Mobile push soft limit. The push is a "come look" nudge; the full per-segment detail
# always lives in notification.txt. A verbose custom elements list (e.g. per_segment_*
# on a large taxonomy) can run long, so the assembled message is truncated to this
# length with an ellipsis. Lead elements are rendered first, so they always survive.
_PUSH_MAX = 200


def _fold_counts(counts: dict) -> dict:
    """Case-fold a label->count map so a lookup by taxonomy casing still finds a bucket
    stored under a case-variant. WHY: build_label_counts keys by the classification value
    as it appears in the master CSV; legacy or hand-edited rows may carry a case variant
    (e.g. 'tech' vs the taxonomy's 'Tech'), which a taxonomy-cased lookup would miss and
    render as 0. Case-variants of the same label are summed, not overwritten."""
    out: dict = {}
    for k, v in (counts or {}).items():
        key = str(k).casefold()
        out[key] = out.get(key, 0) + v
    return out


def _render_token(token: str, summary: dict, fold: str) -> "str | None":
    """Render one notification element token to a display fragment.

    Returns None for unknown tokens — silently skipped so a user editing the config
    cannot break the pipeline. `fold` is casefold()d fallback label used to exclude
    the catch-all from every in-target calculation.

    Known tokens:
      active_total           -> "{N} active"
      new_total              -> "{N} new"
      new_in_target          -> "{N} in-target new"
      in_target_active_total -> "{N} in-target active"
      per_segment_active     -> "Label1 N1, Label2 N2" (non-fallback, active counts)
      per_segment_new        -> "Label1 N1, Label2 N2" (non-fallback, this run's new)
      net                    -> "{+/-N} net"   (opt-in, log-only by default)
      lost                   -> "{N} lost"     (opt-in, log-only by default)
    """
    if token == "active_total":
        return f"{summary.get('new_connections_total', 0)} active"
    if token == "new_total":
        net = summary.get("net_increase_total", 0)
        lost = summary.get("lost_connections", 0)
        return f"{net + lost} new"
    if token == "new_in_target":
        n = sum(c for lab, c in summary.get("new_label_counts", {}).items()
                if lab.casefold() != fold)
        return f"{n} in-target new"
    if token == "in_target_active_total":
        return f"{summary.get('in_target_active_total', 0)} in-target active"
    if token == "per_segment_active":
        labels = summary.get("labels") or []
        lc = _fold_counts(summary.get("label_counts", {}))
        parts = [f"{lab} {lc.get(lab.casefold(), 0)}"
                 for lab in labels if lab.casefold() != fold]
        return ", ".join(parts) if parts else None
    if token == "per_segment_new":
        labels = summary.get("labels") or []
        nc = _fold_counts(summary.get("new_label_counts", {}))
        parts = [f"{lab} {nc.get(lab.casefold(), 0)}"
                 for lab in labels if lab.casefold() != fold]
        return ", ".join(parts) if parts else None
    if token == "net":
        net = summary.get("net_increase_total", 0)
        signed = f"+{net}" if net >= 0 else str(net)
        return f"{signed} net"
    if token == "lost":
        return f"{summary.get('lost_connections', 0)} lost"
    return None  # unknown token — skip silently


def build_report_text(summary: dict) -> str:
    """The full follower-change summary, multi-line, as written to notification.txt.

    This is the LOG — it always shows everything: active total, net, lost, reactivated,
    per-segment new and active. It is NOT affected by the elements config; the config only
    governs the one-line push. The three headline lines and the dynamic per-label "Total
    <label>" lines are the original n8n report body (no hardcoded keys). The per-segment
    block sits between them: this run's NEW followers per in-target segment, then the
    active in-target total. In-target = every taxonomy label except the fallback, so it
    is emitted in taxonomy order and shows a 0 for a segment that gained nobody this
    run — stable for any user-defined taxonomy."""
    lines = [
        f"Overall connection number: {summary['new_connections_total']}",
        f"Net increase: {summary['net_increase_total']}",
        f"Lost connections: {summary['lost_connections']}",
        f"Reactivated: {summary.get('reactivated_count', 0)}",
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


def build_notification_message(summary: dict,
                                notif_config: "dict | None" = None) -> str:
    """The one-line push-notification message (kept well under the 200-char mobile limit).

    Renders from an ordered list of element tokens. When `notif_config` carries an
    `elements` list that governs; when absent (or notif_config is None),
    DEFAULT_NOTIFICATION_ELEMENTS is used — backward-compatible, existing callers that
    pass no config get active total, new count, in-target new, and in-target active.
    Net and lost are NOT in the default push: they diverge from the
    active-count change when reactivations occur, and they are already in the log.

    When the summary carries a `subject_label` (e.g. "ACME CEO"), it is prefixed to the
    message so the reader knows which subject this notification concerns. When absent,
    the generic "LinkedIn followers" prefix is used, preserving backward-compatible output.
    """
    elements = (notif_config or {}).get("elements") if notif_config else None
    if elements is None:
        elements = DEFAULT_NOTIFICATION_ELEMENTS
    fold = summary.get("fallback_label", "").casefold()
    parts = [r for tok in elements
             if (r := _render_token(tok, summary, fold)) is not None]
    # A misconfigured elements list (empty, or all-unknown/typo'd tokens) would otherwise
    # render a bare "LinkedIn followers: ." with no data. Fall back to the default set so a
    # config typo still delivers the useful default push rather than a meaningless one.
    if not parts:
        parts = [r for tok in DEFAULT_NOTIFICATION_ELEMENTS
                 if (r := _render_token(tok, summary, fold)) is not None]
    body = "; ".join(parts)
    subject = (summary.get("subject_label") or "").strip()
    prefix = f"{subject}, LinkedIn followers:" if subject else "LinkedIn followers:"
    msg = f"{prefix} {body}."
    # Enforce the mobile push limit the docstring promises: truncate a runaway custom
    # message (verbose per_segment_* on a big taxonomy) with an ellipsis. The lead
    # elements render first, so the headline (active total) always survives.
    if len(msg) > _PUSH_MAX:
        msg = msg[:_PUSH_MAX - 1].rstrip() + "…"
    return msg


def deliver(summary: dict, workdir: Path,
            notif_config: "dict | None" = None) -> str:
    """Persist the send-ready notification payload and return the report text.

    Writes two artifacts: `notification.txt` (the multi-line report, for a human) and
    `notification.json` ({"message", "report"} — what the skill/orchestrator sends). The
    actual Claude app notification is sent by the CALLER (see module docstring): a
    subprocess cannot call a Claude tool. Returns the report text so the runner can echo
    it. `notif_config` is the parsed `notification` block from config.json (or None to
    use the default element list)."""
    report = build_report_text(summary)
    message = build_notification_message(summary, notif_config)
    Path(workdir, "notification.txt").write_text(report)
    Path(workdir, "notification.json").write_text(
        json.dumps({"message": message, "report": report}, indent=2))
    return report
