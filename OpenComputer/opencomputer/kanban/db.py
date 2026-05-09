"""SQLite-backed Kanban board for multi-profile collaboration.

The board lives at ``<root>/kanban.db`` where ``<root>`` is the **shared
OC root** (the parent of any active profile). Profiles intentionally
collapse onto a single board: it IS the cross-profile coordination
primitive. A worker spawned with ``oc -p <profile>`` joins the same
board as the dispatcher that claimed the task. The same applies to
``<root>/kanban/workspaces/`` and ``<root>/kanban/logs/``.

In standard installs ``<root>`` is ``~/.opencomputer``. In Docker / custom
deployments where ``OC_HOME`` points outside ``~/.opencomputer`` (e.g.
``/opt/oc``), ``<root>`` is ``OC_HOME``. Three env-var overrides
are available (highest precedence first, all optional):

* ``OC_KANBAN_DB`` — pin the database file path directly.
* ``OC_KANBAN_WORKSPACES_ROOT`` — pin the workspaces root directly.
* ``OC_KANBAN_HOME`` — pin the umbrella root that anchors all three
  kanban paths (db + workspaces + logs). Useful for tests and unusual
  deployments where a single override is enough.

The dispatcher injects ``OC_KANBAN_DB`` and
``OC_KANBAN_WORKSPACES_ROOT`` into the worker subprocess env as a
defense-in-depth measure: even if the worker's ``_oc_home()``
resolution somehow disagrees with the dispatcher's (unusual symlink or
Docker layout), the two processes still converge on the same files.

Schema is intentionally small: tasks, task_links, task_comments,
task_events.  The ``workspace_kind`` field decouples coordination from git
worktrees so that research / ops / digital-twin workloads work alongside
coding workloads.  See ``docs/oc-kanban-v1-spec.pdf`` for the full
design specification.

Concurrency strategy: WAL mode + ``BEGIN IMMEDIATE`` for write
transactions + compare-and-swap (CAS) updates on ``tasks.status`` and
``tasks.claim_lock``.  SQLite serializes writers via its WAL lock, so at
most one claimer can win any given task.  Losers observe zero affected
rows and move on -- no retry loops, no distributed-lock machinery.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {"triage", "todo", "ready", "running", "blocked", "done", "archived"}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}

# A running task's claim is valid for 15 minutes; after that the next
# dispatcher tick reclaims it.  Workers that outlive this window should call
# ``heartbeat_claim(task_id)`` periodically.  In practice most kanban
# workloads either finish within 15m or set a longer claim explicitly.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60


# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # 4 KB per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # 8 KB per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # 2 KB per comment


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def kanban_home() -> Path:
    """Return the shared OC root that anchors the kanban board.

    Resolution order:

    1. ``OC_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments).
    2. ``_oc_home()``, which already returns ``<root>``
       when ``OC_HOME`` is ``<root>/profiles/<name>``, and returns
       ``OC_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``OC_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = os.environ.get("OC_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from opencomputer.agent.config import _home as _oc_home
    return _oc_home()


# ---------------------------------------------------------------------------
# Multi-board support (Wave 6.E.8 / Hermes parity)
# ---------------------------------------------------------------------------
#
# Hermes lets users keep multiple boards keyed by a slug:
# ``~/.hermes/kanban/boards/<slug>/{kanban.db,workspaces,logs}``. We
# mirror that with ``OC_KANBAN_BOARD`` env + an active-board state
# file so users can ``oc kanban boards switch <slug>`` without
# re-exporting env on every CLI call.
#
# Resolution precedence (highest → lowest):
#
# 1. ``OC_KANBAN_DB`` env (explicit path pin — already supported)
# 2. ``OC_KANBAN_BOARD`` env (slug; resolves to per-board path)
# 3. Active-board state file at ``<root>/kanban/.active-board``
# 4. Legacy default ``<root>/kanban.db`` (single-board, pre-multi-board)
#
# Migration is lazy: existing single-board users see no change unless
# they call ``oc kanban boards switch <slug>`` or set the env var.

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Wave 6.E.16 — sentinel slug naming the legacy unnamed-default board
# in cross-board operations. The ONLY slug allowed to start with an
# underscore; the regex still rejects user-chosen leading-underscore
# slugs to keep the namespace clean.
DEFAULT_BOARD_SENTINEL = "_default_"


class InvalidBoardSlugError(ValueError):
    """Raised when a board slug fails validation. Slug must be 1-64
    chars, lowercase alphanumeric + hyphens/underscores, starting with
    a letter or digit."""


def validate_slug(slug: str) -> None:
    """Reject malformed slugs. Same rules as hermes plus the
    :data:`DEFAULT_BOARD_SENTINEL` exception.

    Raises :class:`InvalidBoardSlugError` with a user-friendly message
    on failure. Empty / non-str / wrong shape all raise.

    Wave 6.E.16: the sentinel slug ``_default_`` passes validation —
    it names the legacy unnamed-default board in cross-board
    operations. User-chosen leading-underscore slugs still fail.
    """
    if not isinstance(slug, str) or not slug:
        raise InvalidBoardSlugError(
            "board slug must be a non-empty string"
        )
    if slug == DEFAULT_BOARD_SENTINEL:
        return
    if not _SLUG_RE.match(slug):
        raise InvalidBoardSlugError(
            f"board slug {slug!r} must be 1-64 characters, lowercase "
            "alphanumerics + hyphens/underscores, starting with a "
            f"letter or digit (e.g. 'project-x', 'q4_planning'); the "
            f"only allowed underscore-prefix slug is "
            f"{DEFAULT_BOARD_SENTINEL!r} (legacy default)"
        )


def boards_root() -> Path:
    """Directory under which all named boards live.

    ``<kanban_home>/kanban/boards/`` — siblings of the legacy
    single-board ``kanban.db`` (which remains the unnamed default).
    """
    return kanban_home() / "kanban" / "boards"


def _active_board_state_file() -> Path:
    """File that records the currently-switched-to board slug.

    Plain text, single line, slug only. Missing file = legacy default.
    """
    return kanban_home() / "kanban" / ".active-board"


def active_board() -> str | None:
    """Return the active board slug if one is set, else None.

    Resolution: ``OC_KANBAN_BOARD`` env first (allows per-shell overrides
    without touching the state file), then the state file.
    """
    env = os.environ.get("OC_KANBAN_BOARD", "").strip()
    if env:
        try:
            validate_slug(env)
            return env
        except InvalidBoardSlugError:
            # Bad env value — log + fall through to state file rather
            # than crash. Documented behaviour.
            return None
    state_file = _active_board_state_file()
    if not state_file.exists():
        return None
    try:
        slug = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not slug:
        return None
    try:
        validate_slug(slug)
    except InvalidBoardSlugError:
        return None
    return slug


def set_active_board(slug: str | None) -> None:
    """Persist the active board slug to the state file.

    ``None`` (or :data:`DEFAULT_BOARD_SENTINEL`) clears the file — both
    map to the legacy default. The board directory is NOT created
    here — call :func:`board_db_path` and :func:`init_db` to
    materialize the per-board layout.
    """
    state_file = _active_board_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if slug is None or slug == DEFAULT_BOARD_SENTINEL:
        if state_file.exists():
            state_file.unlink()
        return
    validate_slug(slug)
    state_file.write_text(slug, encoding="utf-8")


def board_db_path(slug: str | None) -> Path:
    """Compute the kanban.db path for ``slug``.

    Resolution:
    - ``None`` → legacy unnamed default at ``<kanban_home>/kanban.db``
    - ``DEFAULT_BOARD_SENTINEL`` (``"_default_"``, Wave 6.E.16) → same
      legacy path; lets cross-board operations explicitly target the
      legacy default by name
    - any other validated slug → ``<kanban_home>/kanban/boards/<slug>/kanban.db``
    """
    if slug is None or slug == DEFAULT_BOARD_SENTINEL:
        return kanban_home() / "kanban.db"
    validate_slug(slug)
    return boards_root() / slug / "kanban.db"


def list_boards() -> list[str]:
    """Return all known board slugs (subdirectories of boards_root).

    Returns an empty list when no boards directory exists. Slugs are
    sorted alphabetically.
    """
    root = boards_root()
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and _SLUG_RE.match(d.name)
    )


