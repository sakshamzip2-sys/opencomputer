"""``oc sessions tree`` — delegate-lineage 2026-05-10.

End-to-end CLI tests using a tmp profile so SessionDB writes land in
an isolated sqlite file. We use the typer ``CliRunner`` invoked
through the top-level ``app`` so the test exercises the same
registration path users hit at runtime.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.agent.subagent_store import SubagentStore
from opencomputer.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force the active profile's sessions.db into ``tmp_path``.

    ``opencomputer.agent.config._home`` is the canonical resolver used
    by the session CLI's ``_db()`` factory. Pointing it at tmp_path
    keeps every CLI invocation hermetic.
    """
    home = tmp_path / "profile-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: home
    )
    monkeypatch.setattr(
        "opencomputer.cli_session._home", lambda: home, raising=False
    )
    return home


def _seed_three_generations(home: Path) -> None:
    """Build a parent → 2 children → 1 grandchild lineage in sqlite."""
    db = SessionDB(home / "sessions.db")
    db.create_session(
        "parent-aaaaaaaa", title="root chat", model="claude"
    )
    db.create_session(
        "child-bbbbbbbb",
        parent_session_id="parent-aaaaaaaa",
        title="explore docs",
    )
    db.create_session(
        "child-cccccccc",
        parent_session_id="parent-aaaaaaaa",
        title="run tests",
    )
    db.create_session(
        "grandchild-d",
        parent_session_id="child-bbbbbbbb",
        title="grep readme",
    )

    # Optionally enrich via subagents table so the tree shows agent metadata.
    store = SubagentStore(db.db_path)
    base = datetime.now(UTC)
    store.upsert(
        agent_id="sub-1",
        parent_session_id="parent-aaaaaaaa",
        child_session_id="child-bbbbbbbb",
        parent_agent_id=None,
        goal="explore docs",
        started_at=base,
        state="completed",
        role="leaf",
        agent_template="doc-writer",
        isolation_mode="none",
        depth=0,
    )
    store.update("sub-1", state="completed", ended_at=base)


def test_tree_renders_root_with_descendants(_isolate_profile: Path) -> None:
    _seed_three_generations(_isolate_profile)
    result = runner.invoke(app, ["sessions", "tree", "parent-aaaaaaaa"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Root + every descendant present (8-char prefix in the rendered tree).
    assert "parent-a" in out
    assert "child-bb" in out
    assert "child-cc" in out
    assert "grandchi" in out  # 8-char prefix of grandchild-d
    # Root marker present.
    assert "(root)" in out


def test_tree_walks_up_from_a_grandchild_to_show_full_ancestry(
    _isolate_profile: Path,
) -> None:
    """Asking for a leaf prints the whole tree from the actual root."""
    _seed_three_generations(_isolate_profile)
    result = runner.invoke(app, ["sessions", "tree", "grandchild-d"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "parent-a" in out
    assert "(root)" in out
    assert "grandchi" in out


def test_tree_unknown_session_id_errors_clean(
    _isolate_profile: Path,
) -> None:
    """A bogus id produces a clean error + non-zero exit."""
    result = runner.invoke(app, ["sessions", "tree", "this-does-not-exist"])
    assert result.exit_code != 0, result.output
    # Error message includes the id; it goes to stderr but
    # CliRunner mixes stdout+stderr by default.
    assert "this-does-not-exist" in result.stdout + (result.stderr or "")


def test_tree_for_root_with_no_children_renders_single_node(
    _isolate_profile: Path,
) -> None:
    db = SessionDB(_isolate_profile / "sessions.db")
    db.create_session("lonely-root", title="nothing branches off me")
    result = runner.invoke(app, ["sessions", "tree", "lonely-root"])
    assert result.exit_code == 0, result.output
    assert "lonely-r" in result.output  # 8-char prefix
    assert "(root)" in result.output


def test_tree_accepts_id_prefix(_isolate_profile: Path) -> None:
    _seed_three_generations(_isolate_profile)
    result = runner.invoke(app, ["sessions", "tree", "parent-a"])
    assert result.exit_code == 0, result.output
    assert "parent-a" in result.output


def test_tree_rejects_ambiguous_prefix(_isolate_profile: Path) -> None:
    db = SessionDB(_isolate_profile / "sessions.db")
    db.create_session("xxx-001", title="a")
    db.create_session("xxx-002", title="b")
    result = runner.invoke(app, ["sessions", "tree", "xxx-"])
    assert result.exit_code != 0, result.output
    assert "matches" in result.stdout + (result.stderr or "")


def test_tree_renders_subagent_metadata(_isolate_profile: Path) -> None:
    """When subagents table has matching rows, tree nodes show role +
    agent template + state."""
    _seed_three_generations(_isolate_profile)
    result = runner.invoke(app, ["sessions", "tree", "parent-aaaaaaaa"])
    assert result.exit_code == 0, result.output
    # The seeded subagent for child-bbbbbbbb has role=leaf and
    # agent_template=doc-writer. Both must appear in the rendered tree.
    assert "doc-writer" in result.output
    assert "leaf" in result.output
