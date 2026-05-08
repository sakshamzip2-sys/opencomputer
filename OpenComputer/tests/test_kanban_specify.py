"""Tests for ``opencomputer.kanban.specify`` (Hermes Doc-2 parity, 2026-05-08).

The specify operation is a triage→spec expansion: an LLM rewrites the
task body into a structured spec, then the task is promoted out of
triage. These tests cover:

* Happy path: triage task expands and promotes.
* Wrong status guard: specify on todo/done raises SpecifyError.
* Missing task: specify on unknown id raises SpecifyError.
* Empty LLM response: specify raises SpecifyError, task stays in triage.
* Body length cap: oversized LLM output truncates with marker.
* DB state: ``apply_specify`` writes body + status + 'specified' event.

Tests stub :func:`opencomputer.kanban.specify._call_specifier_model`
directly (monkeypatch) — the real provider is irrelevant to the
contract under test.
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest

from opencomputer.kanban import db as kb
from opencomputer.kanban import specify as sp
from opencomputer.kanban.specify import (
    MAX_BODY_CHARS,
    SpecifyError,
    SpecifyResult,
    specify_task,
)


@pytest.fixture()
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """Fresh kanban DB on disk for each test."""
    db_path = tmp_path / "kanban.sqlite"
    conn = kb.connect(db_path=db_path)
    try:
        yield conn
    finally:
        conn.close()


def _make_triage_task(conn: sqlite3.Connection, *, title: str, body: str = "") -> str:
    """Create a triage-status task and return its id."""
    return kb.create_task(
        conn,
        title=title,
        body=body or None,
        assignee=None,
        triage=True,  # ← create in triage status, not auto-promoted to ready
    )


# ─── Happy path ────────────────────────────────────────────────────────


def test_specify_task_happy_path_promotes_and_expands(conn, monkeypatch) -> None:
    tid = _make_triage_task(
        conn, title="research AI funding 2026", body="rough idea",
    )

    expanded = (
        "## Goal\nUnderstand the AI funding landscape Q1 2026.\n\n"
        "## Approach\n- Survey YC + a16z\n- Note round sizes\n\n"
        "## Definition of Done\n- 1-page brief\n\n"
        "## Out of scope\n- M&A activity"
    )

    async def _fake_call(prompt: str) -> str:
        assert "research AI funding 2026" in prompt
        return expanded

    monkeypatch.setattr(sp, "_call_specifier_model", _fake_call)

    result = asyncio.run(specify_task(conn, task_id=tid))
    assert isinstance(result, SpecifyResult)
    assert result.task_id == tid
    assert result.old_status == "triage"
    assert result.new_status == "todo"
    assert "## Goal" in result.expanded_body
    assert result.truncated is False

    # Confirm the DB row reflects the new state.
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "todo"
    assert task.body == expanded


def test_specify_task_promote_to_ready(conn, monkeypatch) -> None:
    tid = _make_triage_task(conn, title="quick task")

    async def _fake_call(prompt: str) -> str:
        return "## Goal\nDone.\n## Approach\n- one\n## Definition of Done\n- ok\n## Out of scope\n- none"

    monkeypatch.setattr(sp, "_call_specifier_model", _fake_call)

    result = asyncio.run(specify_task(conn, task_id=tid, promote_to="ready"))
    assert result.new_status == "ready"
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "ready"


# ─── Guards ────────────────────────────────────────────────────────────


def test_specify_task_raises_on_missing_task(conn) -> None:
    with pytest.raises(SpecifyError, match="not found"):
        asyncio.run(specify_task(conn, task_id="t_does_not_exist"))


def test_specify_task_raises_on_non_triage_status(conn, monkeypatch) -> None:
    """Tasks promoted out of triage are off-limits to specify — protects
    against accidentally clobbering an already-edited body."""
    tid = _make_triage_task(conn, title="example")
    # Manually promote to todo via apply_specify, then try to re-specify.

    async def _fake_call(prompt: str) -> str:
        return "## Goal\noriginal\n## Approach\n- x\n## Definition of Done\n- ok\n## Out of scope\n- nope"

    monkeypatch.setattr(sp, "_call_specifier_model", _fake_call)
    asyncio.run(specify_task(conn, task_id=tid))

    # Second call should refuse — task is now in todo.
    with pytest.raises(SpecifyError, match="not triage"):
        asyncio.run(specify_task(conn, task_id=tid))


def test_specify_task_raises_on_empty_llm_response(conn, monkeypatch) -> None:
    tid = _make_triage_task(conn, title="empty test")

    async def _fake_call(prompt: str) -> str:
        return "   "  # whitespace only

    monkeypatch.setattr(sp, "_call_specifier_model", _fake_call)

    with pytest.raises(SpecifyError, match="empty"):
        asyncio.run(specify_task(conn, task_id=tid))

    # And confirm the task did NOT get promoted out of triage.
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "triage"


# ─── Truncation ────────────────────────────────────────────────────────


def test_specify_task_caps_oversized_body(conn, monkeypatch) -> None:
    """Bodies > MAX_BODY_CHARS get truncated with a marker."""
    tid = _make_triage_task(conn, title="big spec")
    huge = "## Goal\n" + ("X" * (MAX_BODY_CHARS + 500))

    async def _fake_call(prompt: str) -> str:
        return huge

    monkeypatch.setattr(sp, "_call_specifier_model", _fake_call)

    result = asyncio.run(specify_task(conn, task_id=tid))
    assert result.truncated is True
    # Body length is at or under MAX_BODY_CHARS + the truncation marker.
    assert len(result.expanded_body) <= MAX_BODY_CHARS + len("\n\n[truncated]")
    assert result.expanded_body.endswith("[truncated]")


# ─── apply_specify (DB layer) ──────────────────────────────────────────


def test_apply_specify_emits_specified_event(conn) -> None:
    """The DB function appends a ``specified`` event row — useful for tail/log."""
    tid = _make_triage_task(conn, title="event test")

    ok = kb.apply_specify(
        conn, task_id=tid, expanded_body="new body", new_status="todo",
    )
    assert ok is True

    # Check the events table for our specified event.
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert "specified" in kinds


def test_apply_specify_returns_false_on_unknown_task(conn) -> None:
    ok = kb.apply_specify(
        conn, task_id="t_nope", expanded_body="x", new_status="todo",
    )
    assert ok is False


def test_apply_specify_rejects_invalid_status(conn) -> None:
    tid = _make_triage_task(conn, title="invalid status")
    with pytest.raises(ValueError, match="status must be one of"):
        kb.apply_specify(
            conn, task_id=tid, expanded_body="x", new_status="bogus",
        )