def kanban_db_path() -> Path:
    """Return the path to the active ``kanban.db``.

    Resolution precedence (highest → lowest):

    1. ``OC_KANBAN_DB`` (explicit path pin)
    2. ``OC_KANBAN_BOARD`` env / active-board state file → per-board path
    3. Legacy ``<kanban_home>/kanban.db`` (single-board default)

    The dispatcher injects ``OC_KANBAN_DB`` and ``OC_KANBAN_BOARD``
    into worker subprocess env so workers converge on the same board
    the dispatcher is using.
    """
    override = os.environ.get("OC_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    slug = active_board()
    if slug is not None:
        return board_db_path(slug)
    return kanban_home() / "kanban.db"


def workspaces_root() -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Per-board workspaces live under
    ``<kanban_home>/kanban/boards/<slug>/workspaces/`` so each board's
    scratch state is isolated. Legacy single-board layout uses
    ``<kanban_home>/kanban/workspaces/``.

    ``OC_KANBAN_WORKSPACES_ROOT`` pins the path directly (highest
    precedence) — the dispatcher injects this into worker subprocess
    env as defense-in-depth.
    """
    override = os.environ.get("OC_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = active_board()
    if slug is not None:
        return boards_root() / slug / "workspaces"
    return kanban_home() / "kanban" / "workspaces"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: str | None
    assignee: str | None
    status: str
    priority: int
    created_by: str | None
    created_at: int
    started_at: int | None
    completed_at: int | None
    workspace_kind: str
    workspace_path: str | None
    claim_lock: str | None
    claim_expires: int | None
    tenant: str | None
    result: str | None = None
    idempotency_key: str | None = None
    spawn_failures: int = 0
    worker_pid: int | None = None
    last_spawn_error: str | None = None
    max_runtime_seconds: int | None = None
    last_heartbeat_at: int | None = None
    current_run_id: int | None = None
    workflow_template_id: str | None = None
    current_step_key: str | None = None
    # Force-loaded skills for the worker on this task (appended to the
    # dispatcher's built-in `kanban-worker` via --skills). Stored as a
    # JSON array of skill names. None = use only the defaults; empty
    # list = explicitly no extra skills.
    skills: list | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        keys = set(row.keys())
        # Parse skills JSON blob if present
        skills_value: list | None = None
        if "skills" in keys and row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            tenant=row["tenant"] if "tenant" in keys else None,
            result=row["result"] if "result" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            spawn_failures=row["spawn_failures"] if "spawn_failures" in keys else 0,
            worker_pid=row["worker_pid"] if "worker_pid" in keys else None,
            last_spawn_error=row["last_spawn_error"] if "last_spawn_error" in keys else None,
            max_runtime_seconds=(
                row["max_runtime_seconds"] if "max_runtime_seconds" in keys else None
            ),
            last_heartbeat_at=(
                row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
            ),
            current_run_id=(
                row["current_run_id"] if "current_run_id" in keys else None
            ),
            workflow_template_id=(
                row["workflow_template_id"] if "workflow_template_id" in keys else None
            ),
            current_step_key=(
                row["current_step_key"] if "current_step_key" in keys else None
            ),
            skills=skills_value,
        )


@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """

    id: int
    task_id: str
    profile: str | None
    step_key: str | None
    status: str
    claim_lock: str | None
    claim_expires: int | None
    worker_pid: int | None
    max_runtime_seconds: int | None
    last_heartbeat_at: int | None
    started_at: int
    ended_at: int | None
    outcome: str | None
    summary: str | None
    metadata: dict | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            step_key=row["step_key"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            max_runtime_seconds=row["max_runtime_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=meta,
            error=row["error"],
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: dict | None
    created_at: int
    run_id: int | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    spawn_failures       INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_spawn_error     TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Appended to the dispatcher's built-in `--skills kanban-worker`.
    -- NULL or empty array = no extras.
    skills               TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id    TEXT NOT NULL,
    child_id     TEXT NOT NULL,
    -- Wave 6.E.10 — cross-board dependencies. NULL = link is within
    -- the current board (back-compat). Non-NULL = parent / child lives
    -- in the named board (resolved via boards_root() / <slug> / kanban.db).
    parent_board TEXT,
    child_board  TEXT,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- Wave 6.E.13 — Multi-host write coordination (registered peers).
-- Each row is a peer this instance trusts. The HMAC secret is shared
-- (used by both sides to sign + verify requests).
CREATE TABLE IF NOT EXISTS kanban_remote_hosts (
    slug                     TEXT PRIMARY KEY,
    url                      TEXT NOT NULL,
    hmac_secret              TEXT NOT NULL,
    added_at                 INTEGER NOT NULL,
    last_seen_at             INTEGER,
    -- Wave 6.E.15 — opt in to dir:<path> workspace payload sync.
    -- Both sides must have this on for workspace contents to flow
    -- across the spawn + callback boundary. 0 = off (back-compat).
    workspace_sync_enabled   INTEGER NOT NULL DEFAULT 0
);

-- Wave 6.E.13 — Pending tasks delegated to a remote host. Server-time
-- TTL leases (not client-time) defeat clock-skew attacks. Status
-- transitions to done | failed when the remote sends a callback.
CREATE TABLE IF NOT EXISTS kanban_remote_claims (
    local_task_id   TEXT NOT NULL,
    remote_slug     TEXT NOT NULL,
    remote_task_id  TEXT NOT NULL,
    leased_at       INTEGER NOT NULL,
    lease_until     INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_heartbeat  INTEGER,
    PRIMARY KEY (local_task_id, remote_slug)
);

-- Wave 6.E.9 — Auto-assignment routing rules (Hermes 'out of scope'
-- item). When a ready task has assignee IS NULL, the dispatcher walks
-- this table in priority DESC, id ASC order; the first matching rule
-- wins. pattern_kind values:
--   'title_regex'  — re.search(pattern, task.title)
--   'tenant'       — exact match on task.tenant
--   'default'      — always matches (catch-all)
CREATE TABLE IF NOT EXISTS kanban_assignment_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_kind TEXT NOT NULL,
    pattern      TEXT NOT NULL,
    assignee     TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant          ON tasks(tenant);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency     ON tasks(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_run            ON task_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX IF NOT EXISTS idx_rules_priority        ON kanban_assignment_rules(priority DESC);
CREATE INDEX IF NOT EXISTS idx_remote_claims_lease   ON kanban_remote_claims(lease_until);
CREATE INDEX IF NOT EXISTS idx_remote_claims_status  ON kanban_remote_claims(status);

-- Wave 6.E.17 — Peer-side mirror of "this local task came from peer X
-- via /proxy/spawn and the sender's callback URL is Y." Used by the
-- callback queue (below) to find the right destination + signing key
-- when a delegated task transitions to a terminal state.
CREATE TABLE IF NOT EXISTS kanban_delegated_tasks (
    local_task_id   TEXT PRIMARY KEY,
    sender_slug     TEXT NOT NULL,
    callback_url    TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);

-- Wave 6.E.17 — Outbound callback retry queue. When a peer's worker
-- transitions a delegated task to done|blocked|failed, we enqueue a
-- callback row instead of POSTing inline. The dispatcher's drainer
-- tick walks status='pending' rows whose next_attempt_at <= now,
-- POSTs each, and either marks delivered (2xx) or bumps attempt_count
-- with exponential backoff. After max_attempts the row is marked
-- 'dead' for operator review.
CREATE TABLE IF NOT EXISTS kanban_pending_callbacks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_slug     TEXT NOT NULL,
    callback_url    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_callbacks_due ON kanban_pending_callbacks(status, next_attempt_at);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    WAL mode is enabled on every connection; it's a no-op after the first
    time but keeps the code robust if the DB file is ever re-created.

    The first connection to a given path auto-runs :func:`init_db` so
    fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. Subsequent
    connections skip the schema check via a module-level path cache.
    """
    path = db_path or kanban_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    needs_init = resolved not in _INITIALIZED_PATHS
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if needs_init:
        # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
        # migrations. Cached so subsequent connect() calls in the same
        # process are cheap.
        conn.executescript(SCHEMA_SQL)
        _migrate_add_optional_columns(conn)
        _INITIALIZED_PATHS.add(resolved)
    return conn


def init_db(db_path: Path | None = None) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Kept as a public entry point so CLI ``oc kanban init`` and the
    daemon have something explicit to call. Unlike :func:`connect`'s
    first-time auto-init (which caches by path), ``init_db`` always
    re-runs the migration pass. Callers that know the on-disk schema
    may have drifted — tests that write legacy event kinds directly,
    external tools that upgrade an old DB file — can call this to
    force re-migration.
    """
    path = db_path or kanban_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    # Clear the cache entry so the underlying connect() re-runs the
    # schema + migration pass unconditionally.
    _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path)):
        pass
    return path


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after v1 release to legacy DBs.

    Called by ``init_db`` so opening an old DB is always safe.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tenant" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN tenant TEXT")
    if "result" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
    if "idempotency_key" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency "
            "ON tasks(idempotency_key)"
        )
    if "spawn_failures" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN spawn_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "worker_pid" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN worker_pid INTEGER")
    if "last_spawn_error" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_spawn_error TEXT")
    if "max_runtime_seconds" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN max_runtime_seconds INTEGER")
    if "last_heartbeat_at" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_heartbeat_at INTEGER")
    if "current_run_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_run_id INTEGER")
    if "workflow_template_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN workflow_template_id TEXT")
    if "current_step_key" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_step_key TEXT")
    if "skills" not in cols:
        # JSON array of skill names the dispatcher force-loads into the
        # worker (additive to the built-in `kanban-worker`). NULL is fine
        # for existing rows.
        conn.execute("ALTER TABLE tasks ADD COLUMN skills TEXT")

    # task_events gained a run_id column; back-fill it as NULL for
    # historical events (they predate runs and can't be attributed).
    ev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "run_id" not in ev_cols:
        conn.execute("ALTER TABLE task_events ADD COLUMN run_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_run "
            "ON task_events(run_id, id)"
        )

    # Wave 6.E.10 — task_links gained parent_board / child_board columns
    # for cross-board dependencies. NULL = same-board (back-compat).
    link_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_links)")}
    if "parent_board" not in link_cols:
        conn.execute("ALTER TABLE task_links ADD COLUMN parent_board TEXT")
    if "child_board" not in link_cols:
        conn.execute("ALTER TABLE task_links ADD COLUMN child_board TEXT")

    # Wave 6.E.15 — kanban_remote_hosts gained workspace_sync_enabled
    # column. Existing rows default to 0 (off). Migration is additive.
    rh_table_exists = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='kanban_remote_hosts'",
    ).fetchone() is not None
    if rh_table_exists:
        rh_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(kanban_remote_hosts)")
        }
        if "workspace_sync_enabled" not in rh_cols:
            conn.execute(
                "ALTER TABLE kanban_remote_hosts "
                "ADD COLUMN workspace_sync_enabled INTEGER NOT NULL DEFAULT 0"
            )

    # One-shot backfill: any task that is 'running' before runs existed
    # had its claim_lock / claim_expires / worker_pid on the task row.
    # Synthesize a matching task_runs row so subsequent end-run / heartbeat
    # calls have something to write to. Wrapped in write_txn to serialize
    # against any concurrent dispatcher, and the per-row UPDATE uses
    # ``current_run_id IS NULL`` as a CAS guard so a racing claim can't
    # produce an orphaned row if it interleaves with the backfill pass.
    runs_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_exist:
        with write_txn(conn):
            inflight = conn.execute(
                "SELECT id, assignee, claim_lock, claim_expires, worker_pid, "
                "       max_runtime_seconds, last_heartbeat_at, started_at "
                "FROM tasks "
                "WHERE status = 'running' AND current_run_id IS NULL"
            ).fetchall()
            for row in inflight:
                started = row["started_at"] or int(time.time())
                cur = conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, profile, status,
                        claim_lock, claim_expires, worker_pid,
                        max_runtime_seconds, last_heartbeat_at,
                        started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], row["assignee"], row["claim_lock"],
                        row["claim_expires"], row["worker_pid"],
                        row["max_runtime_seconds"], row["last_heartbeat_at"],
                        started,
                    ),
                )
                # CAS: only install the pointer if nothing else claimed
                # the task between our SELECT and here (shouldn't happen
                # under the write_txn, but belt-and-suspenders). If the
                # CAS fails we've got an orphan run_row — mark it
                # reclaimed so it doesn't look in-flight.
                upd = conn.execute(
                    "UPDATE tasks SET current_run_id = ? "
                    "WHERE id = ? AND current_run_id IS NULL",
                    (cur.lastrowid, row["id"]),
                )
                if upd.rowcount != 1:
                    conn.execute(
                        "UPDATE task_runs SET status = 'reclaimed', "
                        "    outcome = 'reclaimed', ended_at = ? "
                        "WHERE id = ?",
                        (int(time.time()), cur.lastrowid),
                    )

    # One-shot event-kind rename pass. The old names ("ready", "priority",
    # "spawn_auto_blocked") still worked but were awkward on the wire;
    # rename them in-place so existing DBs migrate cleanly. Fires once
    # per DB because after the UPDATE no rows match the old kinds.
    _EVENT_RENAMES = (
        # (old, new)
        ("ready",              "promoted"),
        ("priority",           "reprioritized"),
        ("spawn_auto_blocked", "gave_up"),
    )
    for old, new in _EVENT_RENAMES:
        conn.execute(
            "UPDATE task_events SET kind = ? WHERE kind = ?",
            (new, old),
        )


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_task_id() -> str:
    """Generate a short, URL-safe task id.

    4 hex bytes = ~4.3B possibilities. At 10k tasks the collision
    probability is ~1.2e-5; at 100k it's ~1.2e-3. Previously we used 2
    hex bytes (65k possibilities) which hit the birthday paradox hard:
    ~5% collision probability at 1k tasks, ~50% at 10k. Callers that
    care about idempotency should pass ``idempotency_key`` to
    :func:`create_task` rather than rely on id uniqueness.
    """
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str | None = None,
    assignee: str | None = None,
    created_by: str | None = None,
    workspace_kind: str = "scratch",
    workspace_path: str | None = None,
    tenant: str | None = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: str | None = None,
    max_runtime_seconds: int | None = None,
    skills: Iterable[str] | None = None,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``oc --skills ...`` alongside the built-in
    ``kanban-worker``. Use this to pin a task to a specialist skill
    (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).
    """
    if not title or not title.strip():
        raise ValueError("title is required")
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    parents = tuple(p for p in parents if p)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `oc --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: list[str] | None = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    f"(pass a list of separate names instead of a comma-joined string)"
                )
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        skills_list = cleaned

    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    now = int(time.time())

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                # Determine initial status from parent status, unless the
                # caller is parking this task in triage for a specifier.
                if triage:
                    initial_status = "triage"
                else:
                    initial_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo.
                        rows = conn.execute(
                            "SELECT status FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")",
                            parents,
                        ).fetchall()
                        if any(r["status"] != "done" for r in rows):
                            initial_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        tenant, idempotency_key, max_runtime_seconds, skills
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        initial_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": initial_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "skills": list(skills_list) if skills_list else None,
                    },
                )
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: str | None = None,
    status: str | None = None,
    tenant: str | None = None,
    include_archived: bool = False,
    limit: int | None = None,
) -> list[Task]:
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(assignee)
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def apply_specify(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    expanded_body: str,
    new_status: str = "todo",
) -> bool:
    """Persist the result of a triage→spec expansion.

    Idempotent on identical inputs (re-running specify with the same body
    produces the same row state). Returns False if the task does not
    exist; raises :class:`ValueError` if ``new_status`` is not a member
    of :data:`VALID_STATUSES` (catches typos at the call site rather
    than letting bad data into the DB).

    The function is intentionally decoupled from the LLM call —
    :mod:`opencomputer.kanban.specify` produces ``expanded_body`` and
    delegates persistence here so the DB layer stays sync + testable
    without an LLM dependency.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row:
            return False
        old_status = row["status"]
        conn.execute(
            "UPDATE tasks SET body = ?, status = ? WHERE id = ?",
            (expanded_body, new_status, task_id),
        )
        _append_event(
            conn, task_id, "specified",
            {
                "old_status": old_status, "new_status": new_status,
                "body_chars": len(expanded_body),
            },
        )
        return True


