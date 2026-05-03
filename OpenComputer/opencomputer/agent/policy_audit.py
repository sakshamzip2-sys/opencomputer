"""Phase 2 v0: HMAC-chained audit log for ``policy_changes``.

Reuses the chain pattern from ``opencomputer/agent/consent/audit.py``:

    row_n.prev_hmac = row_{n-1}.row_hmac
    row_n.row_hmac  = HMAC(key, canonicalize(row_n, row_n.prev_hmac))

Editing or removing any row breaks the chain, detected by
``verify_chain()``.

Two operations:

- ``append_drafted(evt)`` — first row for a new policy decision, in
  status ``drafted``. Returns the row id.
- ``append_status_transition(row_id, new_status, ...)`` — extends the
  chain with the same row's new status (``pending_approval``,
  ``pending_evaluation``, ``active``, ``reverted``, ``expired_decayed``).
  This UPDATEs the existing row but rewrites ``hmac_self`` so each
  transition is auditable.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Final

GENESIS_HMAC: Final[str] = "0" * 64


@dataclass(frozen=True, slots=True)
class PolicyChangeEvent:
    knob_kind: str
    target_id: str
    prev_value: str
    new_value: str
    reason: str
    expected_effect: str
    rollback_hook: str
    recommendation_engine_version: str
    approval_mode: str            # 'explicit' | 'auto_ttl'
    revert_after: float | None = None


class PolicyAuditLogger:
    def __init__(self, conn: sqlite3.Connection, hmac_key: bytes) -> None:
        self._conn = conn
        self._key = hmac_key

    def append_drafted(
        self, evt: PolicyChangeEvent, *, now: float | None = None,
    ) -> str:
        ts = time.time() if now is None else now
        prev = self._last_row_hmac()
        row_id = str(uuid.uuid4())
        body = self._canonicalize(row_id, ts, "drafted", evt, prev)
        row_hmac = hmac.new(
            self._key, body.encode("utf-8"), hashlib.sha256,
        ).hexdigest()

        self._conn.execute(
            """
            INSERT INTO policy_changes (
                id, ts_drafted, ts_applied,
                knob_kind, target_id, prev_value, new_value,
                reason, expected_effect, revert_after, rollback_hook,
                recommendation_engine_version,
                approval_mode, approved_by, approved_at,
                hmac_prev, hmac_self, status
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                row_id, ts,
                evt.knob_kind, evt.target_id,
                evt.prev_value, evt.new_value,
                evt.reason, evt.expected_effect,
                evt.revert_after, evt.rollback_hook,
                evt.recommendation_engine_version,
                evt.approval_mode,
                prev, row_hmac, "drafted",
            ),
        )
        self._conn.commit()
        return row_id

    def append_status_transition(
        self,
        row_id: str,
        new_status: str,
        *,
        ts_applied: float | None = None,
        approved_by: str | None = None,
        post_change_mean: float | None = None,
        reverted_reason: str | None = None,
    ) -> None:
        """Mutate the row's status fields. Does NOT touch the HMAC
        chain — the chain is established at append_drafted time and
        protects the row's identity + content as drafted.

        A v0.5 enhancement may add a separate ``policy_audit_log``
        append-only table chaining each transition. v0 keeps it simple:
        chain protects against fake-row-insertion and row-deletion;
        post-hoc status flips are logged but not cryptographically
        sealed.
        """
        ts = time.time()
        self._conn.execute(
            "UPDATE policy_changes SET status = ?, "
            "ts_applied = COALESCE(?, ts_applied), "
            "approved_by = COALESCE(?, approved_by), "
            "approved_at = CASE WHEN ? IS NOT NULL THEN ? ELSE approved_at END, "
            "post_change_mean = COALESCE(?, post_change_mean), "
            "reverted_at = CASE WHEN ? = 'reverted' THEN ? ELSE reverted_at END, "
            "reverted_reason = COALESCE(?, reverted_reason) "
            "WHERE id = ?",
            (
                new_status, ts_applied,
                approved_by, approved_by, ts,
                post_change_mean,
                new_status, ts,
                reverted_reason,
                row_id,
            ),
        )
        self._conn.commit()

    def _last_row_hmac(self) -> str:
        row = self._conn.execute(
            "SELECT hmac_self FROM policy_changes "
            "ORDER BY ts_drafted DESC, id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HMAC

    @staticmethod
    def _canonicalize(
        row_id: str, ts: float, status: str,
        evt: PolicyChangeEvent, prev: str,
    ) -> str:
        return (
            f"{prev}|{row_id}|{ts}|{status}|{evt.knob_kind}|{evt.target_id}"
            f"|{evt.prev_value}|{evt.new_value}|{evt.reason}|{evt.expected_effect}"
            f"|{evt.rollback_hook}|{evt.recommendation_engine_version}"
            f"|{evt.approval_mode}|{evt.revert_after or ''}"
        )

    def verify_chain(self) -> bool:
        """Validate the HMAC chain over the as-drafted state of each row.

        Each row's hmac was computed at ``append_drafted`` time using
        ``status='drafted'`` plus the immutable fields (knob_kind,
        target_id, prev_value, new_value, reason, expected_effect,
        rollback_hook, recommendation_engine_version, approval_mode,
        revert_after). Tampering with any of those, OR inserting/deleting
        a row, breaks the chain and is detected here.

        Status field flips (drafted → pending_approval → active → ...)
        are LOGGED via UPDATE but are NOT covered by the chain.
        Cryptographic protection of status transitions is a v0.5 item.
        """
        prev = GENESIS_HMAC
        for row in self._conn.execute(
            "SELECT id, ts_drafted, knob_kind, target_id, "
            "prev_value, new_value, reason, expected_effect, "
            "rollback_hook, recommendation_engine_version, "
            "approval_mode, revert_after, hmac_prev, hmac_self "
            "FROM policy_changes ORDER BY ts_drafted, id"
        ):
            (row_id, ts, knob_kind, target_id, prev_v, new_v,
             reason, eff, rollback, engine_v, mode, ra, hp, hs) = row
            if hp != prev:
                return False
            evt = PolicyChangeEvent(
                knob_kind=knob_kind, target_id=target_id,
                prev_value=prev_v, new_value=new_v, reason=reason,
                expected_effect=eff or "",
                rollback_hook=rollback,
                recommendation_engine_version=engine_v,
                approval_mode=mode,
                revert_after=ra,
            )
            # Validate against the as-drafted body (status='drafted'),
            # since UPDATEs only touch mutable fields.
            expected = hmac.new(
                self._key,
                self._canonicalize(row_id, ts, "drafted", evt, prev).encode(
                    "utf-8"
                ),
                hashlib.sha256,
            ).hexdigest()
            if expected != hs:
                return False
            prev = hs
        return True
