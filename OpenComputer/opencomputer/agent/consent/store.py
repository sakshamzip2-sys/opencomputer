"""ConsentStore — persistence of ConsentGrants in the session SQLite DB.

Grants live in the `consent_grants` table (added by migration v1→v2).
This store is profile-aware via the passed-in connection — callers
resolve the active profile's DB path before instantiating.

SQLite (not JSON + fcntl) so two concurrent opencomputer processes
(CLI + gateway) can safely upsert without stomping each other. WAL
mode + row-level primary key on (capability_id, scope_filter) handles
all the concurrency we need.
"""
from __future__ import annotations

import sqlite3
import time

from plugin_sdk import ConsentGrant, ConsentTier


class ConsentStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, grant: ConsentGrant) -> None:
        # SQLite allows multiple NULLs in a PRIMARY KEY column, so INSERT
        # OR REPLACE on (capability_id, scope_filter) doesn't dedupe when
        # scope_filter IS NULL. Delete-then-insert handles both NULL and
        # non-NULL scope uniformly.
        self._conn.execute(
            "DELETE FROM consent_grants WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (grant.capability_id, grant.scope_filter, grant.scope_filter),
        )
        self._conn.execute(
            "INSERT INTO consent_grants "
            "(capability_id, scope_filter, tier, granted_at, expires_at, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                grant.capability_id, grant.scope_filter, int(grant.tier),
                grant.granted_at, grant.expires_at, grant.granted_by,
            ),
        )
        self._conn.commit()

    def get(self, capability_id: str, scope_filter: str | None) -> ConsentGrant | None:
        row = self._conn.execute(
            "SELECT capability_id, scope_filter, tier, granted_at, expires_at, granted_by "
            "FROM consent_grants WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (capability_id, scope_filter, scope_filter),
        ).fetchone()
        if row is None:
            return None
        grant = ConsentGrant(
            capability_id=row[0],
            scope_filter=row[1],
            tier=ConsentTier(int(row[2])),
            granted_at=row[3],
            expires_at=row[4],
            granted_by=row[5],
        )
        if grant.expires_at is not None and grant.expires_at <= time.time():
            return None
        return grant

    def revoke(self, capability_id: str, scope_filter: str | None) -> None:
        self._conn.execute(
            "DELETE FROM consent_grants WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (capability_id, scope_filter, scope_filter),
        )
        self._conn.commit()

    def list_active(self) -> list[ConsentGrant]:
        now = time.time()
        rows = self._conn.execute(
            "SELECT capability_id, scope_filter, tier, granted_at, expires_at, granted_by "
            "FROM consent_grants "
            "WHERE expires_at IS NULL OR expires_at > ?",
            (now,),
        ).fetchall()
        return [
            ConsentGrant(
                capability_id=r[0], scope_filter=r[1],
                tier=ConsentTier(int(r[2])),
                granted_at=r[3], expires_at=r[4], granted_by=r[5],
            )
            for r in rows
        ]