def assign_task(conn: sqlite3.Connection, task_id: str, profile: str | None) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    parent_board: str | None = None,
    child_board: str | None = None,
) -> None:
    """Add a parent → child dependency edge.

    ``parent_board`` / ``child_board`` (Wave 6.E.10): NULL means the
    end lives in the current board (back-compat). Non-NULL identifies
    a sibling board by slug — used for cross-board dependencies. The
    cycle check + parent-existence check still runs for same-board
    edges; for cross-board edges we ATTACH the remote DB read-only and
    verify only that the parent task exists there.

    Raises ValueError on self-link, unknown task, or detected cycle.
    Wave 6.E.12: cross-board cycle detection now walks the full
    multi-board graph via :func:`_would_cycle_global` (capped at
    MAX_CROSS_BOARD_HOPS). Same-board cycle detection still uses the
    original optimized walker.
    """
    if parent_id == child_id and parent_board == child_board:
        raise ValueError("a task cannot depend on itself")
    same_board = parent_board is None and child_board is None
    with write_txn(conn):
        if same_board:
            missing = _find_missing_parents(conn, [parent_id, child_id])
            if missing:
                raise ValueError(f"unknown task(s): {', '.join(missing)}")
            if _would_cycle(conn, parent_id, child_id):
                raise ValueError(
                    f"linking {parent_id} -> {child_id} would create a cycle"
                )
        else:
            # Cross-board: child must exist locally; parent must exist
            # in the named parent_board.
            child_actual_board = child_board
            if child_actual_board is None:
                missing = _find_missing_parents(conn, [child_id])
                if missing:
                    raise ValueError(
                        f"unknown child task(s): {', '.join(missing)}"
                    )
            if parent_board is not None:
                parent_db = board_db_path(parent_board)
                if not parent_db.exists():
                    raise ValueError(
                        f"parent_board {parent_board!r} has no kanban.db at {parent_db}"
                    )
                # Use a fresh connection so we don't leak the ATTACH on
                # the long-lived dispatch connection (audit lens A4).
                with sqlite3.connect(str(parent_db)) as parent_conn:
                    parent_conn.row_factory = sqlite3.Row
                    row = parent_conn.execute(
                        "SELECT id FROM tasks WHERE id = ?", (parent_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            f"unknown parent task {parent_id!r} in board "
                            f"{parent_board!r}"
                        )
            # Wave 6.E.12 — global cycle detection. Closes the deferral
            # documented in PR #456.
            if _would_cycle_global(
                conn,
                parent_id=parent_id,
                child_id=child_id,
                parent_board=parent_board,
                child_board=child_board,
            ):
                raise ValueError(
                    f"linking {parent_id}@{parent_board} -> "
                    f"{child_id}@{child_board} would create a cross-board cycle"
                )
        conn.execute(
            "INSERT OR IGNORE INTO task_links "
            "(parent_id, child_id, parent_board, child_board) "
            "VALUES (?, ?, ?, ?)",
            (parent_id, child_id, parent_board, child_board),
        )
        # Demote child if same-board parent isn't done yet. For cross-
        # board parents we conservatively assume not-done (the read is
        # racy + we'd rather hold than promote-then-revert).
        if same_board:
            parent_status = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (parent_id,)
            ).fetchone()["status"]
            if parent_status != "done":
                conn.execute(
                    "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                    (child_id,),
                )
        else:
            # Cross-board: always demote — promote happens via
            # recompute_ready when the cross-board parent reaches done.
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
        _append_event(
            conn, child_id, "linked",
            {
                "parent": parent_id,
                "child": child_id,
                **({"parent_board": parent_board} if parent_board else {}),
                **({"child_board": child_board} if child_board else {}),
            },
        )


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child creates a cycle (same-board).

    A cycle exists iff ``parent_id`` is already a descendant of
    ``child_id`` via existing parent->child links. Walks downward
    from ``child_id`` and checks whether we reach ``parent_id``.
    """
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


# Wave 6.E.12 — Production-grade cross-board cycle detection.
# Closes the deferral documented in PR #456.
MAX_CROSS_BOARD_HOPS = 64


def _would_cycle_global(
    conn: sqlite3.Connection,
    *,
    parent_id: str,
    child_id: str,
    parent_board: str | None,
    child_board: str | None,
) -> bool:
    """Detect a cycle when the proposed edge spans boards.

    Walks descendants of ``(child_board, child_id)`` across boards.
    A cycle exists iff we reach ``(parent_board, parent_id)``. Caps
    walk at :data:`MAX_CROSS_BOARD_HOPS` to bound runtime in the face
    of pathological data.

    Production-grade implementation: scans every named board's
    task_links table to build a global edge map. A cross-board link
    A@x → B@y might be stored in either board's task_links (depending
    on which connection wrote it), so we normalize by walking BOTH
    boards' rows. Each NULL board reference is interpreted relative
    to the row's home board (the file we're scanning).

    Cross-board reads use a short-lived sqlite3 connection per board
    (no ATTACH on the long-lived linker connection — audit lens A4).

    Unreachable boards are treated as leaves. Hitting
    MAX_CROSS_BOARD_HOPS returns True (fail-closed) — better to refuse
    a link than miss a real cycle in pathological data.
    """
    # Wave 6.E.16 — normalize the legacy default to the sentinel slug
    # so the walker's edge-map key shape matches what _ingest_rows
    # produces below. None at the API surface stays an alias for the
    # legacy default (back-compat).
    norm_parent_board = parent_board if parent_board is not None else DEFAULT_BOARD_SENTINEL
    norm_child_board = child_board if child_board is not None else DEFAULT_BOARD_SENTINEL
    target = (norm_parent_board, parent_id)
    seen: set[tuple[str | None, str]] = set()
    stack: list[tuple[str | None, str]] = [(norm_child_board, child_id)]

    # Pre-build a global edge map by scanning every named board's
    # task_links + the current connection's task_links. NULL board
    # references in a row mean "this row's home board" (the file we're
    # scanning right now), so we resolve at scan time, not later.
    global_edges: dict[tuple[str | None, str], list[tuple[str | None, str]]] = {}

    def _ingest_rows(home_slug: str | None, rows) -> None:
        # Wave 6.E.16 — every NULL board reference resolves to the
        # row's home board. None home_slug means we're scanning the
        # legacy default; map it to the sentinel so target keys match.
        home = home_slug if home_slug is not None else DEFAULT_BOARD_SENTINEL
        for r in rows:
            pb = r["parent_board"] if r["parent_board"] else home
            cb = r["child_board"] if r["child_board"] else home
            global_edges.setdefault(
                (pb, r["parent_id"]), [],
            ).append((cb, r["child_id"]))

    # Wave 6.E.16 — scan EVERY board (named + legacy default) via
    # short-lived sqlite3 connections. Don't try to attribute the
    # passed ``conn`` to a particular slug — we can't reliably infer
    # which file it points at, and the rows are persisted on disk so
    # short-lived scans catch them all.
    boards_to_scan: list[str] = list(list_boards())
    legacy_default_path = kanban_home() / "kanban.db"
    if legacy_default_path.exists() and DEFAULT_BOARD_SENTINEL not in boards_to_scan:
        boards_to_scan.append(DEFAULT_BOARD_SENTINEL)
    for slug in boards_to_scan:
        try:
            path = board_db_path(slug)
        except InvalidBoardSlugError:
            continue
        if not path.exists():
            continue
        try:
            other = sqlite3.connect(str(path))
            other.row_factory = sqlite3.Row
            try:
                rows = other.execute(
                    "SELECT parent_id, child_id, parent_board, child_board "
                    "FROM task_links",
                ).fetchall()
                _ingest_rows(slug, rows)
            finally:
                other.close()
        except sqlite3.Error:
            # Best-effort: a corrupt sibling board can't block linker
            continue

    # Walk the merged graph.
    while stack:
        if len(seen) >= MAX_CROSS_BOARD_HOPS:
            return True
        slug, node = stack.pop()
        node_key = (slug, node)
        if node_key == target:
            return True
        if node_key in seen:
            continue
        seen.add(node_key)
        for next_key in global_edges.get(node_key, []):
            stack.append(next_key)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        if cur.rowcount:
            _append_event(
                conn, child_id, "unlinked",
                {"parent": parent_id, "child": child_id},
            )
        return cur.rowcount > 0


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str | None]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str
) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [
        Comment(
            id=r["id"],
            task_id=r["task_id"],
            author=r["author"],
            body=r["body"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r and r["run_id"] is not None else None),
            )
        )
    return out


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    run_id: int | None = None,
) -> None:
    """Record an event row.  Called from within an already-open txn.

    ``run_id`` is optional: pass the current run id so UIs can group
    events by attempt. For events that aren't scoped to a single run
    (task created/edited/archived, dependency promotion) leave it None
    and the row carries NULL.
    """
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: str | None = None,
    error: str | None = None,
    metadata: dict | None = None,
    status: str | None = None,
) -> int | None:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``oc kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,),
    )
    return run_id


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> int | None:
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _synthesize_ended_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: str | None = None,
    error: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Insert a zero-duration, already-closed run row.

    Used when a terminal transition happens on a task that was never
    claimed (CLI user calling ``oc kanban complete <ready-task>
    --summary X``, or dashboard "mark done" on a ready task). Without
    this, the handoff fields (summary / metadata / error) would be
    silently dropped: ``_end_run`` is a no-op because there's no
    current run.

    The synthetic run has ``started_at == ended_at == now`` so it
    shows up in attempt history as "instant" and doesn't skew elapsed
    stats. Caller is responsible for leaving ``current_run_id`` NULL
    (or for clearing it elsewhere in the same txn) since this
    function does NOT touch the tasks row.
    """
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key,
            outcome, outcome,
            summary, error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------

