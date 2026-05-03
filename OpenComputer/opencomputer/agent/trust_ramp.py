"""v0.5: progressive trust ramp with per-knob_kind counters + tiered approval.

Phase A (default per-knob): every recommendation requires explicit
``/policy-approve`` until N safe decisions accumulate FOR THAT KNOB.
Phase B (per-knob): recommendations auto-approve with TTL.

A "safe decision" is a policy_change row that reached:
  - status = 'expired_decayed' (not reverted, decayed naturally), OR
  - status = 'active' for >= 30 days without revert.

Reverted decisions DO NOT count — a revert means the engine got it wrong.

v0.5 additions:
  - safe_decision_count_for(knob_kind): per-knob counter so independent
    knobs build trust independently.
  - next_approval_mode_for(knob_kind): consults a tier mapping
    (recall_penalty → low_blast etc.) to pick (n_required, ttl_days).
    A "high_blast" tier with ttl_days=0 means "always require explicit"
    even after the trust threshold.
"""
from __future__ import annotations

import time

from opencomputer.agent.feature_flags import FeatureFlags

_LONG_ACTIVE_AGE_S = 30 * 86400

#: Default tier mapping. Each knob_kind maps to a tier name; tier names
#: map to (n_required_for_phase_b, ttl_days). ``ttl_days=0`` means even
#: after threshold the mode stays "explicit".
DEFAULT_APPROVAL_TIERS: dict[str, str] = {
    "recall_penalty": "low_blast",
}

DEFAULT_TIER_BEHAVIOR: dict[str, tuple[int, int]] = {
    "low_blast":  (10, 7),
    "med_blast":  (20, 14),
    "high_blast": (50, 0),  # 0 ttl = always explicit
}


class TrustRamp:
    def __init__(self, db, flags: FeatureFlags) -> None:
        self._db = db
        self._flags = flags

    # ─── Counters ────────────────────────────────────────────────────

    def safe_decision_count(self) -> int:
        """Total safe decisions across all knobs (backward-compat)."""
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

    def safe_decision_count_for(self, knob_kind: str) -> int:
        """Per-knob safe-decision counter. v0.5 — independent knobs build
        trust independently so a brand-new high-blast knob doesn't
        inherit a low-blast knob's trust accumulated over months."""
        threshold_ts = time.time() - _LONG_ACTIVE_AGE_S
        with self._db._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM policy_changes
                WHERE knob_kind = ?
                  AND (status = 'expired_decayed'
                       OR (status = 'active' AND ts_applied IS NOT NULL
                           AND ts_applied < ?))
                """,
                (knob_kind, threshold_ts),
            ).fetchone()
        return int(row[0]) if row else 0

    # ─── Tier resolution ─────────────────────────────────────────────

    def _tier_for_knob(self, knob_kind: str) -> str:
        return self._flags.read(
            f"policy_engine.approval_tiers.{knob_kind}",
            DEFAULT_APPROVAL_TIERS.get(knob_kind, "low_blast"),
        )

    def _behavior_for_tier(self, tier: str) -> tuple[int, int]:
        n = self._flags.read(
            f"policy_engine.tier_behavior.{tier}.n_required", None,
        )
        ttl = self._flags.read(
            f"policy_engine.tier_behavior.{tier}.ttl_days", None,
        )
        if n is None or ttl is None:
            return DEFAULT_TIER_BEHAVIOR.get(tier, (10, 7))
        return (int(n), int(ttl))

    # ─── Public API ──────────────────────────────────────────────────

    def n_required(self) -> int:
        """Backward-compat: scalar threshold from the flat policy_engine config."""
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
        """v0 backward-compat: uses global counter + global threshold.

        Does NOT consult the tier system — that's opt-in via
        ``next_approval_mode_for(knob_kind)``. v0 single-knob deployments
        keep their existing behaviour exactly.
        """
        return "explicit" if self.is_phase_a() else "auto_ttl"

    def next_approval_mode_for(self, knob_kind: str) -> str:
        """v0.5: tier-aware mode resolution per knob.

        Threshold logic:
          - Per-knob safe-decision counter (independent trust)
          - Threshold = MIN(tier_default, legacy_global_flag); legacy
            global = 0 forces immediate phase B for tests / dev override
          - tier ``ttl_days=0`` (high_blast) always returns explicit
            even past the threshold
        """
        tier = self._tier_for_knob(knob_kind)
        n_tier, ttl_days = self._behavior_for_tier(tier)
        if ttl_days <= 0:
            return "explicit"

        legacy = self.n_required()
        # legacy flag takes precedence when explicitly tighter (lower)
        # — lets tests / dev override force phase B at count=0.
        n_required = min(n_tier, legacy)
        if self.safe_decision_count_for(knob_kind) < n_required:
            return "explicit"
        return "auto_ttl"
