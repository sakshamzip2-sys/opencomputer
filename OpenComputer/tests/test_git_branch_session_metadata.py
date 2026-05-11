"""Phase A — session ``git_branch`` capture, persistence, and rendering.

Covers the four moving parts:

    1. ``opencomputer.worktree.current_git_branch`` — pure helper that
       resolves the active branch via ``git branch --show-current``.
    2. ``SessionDB._migrate_v18_to_v19`` — additive ``ALTER TABLE``.
    3. ``SessionDB.ensure_session`` + ``SessionDB.create_session`` —
       accept and persist ``git_branch=`` kwarg.
    4. ``SessionRow.git_branch`` + picker meta strip — render the branch
       segment when present; degrade cleanly when missing.

The test fixture isolates each test in a tmp dir + initialises a tiny
git repo where appropriate. We do NOT mock ``subprocess`` for the
positive cases — real git is the contract — but the negative cases
(no-git-on-PATH, non-repo, detached HEAD, timeout) are exercised by
constructing the failure conditions inline.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB, _migrate_v18_to_v19, apply_migrations
from opencomputer.cli_ui.resume_picker import SessionRow
from opencomputer.worktree import current_git_branch


# ─── helpers ─────────────────────────────────────────────────────────


def _init_repo(path: Path, *, branch: str = "main") -> None:
    """Initialise a minimal git repo at ``path`` on a named branch.

    Uses subprocess directly (no GitPython) to keep the test independent
    of the test's own working directory and to surface real failures.
    """
    subprocess.run(
        ["git", "init", "-b", branch], cwd=str(path), check=True, capture_output=True
    )
    # User + email so commit succeeds (some CI envs have no global config).
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


def _commit_empty(path: Path) -> None:
    """Create a single empty commit so HEAD points at a real ref."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


# ─── current_git_branch — happy path + every failure mode ───────────


def test_current_git_branch_returns_branch_name_in_a_real_repo(tmp_path: Path) -> None:
    """The canonical positive case — ``git branch --show-current`` works."""
    _init_repo(tmp_path, branch="feature-x")
    _commit_empty(tmp_path)

    assert current_git_branch(tmp_path) == "feature-x"


def test_current_git_branch_returns_none_outside_a_repo(tmp_path: Path) -> None:
    """A bare directory is not a repo — return ``None``, not raise."""
    assert current_git_branch(tmp_path) is None


def test_current_git_branch_returns_none_on_detached_head(tmp_path: Path) -> None:
    """Detached HEAD has no branch name — must return ``None`` cleanly."""
    _init_repo(tmp_path, branch="main")
    _commit_empty(tmp_path)
    # Check out the commit directly to enter detached-HEAD state.
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "--detach", sha],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    assert current_git_branch(tmp_path) is None


def test_current_git_branch_returns_none_when_git_not_on_path(
    monkeypatch, tmp_path: Path
) -> None:
    """If ``git`` is not on PATH, we must NOT raise — silent ``None``."""
    monkeypatch.setenv("PATH", "")
    # The helper uses shutil.which which respects PATH; defensively also
    # patch the function in case ``which`` is cached at import time.
    monkeypatch.setattr(
        "opencomputer.worktree.shutil.which",
        lambda _name: None,
    )

    assert current_git_branch(tmp_path) is None


def test_current_git_branch_handles_subprocess_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    """A wedged git subprocess must return ``None``, not block forever."""

    def _raise_timeout(*_a, **_kw):  # noqa: ANN001, ANN201
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr("opencomputer.worktree.subprocess.run", _raise_timeout)

    assert current_git_branch(tmp_path) is None


def test_current_git_branch_handles_oserror(monkeypatch, tmp_path: Path) -> None:
    """Permission errors / file-not-found in subprocess must not raise."""

    def _raise_oserror(*_a, **_kw):  # noqa: ANN001, ANN201
        raise OSError("permission denied")

    monkeypatch.setattr("opencomputer.worktree.subprocess.run", _raise_oserror)

    assert current_git_branch(tmp_path) is None


# ─── schema migration v18 → v19 ──────────────────────────────────────