def recompute_ready(conn: sqlite3.Connection) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done``.

    Wave 6.E.10 — supports cross-board parents. When a link has
    ``parent_board`` set, we open a short-lived read-only connection
    to that board's DB and check the parent's status there. Missing
    or unreachable cross-board DBs hold the child in ``todo`` (we
    fail-closed: never promote when we can't verify the dependency).

    Returns the number of tasks promoted. Safe to call inside or
    outside an existing transaction; it opens its own IMMEDIATE txn.
    """
    promoted = 0
    # Cache cross-board reads within one tick to avoid re-opening the
    # same parent_board for every child that depends on it.
    cross_board_cache: dict[str, dict[str, str]] = {}

    def _resolve_cross_board_status(parent_board: str, parent_id: str) -> str | None:
        """Fetch ``parent_id`` status from ``parent_board``'s DB.

        Returns the status string, or None if the DB or task is
        unreachable. Uses the per-tick cache so a board is opened
        at most once per recompute pass.
        """
        cached = cross_board_cache.get(parent_board)
        if cached is None:
            try:
                parent_db = board_db_path(parent_board)
            except InvalidBoardSlugError:
                return None
            if not parent_db.exists():
                cross_board_cache[parent_board] = {}
                return None
            cached = {}
            try:
                with sqlite3.connect(str(parent_db)) as pconn:
                    pconn.row_factory = sqlite3.Row
                    rows = pconn.execute(
                        "SELECT id, status FROM tasks"
                    ).fetchall()
                    for r in rows:
                        cached[r["id"]] = r["status"]
            except sqlite3.Error:
                cross_board_cache[parent_board] = {}
                return None
            cross_board_cache[parent_board] = cached
        return cached.get(parent_id)

    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'todo'"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            # Pull all parent edges including the cross-board info.
            edges = conn.execute(
                "SELECT parent_id, parent_board FROM task_links "
                "WHERE child_id = ?",
                (task_id,),
            ).fetchall()
            all_done = True
            for edge in edges:
                pid = edge["parent_id"]
                pboard = edge["parent_board"]
                if pboard is None:
                    # Same-board parent — check local status
                    prow = conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (pid,),
                    ).fetchone()
                    status = prow["status"] if prow else None
                else:
                    status = _resolve_cross_board_status(pboard, pid)
                if status != "done":
                    all_done = False
                    break
            if all_done and edges:
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                    (task_id,),
                )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
            elif all_done and not edges:
                # No parents: orphan todo → ready (matches old behaviour).
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                    (task_id,),
                )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------

def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    claimer: str | None = None,
) -> Task | None:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + int(ttl_seconds)
    with write_txn(conn):
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    claimer: str | None = None,
) -> bool:
    """Extend a running claim.  Returns True if we still own it.

    Workers that know they'll exceed 15 minutes should call this every
    few minutes to keep ownership.
    """
    expires = int(time.time()) + int(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?",
            (expires, task_id, lock),
        )
        if cur.rowcount == 1:
            run_id = _current_run_id(conn, task_id)
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                    (expires, run_id),
                )
            return True
        return False


def release_stale_claims(conn: sqlite3.Connection) -> int:
    """Reset any ``running`` task whose claim has expired.

    Returns the number of stale claims reclaimed.  Safe to call often.
    """
    now = int(time.time())
    reclaimed = 0
    with write_txn(conn):
        stale = conn.execute(
            "SELECT id, claim_lock FROM tasks "
            "WHERE status = 'running' AND claim_expires IS NOT NULL AND claim_expires < ?",
            (now,),
        ).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running'",
                (row["id"],),
            )
            run_id = _end_run(
                conn, row["id"],
                outcome="reclaimed", status="reclaimed",
                error=f"stale_lock={row['claim_lock']}",
            )
            _append_event(
                conn, row["id"], "reclaimed",
                {"stale_lock": row["claim_lock"]},
                run_id=run_id,
            )
            reclaimed += 1
    return reclaimed


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str | None = None,
    summary: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    Accepts a task that's merely ``ready`` too, so a manual CLI
    completion (``oc kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers don't have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.
    """
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status       = 'done',
                   result       = ?,
                   completed_at = ?,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('running', 'ready', 'blocked')
            """,
            (result, now, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        _append_event(
            conn, task_id, "completed",
            {
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            },
            run_id=run_id,
        )
    # Wave 6.E.17 — if this task was delegated to us by a peer, enqueue
    # a callback so the sender can reconcile their lease + mirror the
    # terminal state on their side. Done OUTSIDE the write_txn so the
    # callback_queue's own write_txn doesn't nest. Failures here MUST
    # NOT roll back the local completion — the queue is best-effort.
    _maybe_enqueue_delegated_callback(
        conn, task_id, "done",
        summary=summary if summary is not None else result,
        result=result,
        metadata=metadata,
    )
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    return True


def _maybe_enqueue_delegated_callback(
    conn: sqlite3.Connection,
    task_id: str,
    outcome: str,
    *,
    summary: str | None = None,
    result: str | None = None,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    """If ``task_id`` is a delegated task (peer-side mirror), enqueue a
    terminal callback for the dispatcher's drainer to deliver.

    Best-effort — exceptions are swallowed since the local transition
    already committed. The queue retries on its own schedule.
    """
    try:
        from opencomputer.kanban.callback_queue import (
            enqueue_callback,
            find_delegated_task,
        )
        delegated = find_delegated_task(conn, task_id)
        if delegated is None:
            return
        sender_slug, callback_url = delegated
        payload = {
            "schema_version": 2,
            "remote_task_id": task_id,
            "outcome": outcome,
        }
        if summary is not None:
            payload["summary"] = summary
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        if metadata is not None:
            payload["metadata"] = metadata
        enqueue_callback(
            conn,
            sender_slug=sender_slug,
            callback_url=callback_url,
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        # Local transition already committed; queue is best-effort.
        # An operator inspecting kanban_pending_callbacks will see a
        # missing row vs. a dead-lettered one — different signals.
        import traceback
        traceback.print_exc()


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str | None = None,
) -> bool:
    """Transition ``running -> blocked``."""
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status       = 'blocked',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('running', 'ready')
            """,
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="blocked", status="blocked",
            summary=reason,
        )
        # Synthesize a run when blocking a never-claimed task so the
        # reason is preserved in attempt history.
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="blocked",
                summary=reason,
            )
        _append_event(conn, task_id, "blocked", {"reason": reason}, run_id=run_id)
    # Wave 6.E.17 — peer-side: blocked is a terminal state for the
    # delegated task from the sender's perspective. Enqueue a callback.
    _maybe_enqueue_delegated_callback(
        conn, task_id, "blocked", error=reason,
    )
    return True


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked -> ready``.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    now = int(time.time())
    with write_txn(conn):
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'blocked'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL "
            "WHERE id = ? AND status = 'blocked'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        _append_event(conn, task_id, "unblocked", None)
        return True


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # If archive happened while a run was still in flight (e.g. user
        # archived a running task from the dashboard), close that run with
        # outcome='reclaimed' so attempt history isn't orphaned.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
        return True


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def resolve_workspace(task: Task) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<kanban-root>/kanban/workspaces/<id>/``,
      where ``<kanban-root>`` is the shared OC root (see
      :func:`kanban_home`). The path is the same for the dispatcher and
      every profile worker, so handoff is path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a git worktree at ``workspace_path``.  Not created
      automatically in v1 -- the kanban-worker skill documents
      ``git worktree add`` as a worker-side step.  Returns the intended path.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root() / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        if not task.workspace_path:
            # Default: .worktrees/<id>/ under CWD.  Worker skill creates it.
            return Path.cwd() / ".worktrees" / task.id
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute worktree path "
                f"{task.workspace_path!r}; use an absolute path"
            )
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(path), task_id),
        )


