"""Round 2B P-8 — background-process auto-notifications.

Coverage:
    (a) clean exit fires a Notification with the expected payload shape
    (b) error exit fires a Notification with the non-zero exit_code
    (c) the default subscriber appends the formatted system message to
        the per-session pending store
    (d) hook firing is fire-and-forget — a raising subscriber doesn't
        crash the watcher (and a second well-behaved subscriber still
        runs)

The first two cases use real subprocesses (cheap shell commands) plus
the real hook engine wired with a recording subscriber so we exercise
the StartProcessTool → watcher → engine → subscriber pipeline end to
end. Case (c) tests the format contract directly. Case (d) injects a
deliberately-failing subscriber alongside a recording one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from opencomputer.agent import bg_notify
from opencomputer.hooks.engine import HookEngine
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent, HookSpec

# ─── Helpers (mirrors test_phase6b.py loader) ──────────────────────────


def _load_bg_module(name: str):
    """Load tools/background.py with a unique module cache name per call."""
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "coding-harness" / "tools" / "background.py"
    spec = importlib.util.spec_from_file_location(f"bg_p8_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"bg_p8_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(tool_name: str, **args):
    return ToolCall(id=f"call-{tool_name}-1", name=tool_name, arguments=args)


@pytest.fixture(autouse=True)
def _reset_bg_state():
    """Each test runs with a clean pending store + default session id provider.

    bg_notify uses module-level globals (lock-protected) so cross-test
    bleed is real if we don't reset. The provider is restored after the
    test so subsequent tests in the wider suite don't pick up stale state.
    """
    bg_notify.reset_pending()
    saved_provider = bg_notify._session_id_provider  # noqa: SLF001
    yield
    bg_notify.reset_pending()
    bg_notify._session_id_provider = saved_provider  # noqa: SLF001


# ─── (a) clean exit fires Notification ─────────────────────────────────


def test_clean_exit_fires_notification_with_payload(monkeypatch):
    """A successful bg process triggers a Notification with payload fields."""
    mod = _load_bg_module("clean_exit")
    bg_notify.set_session_id_provider(lambda: "sess-clean")

    received: list[HookContext] = []

    async def recorder(ctx: HookContext):
        received.append(ctx)
        return None

    # Use a private engine + monkey-patch the global engine reference the
    # watcher imports inside _watch_and_notify.
    engine = HookEngine()
    engine.register(
        HookSpec(event=HookEvent.NOTIFICATION, handler=recorder)
    )
    monkeypatch.setattr("opencomputer.hooks.engine.engine", engine)

    async def lifecycle():
        start = mod.StartProcessTool()
        # Tag the call so the payload carries a deterministic id.
        call = ToolCall(id="bg-call-clean", name="StartProcess",
                        arguments={"command": "echo first && echo second"})
        r = await start.execute(call)
        assert not r.is_error, r.content
        pid = int(r.content.split("pid=")[1].split(")")[0])

        entry = mod._PROCESSES[pid]
        # Wait for both the proc and the notify watcher (which itself
        # awaits proc + drain tasks then fires the hook).
        assert entry.notify_task is not None
        await asyncio.wait_for(entry.notify_task, timeout=5.0)

        # Give the fire-and-forget hook task one event-loop iteration to
        # actually invoke the recorder.
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)

        # Cleanup the entry so subsequent tests don't see leftover state.
        mod._PROCESSES.pop(pid, None)

    asyncio.run(lifecycle())

    assert len(received) == 1, f"expected 1 notification, got {len(received)}"
    ctx = received[0]
    assert ctx.event is HookEvent.NOTIFICATION
    assert ctx.session_id == "sess-clean"

    payload = bg_notify.decode_payload(ctx)
    assert payload is not None, "payload failed to decode"
    assert payload.tool_call_id == "bg-call-clean"
    assert payload.exit_code == 0
    assert "second" in payload.tail_stdout  # last line should be present
    assert payload.tail_stderr == ""
    assert payload.duration_seconds >= 0.0


# ─── (b) error exit fires Notification with non-zero exit_code ────────


def test_error_exit_fires_notification_with_nonzero_exit_code(monkeypatch):
    """A bg proc that exits non-zero is reported with the right code."""
    mod = _load_bg_module("err_exit")
    bg_notify.set_session_id_provider(lambda: "sess-err")

    received: list[HookContext] = []

    async def recorder(ctx: HookContext):
        received.append(ctx)
        return None

    engine = HookEngine()
    engine.register(
        HookSpec(event=HookEvent.NOTIFICATION, handler=recorder)
    )
    monkeypatch.setattr("opencomputer.hooks.engine.engine", engine)

    async def lifecycle():
        start = mod.StartProcessTool()
        call = ToolCall(
            id="bg-call-err",
            name="StartProcess",
            # emit one stderr line, then exit 7 — exits both buffers
            # cleanly so the watcher's drain step actually has something.
            arguments={"command": "echo boom 1>&2; exit 7"},
        )
        r = await start.execute(call)
        assert not r.is_error
        pid = int(r.content.split("pid=")[1].split(")")[0])

        entry = mod._PROCESSES[pid]
        assert entry.notify_task is not None
        await asyncio.wait_for(entry.notify_task, timeout=5.0)
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)
        mod._PROCESSES.pop(pid, None)

    asyncio.run(lifecycle())

    assert len(received) == 1
    payload = bg_notify.decode_payload(received[0])
    assert payload is not None
    assert payload.tool_call_id == "bg-call-err"
    assert payload.exit_code == 7
    assert "boom" in payload.tail_stderr
    assert payload.tail_stdout == ""


# ─── (c) default subscriber appends the system message ────────────────


def test_default_subscriber_appends_system_message():
    """The bg_notify default subscriber stashes the formatted body."""
    payload = bg_notify.BgProcessExit(
        session_id="sess-c",
        tool_call_id="abc-123",
        exit_code=0,
        tail_stdout="line one\nline two",
        tail_stderr="",
        duration_seconds=0.5,
    )
    ctx = bg_notify.make_hook_context(payload)
    spec = bg_notify.build_default_subscriber_spec()

    asyncio.run(spec.handler(ctx))

    pending = bg_notify.consume_pending("sess-c")
    assert len(pending) == 1
    body = pending[0]
    assert "[bg-process #abc-123 exited code=0]" in body
    assert "line two" in body  # tail goes into the body
    # Format pinned: drain consumes — second call returns empty.
    assert bg_notify.consume_pending("sess-c") == []


def test_default_subscriber_ignores_unrelated_notifications():
    """Notifications without the bg-exit marker are silently skipped."""
    from plugin_sdk.core import Message

    ctx = HookContext(
        event=HookEvent.NOTIFICATION,
        session_id="sess-other",
        message=Message(role="system", content="user-facing alert"),
    )
    spec = bg_notify.build_default_subscriber_spec()
    asyncio.run(spec.handler(ctx))
    assert bg_notify.consume_pending("sess-other") == []


def test_format_system_message_truncates_tail_to_100_chars():
    """The rendered tail in the system body is capped at 100 chars."""
    payload = bg_notify.BgProcessExit(
        session_id="sess-fmt",
        tool_call_id="tid",
        exit_code=1,
        # Use a distinctive marker that doesn't collide with any header
        # text — '~' is not used anywhere in format_system_message.
        tail_stdout="~" * 500,
        tail_stderr="",
        duration_seconds=0.0,
    )
    body = bg_notify.format_system_message(payload)
    # Trailing chars must be exactly 100 '~'.
    assert body.endswith("~" * 100)
    prefix, _, tail = body.rpartition("tail: ")
    # Header chunk holds zero '~' (only the truncated tail does).
    assert "~" not in prefix
    assert len(tail) == 100


# ─── (d) hook is fire-and-forget — subscriber error doesn't crash ─────


def test_subscriber_error_does_not_crash_watcher(monkeypatch):
    """A subscriber that raises must not prevent other subscribers from running.

    Reproduces the "fire-and-forget" contract: the watcher fires the hook,
    one subscriber raises, a second subscriber still records the context,
    and the watcher itself returns cleanly. Without this guarantee a
    third-party Notification handler crash could silently lose every
    bg-process exit notification.
    """
    mod = _load_bg_module("ff")
    bg_notify.set_session_id_provider(lambda: "sess-ff")

    received: list[HookContext] = []

    async def boom(ctx: HookContext):
        raise RuntimeError("simulated subscriber failure")

    async def recorder(ctx: HookContext):
        received.append(ctx)
        return None

    engine = HookEngine()
    # Boom registered first so it runs first under FIFO same-priority
    # ordering — proves the recorder still fires after a peer raises.
    engine.register(HookSpec(event=HookEvent.NOTIFICATION, handler=boom))
    engine.register(HookSpec(event=HookEvent.NOTIFICATION, handler=recorder))
    monkeypatch.setattr("opencomputer.hooks.engine.engine", engine)

    async def lifecycle():
        start = mod.StartProcessTool()
        call = ToolCall(id="bg-call-ff", name="StartProcess",
                        arguments={"command": "true"})
        r = await start.execute(call)
        assert not r.is_error
        pid = int(r.content.split("pid=")[1].split(")")[0])

        entry = mod._PROCESSES[pid]
        assert entry.notify_task is not None
        # The watcher itself must finish successfully (no exception).
        await asyncio.wait_for(entry.notify_task, timeout=5.0)
        assert entry.notify_task.exception() is None

        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)
        mod._PROCESSES.pop(pid, None)

    asyncio.run(lifecycle())

    assert len(received) == 1, "well-behaved subscriber should still fire"
    payload = bg_notify.decode_payload(received[0])
    assert payload is not None
    assert payload.exit_code == 0


# ─── Pending-store mechanics ──────────────────────────────────────────


def test_consume_pending_pops_and_clears():
    """consume_pending drains AND clears the per-session list."""
    bg_notify.add_pending("s1", "msg-a")
    bg_notify.add_pending("s1", "msg-b")
    bg_notify.add_pending("s2", "msg-c")
    assert bg_notify.has_pending("s1")
    out = bg_notify.consume_pending("s1")
    assert out == ["msg-a", "msg-b"]
    assert not bg_notify.has_pending("s1")
    assert bg_notify.consume_pending("s2") == ["msg-c"]


def test_add_pending_drops_empty_inputs():
    """Empty session_id or empty text is silently dropped."""
    bg_notify.add_pending("", "msg")
    bg_notify.add_pending("s", "")
    assert bg_notify.consume_pending("s") == []


def test_drain_for_session_is_alias_for_consume():
    """drain_for_session matches consume_pending output (loop-side seam)."""
    bg_notify.add_pending("s-drain", "x")
    assert bg_notify.drain_for_session("s-drain") == ["x"]
    assert bg_notify.consume_pending("s-drain") == []


# ─── decode_payload defenses ──────────────────────────────────────────


def test_decode_payload_returns_none_on_unrelated_event():
    from plugin_sdk.core import Message

    ctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="x",
        message=Message(
            role="system",
            content='{"tool_call_id":"t","exit_code":0}',
            name=bg_notify.BG_PROCESS_EXIT_MARKER,
        ),
    )
    assert bg_notify.decode_payload(ctx) is None


def test_decode_payload_returns_none_on_marker_mismatch():
    from plugin_sdk.core import Message

    ctx = HookContext(
        event=HookEvent.NOTIFICATION,
        session_id="x",
        message=Message(role="system", content="any text"),
    )
    assert bg_notify.decode_payload(ctx) is None


def test_decode_payload_returns_none_on_malformed_json():
    from plugin_sdk.core import Message

    ctx = HookContext(
        event=HookEvent.NOTIFICATION,
        session_id="x",
        message=Message(
            role="system",
            content="not-json",
            name=bg_notify.BG_PROCESS_EXIT_MARKER,
        ),
    )
    assert bg_notify.decode_payload(ctx) is None


# ─── tail_chars helper ────────────────────────────────────────────────


def test_tail_chars_returns_last_n_after_join():
    out = bg_notify.tail_chars(["a" * 150, "b" * 150], limit=200)
    assert len(out) == 200
    assert out.endswith("b" * 150)


def test_tail_chars_returns_full_text_when_under_limit():
    out = bg_notify.tail_chars(["short", "lines"], limit=200)
    assert out == "short\nlines"


def test_tail_chars_handles_empty_input():
    assert bg_notify.tail_chars([], limit=200) == ""
