"""Phase 2 v0 slash commands: /policy-changes, /policy-approve, /policy-revert.

Audit display + manual approval/revert primitives. Reuses the existing
slash-handler call shape; the dispatcher in slash_dispatcher.py wires
them in.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from opencomputer.agent.policy_audit import PolicyAuditLogger

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SlashOutput:
    text: str
    ok: bool = True


async def handle_policy_changes(*, db, args: str = "") -> SlashOutput:
    """``/policy-changes [--days N]`` — show last N days of changes."""
    days = 7
    parts = args.strip().split()
    if "--days" in parts:
        try:
            i = parts.index("--days")
            days = int(parts[i + 1])
        except (IndexError, ValueError):
            return SlashOutput(
                text="usage: /policy-changes [--days N]", ok=False,
            )

    cutoff = time.time() - days * 86400
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, ts_drafted, knob_kind, target_id, reason, status, "
            "approval_mode, recommendation_engine_version "
            "FROM policy_changes WHERE ts_drafted >= ? "
            "ORDER BY ts_drafted DESC",
            (cutoff,),
        ).fetchall()

    if not rows:
        return SlashOutput(text=f"No policy changes in the last {days} days.")

    lines = [f"Policy changes in the last {days} days:"]
    for row in rows:
        cid = row["id"]
        ts_str = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(row["ts_drafted"]),
        )
        lines.append(
            f"  [{ts_str}] {cid[:8]}  {row['knob_kind']} → {row['target_id']}  "
            f"({row['status']}, mode={row['approval_mode']}, "
            f"engine={row['recommendation_engine_version']})\n"
            f"    reason: {row['reason']}"
        )
    return SlashOutput(text="\n".join(lines))


async def handle_policy_approve(
    *, db, args: str, hmac_key: bytes,
) -> SlashOutput:
    """``/policy-approve <id>`` — approve a pending_approval change."""
    cid = args.strip().split()[0] if args.strip() else ""
    if not cid:
        return SlashOutput(text="usage: /policy-approve <id>", ok=False)

    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, status, knob_kind, target_id, new_value "
            "FROM policy_changes WHERE id LIKE ? || '%'",
            (cid,),
        ).fetchone()
        if not row:
            return SlashOutput(
                text=f"no policy change matching {cid}", ok=False,
            )
        full_id = row["id"]
        status = row["status"]
        knob_kind = row["knob_kind"]
        target_id = row["target_id"]
        new_value_json = row["new_value"]

        if status != "pending_approval":
            return SlashOutput(
                text=f"change {cid[:8]} is in status '{status}', "
                "not 'pending_approval'",
                ok=False,
            )

        new_v = json.loads(new_value_json)
        if knob_kind == "recall_penalty":
            # Compute baseline + apply
            mean, std = _compute_baseline(conn)
            conn.execute(
                "UPDATE policy_changes SET pre_change_baseline_mean = ?, "
                "pre_change_baseline_std = ? WHERE id = ?",
                (mean, std, full_id),
            )
            conn.execute(
                "UPDATE episodic_events SET recall_penalty = ?, "
                "recall_penalty_updated_at = ? WHERE id = ?",
                (new_v["recall_penalty"], time.time(), target_id),
            )
        else:
            return SlashOutput(
                text=f"unknown knob_kind: {knob_kind}", ok=False,
            )

        audit = PolicyAuditLogger(conn, hmac_key)
        audit.append_status_transition(
            full_id, "pending_evaluation",
            ts_applied=time.time(),
            approved_by="user",
        )

    return SlashOutput(
        text=f"approved {full_id[:8]}; will be evaluated after N=10 "
        "eligible turns",
    )


async def handle_policy_revert(
    *, db, args: str, hmac_key: bytes,
) -> SlashOutput:
    """``/policy-revert <id>`` — manual revert at any state except already-reverted."""
    cid = args.strip().split()[0] if args.strip() else ""
    if not cid:
        return SlashOutput(text="usage: /policy-revert <id>", ok=False)

    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, status, knob_kind, target_id, rollback_hook "
            "FROM policy_changes WHERE id LIKE ? || '%'",
            (cid,),
        ).fetchone()
        if not row:
            return SlashOutput(
                text=f"no policy change matching {cid}", ok=False,
            )
        full_id = row["id"]
        status = row["status"]
        knob_kind = row["knob_kind"]
        target_id = row["target_id"]
        rollback_hook_json = row["rollback_hook"]

        if status == "reverted":
            return SlashOutput(
                text=f"{full_id[:8]} is already reverted", ok=False,
            )

        rollback = json.loads(rollback_hook_json)
        if knob_kind == "recall_penalty":
            conn.execute(
                "UPDATE episodic_events SET recall_penalty = ?, "
                "recall_penalty_updated_at = ? WHERE id = ?",
                (rollback["value"], time.time(), target_id),
            )

        audit = PolicyAuditLogger(conn, hmac_key)
        audit.append_status_transition(
            full_id, "reverted",
            reverted_reason="user-initiated /policy-revert",
        )

    return SlashOutput(text=f"reverted {full_id[:8]}")


def _compute_baseline(conn) -> tuple[float, float]:
    cutoff = time.time() - 14 * 86400
    rows = conn.execute(
        "SELECT turn_score FROM turn_outcomes "
        "WHERE created_at >= ? AND turn_score IS NOT NULL",
        (cutoff,),
    ).fetchall()
    scores = [r[0] for r in rows]
    if len(scores) < 2:
        return (0.5, 0.1)
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    return (mean, var ** 0.5)
