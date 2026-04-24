"""AuditLogger — HMAC-chained append-only log.

Security model: tamper-EVIDENT, not tamper-proof. SQLite triggers block
UPDATE/DELETE at the engine level, but a user with filesystem access can
still delete the DB or bytewise-edit it. The HMAC-SHA256 chain ensures
any such tamper is DETECTED on `verify_chain()`.

Chain structure:
    row_0.prev_hmac = GENESIS (all zeros)
    row_0.row_hmac  = HMAC(key, canonicalize(row_0, row_0.prev_hmac))
    row_1.prev_hmac = row_0.row_hmac
    row_1.row_hmac  = HMAC(key, canonicalize(row_1, row_1.prev_hmac))
    ...

Editing any row (or removing a row, or reordering rows) breaks the chain.
`verify_chain()` recomputes every row's expected HMAC and returns False
on first mismatch.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GENESIS_HMAC: Final[str] = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEvent:
    session_id: str | None
    actor: str
    action: str
    capability_id: str
    tier: int
    scope: str | None
    decision: str
    reason: str


class AuditLogger:
    def __init__(self, conn: sqlite3.Connection, hmac_key: bytes) -> None:
        self._conn = conn
        self._key = hmac_key

    def append(self, evt: AuditEvent, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        prev = self._last_row_hmac()
        row_body = self._canonicalize(evt, ts, prev)
        row_hmac = hmac.new(
            self._key, row_body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        cur = self._conn.execute(
            """
            INSERT INTO audit_log
                (session_id, timestamp, actor, action, capability_id, tier, scope,
                 decision, reason, prev_hmac, row_hmac)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evt.session_id, ts, evt.actor, evt.action, evt.capability_id,
             evt.tier, evt.scope, evt.decision, evt.reason, prev, row_hmac),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def _last_row_hmac(self) -> str:
        row = self._conn.execute(
            "SELECT row_hmac FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HMAC

    @staticmethod
    def _canonicalize(evt: AuditEvent, ts: float, prev: str) -> str:
        # Fixed-order, pipe-delimited — trivially reproducible from row fields.
        return (
            f"{prev}|{evt.session_id or ''}|{ts}|{evt.actor}|{evt.action}"
            f"|{evt.capability_id}|{evt.tier}|{evt.scope or ''}"
            f"|{evt.decision}|{evt.reason}"
        )

    def verify_chain(self) -> bool:
        prev = GENESIS_HMAC
        for row in self._conn.execute(
            "SELECT prev_hmac, row_hmac, session_id, timestamp, actor, action, "
            "capability_id, tier, scope, decision, reason "
            "FROM audit_log ORDER BY id"
        ):
            if row[0] != prev:
                return False
            evt = AuditEvent(
                session_id=row[2], actor=row[4], action=row[5],
                capability_id=row[6], tier=row[7], scope=row[8],
                decision=row[9], reason=row[10],
            )
            expected = hmac.new(
                self._key,
                self._canonicalize(evt, row[3], row[0]).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if expected != row[1]:
                return False
            prev = row[1]
        return True

    # ─── Chain-head backup / recovery (for post-keyring-wipe cases) ───

    def export_chain_head(self, path: Path) -> None:
        """Write current chain head + row id to a JSON file.

        Used as a user-side backup in case the keyring entry is destroyed.
        With this file in hand, the user can verify that the post-wipe DB
        still matches the head they had at backup time.
        """
        row = self._conn.execute(
            "SELECT id, row_hmac, timestamp FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            payload = {"row_id": 0, "row_hmac": GENESIS_HMAC, "as_of": 0.0}
        else:
            payload = {"row_id": int(row[0]), "row_hmac": row[1], "as_of": row[2]}
        Path(path).write_text(json.dumps(payload, indent=2))

    def import_chain_head(self, path: Path) -> None:
        """Verify a backed-up chain head still matches the current DB.

        Raises ValueError if the row at `row_id` does not match the expected
        `row_hmac`. Informational only — doesn't mutate the DB.
        """
        payload = json.loads(Path(path).read_text())
        if payload["row_id"] == 0:
            # No rows to verify — accept.
            return
        row = self._conn.execute(
            "SELECT row_hmac FROM audit_log WHERE id=?",
            (payload["row_id"],),
        ).fetchone()
        if row is None or row[0] != payload["row_hmac"]:
            raise ValueError("imported chain head does not match DB state")

    def restart_chain(self, *, reason: str) -> None:
        """Append a marker event indicating the chain is restarting.

        Used when the HMAC key is lost; old entries can no longer be verified
        under a new key, but new entries go forward under the new key. Verify
        of pre-restart rows will fail — document this in operator docs.
        """
        self.append(AuditEvent(
            session_id=None, actor="system", action="chain_restart",
            capability_id="", tier=0, scope=None,
            decision="n/a", reason=reason,
        ))
