"""
PluginDemandTracker — record demand for installed-but-disabled plugins.

Sub-project E, Task E2. Not a plugin — plugins MUST NOT import from this
module. Lives in ``opencomputer.plugins`` (core) alongside discovery and
loader.

Purpose
-------
When the LLM calls a tool the active session's ``ToolRegistry`` can't
dispatch, that's a demand signal: the user's intent required a capability
some INSTALLED plugin would provide if enabled. By recording those
signals per-plugin and per-session, the CLI (E5) can surface a prompt
like "Edit was called 4 times this session — enable `coding-harness`?".

Resolution happens WITHOUT loading any plugin code: we use the
``PluginManifest.tool_names`` field (E1) to map a tool name to candidate
plugin ids purely from the cached manifest.

Storage
-------
One SQLite table in the session DB (same file as episodic memory, one DB
per profile). Schema kept deliberately flat — no foreign keys to sessions
because the session row may not exist yet when the tracker first writes
(the tracker is used during dispatch, which can happen before the session
is fully initialised in some edge cases).

Concurrency
-----------
SessionDB already opens its connections in WAL mode. The tracker sets WAL
explicitly too so it remains safe when used against a fresh DB path that
SessionDB hasn't opened yet (e.g. in tests and before the first session).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from opencomputer.plugins.discovery import PluginCandidate

logger = logging.getLogger("opencomputer.plugins.demand_tracker")

# E7 (2026-05-04) — synthetic ``tool_name`` value used by
# :meth:`PluginDemandTracker.scan_user_prompt` so readers can tell
# keyword-match signals apart from real tool-not-found signals without
# a schema migration. The string mirrors a tool name format intentionally
# (it'll never collide with a real tool because of the ``__`` prefix).
USER_PROMPT_KEYWORD_MARKER = "__user_prompt_match__"

# Generic English stopwords + filler that would over-match against
# plugin manifest descriptions. Kept tight — these are words that
# carry no information about user intent.
_STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from",
        "have", "has", "i", "if", "in", "is", "it", "its", "of", "on", "or",
        "the", "this", "that", "to", "was", "were", "will", "with", "you",
        "your", "yours", "we", "us", "our", "they", "them", "their", "he",
        "she", "his", "her", "but", "not", "so", "than", "then", "what",
        "when", "where", "who", "why", "how", "can", "could", "would",
        "should", "may", "might", "must", "shall", "into", "out", "up",
        "down", "over", "under", "all", "any", "some", "each", "every",
        "no", "nor", "only", "own", "same", "such", "too", "very", "just",
        "more", "most", "less", "least", "much", "many", "few", "one", "two",
        "first", "last", "next", "new", "old", "good", "bad", "high", "low",
    ]
)

_WORD_RE = re.compile(r"[a-z0-9_-]+")


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS plugin_demand (
    plugin_id   TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    ts          REAL NOT NULL
);
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS plugin_demand_by_plugin "
    "ON plugin_demand(plugin_id, session_id);"
)


class PluginDemandTracker:
    """Record tool-not-found events against the session DB.

    Parameters
    ----------
    db_path
        The session DB path (``cfg.session.db_path``). Same DB as episodic
        memory — one DB per profile.
    discover_fn
        Callable returning the current list of ``PluginCandidate``s.
        Injected so tests can substitute a fake that doesn't walk the
        filesystem. Production usage: ``lambda: discover(
        standard_search_paths())``.
    active_profile_plugins
        Frozenset of plugin ids enabled for the active profile. ``None``
        means "no filter applied" — record for every matching installed
        plugin. Production: read from ``profile.yaml`` via
        ``ProfileConfig``.
    """

    def __init__(
        self,
        db_path: Path,
        discover_fn: Callable[[], list[PluginCandidate]],
        active_profile_plugins: frozenset[str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.discover_fn = discover_fn
        self.active_profile_plugins = active_profile_plugins
        # Make sure the parent directory exists — tests often hand us a
        # path under tmp_path that may or may not exist yet.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    # ─── connection helper ────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ─── schema ───────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        """Create the ``plugin_demand`` table + index if absent. Idempotent."""
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)
            conn.commit()

    # ─── write path ───────────────────────────────────────────────

    def record_tool_not_found(
        self,
        tool_name: str,
        session_id: str,
        turn_index: int,
    ) -> None:
        """Insert one row per installed-but-disabled plugin that would provide
        ``tool_name``.

        - Candidates are resolved via ``discover_fn()`` — plugins whose
          ``manifest.tool_names`` contains ``tool_name``.
        - If ``active_profile_plugins`` is set, candidates already in that
          set are filtered out (no point recommending a plugin the user
          has already enabled).
        - If no candidate matches, this is a silent no-op. The LLM may
          call hallucinated tool names; we don't want to pollute the
          table with plugin_ids that can never satisfy them.
        """
        try:
            candidates = self.discover_fn()
        except Exception:  # noqa: BLE001
            logger.exception("demand-tracker: discover_fn raised — skipping record")
            return

        matching: list[str] = []
        for cand in candidates:
            # Match against required AND optional tool names so a user
            # calling an opt-in tool (e.g. ``screenshot`` from
            # coding-harness, gated on the ``mss`` pip extra) still gets
            # pointed at the right plugin.
            declared_all = set(cand.manifest.tool_names) | set(
                cand.manifest.optional_tool_names
            )
            if tool_name not in declared_all:
                continue
            if (
                self.active_profile_plugins is not None
                and cand.manifest.id in self.active_profile_plugins
            ):
                continue
            matching.append(cand.manifest.id)

        if not matching:
            return

        ts = time.time()
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO plugin_demand "
                "(plugin_id, tool_name, session_id, turn_index, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (plugin_id, tool_name, session_id, turn_index, ts)
                    for plugin_id in matching
                ],
            )
            conn.commit()

    # ─── read path ────────────────────────────────────────────────

    def recommended_plugins(
        self,
        session_id: str | None = None,
        threshold: int = 3,
        since_turns: int | None = None,
    ) -> list[tuple[str, int]]:
        """Return ``(plugin_id, signal_count)`` pairs ordered by count desc.

        Parameters
        ----------
        session_id
            ``None`` → count across all sessions. Otherwise scoped to the
            given session.
        threshold
            Minimum signal count required for a plugin to be returned.
        since_turns
            If provided, only rows with ``turn_index >= (max_turn -
            since_turns)`` count. When ``session_id`` is set, the max is
            computed within that session; otherwise globally.
        """
        min_turn: int | None = None
        if since_turns is not None:
            with self._connect() as conn:
                if session_id is not None:
                    row = conn.execute(
                        "SELECT MAX(turn_index) FROM plugin_demand WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT MAX(turn_index) FROM plugin_demand"
                    ).fetchone()
                max_turn = row[0] if row and row[0] is not None else None
            if max_turn is None:
                return []
            min_turn = max_turn - since_turns

        where_clauses: list[str] = []
        params: list = []
        if session_id is not None:
            where_clauses.append("session_id = ?")
            params.append(session_id)
        if min_turn is not None:
            where_clauses.append("turn_index >= ?")
            params.append(min_turn)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        sql = (
            "SELECT plugin_id, COUNT(*) AS cnt "
            "FROM plugin_demand "
            f"{where_sql} "
            "GROUP BY plugin_id "
            "HAVING cnt >= ? "
            "ORDER BY cnt DESC, plugin_id ASC"
        )
        params.append(threshold)

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [(row["plugin_id"], int(row["cnt"])) for row in rows]

    def scan_user_prompt(
        self,
        text: str,
        *,
        session_id: str = "",
        turn_index: int = 0,
        min_matches: int = 2,
    ) -> list[str]:
        """Scan a user prompt for keyword matches against disabled plugins.

        E7 (2026-05-04). Tokenizes ``text`` (lowercased word-split), then
        for every NOT-yet-enabled plugin candidate counts how many of the
        plugin's manifest-derived terms appear in the prompt. When the
        count reaches ``min_matches``, records a demand signal in the
        same ``plugin_demand`` table — using
        :data:`USER_PROMPT_KEYWORD_MARKER` as the synthetic tool_name so
        readers can distinguish keyword signals from real tool-not-found
        signals.

        Returns the list of plugin_ids that triggered (for callers that
        want to log/notify). Best-effort: any DB or discovery failure is
        swallowed so the loop never crashes on demand-tracking.
        """
        try:
            candidates = self.discover_fn()
        except Exception:  # noqa: BLE001
            logger.exception("demand-tracker: discover_fn raised in scan_user_prompt")
            return []

        prompt_tokens = _tokenize_prompt(text)
        if not prompt_tokens:
            return []

        triggered: list[str] = []
        rows_to_insert: list[tuple[str, str, str, int, float]] = []
        ts = time.time()
        for cand in candidates:
            if (
                self.active_profile_plugins is not None
                and cand.manifest.id in self.active_profile_plugins
            ):
                continue
            terms = _extract_plugin_terms(cand)
            if not terms:
                continue
            hits = terms & prompt_tokens
            if len(hits) < min_matches:
                continue
            rows_to_insert.append(
                (
                    cand.manifest.id,
                    USER_PROMPT_KEYWORD_MARKER,
                    session_id,
                    turn_index,
                    ts,
                )
            )
            triggered.append(cand.manifest.id)

        if not rows_to_insert:
            return []

        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO plugin_demand "
                    "(plugin_id, tool_name, session_id, turn_index, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows_to_insert,
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("demand-tracker: scan_user_prompt write failed")
            return []
        return triggered

    def signals_by_plugin(
        self,
        session_id: str | None = None,
    ) -> dict[str, list[dict]]:
        """Return ``{plugin_id: [row_dict, ...]}`` for display (E5 CLI).

        Each row dict has keys: ``tool_name``, ``session_id``,
        ``turn_index``, ``ts``.
        """
        where_sql = ""
        params: tuple = ()
        if session_id is not None:
            where_sql = "WHERE session_id = ?"
            params = (session_id,)

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT plugin_id, tool_name, session_id, turn_index, ts "
                "FROM plugin_demand "
                f"{where_sql} "
                "ORDER BY ts ASC",
                params,
            ).fetchall()

        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row["plugin_id"], []).append(
                {
                    "tool_name": row["tool_name"],
                    "session_id": row["session_id"],
                    "turn_index": row["turn_index"],
                    "ts": row["ts"],
                }
            )
        return out

    # ─── maintenance ─────────────────────────────────────────────

    def clear(self, plugin_id: str) -> int:
        """Delete all rows for ``plugin_id``. Returns the rowcount.

        Called when the user enables the plugin (E4) so stale signals
        don't keep recommending an already-active plugin.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM plugin_demand WHERE plugin_id = ?",
                (plugin_id,),
            )
            conn.commit()
            return int(cur.rowcount)


def _tokenize_prompt(text: str) -> set[str]:
    """Lowercase + word-split + stopword filter. Returns a set for O(1) lookup."""
    tokens = set(_WORD_RE.findall(text.lower()))
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _extract_plugin_terms(cand: PluginCandidate) -> set[str]:
    """Search-relevant terms from a plugin candidate.

    Pulls from manifest.id, manifest.description, and manifest.tool_names.
    Filtered through the same stopword + length rules as prompt tokens so
    the intersection check is symmetric.
    """
    terms: set[str] = set()
    terms.update(_WORD_RE.findall(cand.manifest.id.lower()))
    terms.update(_WORD_RE.findall((cand.manifest.description or "").lower()))
    for tn in cand.manifest.tool_names:
        terms.update(_WORD_RE.findall(tn.lower()))
    return {t for t in terms if t not in _STOPWORDS and len(t) > 1}


__all__ = [
    "USER_PROMPT_KEYWORD_MARKER",
    "PluginDemandTracker",
]
