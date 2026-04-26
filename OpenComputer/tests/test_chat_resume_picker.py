"""``opencomputer chat --resume {last,pick}`` interactive surface.

CLAUDE.md §5 Phase 15.A — checkpoint table shipped, CLI surface was
missing. Without this, ``--resume`` only worked when the user had
copied a UUID from `opencomputer sessions`. The picker closes that
loop without adding a new top-level command.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _seed_sessions(tmp_path: Path, n: int = 3) -> Path:
    """Build a SessionDB with n sessions ordered newest-first."""
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    now = time.time()
    for i in range(n):
        db.create_session(
            session_id=f"session-{i:02d}",
            platform="cli",
            model="claude-haiku-4-5",
            title=f"test session {i}",
        )
        # Backdate so list_sessions returns deterministic ordering.
        with db._connect() as conn:  # noqa: SLF001 — test-internal
            conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (now - i, f"session-{i:02d}"),
            )
    return db_path


def _patch_default_config_to(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make _resolve_resume_target see our seeded DB."""
    from opencomputer import cli
    from opencomputer.agent.config import default_config as real_default_config

    def fake_default_config():
        cfg = real_default_config()
        from dataclasses import replace

        return replace(cfg, session=replace(cfg.session, db_path=tmp_db))

    # The helper imports lazily inside the function — patch at the
    # source so the lookup hits our shim.
    monkeypatch.setattr(
        "opencomputer.agent.config.default_config", fake_default_config
    )


def test_resume_last_returns_most_recent_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer import cli

    db_path = _seed_sessions(tmp_path, n=3)
    _patch_default_config_to(db_path, monkeypatch)

    resolved = cli._resolve_resume_target("last")
    assert resolved == "session-00", "expected most-recent (newest started_at)"


def test_resume_last_returns_none_when_no_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty DB → None so caller falls back to a fresh session."""
    from opencomputer import cli
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "empty.db"
    SessionDB(db_path)
    _patch_default_config_to(db_path, monkeypatch)

    assert cli._resolve_resume_target("last") is None


def test_resume_pick_resolves_user_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User types '2' at the picker prompt → second-most-recent session."""
    from opencomputer import cli

    db_path = _seed_sessions(tmp_path, n=3)
    _patch_default_config_to(db_path, monkeypatch)

    with patch("rich.prompt.Prompt.ask", return_value="2"):
        resolved = cli._resolve_resume_target("pick")
    assert resolved == "session-01", (
        "expected session at index 2 (1-based) = second newest"
    )


def test_resume_pick_blank_choice_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User hits Enter at the picker → None → caller starts fresh."""
    from opencomputer import cli

    db_path = _seed_sessions(tmp_path, n=3)
    _patch_default_config_to(db_path, monkeypatch)

    with patch("rich.prompt.Prompt.ask", return_value=""):
        resolved = cli._resolve_resume_target("pick")
    assert resolved is None


def test_resume_pick_with_no_sessions_returns_none_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty DB + 'pick' → don't even show the prompt."""
    from opencomputer import cli
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "empty.db"
    SessionDB(db_path)
    _patch_default_config_to(db_path, monkeypatch)

    asked: list[bool] = []

    def fake_ask(*_a, **_k):
        asked.append(True)
        return ""

    with patch("rich.prompt.Prompt.ask", side_effect=fake_ask):
        resolved = cli._resolve_resume_target("pick")

    assert resolved is None
    assert asked == [], "must not prompt when there's nothing to pick"


def test_resume_passthrough_unknown_spec_returns_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-magic value (e.g. a UUID-shaped string) is NOT one of our
    magic spellings — chat() never calls _resolve_resume_target for
    those. This test pins the policy so a future refactor doesn't
    accidentally route real UUIDs through the picker."""
    from opencomputer import cli
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "any.db"
    SessionDB(db_path)
    _patch_default_config_to(db_path, monkeypatch)

    # The function would crash on an unrecognised spec because of the
    # ``spec == "pick"`` branch below — confirm we only ever route
    # ``last`` and ``pick`` here. (chat() guards with the membership
    # check; we re-assert at this layer too.)
    import inspect

    src = inspect.getsource(cli.chat)
    assert 'resume in ("last", "pick")' in src, (
        "chat() must guard the resolver call with the magic-spec membership check"
    )
