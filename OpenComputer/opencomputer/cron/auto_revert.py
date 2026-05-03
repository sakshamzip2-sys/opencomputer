"""Phase 2 v0 statistical auto-revert.

Runs every 6 hours. For each ``pending_evaluation`` policy_change:

  - Count post-change eligible turns (turns where this memory could
    have been cited under penalty=0).
  - If count < min_eligible_turns_for_revert: status stays
    pending_evaluation (HARD GATE: never auto-revert on small samples).
  - Else compare post mean vs baseline:
    * post < baseline - σ * std → auto-revert
    * post within ±1σ of baseline → mark active (passed evaluation)
    * post > baseline + σ → mark active (positive)
"""
from __future__ import annotations

import json
import logging
import time

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.policy_audit import PolicyAuditLogger

_logger = logging.getLogger(__name__)


def run_auto_revert_due(*, db, flags: FeatureFlags, hmac_key: bytes) -> int:
    """Returns count of state transitions performed."""
    n_min = int(
        flags.read("policy_engine.min_eligible_turns_for_revert", 10)
    )
    sigma = float(flags.read("policy_engine.revert_threshold_sigma", 1.0))
    transitions = 0

    with db._connect() as conn:
        audit = PolicyAuditLogger(conn, hmac_key)

        pending = conn.execute(
            "SELECT id, ts_applied, target_id, "
            "pre_change_baseline_mean, pre_change_baseline_std, "
            "rollback_hook FROM policy_changes "
            "WHERE status = 'pending_evaluation'"
        ).fetchall()

        for row in pending:
            change_id = row["id"]
            applied_at = row["ts_applied"]
            target_id = row["target_id"]
            baseline_mean = row["pre_change_baseline_mean"]
            baseline_std = row["pre_change_baseline_std"]
            rollback_hook_json = row["rollback_hook"]

            post_rows = conn.execute(
                "SELECT turn_score FROM turn_outcomes "
                "WHERE created_at >= ? AND turn_score IS NOT NULL",
                (applied_at,),
            ).fetchall()
            post_scores = [r[0] for r in post_rows]
            eligible_n = len(post_scores)

            conn.execute(
                "UPDATE policy_changes SET eligible_turn_count = ? "
                "WHERE id = ?",
                (eligible_n, change_id),
            )

            if eligible_n < n_min:
                continue  # HARD GATE

            post_mean = sum(post_scores) / eligible_n

            if (
                baseline_std is not None
                and baseline_mean is not None
                and post_mean < baseline_mean - sigma * baseline_std
            ):
                rollback = json.loads(rollback_hook_json)
                _execute_rollback(conn, target_id, rollback)
                audit.append_status_transition(
                    change_id, "reverted",
                    post_change_mean=post_mean,
                    reverted_reason=(
                        f"statistical: post_mean {post_mean:.3f} < "
                        f"baseline {baseline_mean:.3f} - "
                        f"{sigma:.1f}σ (std {baseline_std:.3f}, "
                        f"N={eligible_n})"
                    ),
                )
                transitions += 1
            else:
                audit.append_status_transition(
                    change_id, "active",
                    post_change_mean=post_mean,
                )
                transitions += 1

    return transitions


def _execute_rollback(conn, target_id: str, rollback: dict) -> None:
    if rollback["action"] != "set":
        raise NotImplementedError(
            f"unsupported rollback action: {rollback['action']}"
        )
    field = rollback["field"]
    value = rollback["value"]
    if field != "recall_penalty":
        raise NotImplementedError(f"unsupported field: {field}")
    conn.execute(
        "UPDATE episodic_events SET recall_penalty = ?, "
        "recall_penalty_updated_at = ? WHERE id = ?",
        (value, time.time(), target_id),
    )