# ---------------------------------------------------------------------------
# Dispatcher (one-shot pass)
# ---------------------------------------------------------------------------

# After this many consecutive `spawn_failed` events on a task, the dispatcher
# stops retrying and parks the task in ``blocked`` with a reason so a human
# can investigate. Prevents the dispatcher from thrashing forever on a task
# whose profile doesn't exist, whose workspace is unmountable, etc.
DEFAULT_SPAWN_FAILURE_LIMIT = 5

# Max bytes to keep in a single worker log file. The dispatcher truncates
# and rotates on spawn if the file is larger than this at spawn time.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""


def _pid_alive(pid: int | None) -> bool:
    """Return True if ``pid`` is still running on this host.

    Cross-platform: uses ``os.kill(pid, 0)`` on POSIX and ``OpenProcess``
    on Windows. Returns False for falsy PIDs or on any OS error.

    **Zombie handling (Linux):** ``os.kill(pid, 0)`` succeeds against
    zombie processes (post-exit, pre-reap) because the process table
    entry still exists. A worker that exits without being reaped by its
    parent would stay "alive" to the dispatcher forever. Dispatcher
    workers are started via ``start_new_session=True`` + intentional
    Popen handle abandonment, so init reaps them quickly — but during
    the window between exit and reap, we'd otherwise see stale "alive"
    signals. On Linux we additionally peek at ``/proc/<pid>/status``
    and treat ``State: Z`` as dead. On other POSIX or on Windows the
    zombie check is a no-op.
    """
    if not pid or pid <= 0:
        return False
    try:
        if hasattr(os, "kill"):
            os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, we just can't signal it.
        return True
    except OSError:
        return False
    # Still here → kill(0) succeeded. Check for zombie on Linux.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            # PermissionError shouldn't happen for our own children but
            # be defensive.
            pass
    return True


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: str | None = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Called by long-running workers as a liveness signal orthogonal to
    the PID check. A worker that forks a long-lived child (train loop,
    video encode, web crawl) can have its Python still alive while the
    actual work process is stuck; periodic heartbeats catch that.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? "
            "WHERE id = ? AND status = 'running'",
            (now, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        _append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    Sends SIGTERM, waits a short grace window, then SIGKILL. Emits a
    ``timed_out`` event and drops the task back to ``ready`` so the next
    dispatcher tick re-spawns it — unless the spawn-failure circuit
    breaker has already given up, in which case the task stays blocked
    where ``_record_spawn_failure`` parked it.

    Runs host-local: only tasks claimed by this host are candidates
    (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a
    test hook; defaults to ``os.kill`` on POSIX.
    """
    import signal
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"

    rows = conn.execute(
        "SELECT id, worker_pid, started_at, max_runtime_seconds, claim_lock "
        "FROM tasks "
        "WHERE status = 'running' AND max_runtime_seconds IS NOT NULL "
        "  AND started_at IS NOT NULL AND worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        elapsed = now - int(row["started_at"])
        if elapsed < int(row["max_runtime_seconds"]):
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL. Keep it simple: 5 s grace. Workers that
        # want a cleaner shutdown can install their own SIGTERM handler
        # before the grace expires.
        killed = False
        kill = signal_fn if signal_fn is not None else (
            os.kill if hasattr(os, "kill") else None
        )
        if kill is not None:
            try:
                kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            # Short polling wait — no time.sleep on the write txn.
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                try:
                    kill(pid, signal.SIGKILL)
                    killed = True
                except (ProcessLookupError, OSError):
                    pass

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (tid,),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                }
                run_id = _end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                _append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
                )
                timed_out.append(tid)
    return timed_out


def set_max_runtime(
    conn: sqlite3.Connection,
    task_id: str,
    seconds: int | None,
) -> bool:
    """Set or clear the per-task max_runtime_seconds. Returns True on
    success."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?",
            (int(seconds) if seconds is not None else None, task_id),
        )
    return cur.rowcount == 1


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and drops the task back to ``ready``.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).
    """
    crashed: list[str] = []
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            if _pid_alive(row["worker_pid"]):
                continue
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running'",
                (row["id"],),
            )
            if cur.rowcount == 1:
                run_id = _end_run(
                    conn, row["id"],
                    outcome="crashed", status="crashed",
                    error=f"pid {int(row['worker_pid'])} not alive",
                    metadata={
                        "pid": int(row["worker_pid"]),
                        "claimer": row["claim_lock"],
                    },
                )
                _append_event(
                    conn, row["id"], "crashed",
                    {"pid": int(row["worker_pid"]), "claimer": row["claim_lock"]},
                    run_id=run_id,
                )
                crashed.append(row["id"])
    return crashed


def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
) -> bool:
    """Release the claim, increment the failure counter, maybe auto-block.

    Returns True when the task was auto-blocked (N failures exceeded),
    False when it was just released back to ``ready`` for another try.
    """
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT spawn_failures FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        failures = int(row["spawn_failures"]) + 1 if row else 1
        if failures >= failure_limit:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "spawn_failures = ?, last_spawn_error = ? "
                "WHERE id = ? AND status IN ('running', 'ready')",
                (failures, error[:500], task_id),
            )
            run_id = _end_run(
                conn, task_id,
                outcome="gave_up", status="gave_up",
                error=error[:500],
                metadata={"failures": failures},
            )
            _append_event(
                conn, task_id, "gave_up",
                {"failures": failures, "error": error[:500]},
                run_id=run_id,
            )
            blocked = True
        else:
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "spawn_failures = ?, last_spawn_error = ? "
                "WHERE id = ? AND status = 'running'",
                (failures, error[:500], task_id),
            )
            run_id = _end_run(
                conn, task_id,
                outcome="spawn_failed", status="spawn_failed",
                error=error[:500],
                metadata={"failures": failures},
            )
            _append_event(
                conn, task_id, "spawn_failed",
                {"error": error[:500], "failures": failures},
                run_id=run_id,
            )
    # Wave 6.E.17 — peer-side: an auto-block on a delegated task is a
    # terminal failure from the sender's perspective. Enqueue a
    # "failed" callback so the sender can mirror the state. Outside
    # the write_txn so the callback_queue's own txn doesn't nest.
    if blocked:
        _maybe_enqueue_delegated_callback(
            conn, task_id, "failed", error=error[:500],
            metadata={"failures": int(failures), "kind": "spawn_auto_blocked"},
        )
    return blocked


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + emit a ``spawned`` event.

    The event's payload carries the pid so a human reading ``oc kanban
    tail`` can correlate log lines with OS-level traces without opening
    the drawer.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (int(pid), task_id),
        )
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (int(pid), run_id),
            )
        _append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)


def _clear_spawn_failures(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the failure counter after a successful spawn."""
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET spawn_failures = 0, last_spawn_error = NULL "
            "WHERE id = ?",
            (task_id,),
        )


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    dry_run: bool = False,
    max_spawn: int | None = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path) -> Optional[int]``. The return
         value (if any) is recorded as ``worker_pid`` so subsequent ticks
         can detect crashes before the TTL expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    """
    result = DispatchResult()
    result.reclaimed = release_stale_claims(conn)
    result.crashed = detect_crashed_workers(conn)
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = recompute_ready(conn)

    ready_rows = conn.execute(
        "SELECT id, assignee, title, tenant FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    spawned = 0
    for row in ready_rows:
        if max_spawn is not None and spawned >= max_spawn:
            break
        assignee = row["assignee"]
        if not assignee:
            # Wave 6.E.9 — auto-assignment routing rules (Hermes
            # 'out of scope' item closed). Only consult rules when
            # the task has no explicit assignee. Stays inside the
            # outer dispatch transaction so two simultaneous
            # dispatchers can't double-assign.
            assignee = resolve_assignee(
                conn, title=row["title"] or "", tenant=row["tenant"],
            )
            if assignee:
                with write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET assignee = ? WHERE id = ?",
                        (assignee, row["id"]),
                    )
            else:
                result.skipped_unassigned.append(row["id"])
                continue
        if dry_run:
            # Use the locally-resolved ``assignee`` so dry-run preview
            # reflects auto-assigned values, not stale row data.
            result.spawned.append((row["id"], assignee, ""))
            continue
        claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            continue
        try:
            workspace = resolve_workspace(claimed)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            _clear_spawn_failures(conn, claimed.id)
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
    return result


def _rotate_worker_log(log_path: Path, max_bytes: int) -> None:
    """Rotate ``<log>`` to ``<log>.1`` if it exceeds ``max_bytes``.

    Single-generation rotation — one old file kept, newer one replaces it.
    Keeps disk usage bounded while still giving the user a chance to grab
    the prior run's output.
    """
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size <= max_bytes:
            return
        rotated = log_path.with_suffix(log_path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
        except OSError:
            pass
        log_path.rename(rotated)
    except OSError:
        pass


def _resolve_oc_executable() -> list[str]:
    """Return the argv prefix for spawning a kanban worker.

    Three-tier resolution so the dispatcher works regardless of how the
    parent was launched (interactive shell, systemd, launchd, Docker,
    `pip install --user`, venv, ...):

    1. ``shutil.which("oc")`` — fastest path; honours the dispatcher's
       inherited ``$PATH``. Works for the common `pip install --user`
       case where the user's shell adds ``~/.local/bin`` to PATH.
    2. ``Path(sys.executable).parent / "oc"`` — covers venv installs
       where the CLI script sits next to the interpreter even if the
       venv's ``bin/`` is not on the dispatcher's ``$PATH``. This is
       the most common cause of the "`oc` not on PATH" spawn failure:
       the dispatcher inherits a daemon-launch ``$PATH`` (launchd /
       systemd) that doesn't include the venv.
    3. ``[sys.executable, "-m", "opencomputer"]`` — bulletproof
       bootstrap when neither script is reachable. The ``opencomputer``
       package always exposes ``__main__.py``-style module dispatch
       via ``opencomputer.cli:main``, so spawning the same Python
       interpreter as the parent dispatcher is guaranteed to work.

    Returns a list (rather than a single string) so callers can splat
    it into argv: ``cmd = [*_resolve_oc_executable(), "-p", profile, ...]``.
    """
    import shutil
    import sys

    # 1. PATH lookup
    found = shutil.which("oc")
    if found:
        return [found]

    # 2. Sibling of sys.executable (venv layout)
    sibling = Path(sys.executable).parent / "oc"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]

    # 3. Module-form bootstrap — guaranteed to work because
    # ``opencomputer`` always declares the cli entry point.
    return [sys.executable, "-m", "opencomputer"]


def _default_spawn(task: Task, workspace: str) -> int | None:
    """Fire-and-forget ``oc -p <profile> chat -q ...`` subprocess.

    Returns the spawned child's PID so the dispatcher can detect crashes
    before the claim TTL expires. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    Wave 6.E.17 — when ``task.assignee`` looks like ``"<slug>/<profile>"``
    and that slug is registered in ``kanban_remote_hosts``, delegate to
    the peer via :func:`opencomputer.kanban.remote_dispatch.delegate_task_to_remote`
    instead of forking a local subprocess. Returns ``None`` for the PID
    because the work runs on the peer; lease liveness is tracked via
    ``kanban_remote_claims`` rows that the heartbeat tick refreshes.
    """
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    # Wave 6.E.17 — short-circuit to remote delegation when the assignee
    # encodes a peer slug. Lazy import to keep the module-load cost down
    # for single-host installs (httpx + remote_hosts only loaded when a
    # task actually targets a peer).
    from opencomputer.kanban import remote_dispatch as _rd
    parsed = _rd.parse_remote_assignee(task.assignee)
    if parsed is not None:
        slug, profile = parsed
        with contextlib.closing(connect()) as _rconn:
            from opencomputer.kanban.remote_hosts import find_remote_host
            host = find_remote_host(_rconn, slug)
            if host is None:
                # The assignee names a peer we don't know about. Raise
                # so the existing spawn-failure counter records it; after
                # ``failure_limit`` consecutive failures the task gets
                # auto-blocked rather than thrashing forever.
                raise ValueError(
                    f"unknown peer slug {slug!r} (assignee={task.assignee!r}); "
                    f"register via `oc kanban remote add {slug} <url>`"
                )
            callback_url = os.environ.get("OC_KANBAN_LOCAL_CALLBACK_URL", "").strip()
            if not callback_url:
                raise ValueError(
                    "OC_KANBAN_LOCAL_CALLBACK_URL is not set; cannot delegate "
                    f"task {task.id} to peer {slug!r}. Export the env var "
                    "(e.g. http://<our-host>:9119/api/plugins/kanban/proxy/callback) "
                    "before starting the dispatcher."
                )
            _rd.delegate_task_to_remote(
                _rconn, task=task, host=host, profile=profile,
                local_callback_url=callback_url,
            )
        # No local PID — the worker is on the peer. Lease liveness is
        # tracked by the heartbeat tick + the kanban_remote_claims row.
        return None

    prompt = f"work kanban task {task.id}"
    env = dict(os.environ)
    if task.tenant:
        env["OC_TENANT"] = task.tenant
    env["OC_KANBAN_TASK"] = task.id
    env["OC_KANBAN_WORKSPACE"] = workspace
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`oc -p <name>`
    # rewrites OC_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `_oc_home()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["OC_KANBAN_DB"] = str(kanban_db_path())
    env["OC_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root())
    # Wave 6.E.8 — propagate active-board context to the worker so
    # the kanban_* tools resolve to the same board the dispatcher
    # used to claim this task. Without this, a worker subprocess
    # would re-resolve via the active-board state file (which the
    # user may have switched between dispatch + spawn).
    _active = active_board()
    if _active:
        env["OC_KANBAN_BOARD"] = _active
    # OC_PROFILE is the author the kanban_comment tool defaults to.
    # `oc -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["OC_PROFILE"] = task.assignee

    cmd = [
        *_resolve_oc_executable(),
        "-p", task.assignee,
        # Auto-load the kanban-worker skill so every dispatched worker
        # has the pattern library (good summary/metadata shapes, retry
        # diagnostics, block-reason examples) in its context, even if
        # the profile hasn't wired it into skills config. The MANDATORY
        # lifecycle is already in the system prompt via KANBAN_GUIDANCE;
        # this skill is the deeper reference. Users can point a profile
        # at a different/additional skill via config if they want —
        # --skills is additive to the profile's default skill set.
        "--skills", "kanban-worker",
    ]
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    # Dedupe against the built-in so we don't double-load kanban-worker
    # if a task author asks for it explicitly.
    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                cmd.extend(["--skills", sk])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    # Redirect output to a per-task log under <kanban-root>/kanban/logs/.
    # Anchored at the shared kanban root, not the worker's profile home,
    # so `oc kanban tail` reads the same file the worker writes to.
    log_dir = kanban_home() / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    _rotate_worker_log(log_path, DEFAULT_LOG_ROTATE_BYTES)

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")  # noqa: SIM115 — fd lifetime owned by Popen, not this fn
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`oc` executable not found on PATH. "
            "Install OC Agent or activate its venv before running the kanban dispatcher."
        )
    # NOTE: we intentionally do NOT close log_f here — we want Popen's
    # child process to keep writing after this function returns.  The
    # handle is kept alive by the child's inheritance.  The parent's
    # reference goes out of scope and is GC'd, but the OS-level FD stays
    # open in the child until the child exits.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: int | None = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``oc kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.
    """
    import signal
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only when running on the main thread — tests call
    # this inline from worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (ValueError, OSError):
                    pass

    while not stop_event.is_set():
        try:
            with contextlib.closing(connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass
            # Wave 6.E.17 — refresh leases on pending remote claims.
            # Same logic as gateway/kanban_dispatcher.py::_tick_heartbeats
            # but inline here for the standalone `oc kanban daemon` path.
            try:
                _heartbeat_pending_remote_claims()
            except Exception:
                import traceback
                traceback.print_exc()
            # Wave 6.E.17 — drain outbound callback queue (peer side).
            try:
                _drain_pending_callbacks()
            except Exception:
                import traceback
                traceback.print_exc()
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


def _heartbeat_pending_remote_claims() -> None:
    """Refresh leases on pending ``kanban_remote_claims`` near expiry.

    Module-level helper shared by ``run_daemon`` (this file) and the
    gateway's ``KanbanDispatcherLoop``. Suppresses per-claim error spam
    by tracking which slugs have already failed in this pass.
    """
    from opencomputer.kanban import remote_dispatch as _rd
    from opencomputer.kanban.remote_hosts import find_remote_host

    with contextlib.closing(connect()) as conn:
        pending = _rd.list_pending_remote_claims(conn)
        if not pending:
            return
        now = int(time.time())
        lead = _rd.HEARTBEAT_LEAD_SECONDS
        slug_failed: set[str] = set()
        for claim in pending:
            if claim.lease_until - now > lead:
                continue
            if claim.remote_slug in slug_failed:
                continue
            host = find_remote_host(conn, claim.remote_slug)
            if host is None:
                slug_failed.add(claim.remote_slug)
                continue
            try:
                _rd.heartbeat_remote_claim(conn, claim=claim, host=host)
            except _rd.RemoteDispatchError:
                slug_failed.add(claim.remote_slug)


def _drain_pending_callbacks() -> None:
    """Peer-side: deliver due callbacks from ``kanban_pending_callbacks``.

    Helper for the standalone ``oc kanban daemon`` path. The gateway
    loop has its own ``_tick_callback_drainer`` with logging; this is
    the silent equivalent.
    """
    from urllib.parse import urlparse

    import httpx

    from opencomputer.kanban import callback_queue as _cq
    from opencomputer.kanban.remote_hosts import find_remote_host, signed_headers

    with contextlib.closing(connect()) as conn:
        due = _cq.next_due(conn, now=int(time.time()))
        if not due:
            return
        slug_failed: set[str] = set()
        for cb in due:
            if cb.sender_slug in slug_failed:
                continue
            host = find_remote_host(conn, cb.sender_slug)
            if host is None:
                slug_failed.add(cb.sender_slug)
                _cq.mark_attempted(
                    conn, cb.id,
                    error=f"sender slug {cb.sender_slug!r} no longer registered",
                    max_attempts=1,
                )
                continue
            body = cb.payload_json.encode("utf-8")
            sig_path = urlparse(cb.callback_url).path or "/"
            headers = signed_headers(
                secret=host.hmac_secret,
                method="POST",
                path=sig_path,
                body=body,
                extra={"Content-Type": "application/json"},
            )
            sep = "&" if "?" in cb.callback_url else "?"
            target = f"{cb.callback_url}{sep}slug={host.slug}"
            try:
                resp = httpx.post(target, content=body, headers=headers, timeout=10.0)
            except httpx.RequestError as exc:
                slug_failed.add(cb.sender_slug)
                _cq.mark_attempted(conn, cb.id, error=f"network: {exc}")
                continue
            if 200 <= resp.status_code < 300:
                _cq.mark_delivered(conn, cb.id)
            else:
                slug_failed.add(cb.sender_slug)
                _cq.mark_attempted(
                    conn, cb.id,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------

def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Return the full text a worker should read to understand its task.

    Order:
      1. Task title (mandatory).
      2. Task body (optional opening post, capped at 8 KB).
      3. Prior attempts on THIS task (most recent ``_CTX_MAX_PRIOR_ATTEMPTS``
         shown; older attempts collapsed into a one-line summary).
         Each attempt's ``summary`` / ``error`` / ``metadata`` capped at
         ``_CTX_MAX_FIELD_BYTES`` each.
      4. Structured handoff results of every done parent task. Prefers
         ``run.summary`` / ``run.metadata`` when the parent was executed
         via a run; falls back to ``task.result`` for older data. Same
         per-field cap.
      5. Cross-task role history for the assignee (most recent 5
         completed runs on other tasks).
      6. Comment thread (most recent ``_CTX_MAX_COMMENTS`` shown, older
         collapsed).

    All caps exist so worker prompts stay bounded even on pathological
    boards (retry-heavy tasks, comment storms). The per-field char cap
    prevents a single 1 MB summary from dominating context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    def _cap(s: str | None, limit: int = _CTX_MAX_FIELD_BYTES) -> str:
        """Truncate a string to `limit` chars with a visible ellipsis."""
        if not s:
            return ""
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

    lines: list[str] = []
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    lines.append("")

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # Prior attempts — show closed runs so a retrying worker sees the
    # history. Skip the currently-active run (that's this worker).
    # Cap at _CTX_MAX_PRIOR_ATTEMPTS most-recent closed runs; older
    # attempts get collapsed into a one-line marker so the worker knows
    # more exist without bloating the prompt.
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    # list_runs returns ascending by started_at; "most recent" = last N
    if len(all_prior) > _CTX_MAX_PRIOR_ATTEMPTS:
        omitted = len(all_prior) - _CTX_MAX_PRIOR_ATTEMPTS
        shown = all_prior[-_CTX_MAX_PRIOR_ATTEMPTS:]
        first_shown_idx = omitted + 1
    else:
        omitted = 0
        shown = all_prior
        first_shown_idx = 1
    if shown:
        lines.append("## Prior attempts on this task")
        if omitted:
            lines.append(
                f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} "
                f"omitted; showing most recent {len(shown)})_"
            )
        for offset, run in enumerate(shown):
            idx = first_shown_idx + offset
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
            profile = run.profile or "(unknown)"
            outcome = run.outcome or run.status
            lines.append(f"### Attempt {idx} — {outcome} ({profile}, {ts})")
            if run.summary and run.summary.strip():
                lines.append(_cap(run.summary))
            if run.error and run.error.strip():
                lines.append(f"_error_: {_cap(run.error)}")
            if run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.append("")

    # Parents: prefer the most-recent 'completed' run's summary + metadata,
    # fall back to ``task.result`` when no run rows exist (legacy DBs,
    # or tasks completed before the runs table landed).
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    parent_ids = [r["parent_id"] for r in parent_rows]

    if parent_ids:
        wrote_header = False
        for pid in parent_ids:
            pt = get_task(conn, pid)
            if not pt or pt.status != "done":
                continue
            runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
            runs.sort(key=lambda r: r.started_at, reverse=True)
            run = runs[0] if runs else None

            if not wrote_header:
                lines.append("## Parent task results")
                wrote_header = True
            lines.append(f"### {pid}")

            body_lines: list[str] = []
            if run is not None and run.summary and run.summary.strip():
                body_lines.append(_cap(run.summary))
            elif pt.result:
                body_lines.append(_cap(pt.result))
            else:
                body_lines.append("(no result recorded)")

            if run is not None and run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.extend(body_lines)
            lines.append("")

    # Cross-task role history: what else has THIS assignee completed
    # recently? Gives the worker implicit continuity — "I'm the reviewer
    # and my last three reviews focused on security" — without forcing
    # the user to wire anything into SOUL.md / MEMORY.md. Bounded to the
    # most recent 5 completed runs, excluding this task so the retry
    # section above isn't duplicated. Safe on assignee=None (skipped).
    if task.assignee:
        role_rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? "
            "  AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (task.assignee, task_id),
        ).fetchall()
        if role_rows:
            lines.append(f"## Recent work by @{task.assignee}")
            for row in role_rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"]))
                )
                s = (row["summary"] or "").strip().splitlines()
                first = s[0][:200] if s else "(no summary)"
                lines.append(f"- {row['id']} — {row['title']} ({ts}): {first}")
            lines.append("")

    # Comments: cap at the most-recent _CTX_MAX_COMMENTS so
    # comment-storm tasks don't blow out the worker's prompt. Older
    # comments summarised in a one-line marker like prior attempts.
    all_comments = list_comments(conn, task_id)
    if len(all_comments) > _CTX_MAX_COMMENTS:
        omitted_c = len(all_comments) - _CTX_MAX_COMMENTS
        shown_c = all_comments[-_CTX_MAX_COMMENTS:]
    else:
        omitted_c = 0
        shown_c = all_comments
    if shown_c:
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(shown_c)})_"
            )
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            lines.append(f"**{c.author}** ({ts}):")
            lines.append(_cap(c.body, _CTX_MAX_COMMENT_BYTES))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        by_assignee.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    age_since_created = now - int(task.created_at) if task.created_at else None
    age_since_started = (
        now - int(task.started_at) if task.started_at else None
    )
    time_to_complete = (
        int(task.completed_at) - int(task.started_at or task.created_at)
        if task.completed_at else None
    )
    return {
        "created_age_seconds": age_since_created,
        "started_age_seconds": age_since_started,
        "time_to_complete_seconds": time_to_complete,
    }


