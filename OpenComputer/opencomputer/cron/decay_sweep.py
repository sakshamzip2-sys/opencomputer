"""Phase 2 v0 nightly decay sweep.

Two responsibilities:

1. Mark ``active`` recall_penalty changes whose effective penalty
   (after decay) has dropped below 0.05 as ``expired_decayed``. The
   memory's ranking is now effectively neutral; the decision is
   considered safely concluded.

2. Discard ``pending_approval`` rows older than 7 days — auto-cleanup
   so abandoned drafts don't accumulate forever.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.policy_audit_log import PolicyAuditLog
from opencomputer.agent.recall_synthesizer import decay_factor

_logger = logging.getLogger(__name__)
_PENDING_DISCARD_WINDOW_S = 7 * 86400


@dataclass(slots=True)
class DecaySweepResult:
    expired_count: int = 0
    pending_discarded: int = 0


def run_decay_sweep(*, db, hmac_key: bytes) -> DecaySweepResult:
    result = DecaySweepResult()
    now = time.time()

    with db._connect() as conn:
        audit = PolicyAuditLogger(conn, hmac_key)
        audit_log = PolicyAuditLog(conn, hmac_key)

        active_rows = conn.execute(
            """
            SELECT pc.id AS cid, ee.recall_penalty AS pen,
                   ee.recall_penalty_updated_at AS upd
            FROM policy_changes pc
            JOIN episodic_events ee ON ee.id = pc.target_id
            WHERE pc.status = 'active'
              AND pc.knob_kind = 'recall_penalty'
            """
        ).fetchall()

        for row in active_rows:
            cid = row["cid"]
            penalty = row["pen"]
            updated_at = row["upd"]
            if penalty is None or updated_at is None:
                continue
            age_days = (now - updated_at) / 86400
            effective = penalty * decay_factor(age_days=age_days)
            if effective < 0.05:
                audit.append_status_transition(cid, "expired_decayed")
                audit_log.append_transition(
                    change_id=cid, status="expired_decayed",
                    actor="cron.decay_sweep",
                    reason=f"effective penalty {effective:.4f} < 0.05",
                )
                result.expired_count += 1

        cutoff = now - _PENDING_DISCARD_WINDOW_S
        pending_rows = conn.execute(
            "SELECT id FROM policy_changes WHERE status = 'pending_approval' "
            "AND ts_drafted < ?",
            (cutoff,),
        ).fetchall()
        for row in pending_rows:
            audit.append_status_transition(
                row["id"], "expired_decayed",
                reverted_reason="pending_approval auto-discarded after 7 days",
            )
            audit_log.append_transition(
                change_id=row["id"], status="expired_decayed",
                actor="cron.decay_sweep",
                reason="pending_approval auto-discarded after 7 days",
            )
            result.pending_discarded += 1

    return result
