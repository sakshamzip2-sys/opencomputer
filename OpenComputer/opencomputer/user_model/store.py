"""
SQLite-backed CRUD for the user-model graph (Phase 3.C, F4 layer).

Schema lives at ``<profile_home>/user_model/graph.sqlite``. Pattern
mirrors :mod:`opencomputer.agent.state` and
:mod:`opencomputer.inference.storage`:

* WAL mode for concurrent readers during writes.
* Application-level retry+jitter on ``SQLITE_BUSY``.
* Idempotent migrations via a ``schema_version`` row.
* FTS5 virtual table over ``nodes.value`` with auto-syncing triggers.

Phase 3.D decay / drift reads from :meth:`UserModelStore.list_edges` and
writes via :meth:`UserModelStore.update_edge_recency_weight` — those
are the documented consumption points.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home
from plugin_sdk.user_model import Edge, EdgeKind, Node, NodeKind

#: Incremented when the SQLite schema is extended. Migrations advance
#: the DB from its stored version to :data:`SCHEMA_VERSION` via
#: :func:`apply_migrations`. v1 = baseline (nodes + edges + FTS5).
#: v2 (Phase 4 of catch-up plan) = adds ``edges.source`` provenance column.
SCHEMA_VERSION = 2


DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id        TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    value          TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    confidence     REAL NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_nodes_kind
    ON nodes(kind, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_kind_value
    ON nodes(kind, value);

CREATE TABLE IF NOT EXISTS edges (
    edge_id             TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    from_node           TEXT NOT NULL,
    to_node             TEXT NOT NULL,
    salience            REAL NOT NULL,
    confidence          REAL NOT NULL,
    recency_weight      REAL NOT NULL,
    source_reliability  REAL NOT NULL,
    decay_rate          REAL NOT NULL,
    created_at          REAL NOT NULL,
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (from_node) REFERENCES nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY (to_node)   REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_from
    ON edges(from_node);
CREATE INDEX IF NOT EXISTS idx_edges_to
    ON edges(to_node);
CREATE INDEX IF NOT EXISTS idx_edges_kind
    ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_created_at_desc
    ON edges(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    value,
    content='nodes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Auto-sync triggers: every change to nodes.value is mirrored into the
-- FTS5 shadow index. ``content_rowid='rowid'`` means we reference the
-- implicit SQLite rowid (since node_id is TEXT, not INTEGER). The
-- delete/update triggers use the 'delete' command form required for
-- contentless FTS5 tables.
CREATE TRIGGER IF NOT EXISTS nodes_fts_insert
AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, value) VALUES (new.rowid, new.value);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_delete
AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, value) VALUES ('delete', old.rowid, old.value);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_update
AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, value) VALUES ('delete', old.rowid, old.value);
    INSERT INTO nodes_fts(rowid, value) VALUES (new.rowid, new.value);
END;
"""


# ---------------------------------------------------------------------------
# Migration framework — mirror opencomputer/agent/state.py::apply_migrations
# ---------------------------------------------------------------------------