# ---------------------------------------------------------------------------
# Notification subscriptions (used by the gateway kanban-notifier)
# ---------------------------------------------------------------------------

def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread)."""
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, platform, chat_id, thread_id or "", user_id, now),
        )


def list_notify_subs(
    conn: sqlite3.Connection, task_id: str | None = None,
) -> list[dict]:
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kanban_notify_subs").fetchall()
    return [dict(r) for r in rows]


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Wave 6.E.9 — Auto-assignment routing (Hermes 'out of scope' item)
# ---------------------------------------------------------------------------

VALID_RULE_KINDS = ("title_regex", "tenant", "default")


class InvalidRuleError(ValueError):
    """Raised when an assignment-rule definition is malformed."""


def add_assignment_rule(
    conn: sqlite3.Connection,
    *,
    pattern_kind: str,
    pattern: str,
    assignee: str,
    priority: int = 0,
) -> int:
    """Insert one rule. Returns the new row id.

    Validates ``pattern_kind`` against :data:`VALID_RULE_KINDS` and,
    for ``title_regex``, that the pattern compiles. Catastrophic-
    backtracking protection (audit lens A3): we don't run the pattern
    against arbitrary input here, but we do reject obviously malformed
    regexes at insert time so users see the error early.
    """
    if pattern_kind not in VALID_RULE_KINDS:
        raise InvalidRuleError(
            f"pattern_kind must be one of {VALID_RULE_KINDS}, got {pattern_kind!r}"
        )
    if pattern_kind == "title_regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise InvalidRuleError(
                f"title_regex pattern {pattern!r} does not compile: {exc}"
            ) from exc
    if not assignee or not isinstance(assignee, str):
        raise InvalidRuleError("assignee must be a non-empty string")
    if not isinstance(priority, int):
        raise InvalidRuleError("priority must be an integer")
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO kanban_assignment_rules "
            "(pattern_kind, pattern, assignee, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pattern_kind, pattern, assignee, priority, now),
        )
    return int(cur.lastrowid)


def list_assignment_rules(conn: sqlite3.Connection) -> list[dict]:
    """Return all rules ordered by priority DESC, id ASC."""
    rows = conn.execute(
        "SELECT * FROM kanban_assignment_rules "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_assignment_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Delete one rule. Returns True if it existed."""
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_assignment_rules WHERE id = ?",
            (int(rule_id),),
        )
    return cur.rowcount > 0


