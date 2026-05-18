"""System-A → System-B built-in command bridge (Recipe 2).

``oc chat`` dispatches through System B (``cli_ui/slash``); the
gateway/wire/ACP path dispatches through System A
(``agent/slash_commands``). They drifted — many System-A commands had
no System-B ``CommandDef`` and could not be typed in chat.

``sync_builtin_commands`` surfaces System-A commands in System B's
registry; :func:`dispatch_slash` falls through to ``on_builtin_dispatch``
for any command without a native System-B handler.
"""
from __future__ import annotations

import pytest

from opencomputer.cli_ui import slash, slash_handlers
from opencomputer.cli_ui.slash import (
    CommandDef,
    register_extra_commands,
    resolve_command,
)
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    dispatch_slash,
    sync_builtin_commands,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    reg = list(slash.SLASH_REGISTRY)
    lookup = dict(slash._LOOKUP)
    handlers = dict(slash_handlers._HANDLERS)
    yield
    slash.SLASH_REGISTRY[:] = reg
    slash._LOOKUP.clear()
    slash._LOOKUP.update(lookup)
    slash_handlers._HANDLERS.clear()
    slash_handlers._HANDLERS.update(handlers)


class _FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.lines.append(" ".join(str(a) for a in args))


def _ctx(bridge) -> SlashContext:  # noqa: ANN001
    return SlashContext(
        console=_FakeConsole(),
        session_id="s1",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=dict,
        get_session_list=list,
        on_builtin_dispatch=bridge,
    )


# ── dispatch fallthrough ─────────────────────────────────────────────


def test_unknown_command_falls_through_to_bridge() -> None:
    calls: list[tuple[str, str]] = []

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        calls.append((name, args))
        return (True, "bridged output")

    ctx = _ctx(_bridge)
    result = dispatch_slash("/copy hello world", ctx)
    assert result.handled is True
    assert calls == [("copy", "hello world")]
    assert "bridged output" in ctx.console.lines  # type: ignore[attr-defined]


def test_commanddef_without_handler_falls_through() -> None:
    """A CommandDef registered for /help discoverability but with no
    _HANDLERS entry must bridge, not KeyError-crash."""
    register_extra_commands([CommandDef(name="sethome", description="x")])
    assert "sethome" not in slash_handlers._HANDLERS

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        return (True, f"ran {name}")

    ctx = _ctx(_bridge)
    result = dispatch_slash("/sethome --list", ctx)
    assert result.handled is True
    assert "ran sethome" in ctx.console.lines  # type: ignore[attr-defined]


def test_native_handler_wins_over_bridge() -> None:
    """A command with a native System-B handler must not hit the bridge."""
    bridged: list[str] = []

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        bridged.append(name)
        return (True, "")

    ctx = _ctx(_bridge)
    dispatch_slash("/help", ctx)
    assert bridged == []  # /help has a native handler


def test_bridge_miss_reports_unknown_command() -> None:
    def _bridge(name: str, args: str) -> tuple[bool, str]:
        return (False, "")

    ctx = _ctx(_bridge)
    result = dispatch_slash("/totally-not-real", ctx)
    assert result.handled is True
    assert any(
        "unknown command" in line
        for line in ctx.console.lines  # type: ignore[attr-defined]
    )


def test_bridge_receives_canonical_lowercase_name() -> None:
    """F1 (review followup) — ``resolve_command`` canonicalises but the
    System-A registry is keyed lowercase. If ``dispatch_slash`` passed
    the raw mixed-case ``name`` to ``on_builtin_dispatch`` the bridge
    would miss every ``/COPY``-style command."""
    captured: list[str] = []

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        captured.append(name)
        return (True, "")

    register_extra_commands([CommandDef(name="copy", description="x")])
    assert "copy" not in slash_handlers._HANDLERS  # bridges, not native

    ctx = _ctx(_bridge)
    dispatch_slash("/COPY hello", ctx)
    assert captured == ["copy"]