MIGRATIONS: dict[tuple[int, int], str] = {
    (0, 1): "_migrate_v0_to_v1",
    (1, 2): "_migrate_v1_to_v2",
}


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return stored schema version. Returns 0 on fresh DBs (no table yet)."""
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _bump_schema_version(conn: sqlite3.Connection, v: int) -> None:
    """Replace the single schema_version row."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (v,))


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Apply the v1 baseline DDL — nodes + edges + FTS5 + triggers. Idempotent."""
    conn.executescript(DDL_V1)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``edges.source`` provenance column for Phase 4 hybrid cycle-prevention.

    Default value 'unknown' so existing rows carry forward without
    rewriting. New writes must specify a meaningful source
    ('motif_importer', 'honcho_synthesis', 'user_explicit', etc.) — the
    importer was updated in the same change to do so.

    Idempotent: re-runs are no-ops because ALTER TABLE ADD COLUMN with
    DEFAULT and NOT NULL is single-shot. We use a try/except sentinel
    to handle the "column already exists" case (which can happen if a
    user migrated mid-flight).
    """
    cur = conn.execute("PRAGMA table_info(edges)")
    cols = {row[1] for row in cur.fetchall()}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE edges ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
        )
    # Index even if the column was already there — IF NOT EXISTS makes
    # it safe.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)")


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance DB from stored schema_version to SCHEMA_VERSION. Idempotent."""
    current = _read_schema_version(conn)
    while current < SCHEMA_VERSION:
        fn_name = MIGRATIONS[(current, current + 1)]
        globals()[fn_name](conn)
        _bump_schema_version(conn, current + 1)
        current += 1
    conn.commit()


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def _default_db_path() -> Path:
    """Return ``<profile_home>/user_model/graph.sqlite``, creating dirs."""
    p = _home() / "user_model"
    p.mkdir(parents=True, exist_ok=True)
    return p / "graph.sqlite"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class UserModelStore:
    """Thin SQLite wrapper for :class:`Node` / :class:`Edge` CRUD.

    Parameters
    ----------
    db_path:
        Override the default location. ``None`` (the production
        default) uses ``<profile_home>/user_model/graph.sqlite``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            apply_migrations(conn)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction with retry+jitter on SQLITE_BUSY.

        Pattern adapted from :mod:`opencomputer.agent.state` and
        :mod:`opencomputer.inference.storage`. Up to 5 retries with
        20–150 ms jitter — friendly to short-lived contention from
        multiple writers (CLI + importer + future decay job).
        """
        conn = self._connect()
        attempts = 0
        max_attempts = 5
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                attempts += 1
                if attempts >= max_attempts:
                    raise
                time.sleep(random.uniform(0.02, 0.15))
            finally:
                conn.close()

    # ─── Node CRUD ────────────────────────────────────────────────────

    @staticmethod
    def _node_row(n: Node) -> tuple[Any, ...]:
        return (
            n.node_id,
            n.kind,
            n.value,
            float(n.created_at),
            float(n.last_seen_at),
            float(n.confidence),
            json.dumps(dict(n.metadata)),
        )

    _INSERT_NODE_SQL = (
        "INSERT OR REPLACE INTO nodes "
        "(node_id, kind, value, created_at, last_seen_at, confidence, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    def insert_node(self, node: Node) -> None:
        """Insert / replace one node by ``node_id``."""
        with self._txn() as conn:
            conn.execute(self._INSERT_NODE_SQL, self._node_row(node))

    def upsert_node(
        self,
        *,
        kind: NodeKind,
        value: str,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Node:
        """Find-or-create a node by ``(kind, value)``.

        If a node with the same ``(kind, value)`` exists, bump
        ``last_seen_at`` to now and optionally raise ``confidence``
        (never lower it — repeat assertions are evidence of durability).
        Otherwise insert a fresh node.

        Returns the final stored node either way, so callers can chain
        into :meth:`insert_edge` without a follow-up query.
        """
        now = time.time()
        with self._txn() as conn:
            existing = conn.execute(
                "SELECT * FROM nodes WHERE kind = ? AND value = ? LIMIT 1",
                (kind, value),
            ).fetchone()

            if existing is not None:
                # Repeat assertion — bump last_seen and optionally take
                # the max of old/new confidence. Never shrink confidence
                # on upsert: drift should be explicit via a contradicts
                # edge, not a silent overwrite.
                new_conf = float(existing["confidence"])
                if confidence is not None:
                    new_conf = max(new_conf, float(confidence))
                conn.execute(
                    "UPDATE nodes SET last_seen_at = ?, confidence = ? "
                    "WHERE node_id = ?",
                    (now, new_conf, existing["node_id"]),
                )
                # Re-read the row we just updated so callers get the
                # materialised state (including the pre-existing id +
                # created_at).
                row = conn.execute(
                    "SELECT * FROM nodes WHERE node_id = ?",
                    (existing["node_id"],),
                ).fetchone()
                return self._row_to_node(row)

            node = Node(
                kind=kind,
                value=value,
                confidence=confidence if confidence is not None else 0.5,
                created_at=now,
                last_seen_at=now,
                metadata=metadata or {},
            )
            conn.execute(self._INSERT_NODE_SQL, self._node_row(node))
            return node

    def get_node(self, node_id: str) -> Node | None:
        """Fetch a node by id. Returns ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return self._row_to_node(row) if row else None

    def list_nodes(
        self,
        kinds: Sequence[NodeKind] | None = None,
        limit: int = 100,
    ) -> list[Node]:
        """Return nodes, newest-last-seen first.

        Parameters
        ----------
        kinds:
            Optional whitelist — if set, only return nodes with
            ``kind`` in the sequence.
        limit:
            Cap on rows returned. Default 100.
        """
        args: list[Any] = []
        where = ""
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            where = f"WHERE kind IN ({placeholders})"
            args.extend(list(kinds))
        sql = (
            f"SELECT * FROM nodes {where} "
            "ORDER BY last_seen_at DESC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_node(r) for r in rows]

    def search_nodes_fts(self, query: str, limit: int = 20) -> list[Node]:
        """Return nodes whose ``value`` matches the FTS5 ``query``.

        Uses the ``nodes_fts`` virtual table. Ordering is by FTS5
        default relevance. The query string is passed through to SQLite;
        malformed queries return an empty list rather than raising (the
        common case is a user typo that shouldn't break the CLI).
        """
        if not query.strip():
            return []
        sql = (
            "SELECT nodes.* FROM nodes_fts "
            "JOIN nodes ON nodes.rowid = nodes_fts.rowid "
            "WHERE nodes_fts MATCH ? "
            "ORDER BY rank LIMIT ?"
        )
        with self._connect() as conn:
            try:
                rows = conn.execute(sql, (query, int(limit))).fetchall()
            except sqlite3.OperationalError:
                # FTS5 rejects malformed queries with OperationalError —
                # return empty rather than crash the CLI.
                return []
        return [self._row_to_node(r) for r in rows]

    def count_nodes(self, kinds: Sequence[NodeKind] | None = None) -> int:
        """Return the number of nodes (optionally filtered by kinds)."""
        with self._connect() as conn:
            if not kinds:
                row = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
            else:
                placeholders = ",".join("?" * len(kinds))
                row = conn.execute(
                    f"SELECT COUNT(*) FROM nodes WHERE kind IN ({placeholders})",
                    tuple(kinds),
                ).fetchone()
        return int(row[0]) if row else 0

    def delete_node(self, node_id: str) -> int:
        """Delete one node by id.

        Incident edges are dropped via the ``ON DELETE CASCADE`` foreign
        keys on the ``edges`` table. Returns the number of node rows
        deleted (0 or 1).
        """
        with self._txn() as conn:
            cur = conn.execute(
                "DELETE FROM nodes WHERE node_id = ?",
                (node_id,),
            )
            return int(cur.rowcount or 0)

    # ─── Edge CRUD ────────────────────────────────────────────────────

    @staticmethod
    def _edge_row(e: Edge) -> tuple[Any, ...]:
        return (
            e.edge_id,
            e.kind,
            e.from_node,
            e.to_node,
            float(e.salience),
            float(e.confidence),
            float(e.recency_weight),
            float(e.source_reliability),
            float(e.decay_rate),
            float(e.created_at),
            json.dumps(dict(e.evidence)),
            e.source,
        )

    _INSERT_EDGE_SQL = (
        "INSERT OR REPLACE INTO edges "
        "(edge_id, kind, from_node, to_node, salience, confidence, "
        "recency_weight, source_reliability, decay_rate, created_at, "
        "evidence_json, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def insert_edge(self, edge: Edge) -> None:
        """Insert / replace one edge by ``edge_id``.

        Foreign keys enforce that both endpoints exist — callers should
        :meth:`insert_node` / :meth:`upsert_node` first.
        """
        with self._txn() as conn:
            conn.execute(self._INSERT_EDGE_SQL, self._edge_row(edge))

    def get_edge(self, edge_id: str) -> Edge | None:
        """Fetch an edge by id. Returns ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM edges WHERE edge_id = ?",
                (edge_id,),
            ).fetchone()
        return self._row_to_edge(row) if row else None

    def list_edges(
        self,
        *,
        kind: EdgeKind | None = None,
        from_node: str | None = None,
        to_node: str | None = None,
        limit: int = 200,
    ) -> list[Edge]:
        """Return edges matching the filters, newest first.

        Parameters
        ----------
        kind:
            Restrict to one :data:`EdgeKind`. ``None`` returns all.
        from_node / to_node:
            Either/both can pin the endpoint. ``from_node`` alone
            lists outgoing edges; ``to_node`` alone lists incoming.
        limit:
            Cap on rows returned. Default 200.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        if from_node is not None:
            clauses.append("from_node = ?")
            args.append(from_node)
        if to_node is not None:
            clauses.append("to_node = ?")
            args.append(to_node)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM edges {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def count_edges(self, kinds: Sequence[EdgeKind] | None = None) -> int:
        """Return the number of edges (optionally filtered by kinds)."""
        with self._connect() as conn:
            if not kinds:
                row = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
            else:
                placeholders = ",".join("?" * len(kinds))
                row = conn.execute(
                    f"SELECT COUNT(*) FROM edges WHERE kind IN ({placeholders})",
                    tuple(kinds),
                ).fetchone()
        return int(row[0]) if row else 0

    def update_edge_recency_weight(self, edge_id: str, weight: float) -> None:
        """Update the ``recency_weight`` of one edge.

        Phase 3.D background decay job calls this per-edge after
        applying the exponential formula. Weight is clamped to
        ``[0.0, 1.0]`` before write — callers don't need to defensively
        clamp on their side.
        """
        clamped = max(0.0, min(1.0, float(weight)))
        with self._txn() as conn:
            conn.execute(
                "UPDATE edges SET recency_weight = ? WHERE edge_id = ?",
                (clamped, edge_id),
            )

    # ─── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Node:
        """Reconstruct a :class:`Node` from a SQLite row.

        Bad JSON in ``metadata_json`` is tolerated — corrupt rows
        surface as nodes with empty metadata, rather than breaking
        the whole list.
        """
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return Node(
            node_id=row["node_id"],
            kind=row["kind"],
            value=row["value"],
            created_at=float(row["created_at"]),
            last_seen_at=float(row["last_seen_at"]),
            confidence=float(row["confidence"]),
            metadata=metadata,
        )

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        """Reconstruct an :class:`Edge` from a SQLite row.

        Bad JSON in ``evidence_json`` is tolerated — corrupt rows
        surface as edges with empty evidence, rather than breaking
        ``list_edges()``.
        """
        try:
            evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else {}
        except (json.JSONDecodeError, TypeError):
            evidence = {}
        # Phase 4: ``source`` may be missing on rows written before the
        # v1→v2 migration ran in this process; fall back to "unknown".
        try:
            source = row["source"] or "unknown"
        except (IndexError, KeyError):
            source = "unknown"
        return Edge(
            edge_id=row["edge_id"],
            kind=row["kind"],
            from_node=row["from_node"],
            to_node=row["to_node"],
            salience=float(row["salience"]),
            confidence=float(row["confidence"]),
            recency_weight=float(row["recency_weight"]),
            source_reliability=float(row["source_reliability"]),
            decay_rate=float(row["decay_rate"]),
            created_at=float(row["created_at"]),
            evidence=evidence,
            source=source,
        )


__all__ = [
    "UserModelStore",
    "SCHEMA_VERSION",
    "apply_migrations",
]