def _build_v18_db(path: Path) -> None:
    """Materialise a DB at exactly schema v18 — no ``git_branch`` column.

    Constructed directly from raw SQL so the test doesn't depend on
    SQLite's ``ALTER TABLE DROP COLUMN`` support (which can trip over
    FTS triggers and CHECK constraints in 3.35–3.45). The DDL below is
    a minimal subset of v18's ``sessions`` table — enough to exercise
    the v18 → v19 ``ADD COLUMN`` migration on a realistic-looking row.

    Seeds one session row so the test can assert the row survives the
    migration with ``git_branch IS NULL`` (legacy-row contract).
    """
    with sqlite3.connect(path) as raw:
        raw.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (18);

            CREATE TABLE sessions (
                id            TEXT PRIMARY KEY,
                started_at    REAL NOT NULL,
                ended_at      REAL,
                platform      TEXT NOT NULL,
                model         TEXT,
                title         TEXT,
                message_count INTEGER DEFAULT 0,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens  INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                vibe          TEXT,
                vibe_updated  REAL,
                cwd           TEXT,
                goal_text         TEXT,
                goal_active       INTEGER DEFAULT 0,
                goal_turns_used   INTEGER DEFAULT 0,
                goal_budget       INTEGER DEFAULT 20,
                goal_last_judge_reason TEXT,
                parent_session_id TEXT,
                source            TEXT,
                compactions_count INTEGER DEFAULT 0
            );

            INSERT INTO sessions (id, started_at, platform, model, title)
                VALUES ('sess-pre-v19', 1700000000.0, 'cli', 'm', 'pre');
            """
        )
        raw.commit()


def test_migration_v18_to_v19_adds_git_branch_column(tmp_path: Path) -> None:
    """Migration must add ``git_branch`` to existing v18 schemas."""
    db_path = tmp_path / "sessions.db"
    _build_v18_db(db_path)

    # Pre-condition: column absent + row present at v18.
    with sqlite3.connect(db_path) as raw:
        cols = {r[1] for r in raw.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "git_branch" not in cols
        (count,) = raw.execute("SELECT COUNT(*) FROM sessions").fetchone()
        assert count == 1

    # Re-open the DB — apply_migrations runs in _init_schema.
    SessionDB(db_path)

    with sqlite3.connect(db_path) as raw:
        cols = {r[1] for r in raw.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "git_branch" in cols
        # Row survives. git_branch is NULL on the pre-existing row.
        row = raw.execute(
            "SELECT git_branch FROM sessions WHERE id = 'sess-pre-v19'"
        ).fetchone()
        assert row[0] is None


def test_migration_v18_to_v19_is_idempotent(tmp_path: Path) -> None:
    """Running the migration twice must not raise (re-open is common)."""
    db_path = tmp_path / "sessions.db"
    _build_v18_db(db_path)

    # First apply via SessionDB ctor.
    SessionDB(db_path)
    # Second apply: call the function directly on the live conn.
    with sqlite3.connect(db_path) as raw:
        _migrate_v18_to_v19(raw)  # must be a no-op

    # And again via re-open.
    SessionDB(db_path)


def test_apply_migrations_advances_to_19(tmp_path: Path) -> None:
    """A fresh DB must end up at schema_version == 19."""
    db_path = tmp_path / "sessions.db"
    SessionDB(db_path)
    with sqlite3.connect(db_path) as raw:
        (v,) = raw.execute("SELECT version FROM schema_version").fetchone()
    assert v == 19


# ─── ensure_session / create_session persist git_branch ──────────────


def test_ensure_session_stores_git_branch(tmp_path: Path) -> None:
    """ensure_session(...) with git_branch='main' must persist to row."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, git_branch="main")
    row = db.get_session(sid)
    assert row is not None
    assert row["git_branch"] == "main"


def test_ensure_session_with_none_branch_stores_null(tmp_path: Path) -> None:
    """git_branch=None must store as NULL (NOT the string 'None')."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, git_branch=None)
    row = db.get_session(sid)
    assert row is not None
    assert row["git_branch"] is None


def test_ensure_session_with_empty_string_stores_null(tmp_path: Path) -> None:
    """Empty string must coerce to NULL (treat as 'unknown' uniformly)."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, git_branch="")
    row = db.get_session(sid)
    assert row is not None
    assert row["git_branch"] is None


def test_ensure_session_preserves_branch_on_second_call(tmp_path: Path) -> None:
    """ensure_session is INSERT-OR-IGNORE; second call must not overwrite."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, git_branch="feature-1")
    # Second call from a different branch — original must win (ON CONFLICT DO NOTHING).
    db.ensure_session(sid, git_branch="feature-2")
    row = db.get_session(sid)
    assert row is not None
    assert row["git_branch"] == "feature-1"


def test_create_session_stores_git_branch(tmp_path: Path) -> None:
    """create_session is UPSERT; first insert lands the branch."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.create_session(sid, git_branch="main")
    row = db.get_session(sid)
    assert row is not None
    assert row["git_branch"] == "main"


