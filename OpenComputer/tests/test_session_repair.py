"""``oc session repair`` — permanent DB cleanup of orphan ``tool_use`` rows.

Two layers under test:

* ``SessionDB.replace_session_messages`` — atomic rewrite primitive.
* ``oc session repair`` Typer command — composes the primitive with
  ``reconcile_orphan_tool_calls`` to permanently materialize wire-side
  synthetic ``<INTERRUPTED>`` placeholders.

The wire-side reconciler at session resume already auto-heals every
session, so the user-visible 400 BadRequest is gone immediately after
the asyncio cross-loop fix. ``oc session repair`` exists for users who
want their DB clean for export, debugging, audit, or migration to a
new harness — its role is "make it permanent", not "make it work."
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.loop import (
    ORPHAN_TOOL_RESULT_PLACEHOLDER,
    reconcile_orphan_tool_calls,
)
from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import session_app
from plugin_sdk.core import Message, ToolCall

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _orphan_session(db: SessionDB, sid: str, tu_id: str = "toolu_orphan_42") -> None:
    """Seed a session whose history ends mid-dispatch — orphan tool_use."""
    db.create_session(sid, platform="cli", model="claude-opus-4-7", title="Crashed")
    db.append_message(sid, Message(role="user", content="run a thing"))
    db.append_message(
        sid,
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=tu_id, name="Bash", arguments={"cmd": "ls"})],
        ),
    )
    # crash here — no tool_result row written.
    db.append_message(sid, Message(role="user", content="ok keep going"))


# ─── Layer 1: SessionDB.replace_session_messages ────────────────────


def test_replace_session_messages_rewrites_atomically(isolated_home: Path) -> None:
    """The primitive deletes all rows then reinserts the new list, all
    inside one transaction. ``message_count`` is reset to the new count.
    ``messages.id`` is reassigned by the autoincrement (callers must not
    rely on stable IDs across the rewrite).
    """
    db = SessionDB(isolated_home / "sessions.db")
    sid = "sess-repair-01"
    _orphan_session(db, sid)
    pre = db.get_messages(sid)
    assert len(pre) == 3
    pre_meta = db.get_session(sid)
    assert pre_meta is not None
    assert pre_meta["message_count"] == 3

    reconciled, n_inserted = reconcile_orphan_tool_calls(pre)
    assert n_inserted == 1
    new_ids = db.replace_session_messages(sid, reconciled)
    assert len(new_ids) == 4
    assert all(nid > 0 for nid in new_ids)

    post = db.get_messages(sid)
    assert len(post) == 4
    # Order: user → assistant(tool_use) → tool(synthetic) → user
    assert post[0].role == "user"
    assert post[1].role == "assistant"
    assert post[1].tool_calls is not None
    assert post[2].role == "tool"
    assert post[2].tool_call_id == "toolu_orphan_42"
    assert post[2].content == ORPHAN_TOOL_RESULT_PLACEHOLDER
    assert post[3].role == "user"

    # Session metadata reflects the new count.
    post_meta = db.get_session(sid)
    assert post_meta is not None
    assert post_meta["message_count"] == 4


def test_replace_session_messages_idempotent_via_reconciler(isolated_home: Path) -> None:
    """Running repair twice on the same session must be a no-op the
    second time. The reconciler is already idempotent; combined with
    the rewrite this means re-running the CLI is safe."""
    db = SessionDB(isolated_home / "sessions.db")
    sid = "sess-repair-idem"
    _orphan_session(db, sid)
    msgs = db.get_messages(sid)
    reconciled, n = reconcile_orphan_tool_calls(msgs)
    assert n == 1
    db.replace_session_messages(sid, reconciled)

    # Second pass: reconciler sees no orphans now.
    msgs2 = db.get_messages(sid)
    _, n2 = reconcile_orphan_tool_calls(msgs2)
    assert n2 == 0


def test_replace_session_messages_rejects_empty_session_id(isolated_home: Path) -> None:
    db = SessionDB(isolated_home / "sessions.db")
    with pytest.raises(ValueError):
        db.replace_session_messages("", [])


def test_replace_session_messages_does_not_touch_other_sessions(
    isolated_home: Path,
) -> None:
    """Rewriting session A must leave session B's rows alone — the
    DELETE is scoped by ``WHERE session_id = ?``."""
    db = SessionDB(isolated_home / "sessions.db")
    sid_a = "sess-A-AAAA"
    sid_b = "sess-B-BBBB"
    _orphan_session(db, sid_a, tu_id="toolu_a")
    db.create_session(sid_b, platform="cli", model="claude-opus-4-7", title="Other")
    db.append_message(sid_b, Message(role="user", content="hello B"))
    db.append_message(sid_b, Message(role="assistant", content="hi"))

    reconciled, _ = reconcile_orphan_tool_calls(db.get_messages(sid_a))
    db.replace_session_messages(sid_a, reconciled)

    # B is untouched.
    msgs_b = db.get_messages(sid_b)
    assert len(msgs_b) == 2
    assert msgs_b[0].content == "hello B"
    assert msgs_b[1].content == "hi"


# ─── Layer 2: ``oc session repair`` CLI ─────────────────────────────


def test_repair_cli_dry_run_reports_without_writing(isolated_home: Path) -> None:
    db = SessionDB(isolated_home / "sessions.db")
    sid = "abcdef0123456789"
    _orphan_session(db, sid)
    result = runner.invoke(session_app, ["repair", "--all", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "1" in result.stdout
    assert "session(s) need repair" in result.stdout
    assert "dry-run" in result.stdout.lower()
    # DB unchanged.
    msgs = db.get_messages(sid)
    assert len(msgs) == 3
    assert all(m.role != "tool" for m in msgs)


def test_repair_cli_all_yes_rewrites_db(isolated_home: Path) -> None:
    db = SessionDB(isolated_home / "sessions.db")
    sid = "fedcba0987654321"
    _orphan_session(db, sid)
    result = runner.invoke(session_app, ["repair", "--all", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "repaired" in result.stdout.lower()
    msgs = db.get_messages(sid)
    assert len(msgs) == 4
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == ORPHAN_TOOL_RESULT_PLACEHOLDER


def test_repair_cli_no_op_when_clean(isolated_home: Path) -> None:
    db = SessionDB(isolated_home / "sessions.db")
    sid = "cleanclean000000"
    db.create_session(sid, platform="cli", model="claude-opus-4-7", title="Clean")
    # Properly-paired tool_use + tool_result.
    db.append_messages_batch(
        sid,
        [
            Message(role="user", content="run"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="toolu_ok", name="Read", arguments={})],
            ),
            Message(role="tool", content="contents", tool_call_id="toolu_ok"),
        ],
    )
    result = runner.invoke(session_app, ["repair", "--all", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "nothing to repair" in result.stdout.lower()


def test_repair_cli_specific_session_by_prefix(isolated_home: Path) -> None:
    db = SessionDB(isolated_home / "sessions.db")
    sid = "deadbeef00000000"
    _orphan_session(db, sid)
    # 8-char prefix should match.
    result = runner.invoke(session_app, ["repair", "deadbeef", "--yes"])
    assert result.exit_code == 0, result.stdout
    msgs = db.get_messages(sid)
    assert len(msgs) == 4
    assert any(m.role == "tool" for m in msgs)


def test_repair_cli_rejects_both_id_and_all(isolated_home: Path) -> None:
    result = runner.invoke(session_app, ["repair", "abcd", "--all"])
    assert result.exit_code == 2
    assert "either" in result.stdout.lower()


def test_repair_cli_rejects_neither_id_nor_all(isolated_home: Path) -> None:
    result = runner.invoke(session_app, ["repair"])
    assert result.exit_code == 2


def test_repair_cli_unknown_session_prefix(isolated_home: Path) -> None:
    result = runner.invoke(session_app, ["repair", "ghostid", "--yes"])
    assert result.exit_code == 1
    assert "no session" in result.stdout.lower()


def test_repair_cli_user_can_decline_at_prompt(isolated_home: Path) -> None:
    """When ``--yes`` is omitted, the user can type ``n`` to abort."""
    db = SessionDB(isolated_home / "sessions.db")
    sid = "abortabort000000"
    _orphan_session(db, sid)
    result = runner.invoke(session_app, ["repair", "--all"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.stdout.lower()
    # DB still corrupt.
    msgs = db.get_messages(sid)
    assert len(msgs) == 3


def test_repair_cli_partial_pairing_inserts_only_missing(isolated_home: Path) -> None:
    """Assistant with multiple tool_calls, only some have tool_results.

    The repair must insert synthetics ONLY for the missing ones — never
    duplicate the real results."""
    db = SessionDB(isolated_home / "sessions.db")
    sid = "partial000000000"
    db.create_session(sid, platform="cli", model="claude-opus-4-7", title="Partial")
    db.append_messages_batch(
        sid,
        [
            Message(role="user", content="run multi"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="t_a", name="Read", arguments={}),
                    ToolCall(id="t_b", name="Bash", arguments={}),
                    ToolCall(id="t_c", name="Grep", arguments={}),
                ],
            ),
            # Only t_a's result was persisted.
            Message(role="tool", content="real result for a", tool_call_id="t_a"),
            Message(role="user", content="next"),
        ],
    )
    result = runner.invoke(session_app, ["repair", "--all", "--yes"])
    assert result.exit_code == 0, result.stdout

    msgs = db.get_messages(sid)
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert len(tool_msgs) == 3
    by_id = {m.tool_call_id: m.content for m in tool_msgs}
    assert by_id["t_a"] == "real result for a"  # real result preserved
    assert by_id["t_b"] == ORPHAN_TOOL_RESULT_PLACEHOLDER
    assert by_id["t_c"] == ORPHAN_TOOL_RESULT_PLACEHOLDER
