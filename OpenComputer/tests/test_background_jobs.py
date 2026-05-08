"""Tests for the /background slash command + BackgroundJobRegistry.

Two layers:
1. Registry-only — submit/list/show/eviction/error semantics with a fake factory.
2. Slash-command level — `/background ...` parsing + dispatch via SlashCommand.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.background_jobs import (
    BackgroundJob,
    BackgroundJobRegistry,
    get_default_registry,
    reset_for_tests,
)
from opencomputer.agent.slash_commands_impl.background_cmd import BackgroundCommand
from plugin_sdk.runtime_context import RuntimeContext

# ─── Registry tests ──────────────────────────────────────────────────


def _fake_factory(*, final_text: str = "result", iterations: int = 3, sid: str = "sess-x"):
    """Return a factory callable that produces a mock loop returning final_text."""
    def _make():
        loop_obj = MagicMock()
        result_obj = MagicMock(
            final_message=MagicMock(content=final_text),
            iterations=iterations,
            session_id=sid,
        )
        loop_obj.run_conversation = AsyncMock(return_value=result_obj)
        return loop_obj
    return _make


@pytest.fixture(autouse=True)
def _clear_default_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def test_submit_without_factory_raises():
    reg = BackgroundJobRegistry()
    with pytest.raises(RuntimeError, match="no AgentLoop factory"):
        reg.submit("hi")


def test_submit_empty_prompt_raises():
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory())
    with pytest.raises(ValueError, match="empty"):
        reg.submit("")


def test_submit_returns_short_job_id_and_records_pending():
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory())
    jid = reg.submit("hello world")
    assert isinstance(jid, str)
    assert 8 <= len(jid) <= 16
    snap = reg.get(jid)
    assert snap is not None
    # Right after submit the worker may still be pending or already running.
    assert snap.status in ("pending", "running", "complete")
    assert snap.prompt == "hello world"


def test_completed_job_has_result_and_metadata():
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory(final_text="background reply", iterations=5, sid="abc"))
    jid = reg.submit("do work")
    # Poll briefly for the worker thread to finish — fake factory is fast.
    for _ in range(100):
        snap = reg.get(jid)
        if snap is not None and snap.status == "complete":
            break
        time.sleep(0.01)
    snap = reg.get(jid)
    assert snap is not None and snap.status == "complete"
    assert snap.result == "background reply"
    assert snap.iterations == 5
    assert snap.session_id == "abc"
    assert snap.error is None
    assert snap.completed_at is not None


def test_failed_job_captures_error():
    reg = BackgroundJobRegistry()

    def _failing_factory():
        loop_obj = MagicMock()
        loop_obj.run_conversation = AsyncMock(side_effect=RuntimeError("boom"))
        return loop_obj

    reg.set_factory(_failing_factory)
    jid = reg.submit("doomed")
    for _ in range(100):
        snap = reg.get(jid)
        if snap is not None and snap.status == "error":
            break
        time.sleep(0.01)
    snap = reg.get(jid)
    assert snap is not None and snap.status == "error"
    assert "RuntimeError" in (snap.error or "")
    assert snap.result is None


def test_list_recent_orders_newest_first():
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory())
    j1 = reg.submit("first")
    time.sleep(0.01)
    j2 = reg.submit("second")
    time.sleep(0.05)
    listed = reg.list_recent(limit=10)
    ids = [j.job_id for j in listed]
    # Newest first: j2 should come before j1.
    assert ids.index(j2) < ids.index(j1)


def test_get_unknown_id_returns_none():
    reg = BackgroundJobRegistry()
    assert reg.get("nope") is None


def test_eviction_drops_completed_when_full():
    """When the registry is at capacity, completed jobs get evicted to make room."""
    reg = BackgroundJobRegistry(max_jobs=3)
    reg.set_factory(_fake_factory())
    ids = []
    for i in range(3):
        ids.append(reg.submit(f"job {i}"))
    # Wait for all to complete.
    for _ in range(200):
        if all((reg.get(i) or BackgroundJob("", "", "running", 0)).status in ("complete", "error") for i in ids):
            break
        time.sleep(0.01)
    # Submit one more — should evict the oldest completed.
    new_id = reg.submit("job 3")
    assert reg.get(ids[0]) is None  # evicted
    assert reg.get(new_id) is not None


# ─── Slash command tests ─────────────────────────────────────────────


def _run_slash(args: str, runtime: RuntimeContext | None = None) -> str:
    cmd = BackgroundCommand()
    runtime = runtime or RuntimeContext()
    result = asyncio.run(cmd.execute(args, runtime))
    return result.output


def test_slash_no_args_shows_help():
    out = _run_slash("")
    assert "/background" in out
    assert "list" in out
    assert "show" in out


def test_slash_start_without_factory_returns_error():
    # Default registry has no factory after reset_for_tests.
    out = _run_slash("hello")
    assert "factory not registered" in out


def test_slash_start_submits_and_returns_job_id():
    reg = get_default_registry()
    reg.set_factory(_fake_factory(final_text="bg-result"))
    out = _run_slash("research that paper")
    assert "started background job" in out
    # Wait for completion, then list shows the job.
    for _ in range(100):
        if reg.list_recent(1) and reg.list_recent(1)[0].status == "complete":
            break
        time.sleep(0.01)
    assert reg.list_recent(1)[0].status == "complete"


def test_slash_explicit_start_keyword():
    reg = get_default_registry()
    reg.set_factory(_fake_factory())
    out = _run_slash("start go check the deploy")
    assert "started background job" in out
    jobs = reg.list_recent(1)
    assert jobs[0].prompt == "go check the deploy"


def test_slash_list_with_no_jobs():
    out = _run_slash("list")
    assert "no background jobs" in out


def test_slash_list_renders_jobs():
    reg = get_default_registry()
    reg.set_factory(_fake_factory(final_text="ok"))
    reg.submit("one")
    reg.submit("two")
    # Wait for both to finish so the rendered status is stable.
    for _ in range(100):
        recent = reg.list_recent(2)
        if len(recent) == 2 and all(j.status == "complete" for j in recent):
            break
        time.sleep(0.01)
    out = _run_slash("list")
    assert "recent background jobs" in out
    # Both prompts should appear in the list output.
    assert "one" in out
    assert "two" in out


def test_slash_show_missing_id():
    out = _run_slash("show")
    assert "missing job id" in out


def test_slash_show_unknown_id():
    out = _run_slash("show ffffffff")
    assert "no job with id" in out


def test_slash_show_renders_complete_result():
    reg = get_default_registry()
    reg.set_factory(_fake_factory(final_text="final answer", iterations=2, sid="sess123abc456"))
    jid = reg.submit("explain X")
    for _ in range(100):
        snap = reg.get(jid)
        if snap and snap.status == "complete":
            break
        time.sleep(0.01)
    out = _run_slash(f"show {jid}")
    assert "status=complete" in out
    assert "final answer" in out
    assert "iters=2" in out
    assert "sess123abc456"[:12] in out


def test_slash_plan_mode_propagates_into_job():
    reg = get_default_registry()
    captured_runtimes: list = []

    def _capture_factory():
        loop_obj = MagicMock()

        async def _capture(prompt, runtime=None):
            captured_runtimes.append(runtime)
            return MagicMock(final_message=MagicMock(content="ok"), iterations=1, session_id="s")

        loop_obj.run_conversation = AsyncMock(side_effect=_capture)
        return loop_obj

    reg.set_factory(_capture_factory)
    runtime = RuntimeContext(plan_mode=True)
    out = _run_slash("read the spec carefully", runtime=runtime)
    assert "started" in out
    for _ in range(100):
        if captured_runtimes:
            break
        time.sleep(0.01)
    assert captured_runtimes
    assert captured_runtimes[0].plan_mode is True


def test_completion_notifier_called_on_success():
    """Notifier is invoked synchronously from the worker thread when a job completes."""
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory(final_text="bg-result"))
    captured: list = []

    def _notifier(job):
        captured.append(job)

    reg.set_completion_notifier(_notifier)
    jid = reg.submit("hi", parent_session_id="parent-123")
    for _ in range(100):
        if captured:
            break
        time.sleep(0.01)
    assert len(captured) == 1
    job = captured[0]
    assert job.job_id == jid
    assert job.status == "complete"
    assert job.result == "bg-result"
    assert job.parent_session_id == "parent-123"


def test_completion_notifier_called_on_error():
    """Notifier fires for error states too — recipient should be able to render the failure."""
    reg = BackgroundJobRegistry()

    def _failing_factory():
        loop_obj = MagicMock()
        loop_obj.run_conversation = AsyncMock(side_effect=ValueError("nope"))
        return loop_obj

    reg.set_factory(_failing_factory)
    seen: list = []
    reg.set_completion_notifier(lambda j: seen.append(j))
    reg.submit("doomed")
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)
    assert len(seen) == 1
    assert seen[0].status == "error"
    assert "ValueError" in (seen[0].error or "")


def test_completion_notifier_exception_swallowed():
    """A misbehaving notifier must not tear down the worker thread."""
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory(final_text="ok"))

    def _bad_notifier(job):
        raise RuntimeError("notifier blew up")

    reg.set_completion_notifier(_bad_notifier)
    jid = reg.submit("hi")
    for _ in range(100):
        snap = reg.get(jid)
        if snap and snap.status == "complete":
            break
        time.sleep(0.01)
    # Job still finished cleanly even though the notifier raised.
    snap = reg.get(jid)
    assert snap is not None and snap.status == "complete"
    assert snap.result == "ok"


def test_parent_session_id_recorded_on_job():
    reg = BackgroundJobRegistry()
    reg.set_factory(_fake_factory())
    jid = reg.submit("research X", parent_session_id="parent-abc")
    snap = reg.get(jid)
    assert snap is not None and snap.parent_session_id == "parent-abc"


def test_time_based_retention_prunes_old_completed_jobs():
    """Jobs older than retain_seconds get pruned on next submit."""
    reg = BackgroundJobRegistry(retain_seconds=0.1)
    reg.set_factory(_fake_factory())
    j_old = reg.submit("old job")
    for _ in range(100):
        snap = reg.get(j_old)
        if snap and snap.status == "complete":
            break
        time.sleep(0.01)
    # Wait past the retention window, then submit a new job.
    time.sleep(0.15)
    j_new = reg.submit("new job")
    # The old job should now be gone; the new one present.
    assert reg.get(j_old) is None
    assert reg.get(j_new) is not None


def test_retention_disabled_when_zero():
    """retain_seconds=0 disables pruning entirely."""
    reg = BackgroundJobRegistry(retain_seconds=0)
    reg.set_factory(_fake_factory())
    j_old = reg.submit("old job")
    for _ in range(100):
        snap = reg.get(j_old)
        if snap and snap.status == "complete":
            break
        time.sleep(0.01)
    time.sleep(0.05)
    reg.submit("new")
    # Old job survives.
    assert reg.get(j_old) is not None


def test_running_jobs_never_pruned():
    """Pruning never removes still-running jobs even if their started_at is ancient."""
    reg = BackgroundJobRegistry(retain_seconds=0.01)

    # Slow factory keeps the job in a running state.
    def _slow_factory():
        loop_obj = MagicMock()

        async def _slow(prompt, runtime=None):
            await asyncio.sleep(0.5)
            return MagicMock(final_message=MagicMock(content="late"), iterations=1, session_id="s")

        loop_obj.run_conversation = AsyncMock(side_effect=_slow)
        return loop_obj

    reg.set_factory(_slow_factory)
    jid = reg.submit("running-job")
    time.sleep(0.05)
    # Submit another job — pruning runs but the running one survives.
    reg.submit("trigger pruning")
    snap = reg.get(jid)
    assert snap is not None
    assert snap.status in ("pending", "running")


def test_slash_command_passes_session_id_from_runtime():
    """The slash command should capture runtime.custom['session_id'] and pass it through."""
    reg = get_default_registry()
    reg.set_factory(_fake_factory())
    runtime = RuntimeContext(custom={"session_id": "fg-session-abc"})
    out = _run_slash("research", runtime=runtime)
    assert "started" in out
    jobs = reg.list_recent(1)
    assert jobs[0].parent_session_id == "fg-session-abc"


def test_format_background_completion_text_complete():
    from opencomputer.agent.background_jobs import BackgroundJob as Bj
    from opencomputer.gateway.dispatch import _format_background_completion_text

    job = Bj(
        job_id="ab12",
        prompt="explain X clearly",
        status="complete",
        started_at=0,
        result="here is the explanation",
    )
    out = _format_background_completion_text(job)
    assert "✓ background ab12 done" in out
    assert "explain X clearly" in out
    assert "here is the explanation" in out


def test_format_background_completion_text_error():
    from opencomputer.agent.background_jobs import BackgroundJob as Bj
    from opencomputer.gateway.dispatch import _format_background_completion_text

    job = Bj(
        job_id="cd34",
        prompt="oops",
        status="error",
        started_at=0,
        error="RuntimeError: nope",
    )
    out = _format_background_completion_text(job)
    assert "✗ background cd34 failed" in out
    assert "RuntimeError" in out


def test_dispatch_notifier_skips_when_no_parent_session():
    """Notifier with a job that has no parent_session_id should no-op silently."""
    from opencomputer.agent.background_jobs import BackgroundJob as Bj

    # Build a minimal Dispatch standin — we only need the methods the notifier touches.
    from opencomputer.gateway.dispatch import Dispatch

    fake_loop_obj = MagicMock()
    fake_loop_obj.config = MagicMock()
    dispatch = Dispatch.__new__(Dispatch)
    dispatch._session_channels = {}
    dispatch._main_loop = None  # not bound — should still no-op safely

    job = Bj(job_id="x", prompt="p", status="complete", started_at=0, result="r")
    # No parent_session_id → early return, no exception.
    dispatch.background_completion_notifier(job)


def test_dispatch_notifier_skips_when_no_binding():
    """Notifier with parent_session_id that isn't in _session_channels should no-op."""
    from opencomputer.agent.background_jobs import BackgroundJob as Bj
    from opencomputer.gateway.dispatch import Dispatch

    dispatch = Dispatch.__new__(Dispatch)
    dispatch._session_channels = {}  # empty
    dispatch._main_loop = None

    job = Bj(
        job_id="y",
        prompt="p",
        status="complete",
        started_at=0,
        result="r",
        parent_session_id="unknown-session",
    )
    # No binding → early return, no exception.
    dispatch.background_completion_notifier(job)


def test_slash_command_registered_as_builtin():
    """Ensure /background is registered into the global slash registry."""
    from opencomputer.agent.slash_commands import (
        _BUILTIN_COMMANDS,
        register_builtin_slash_commands,
    )
    from opencomputer.plugins.registry import registry as _plugin_registry

    assert BackgroundCommand in _BUILTIN_COMMANDS
    register_builtin_slash_commands()  # must be idempotent

    # And resolves by name through the plugin registry's slash_commands map.
    cmd = _plugin_registry.slash_commands.get("background")
    assert cmd is not None, "/background should be registered after register_builtin_slash_commands()"
    assert isinstance(cmd, BackgroundCommand)
    # /bg alias should resolve to the same instance.
    assert _plugin_registry.slash_commands.get("bg") is cmd
