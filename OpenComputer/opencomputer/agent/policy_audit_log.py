"""v0.5 append-only HMAC chain over policy_changes status transitions.

Closes a v0 deferral: in v0 the chain in ``policy_changes`` protects
the as-drafted state of each row; status transitions are logged via
UPDATE but NOT cryptographically sealed. v0.5 introduces this second
chain that DOES seal every transition.

Both chains co-exist:

  - ``policy_changes`` (existing): one row per logical decision.
    ``hmac_self`` chains immutable as-drafted fields. Tampering with
    knob_kind/target_id/prev_value/new_value/reason etc. breaks the
    chain.

  - ``policy_audit_log`` (new): append-only history. One row per
    status transition (drafted, pending_approval, pending_evaluation,
    active, reverted, expired_decayed). Each row chains
    ``(change_id, ts, status, actor, reason)`` so the lifecycle is
    independently verifiable.

Production callers: cron/auto_revert, cron/decay_sweep,
cron/policy_engine_tick, slash/policy.handle_policy_approve,
slash/policy.handle_policy_revert. Each existing
``audit.append_status_transition()`` call now ALSO writes a row here.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from typing import Final

GENESIS_HMAC: Final[str] = "0" * 64


class PolicyAuditLog:
    def __init__(self, conn: sqlite3.Connection, hmac_key: bytes) -> None:
        self._conn = conn
        self._key = hmac_key

    def append_transition(
        self,
        *,
        change_id: str,
        status: str,
        actor: str | None = None,
        reason: str | None = None,
        now: float | None = None,
    ) -> int:
        """Append one transition row. Returns the new row's id."""
        ts = time.time() if now is None else now
        prev = self._last_hmac()
        body = self._canonicalize(change_id, ts, status, actor, reason, prev)
        row_hmac = hmac.new(
            self._key, body.encode("utf-8"), hashlib.sha256,
        ).hexdigest()

        cur = self._conn.execute(
            "INSERT INTO policy_audit_log "
            "(change_id, ts, status, actor, reason, hmac_prev, hmac_self) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (change_id, ts, status, actor, reason, prev, row_hmac),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def _last_hmac(self) -> str:
        row = self._conn.execute(
            "SELECT hmac_self FROM policy_audit_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HMAC

    @staticmethod
    def _canonicalize(
        change_id: str, ts: float, status: str,
        actor: str | None, reason: str | None, prev: str,
    ) -> str:
        return (
            f"{prev}|{change_id}|{ts}|{status}|{actor or ''}|{reason or ''}"
        )

    def verify_chain(self) -> bool:
        """Validate every row's HMAC link. Returns False on any tamper."""
        prev = GENESIS_HMAC
        for row in self._conn.execute(
            "SELECT change_id, ts, status, actor, reason, hmac_prev, hmac_self "
            "FROM policy_audit_log ORDER BY id"
        ):
            cid, ts, status, actor, reason, hp, hs = row
            if hp != prev:
                return False
            expected = hmac.new(
                self._key,
                self._canonicalize(cid, ts, status, actor, reason, prev).encode(
                    "utf-8"
                ),
                hashlib.sha256,
            ).hexdigest()
            if expected != hs:
                return False
            prev = hs
        return True

    def transitions_for(self, change_id: str) -> list[dict]:
        """Read-only fetch of every transition for a single change_id,
        ordered chronologically. Convenient for /policy-changes display."""
        rows = self._conn.execute(
            "SELECT ts, status, actor, reason FROM policy_audit_log "
            "WHERE change_id = ? ORDER BY ts",
            (change_id,),
        ).fetchall()
        return [
            {"ts": r[0], "status": r[1], "actor": r[2], "reason": r[3]}
            for r in rows
        ]