def resolve_assignee(
    conn: sqlite3.Connection,
    *,
    title: str,
    tenant: str | None,
) -> str | None:
    """Walk the rules table for a match. Returns the assignee or None.

    Audit lens A3 mitigation: regex rules are run with a try/except so
    a single rule's catastrophic backtracking can't poison the whole
    dispatcher. A failed regex is logged + skipped.

    Audit lens A9: the dispatcher MUST call this inside its claim
    transaction so two simultaneous dispatchers can't double-assign.
    """
    rows = conn.execute(
        "SELECT pattern_kind, pattern, assignee FROM kanban_assignment_rules "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()
    for r in rows:
        kind = r["pattern_kind"]
        pat = r["pattern"]
        assignee = r["assignee"]
        try:
            if kind == "default":
                return assignee
            if kind == "tenant":
                if tenant is not None and tenant == pat:
                    return assignee
            elif kind == "title_regex":
                if re.search(pat, title or ""):
                    return assignee
        except re.error:
            # Bad regex — skip + continue. Already validated at add
            # time, so this is defensive.
            continue
    return None


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    kinds: Iterable[str] | None = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a given subscription.

    Only events with ``id > last_event_id`` are returned. The subscription's
    cursor is NOT advanced here; call :func:`advance_notify_cursor` after
    the gateway has successfully delivered the notifications.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (task_id, platform, chat_id, thread_id or ""),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    new_cursor: int,
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------

def gc_events(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete task_events rows older than ``older_than_seconds`` for tasks
    in a terminal state (``done`` or ``archived``). Returns the number of
    rows deleted. Running / ready / blocked tasks keep their full event
    history."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(
    *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete worker log files older than ``older_than_seconds``. Returns
    the number of files removed. Kept separate from ``gc_events`` because
    log files live on disk, not in SQLite."""
    log_dir = kanban_home() / "kanban" / "logs"
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------

def worker_log_path(task_id: str) -> Path:
    """Return the path to a worker's log file. The file may not exist
    (task never spawned, or log already GC'd)."""
    return kanban_home() / "kanban" / "logs" / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: int | None = None,
) -> str | None:
    """Read the worker log for ``task_id``. Returns None if the file
    doesn't exist. If ``tail_bytes`` is set, only the last N bytes are
    returned (useful for the dashboard drawer which shouldn't page megabytes)."""
    path = worker_log_path(task_id)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip a partial line if we tailed mid-line. But if the
                # window has no newline at all (one giant log line),
                # readline() would eat everything — in that case don't
                # skip and return the raw tail.
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------

def list_profiles_on_disk() -> list[str]:
    """Return the set of named profiles discovered on disk.

    Reads ``~/.opencomputer/profiles/`` directly so this module has no import
    dependency on ``oc_cli.profiles`` (which pulls in a large chunk
    of the CLI startup path). Only returns directories that contain a
    ``config.yaml`` — a bare dir without config isn't a real profile.
    """
    try:
        home = Path.home() / ".oc" / "profiles"
    except Exception:
        return []
    if not home.is_dir():
        return []
    names: list[str] = []
    try:
        for entry in sorted(home.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "config.yaml").is_file():
                names.append(entry.name)
    except OSError:
        return names
    return names


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """Return every assignee name known to the board or on disk.

    Each entry is ``{"name": str, "on_disk": bool, "counts": {status: n}}``.
    A name is included when it's a configured profile on disk OR when
    any non-archived task has it as the assignee. Used by:

    - ``oc kanban assignees`` for the terminal.
    - The dashboard assignee dropdown (so a fresh profile appears in
      the picker even before it's been given any task).
    - Router-profile heuristics ("who's overloaded?") without scanning
      the whole board.
    """
    on_disk = set(list_profiles_on_disk())

    # Count tasks per (assignee, status), excluding archived.
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    names = sorted(on_disk | set(counts.keys()))
    return [
        {
            "name": name,
            "on_disk": name in on_disk,
            "counts": counts.get(name, {}),
        }
        for name in names
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------

def list_runs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_active: bool = True,
) -> list[Run]:
    """Return all runs for ``task_id`` in start order.

    ``include_active=True`` (default) includes the currently-running
    attempt if any. Set False to return only closed runs (useful for
    "how many prior attempts have there been?" checks).
    """
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Run | None:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    return Run.from_row(row) if row else None


def active_run(conn: sqlite3.Connection, task_id: str) -> Run | None:
    """Return the currently-open run for ``task_id`` (``ended_at IS NULL``)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Run | None:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None
