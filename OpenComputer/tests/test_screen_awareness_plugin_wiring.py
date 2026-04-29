"""Tests that the screen-awareness plugin's register(api) wires the
tool, hooks, and injection provider when state.enabled=True."""
from __future__ import annotations

from pathlib import Path
from unittest import mock


def test_register_disabled_by_default(tmp_path: Path):
    """Default state file (or missing file) -> enabled=False -> register
    is a no-op (privacy-first)."""
    from extensions.screen_awareness.plugin import register

    api = mock.MagicMock()
    api.profile_home = tmp_path
    register(api)
    api.register_tool.assert_not_called()
    api.register_injection_provider.assert_not_called()
    api.register_hook.assert_not_called()


def test_register_when_enabled_wires_tool_and_provider(tmp_path: Path):
    from extensions.screen_awareness.plugin import register
    from extensions.screen_awareness.state import (
        ScreenAwarenessState,
        save_state,
    )

    save_state(tmp_path, ScreenAwarenessState(enabled=True))
    api = mock.MagicMock()
    api.profile_home = tmp_path
    register(api)
    api.register_tool.assert_called_once()
    api.register_injection_provider.assert_called_once()
    # Hooks: BEFORE_MESSAGE_WRITE + PRE_TOOL_USE + POST_TOOL_USE + TRANSFORM_TOOL_RESULT
    assert api.register_hook.call_count == 4


def test_registered_tool_is_recall_screen(tmp_path: Path):
    from extensions.screen_awareness.plugin import register
    from extensions.screen_awareness.recall_tool import RecallScreenTool
    from extensions.screen_awareness.state import (
        ScreenAwarenessState,
        save_state,
    )

    save_state(tmp_path, ScreenAwarenessState(enabled=True))
    api = mock.MagicMock()
    api.profile_home = tmp_path
    register(api)
    tool_arg = api.register_tool.call_args[0][0]
    assert isinstance(tool_arg, RecallScreenTool)


def test_registered_provider_is_screen_context(tmp_path: Path):
    from extensions.screen_awareness.injection_provider import ScreenContextProvider
    from extensions.screen_awareness.plugin import register
    from extensions.screen_awareness.state import (
        ScreenAwarenessState,
        save_state,
    )

    save_state(tmp_path, ScreenAwarenessState(enabled=True))
    api = mock.MagicMock()
    api.profile_home = tmp_path
    register(api)
    provider_arg = api.register_injection_provider.call_args[0][0]
    assert isinstance(provider_arg, ScreenContextProvider)


def test_registered_hooks_use_async_handlers(tmp_path: Path):
    """Verify HookSpec.handler is async — not a sync callback."""
    import asyncio

    from extensions.screen_awareness.plugin import register
    from extensions.screen_awareness.state import (
        ScreenAwarenessState,
        save_state,
    )

    save_state(tmp_path, ScreenAwarenessState(enabled=True))
    api = mock.MagicMock()
    api.profile_home = tmp_path
    register(api)
    for call in api.register_hook.call_args_list:
        spec = call[0][0]
        # HookSpec.handler must be async
        assert asyncio.iscoroutinefunction(spec.handler), (
            f"hook {spec.event} handler must be async"
        )