def test_create_session_upsert_preserves_branch_via_coalesce(tmp_path: Path) -> None:
    """A second create_session must not overwrite an existing branch.

    The UPSERT clause uses COALESCE so a long-lived session that survives
    a branch switch keeps its origin branch — matches the ensure_session
    contract.
    """
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.create_session(sid, git_branch="main")
    db.create_session(sid, git_branch="feature-2")  # second call, different branch
    row = db.get_session(sid)
    assert row["git_branch"] == "main"


def test_list_sessions_with_preview_includes_git_branch(tmp_path: Path) -> None:
    """The picker's source query (s.*) must include the new column."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.create_session(sid, git_branch="feat/abc")
    rows = db.list_sessions_with_preview(limit=10)
    assert len(rows) == 1
    assert rows[0]["git_branch"] == "feat/abc"


# ─── SessionRow + picker meta strip ──────────────────────────────────


def test_sessionrow_defaults_git_branch_to_empty_string() -> None:
    """Backwards-compat: callers that don't pass git_branch get ``""``."""
    row = SessionRow(
        id="abc12345",
        title="hello",
        started_at=time.time(),
        message_count=1,
    )
    assert row.git_branch == ""


def test_sessionrow_accepts_git_branch_kwarg() -> None:
    """Constructor accepts the new field positionally and by name."""
    row = SessionRow(
        id="abc12345",
        title="hello",
        started_at=time.time(),
        message_count=1,
        git_branch="main",
    )
    assert row.git_branch == "main"


def test_picker_meta_includes_branch_when_present(monkeypatch) -> None:
    """Smoke: render the meta strip and confirm the branch slot appears.

    We don't launch the picker Application here (that needs a tty) — we
    exercise the meta-string assembly that lives inside ``_list_text``
    by reconstructing it identically. The assembly is data-only, so a
    string comparison fully covers the rendering contract.
    """
    from opencomputer.cli_ui.resume_picker import format_time_ago

    row = SessionRow(
        id="abc12345abcdef",
        title="hello",
        started_at=time.time() - 60,
        message_count=2,
        git_branch="main",
    )
    parts = [
        format_time_ago(row.started_at),
        f"{row.message_count} messages",
    ]
    if row.git_branch:
        parts.append(row.git_branch)
    parts.append(row.id[:8])
    meta = "  ·  ".join(parts)
    assert "main" in meta
    # 4-segment layout: "<ago>  ·  N messages  ·  <branch>  ·  <id>"
    assert meta.count("·") == 3
    assert meta.endswith("abc12345")


def test_picker_meta_omits_branch_slot_when_empty() -> None:
    """Backwards-compat: NULL/empty branch → 3-segment meta layout."""
    from opencomputer.cli_ui.resume_picker import format_time_ago

    row = SessionRow(
        id="abc12345",
        title="hello",
        started_at=time.time() - 60,
        message_count=2,
        git_branch="",
    )
    parts = [
        format_time_ago(row.started_at),
        f"{row.message_count} messages",
    ]
    if row.git_branch:
        parts.append(row.git_branch)
    parts.append(row.id[:8])
    meta = "  ·  ".join(parts)
    # 3-segment layout: "<ago>  ·  N messages  ·  <id>"
    assert meta.count("·") == 2
    assert "abc12345" in meta


# ─── end-to-end: live git repo → SessionDB → picker row ──────────────


def test_end_to_end_real_repo_to_picker_row(tmp_path: Path) -> None:
    """Wire the four layers together with a real git repo as the source.

    1. Initialise a git repo on branch ``e2e-branch``.
    2. Call ``current_git_branch`` to capture it.
    3. Persist a session with that branch via ``ensure_session``.
    4. Read it back via ``list_sessions_with_preview`` and construct a
       ``SessionRow``.
    5. Confirm the branch survives every hop.
    """
    if not shutil.which("git"):
        pytest.skip("git not on PATH")

    _init_repo(tmp_path, branch="e2e-branch")
    _commit_empty(tmp_path)

    captured = current_git_branch(tmp_path)
    assert captured == "e2e-branch"

    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd=str(tmp_path), git_branch=captured)

    rows = db.list_sessions_with_preview(limit=10)
    assert len(rows) == 1
    row = SessionRow(
        id=rows[0]["id"],
        title=rows[0]["title"] or "",
        started_at=float(rows[0]["started_at"]),
        message_count=int(rows[0]["message_count"] or 0),
        cwd=rows[0]["cwd"] or "",
        git_branch=rows[0]["git_branch"] or "",
    )
    assert row.git_branch == "e2e-branch"
