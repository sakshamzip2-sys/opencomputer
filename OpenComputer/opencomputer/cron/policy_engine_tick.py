"""Phase 2 v0 nightly cron: gate, recommend, draft.

Order of gates (each can return early with a distinct reason):
  1. Kill switch (feature_flags.policy_engine.enabled)
  2. Daily budget (max N changes per 24h)
  3. Engine recommendation (returns Recommendation or no-op)
  4. Trust ramp (decides approval_mode)
  5. Apply or stage as pending_approval
"""
from __future__ import annotations

import json
import logging
import time
from enum import Enum

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.policy_audit import PolicyChangeEvent as _AuditEvent
from opencomputer.agent.policy_audit_log import PolicyAuditLog
from opencomputer.agent.trust_ramp import TrustRamp
from opencomputer.evolution.policy_engine import MostCitedBelowMedianV1

_logger = logging.getLogger(__name__)


class EngineTickResult(str, Enum):
    KILL_SWITCH_OFF = "kill_switch_off"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ENGINE_NOOP = "engine_noop"
    DRAFTED_PENDING = "drafted_pending"
    DRAFTED_AUTO_APPLIED = "drafted_auto_applied"


def run_engine_tick(*, db, flags: FeatureFlags, hmac_key: bytes) -> EngineTickResult:
    if not flags.read("policy_engine.enabled", True):
        _logger.info("kill switch off — skipping engine tick")
        return EngineTickResult.KILL_SWITCH_OFF

    budget = int(flags.read("policy_engine.daily_change_budget", 3))
    cutoff = time.time() - 86400
    with db._connect() as conn:
        applied_today = conn.execute(
            "SELECT COUNT(*) FROM policy_changes "
            "WHERE ts_applied IS NOT NULL AND ts_applied >= ? "
            "AND status NOT IN ('reverted')",
            (cutoff,),
        ).fetchone()[0]
    if applied_today >= budget:
        _logger.info(
            "daily budget hit (%d/%d) — skipping", applied_today, budget,
        )
        return EngineTickResult.BUDGET_EXHAUSTED

    engine = MostCitedBelowMedianV1(
        min_citations=5,
        cooldown_days=7,
        deviation_threshold=float(
            flags.read("policy_engine.minimum_deviation_threshold", 0.10)
        ),
        penalty_step=0.20,
        penalty_cap=0.80,
    )
    rec = engine.recommend(db)
    if rec.is_noop():
        _logger.info("engine noop: %s", rec.noop_reason)
        return EngineTickResult.ENGINE_NOOP

    ramp = TrustRamp(db, flags)
    # Task E: tier-aware mode selection — recall_penalty defaults to
    # low_blast (10 safe / 7d TTL) which matches v0 behavior.
    mode = ramp.next_approval_mode_for(rec.knob_kind)
    revert_after = time.time() + 7 * 86400 if mode == "auto_ttl" else None

    with db._connect() as conn:
        audit = PolicyAuditLogger(conn, hmac_key)
        audit_log = PolicyAuditLog(conn, hmac_key)
        evt = _AuditEvent(
            knob_kind=rec.knob_kind,
            target_id=str(rec.target_id),
            prev_value=json.dumps(rec.prev_value),
            new_value=json.dumps(rec.new_value),
            reason=rec.reason,
            expected_effect=rec.expected_effect,
            rollback_hook=json.dumps(rec.rollback_hook),
            recommendation_engine_version=rec.engine_version,
            approval_mode=mode,
            revert_after=revert_after,
        )
        row_id = audit.append_drafted(evt)
        audit_log.append_transition(
            change_id=row_id, status="drafted",
            actor="cron.engine_tick",
            reason=f"engine={rec.engine_version}",
        )

        if mode == "auto_ttl":
            baseline = _baseline_for(conn)
            _apply_recall_penalty_change(conn, rec, baseline, row_id)
            audit.append_status_transition(
                row_id, "pending_evaluation",
                ts_applied=time.time(),
                approved_by="auto",
            )
            audit_log.append_transition(
                change_id=row_id, status="pending_evaluation",
                actor="auto",
                reason=f"auto-approved tier=auto_ttl revert_after={revert_after}",
            )
            final_status = "pending_evaluation"
            tick_result = EngineTickResult.DRAFTED_AUTO_APPLIED
            _logger.info(
                "auto-approved %s mode=%s revert_after=%s",
                row_id, mode, revert_after,
            )
        else:
            audit.append_status_transition(row_id, "pending_approval")
            audit_log.append_transition(
                change_id=row_id, status="pending_approval",
                actor="cron.engine_tick",
                reason="explicit-approval mode",
            )
            final_status = "pending_approval"
            tick_result = EngineTickResult.DRAFTED_PENDING
            _logger.info(
                "drafted pending approval %s mode=%s", row_id, mode,
            )

    _publish_policy_change_event(
        change_id=row_id,
        knob_kind=rec.knob_kind,
        target_id=str(rec.target_id),
        status=final_status,
        approval_mode=mode,
        engine_version=rec.engine_version,
        reason=rec.reason,
    )
    return tick_result


def _publish_policy_change_event(**kwargs) -> None:
    """Fire PolicyChangeEvent on the default bus. Best-effort; never
    propagates exceptions (a publish failure must not break the cron)."""
    try:
        from opencomputer.ingestion.bus import get_default_bus
        from plugin_sdk.ingestion import PolicyChangeEvent

        bus = get_default_bus()
        if bus is None:
            return
        bus.publish(PolicyChangeEvent(source="cron.policy_engine", **kwargs))
    except Exception as e:  # noqa: BLE001
        _logger.warning("PolicyChangeEvent publish failed: %s", e)


def _baseline_for(conn) -> tuple[float, float]:
    """Compute pre-change baseline mean + std of turn_score."""
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


def _apply_recall_penalty_change(conn, rec, baseline, row_id) -> None:
    new_penalty = rec.new_value["recall_penalty"]
    conn.execute(
        "UPDATE episodic_events SET recall_penalty = ?, "
        "recall_penalty_updated_at = ? WHERE id = ?",
        (new_penalty, time.time(), rec.target_id),
    )
    conn.execute(
        "UPDATE policy_changes SET pre_change_baseline_mean = ?, "
        "pre_change_baseline_std = ? WHERE id = ?",
        (baseline[0], baseline[1], row_id),
    )
