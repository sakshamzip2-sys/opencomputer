"""Phase L — ``oc resume --from-pr NUMBER`` resolution.

Coverage:

    1. ``_resolve_pr_to_branch`` happy path (mocked gh subprocess).
    2. Every failure mode of the gh shell-out (not on PATH, non-zero
       exit, timeout, OSError, malformed JSON, missing headRefName).
    3. ``_resolve_pr_to_session_id`` end-to-end: maps PR → branch →
       session id via SessionDB.
    4. Edge cases: no matching session, DB error, multiple matches
       (most recent wins).
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli import _resolve_pr_to_branch, _resolve_pr_to_session_id

# ─── _resolve_pr_to_branch ────────────────────────────────────────────


def _mock_gh_run(*, returncode: int, stdout: str = "", stderr: str = ""):
    """Build a mock subprocess.CompletedProcess for gh shell-outs."""
    mock = subprocess.CompletedProcess(
        args=["gh", "pr", "view"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return mock


def test_resolve_pr_to_branch_returns_head_ref_on_success() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(
                 returncode=0, stdout='{"headRefName":"feat/auth-refactor"}'
             ),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch == "feat/auth-refactor"


def test_resolve_pr_to_branch_returns_none_when_gh_missing() -> None:
    with patch("shutil.which", return_value=None):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_on_nonzero_exit() -> None:
    """gh failed (PR doesn't exist, auth missing, etc.) — None + diagnostic."""
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(
                 returncode=1, stderr="GraphQL: Could not resolve to a PullRequest"
             ),
         ):
        branch = _resolve_pr_to_branch(99999)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_on_timeout() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=10),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_on_oserror() -> None:
    """Permission denied / file not found — must not raise."""
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch("subprocess.run", side_effect=OSError("permission denied")):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_on_malformed_json() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(returncode=0, stdout="not json{{"),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_when_payload_lacks_headrefname() -> None:
    """gh succeeded but the JSON has the wrong shape."""
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(
                 returncode=0, stdout='{"someOtherField":"value"}'
             ),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_when_headrefname_is_empty() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(returncode=0, stdout='{"headRefName":""}'),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


def test_resolve_pr_to_branch_returns_none_when_headrefname_is_not_string() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch(
             "subprocess.run",
             return_value=_mock_gh_run(returncode=0, stdout='{"headRefName":null}'),
         ):
        branch = _resolve_pr_to_branch(123)
    assert branch is None


# ─── _resolve_pr_to_session_id end-to-end ─────────────────────────────


def _seed_db(tmp_path: Path) -> tuple[SessionDB, str]:
    """Make a SessionDB with a session on branch 'feat/x'. Return (db, sid)."""
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    sid = uuid.uuid4().hex
    db.ensure_session(sid, cwd="/work/proj", git_branch="feat/x")
    return db, sid


def _mock_config(db_path: Path) -> object:
    """Return a config-shaped namespace that satisfies cfg.session.db_path."""
    from types import SimpleNamespace
    return SimpleNamespace(session=SimpleNamespace(db_path=db_path))


def test_resolve_pr_to_session_id_returns_session_id_when_found(
    tmp_path: Path, monkeypatch
) -> None:
    db, sid = _seed_db(tmp_path)
    monkeypatch.setattr(
        "opencomputer.cli.load_config",
        lambda: _mock_config(tmp_path / "sessions.db"),
    )

    with patch("opencomputer.cli._resolve_pr_to_branch", return_value="feat/x"):
        result = _resolve_pr_to_session_id(42)

    assert result == sid


def test_resolve_pr_to_session_id_returns_none_when_pr_resolve_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """If gh resolution fails, _resolve_pr_to_session_id short-circuits."""
    monkeypatch.setattr(
        "opencomputer.cli.load_config",
        lambda: _mock_config(tmp_path / "sessions.db"),
    )

    with patch("opencomputer.cli._resolve_pr_to_branch", return_value=None):
        result = _resolve_pr_to_session_id(42)

    assert result is None


def test_resolve_pr_to_session_id_returns_none_when_no_session_matches(
    tmp_path: Path, monkeypatch
) -> None:
    """gh resolves but no session exists for that branch yet."""
    db, _sid = _seed_db(tmp_path)  # has 'feat/x', NOT 'feat/y'
    monkeypatch.setattr(
        "opencomputer.cli.load_config",
        lambda: _mock_config(tmp_path / "sessions.db"),
    )

    with patch("opencomputer.cli._resolve_pr_to_branch", return_value="feat/y"):
        result = _resolve_pr_to_session_id(99)

    assert result is None


def test_resolve_pr_to_session_id_picks_most_recent_match(
    tmp_path: Path, monkeypatch
) -> None:
    """Two sessions on the same branch → return the most recent."""
    import time as _time

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    old_sid = uuid.uuid4().hex
    new_sid = uuid.uuid4().hex
    db.ensure_session(old_sid, cwd="/work/proj", git_branch="feat/x")
    _time.sleep(0.01)  # nudge timestamp
    db.ensure_session(new_sid, cwd="/work/proj", git_branch="feat/x")
    monkeypatch.setattr(
        "opencomputer.cli.load_config", lambda: _mock_config(db_path)
    )

    with patch("opencomputer.cli._resolve_pr_to_branch", return_value="feat/x"):
        result = _resolve_pr_to_session_id(123)

    # SessionDB sorts DESC by started_at — the newer session wins.
    assert result == new_sid


def test_resolve_pr_to_session_id_survives_db_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A broken DB returns None — never propagates the exception.

    SessionDB is imported INSIDE _resolve_pr_to_session_id, so the
    correct patch target is at the source module
    (``opencomputer.agent.state.SessionDB``), not the call site.
    """
    monkeypatch.setattr(
        "opencomputer.cli.load_config",
        lambda: _mock_config(tmp_path / "sessions.db"),
    )

    class _BrokenDB:
        def list_sessions_with_preview(self, **_kw):  # noqa: ANN201
            raise RuntimeError("disk full")

    with patch(
        "opencomputer.cli._resolve_pr_to_branch", return_value="feat/x"
    ), patch(
        "opencomputer.agent.state.SessionDB", return_value=_BrokenDB()
    ):
        result = _resolve_pr_to_session_id(42)

    assert result is None
