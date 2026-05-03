"""Phase 2 v0 slash commands: /policy-changes, /policy-approve, /policy-revert.

Audit display + manual approval/revert primitives.

Two surfaces, same logic:
  - Bare async handlers (handle_policy_*): used by tests + the CLI
    wrappers in cli.py (oc policy show).
  - SlashCommand subclasses (PolicyChangesCommand etc.): registered with
    the agent loop's slash dispatcher so users can type /policy-changes
    inside a chat turn.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.policy_audit_log import PolicyAuditLog
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

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
        audit_log = PolicyAuditLog(conn, hmac_key)
        audit_log.append_transition(
            change_id=full_id, status="pending_evaluation",
            actor="user", reason="manual /policy-approve",
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
        audit_log = PolicyAuditLog(conn, hmac_key)
        audit_log.append_transition(
            change_id=full_id, status="reverted",
            actor="user", reason="manual /policy-revert",
        )

    _publish_reverted_event(
        change_id=full_id,
        knob_kind=knob_kind,
        target_id=str(target_id),
        reverted_reason="user-initiated /policy-revert",
    )
    return SlashOutput(text=f"reverted {full_id[:8]}")


def _publish_reverted_event(**kwargs) -> None:
    try:
        from opencomputer.ingestion.bus import get_default_bus
        from plugin_sdk.ingestion import PolicyRevertedEvent

        bus = get_default_bus()
        if bus is None:
            return
        bus.publish(PolicyRevertedEvent(source="slash.policy_revert", **kwargs))
    except Exception as e:  # noqa: BLE001
        _logger.warning("PolicyRevertedEvent publish failed: %s", e)


# ─── SlashCommand class wrappers (registered into the dispatcher) ───


def _resolve_db_and_key(runtime: RuntimeContext):
    """Pull SessionDB + HMAC key from runtime + active profile."""
    from opencomputer.agent.config import _home
    from opencomputer.agent.policy_audit_key import get_policy_audit_hmac_key

    db = runtime.custom.get("session_db")
    if db is None:
        from opencomputer.agent.config_store import default_config
        from opencomputer.agent.state import SessionDB
        cfg = default_config()
        db = SessionDB(cfg.session.db_path)
    key = get_policy_audit_hmac_key(_home())
    return db, key


class PolicyChangesCommand(SlashCommand):
    name = "policy-changes"
    description = (
        "List recent policy-engine decisions: /policy-changes [--days N]"
    )

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        db, _ = _resolve_db_and_key(runtime)
        out = await handle_policy_changes(db=db, args=args)
        return SlashCommandResult(output=out.text, handled=True)


class PolicyApproveCommand(SlashCommand):
    name = "policy-approve"
    description = (
        "Approve a pending_approval policy change: /policy-approve <id>"
    )

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        db, key = _resolve_db_and_key(runtime)
        out = await handle_policy_approve(db=db, args=args, hmac_key=key)
        return SlashCommandResult(output=out.text, handled=True)


class PolicyRevertCommand(SlashCommand):
    name = "policy-revert"
    description = "Manually revert a policy change: /policy-revert <id>"

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        db, key = _resolve_db_and_key(runtime)
        out = await handle_policy_revert(db=db, args=args, hmac_key=key)
        return SlashCommandResult(output=out.text, handled=True)


class PolicyMetricsCommand(SlashCommand):
    name = "policy-metrics"
    description = "Show recommendation-engine quality stats (last 30d)"

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        from opencomputer.evolution.engine_metrics import compute_engine_quality

        db, _ = _resolve_db_and_key(runtime)
        days = 30
        parts = args.strip().split()
        if "--days" in parts:
            try:
                days = int(parts[parts.index("--days") + 1])
            except (IndexError, ValueError):
                pass

        metrics = compute_engine_quality(db, days=days)
        if not metrics:
            return SlashCommandResult(
                output=f"No engine activity in last {days}d.", handled=True,
            )

        lines = [f"Engine quality (last {days} days):"]
        for m in metrics:
            lines.append(
                f"\n  {m.engine_version}\n"
                f"    recs={m.n_recommendations} "
                f"(pending={m.n_pending} active={m.n_active} "
                f"expired={m.n_expired_decayed} reverted={m.n_reverted})\n"
                f"    unrevert_rate={m.unrevert_rate:.1%}  "
                f"revert_rate={m.revert_rate:.1%}"
            )
        return SlashCommandResult(output="\n".join(lines), handled=True)


class PolicyToolRiskCommand(SlashCommand):
    name = "policy-tool-risk"
    description = "Per-tool risk signals: error rate + self-cancel rate"

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        from opencomputer.evolution.tool_risk import compute_tool_risk

        db, _ = _resolve_db_and_key(runtime)
        days = 7
        parts = args.strip().split()
        if "--days" in parts:
            try:
                days = int(parts[parts.index("--days") + 1])
            except (IndexError, ValueError):
                pass

        rows = compute_tool_risk(db, days=days)
        if not rows:
            return SlashCommandResult(
                output=f"No tool_usage in last {days}d.", handled=True,
            )

        lines = [
            f"Tool risk (last {days} days):",
            f"{'tool':<24} {'calls':>6} {'err%':>6} {'cancel%':>8} {'avg ms':>8}",
        ]
        for r in rows:
            lines.append(
                f"{r.tool:<24} {r.n_calls:>6} "
                f"{r.error_rate * 100:>5.1f}% "
                f"{r.self_cancel_rate * 100:>7.1f}% "
                f"{r.mean_duration_ms:>8.0f}"
            )
        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = [
    "PolicyApproveCommand",
    "PolicyChangesCommand",
    "PolicyMetricsCommand",
    "PolicyRevertCommand",
    "PolicyToolRiskCommand",
    "handle_policy_approve",
    "handle_policy_changes",
    "handle_policy_revert",
]


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
