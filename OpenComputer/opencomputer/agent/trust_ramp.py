"""Phase 2 v0: progressive trust ramp.

Phase A (default): every recommendation requires explicit
``/policy-approve`` until N safe decisions accumulate.
Phase B (after): recommendations auto-approve with TTL.

A "safe decision" is a policy_change row that reached:
  - status = 'expired_decayed' (not reverted, decayed naturally), OR
  - status = 'active' for >= 30 days without revert.

Reverted decisions DO NOT count — a revert means the engine got it wrong.
"""
from __future__ import annotations

import time

from opencomputer.agent.feature_flags import FeatureFlags


_LONG_ACTIVE_AGE_S = 30 * 86400


class TrustRamp:
    def __init__(self, db, flags: FeatureFlags) -> None:
        self._db = db
        self._flags = flags

    def safe_decision_count(self) -> int:
        threshold_ts = time.time() - _LONG_ACTIVE_AGE_S
        with self._db._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM policy_changes
                WHERE status = 'expired_decayed'
                   OR (status = 'active' AND ts_applied IS NOT NULL
                       AND ts_applied < ?)
                """,
                (threshold_ts,),
            ).fetchone()
        return int(row[0]) if row else 0

    def n_required(self) -> int:
        return int(
            self._flags.read(
                "policy_engine.auto_approve_after_n_safe_decisions", 10,
            )
        )

    def is_phase_a(self) -> bool:
        return self.safe_decision_count() < self.n_required()

    def is_phase_b(self) -> bool:
        return not self.is_phase_a()

    def next_approval_mode(self) -> str:
        return "explicit" if self.is_phase_a() else "auto_ttl"
