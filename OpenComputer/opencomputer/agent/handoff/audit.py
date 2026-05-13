"""HandoffAuditLogger — append every profile swap to the existing audit_log.

Reuses the HMAC-chained ``audit_log`` table managed by
:class:`opencomputer.agent.consent.audit.AuditLogger`. Each swap (manual,
auto, or aborted) becomes one row with ``action='profile_swap'``.

Why reuse vs. new table: the chain semantics are identical (append-only,
tamper-evident, verify_chain() valid across mixed action types). A second
chain doubles the keyring + bootstrap surface for the same security
property. Mixed actions in one chain is the documented pattern of
``consent/audit.py``.

Capability mapping:
    action          ``profile_swap``
    capability_id   ``profile:<target_profile_id>``
    actor           ``auto_swap`` | ``manual_handoff`` | ``cli``
    tier            ``0`` (no consent tiering for profile swaps)
    scope           classifier persona+confidence (auto) or ``""`` (manual)
    decision        ``allow`` | ``abort`` | ``deferred``
    reason          free-form telemetry string
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger

_log = logging.getLogger("opencomputer.agent.handoff.audit")

SwapTrigger = Literal["auto", "manual", "cli"]
SwapOutcome = Literal["allow", "abort", "deferred"]


@dataclass(frozen=True, slots=True)
class SwapAuditEvent:
    """One row in the handoff audit log."""
    session_id: str
    source_profile: str
    target_profile: str
    trigger: SwapTrigger
    outcome: SwapOutcome
    reason: str
    classifier_persona: str = ""
    classifier_confidence: float | None = None
    handoff_path: str = ""

    def to_audit_event(self) -> AuditEvent:
        """Pack into the underlying chain row.

        ``scope`` carries classifier persona+confidence as a single
        pipe-delimited string so the verify_chain() implementation
        (which is byte-stable on canonical form) doesn't need to know
        about our sub-schema.
        """
        scope_parts: list[str] = []
        if self.classifier_persona:
            scope_parts.append(f"persona={self.classifier_persona}")
        if self.classifier_confidence is not None:
            scope_parts.append(f"conf={self.classifier_confidence:.3f}")
        if self.handoff_path:
            scope_parts.append(f"handoff={self.handoff_path}")
        scope = "|".join(scope_parts) or ""

        return AuditEvent(
            session_id=self.session_id or None,
            actor=_TRIGGER_TO_ACTOR[self.trigger],
            action="profile_swap",
            capability_id=f"profile:{self.target_profile}",
            tier=0,
            scope=scope,
            decision=self.outcome,
            reason=self.reason or f"{self.source_profile}->{self.target_profile}",
        )


_TRIGGER_TO_ACTOR: dict[str, str] = {
    "auto": "auto_swap",
    "manual": "manual_handoff",
    "cli": "cli",
}


class HandoffAuditLogger:
    """Thin wrapper around :class:`AuditLogger` for handoff-specific rows.

    Construct with a path to the existing audit DB and a 32-byte HMAC
    key (the same key the consent layer uses — fetched from keyring at
    session bootstrap). The connection is kept open for the lifetime of
    the instance; ``close()`` releases it cleanly.

    All append operations are best-effort: a failed audit write logs at
    WARN and does NOT raise. Audit is observability, not a gate on the
    swap. (A failed swap write would have surfaced earlier in
    inbox.write() — by the time we reach audit, the swap is already
    committed or already aborted.)
    """

    def __init__(self, db_path: Path, hmac_key: bytes) -> None:
        if not isinstance(db_path, Path):
            raise TypeError(
                f"db_path must be Path, got {type(db_path).__name__}"
            )
        if not isinstance(hmac_key, (bytes, bytearray)):
            raise TypeError("hmac_key must be bytes")
        if len(hmac_key) < 16:
            raise ValueError(
                f"hmac_key must be at least 16 bytes (got {len(hmac_key)})"
            )

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._ensure_schema(self._conn)
        self._inner = AuditLogger(self._conn, bytes(hmac_key))

    def append(self, evt: SwapAuditEvent) -> int | None:
        """Append a swap audit row. Returns rowid on success, None on
        failure (never raises)."""
        if not isinstance(evt, SwapAuditEvent):
            _log.warning(
                "audit append refused: not a SwapAuditEvent (got %s)",
                type(evt).__name__,
            )
            return None
        try:
            rowid = self._inner.append(evt.to_audit_event())
        except sqlite3.Error as e:
            _log.warning(
                "swap audit write failed (action=profile_swap, "
                "%s->%s): %s",
                evt.source_profile, evt.target_profile, e,
            )
            return None
        except Exception as e:  # noqa: BLE001 — defensive
            _log.warning("swap audit append unexpected error: %s", e)
            return None
        _log.debug(
            "swap audit row %d: %s %s->%s (%s)",
            rowid, evt.trigger, evt.source_profile,
            evt.target_profile, evt.outcome,
        )
        return rowid

    def verify_chain(self) -> bool:
        """Re-validate the entire HMAC chain. Used by ``oc memory doctor``."""
        try:
            return self._inner.verify_chain()
        except Exception as e:  # noqa: BLE001 — defensive
            _log.warning("audit verify_chain raised: %s", e)
            return False

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Create the audit_log table if missing.

        Matches the schema in ``state.py`` (the canonical schema lives
        there for the session DB; for the consent DB it's instantiated
        on first connect). Idempotent.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT,
                timestamp     REAL NOT NULL,
                actor         TEXT NOT NULL,
                action        TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                tier          INTEGER NOT NULL,
                scope         TEXT,
                decision      TEXT NOT NULL,
                reason        TEXT,
                prev_hmac     TEXT NOT NULL,
                row_hmac      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_action_ts
                ON audit_log(action, timestamp);
            -- Tamper-evident: block UPDATE/DELETE at the engine layer.
            -- Defensive only; filesystem-level deletes still possible.
            CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(FAIL, 'audit_log is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(FAIL, 'audit_log is append-only');
                END;
            """
        )


__all__ = ["HandoffAuditLogger", "SwapAuditEvent"]