def test_bridge_output_printed_without_markup_parsing() -> None:
    """F2 (review followup) — System-A command output is plain text and
    may contain stray ``[brackets]`` (file lists, exception reprs).
    ``ctx.console.print(output)`` MUST pass ``markup=False`` or Rich
    will try to parse ``[done]`` as a markup tag and either error or
    drop the bracketed text."""
    class _SpyConsole:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple, dict]] = []

        def print(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.calls.append((args, kwargs))

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        return (True, "result: [done]")

    spy = _SpyConsole()
    register_extra_commands([CommandDef(name="status", description="x")])
    ctx = SlashContext(
        console=spy,
        session_id="s1",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=dict,
        get_session_list=list,
        on_builtin_dispatch=_bridge,
    )
    dispatch_slash("/status", ctx)
    # The bridge-output print must be ``markup=False`` keyword arg
    # AND the literal string with brackets must be the first arg.
    output_calls = [c for c in spy.calls if c[0] and "result:" in str(c[0][0])]
    assert output_calls, f"expected an output print, got {spy.calls!r}"
    args, kwargs = output_calls[0]
    assert args[0] == "result: [done]"
    assert kwargs.get("markup") is False, (
        f"bridge output must use markup=False; got kwargs={kwargs!r}"
    )


def test_pending_close_pattern_suppresses_runtime_warning() -> None:
    """F3 (review followup) — ``cli._on_builtin_dispatch`` binds the
    dispatch coroutine to a local and ``.close()``-es it on the
    RuntimeError fallback path. Without that, ``asyncio.run`` raising
    before awaiting (e.g. "loop is already running" in a sub-event-loop
    context) leaves an unawaited coroutine that emits
    ``RuntimeWarning: coroutine '_bi_dispatch' was never awaited`` at
    GC and leaks its frames.

    The closure itself is nested in cli.py and resists direct unit
    invocation; this test exercises the *pattern* used by the fix —
    matches ``slash_commands.try_dispatch_agent_slash:295``.
    """
    import gc
    import warnings

    async def _fake_dispatch():
        return None

    # WITH the fix: bind, try/except, close-on-fallback.
    pending = _fake_dispatch()
    try:
        raise RuntimeError("synthetic: emulate asyncio.run loop-already-running")
    except RuntimeError:
        pending.close()

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        del pending
        gc.collect()
        never_awaited = [
            w for w in captured if "never awaited" in str(w.message)
        ]

    assert not never_awaited, (
        "pending.close() must suppress 'coroutine was never awaited'; got: "
        f"{[str(w.message) for w in never_awaited]!r}"
    )


# ── sync_builtin_commands ────────────────────────────────────────────


def test_sync_surfaces_system_a_only_command() -> None:
    """`/copy` (System-A CopyCommand) has no native System-B CommandDef
    before sync, and resolves after."""
    assert resolve_command("copy") is None
    synced = sync_builtin_commands()
    assert "copy" in synced
    assert resolve_command("copy") is not None


def test_sync_skips_command_already_native_via_alias() -> None:
    """System-A `title` collides with System-B `/rename`'s alias and
    must not be re-registered."""
    synced = sync_builtin_commands()
    assert "title" not in synced
    # /title still resolves — to the original /rename CommandDef.
    resolved = resolve_command("title")
    assert resolved is not None
    assert resolved.name == "rename"


def test_sync_is_idempotent() -> None:
    sync_builtin_commands()
    after_first = len(slash.SLASH_REGISTRY)
    sync_builtin_commands()
    assert len(slash.SLASH_REGISTRY) == after_first


def test_synced_command_has_no_native_handler_so_it_bridges() -> None:
    """A synced command must dispatch through the bridge, not _HANDLERS."""
    sync_builtin_commands()
    assert resolve_command("copy") is not None
    assert "copy" not in slash_handlers._HANDLERS

    seen: list[str] = []

    def _bridge(name: str, args: str) -> tuple[bool, str]:
        seen.append(name)
        return (True, "ok")

    dispatch_slash("/copy text", _ctx(_bridge))
    assert seen == ["copy"]
