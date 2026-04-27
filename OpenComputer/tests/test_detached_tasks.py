"""Tier-B item 23 — detached tasks (store + runner + spawn tool + CLI)."""

from __future__ import annotations

import asyncio
import time

import pytest
from typer.testing import CliRunner

from opencomputer.tasks import (
    Task,
    TaskNotFound,
    TaskRunner,
    TaskRunnerConfig,
    TaskStore,
)

# ──────────────────────────── store ────────────────────────────


def test_create_task_returns_queued(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="analyze ABC stock")
    assert task.status == "queued"
    assert task.prompt == "analyze ABC stock"
    assert len(task.id) >= 8


def test_get_unknown_raises(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    with pytest.raises(TaskNotFound):
        store.get("does-not-exist")


def test_lifecycle_queued_to_done(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    store.complete(task.id, "result text")
    fetched = store.get(task.id)
    assert fetched.status == "done"
    assert fetched.output == "result text"
    assert fetched.completed_at is not None


def test_complete_rejects_non_running(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    # Skipping mark_running — the row is still queued.
    with pytest.raises(TaskNotFound):
        store.complete(task.id, "out")


def test_fail_records_error(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    store.fail(task.id, "TypeError: bad")
    f = store.get(task.id)
    assert f.status == "failed"
    assert "TypeError: bad" in (f.error or "")


def test_cancel_queued_returns_true(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    assert store.cancel(task.id) is True
    assert store.get(task.id).status == "cancelled"


def test_cancel_already_done_returns_false(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    store.complete(task.id, "out")
    assert store.cancel(task.id) is False


def test_list_filters_by_status(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    store.create(prompt="a")
    store.create(prompt="b")
    qd = store.create(prompt="c")
    store.mark_running(qd.id)
    store.complete(qd.id, "ok")

    queued = store.list_(status="queued")
    done = store.list_(status="done")
    assert len(queued) == 2
    assert len(done) == 1
    assert done[0].output == "ok"


def test_list_queued_is_oldest_first(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    a = store.create(prompt="a")
    time.sleep(0.01)
    b = store.create(prompt="b")
    rows = store.list_queued()
    assert rows[0].id == a.id
    assert rows[1].id == b.id


def test_mark_orphaned_running(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    n = store.mark_orphaned_running()
    assert n == 1
    assert store.get(task.id).status == "orphaned"


def test_record_progress(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    store.record_progress(task.id, "50% done")
    assert store.get(task.id).progress == "50% done"


# ──────────────────────────── runner ────────────────────────────


@pytest.mark.asyncio
async def test_runner_drains_queue_with_stub_executor(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")

    async def stub(t: Task) -> str:
        return f"executed {t.prompt}"

    runner = TaskRunner(
        store, executor=stub,
        config=TaskRunnerConfig(poll_interval_seconds=0.05),
    )
    runner_task = asyncio.create_task(runner.run_forever())

    # Wait until the runner has completed it.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if store.get(task.id).status == "done":
            break

    runner.stop()
    await asyncio.wait_for(runner_task, timeout=3.0)

    final = store.get(task.id)
    assert final.status == "done"
    assert final.output == "executed hi"


@pytest.mark.asyncio
async def test_runner_marks_failed_on_executor_exception(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="bad")

    async def bad(_t: Task) -> str:
        raise RuntimeError("boom")

    runner = TaskRunner(
        store, executor=bad,
        config=TaskRunnerConfig(poll_interval_seconds=0.05),
    )
    runner_task = asyncio.create_task(runner.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if store.get(task.id).status == "failed":
            break
    runner.stop()
    await asyncio.wait_for(runner_task, timeout=3.0)
    f = store.get(task.id)
    assert f.status == "failed"
    assert "RuntimeError" in (f.error or "")


@pytest.mark.asyncio
async def test_runner_recover_orphaned_marks_running_rows(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    t1 = store.create(prompt="a")
    t2 = store.create(prompt="b")
    store.mark_running(t1.id)
    store.mark_running(t2.id)
    runner = TaskRunner(store)
    n = await runner.recover_orphaned()
    assert n == 2
    assert store.get(t1.id).status == "orphaned"
    assert store.get(t2.id).status == "orphaned"


@pytest.mark.asyncio
async def test_runner_calls_notifier_on_done(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi")

    async def stub(_t: Task) -> str:
        return "ok"

    notifications: list[tuple[str, str]] = []

    async def notify(task_id: str, output: str) -> None:
        notifications.append((task_id, output))

    runner = TaskRunner(
        store, executor=stub, notifier=notify,
        config=TaskRunnerConfig(poll_interval_seconds=0.05),
    )
    runner_task = asyncio.create_task(runner.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if store.get(task.id).delivery_status == "delivered":
            break
    runner.stop()
    await asyncio.wait_for(runner_task, timeout=3.0)

    assert notifications == [(task.id, "ok")]
    assert store.get(task.id).delivery_status == "delivered"


@pytest.mark.asyncio
async def test_runner_skips_notifier_on_silent_policy(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    task = store.create(prompt="hi", notify_policy="silent")

    async def stub(_t: Task) -> str:
        return "ok"

    notify_calls = []

    async def notify(task_id: str, output: str) -> None:
        notify_calls.append((task_id, output))

    runner = TaskRunner(
        store, executor=stub, notifier=notify,
        config=TaskRunnerConfig(poll_interval_seconds=0.05),
    )
    runner_task = asyncio.create_task(runner.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if store.get(task.id).status == "done":
            break
    runner.stop()
    await asyncio.wait_for(runner_task, timeout=3.0)

    assert notify_calls == []  # silent = no notification


# ──────────────────────────── spawn tool ────────────────────────────


@pytest.mark.asyncio
async def test_spawn_tool_creates_queued_task(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.tools.spawn_detached_task import SpawnDetachedTaskTool
    from plugin_sdk.core import ToolCall

    tool = SpawnDetachedTaskTool()
    result = await tool.execute(
        ToolCall(
            id="c1", name="SpawnDetachedTask",
            arguments={"prompt": "do a deep market analysis"},
        )
    )
    assert not result.is_error
    assert "Detached task started" in result.content
    # And a queued row should exist
    store = TaskStore(tmp_path / "sessions.db")
    rows = store.list_queued()
    assert len(rows) == 1
    assert rows[0].prompt == "do a deep market analysis"


@pytest.mark.asyncio
async def test_spawn_tool_rejects_empty_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.tools.spawn_detached_task import SpawnDetachedTaskTool
    from plugin_sdk.core import ToolCall

    tool = SpawnDetachedTaskTool()
    result = await tool.execute(
        ToolCall(id="c1", name="SpawnDetachedTask", arguments={"prompt": ""})
    )
    assert result.is_error
    assert "prompt" in result.content.lower()


@pytest.mark.asyncio
async def test_spawn_tool_rejects_invalid_notify_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.tools.spawn_detached_task import SpawnDetachedTaskTool
    from plugin_sdk.core import ToolCall

    tool = SpawnDetachedTaskTool()
    result = await tool.execute(
        ToolCall(
            id="c1", name="SpawnDetachedTask",
            arguments={"prompt": "x", "notify_policy": "weird-mode"},
        )
    )
    assert result.is_error
    assert "notify_policy" in result.content


# ──────────────────────────── CLI ────────────────────────────


_runner = CliRunner()


def test_cli_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app
    result = _runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0
    assert "No detached tasks" in result.stdout


def test_cli_list_renders_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    store = TaskStore(tmp_path / "sessions.db")
    store.create(prompt="research market")
    from opencomputer.cli import app
    result = _runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0
    assert "research market" in result.stdout
    assert "queued" in result.stdout


def test_cli_show_existing_task(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    store = TaskStore(tmp_path / "sessions.db")
    task = store.create(prompt="hi")
    store.mark_running(task.id)
    store.complete(task.id, "the result")
    from opencomputer.cli import app
    result = _runner.invoke(app, ["task", "show", task.id])
    assert result.exit_code == 0
    assert "done" in result.stdout
    assert "the result" in result.stdout


def test_cli_show_unknown_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    TaskStore(tmp_path / "sessions.db")  # init the DB
    from opencomputer.cli import app
    result = _runner.invoke(app, ["task", "show", "no-such-id"])
    assert result.exit_code != 0


def test_cli_cancel_changes_status(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    store = TaskStore(tmp_path / "sessions.db")
    task = store.create(prompt="hi")
    from opencomputer.cli import app
    result = _runner.invoke(app, ["task", "cancel", task.id])
    assert result.exit_code == 0
    assert store.get(task.id).status == "cancelled"
