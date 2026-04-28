"""PR #221 follow-up: ``PluginAPI._dispatch`` binding (Item 1).

The gateway constructs a ``Dispatch`` instance and must wire it onto the
shared :class:`PluginAPI` so plugin-side helpers (Discord ``/reset``,
future approval-callback plumbing) can reach the live per-chat lock map
without importing :mod:`opencomputer.gateway.dispatch` directly.

Without this binding, ``shared_api._dispatch is None`` and
``DiscordAdapter._reset_session``'s lock-clear branch silently no-ops —
leaving stuck locks until process restart.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.loop import AgentLoop
from opencomputer.gateway.dispatch import session_id_for
from opencomputer.gateway.server import Gateway
from opencomputer.plugins.loader import PluginAPI
from opencomputer.plugins.registry import registry as plugin_registry


# ---------------------------------------------------------------------------
# PluginAPI._bind_dispatch — the setter contract
# ---------------------------------------------------------------------------


def test_plugin_api_dispatch_defaults_to_none() -> None:
    """A fresh PluginAPI has _dispatch = None until the gateway binds it."""
    api = PluginAPI(
        tool_registry=SimpleNamespace(names=lambda: []),
        hook_engine=SimpleNamespace(_hooks={}),
        provider_registry={},
        channel_registry={},
    )
    assert api._dispatch is None


def test_bind_dispatch_assigns_and_is_idempotent() -> None:
    """``_bind_dispatch`` stores its argument and tolerates rebinding."""
    api = PluginAPI(
        tool_registry=SimpleNamespace(names=lambda: []),
        hook_engine=SimpleNamespace(_hooks={}),
        provider_registry={},
        channel_registry={},
    )
    sentinel_a = object()
    sentinel_b = object()
    api._bind_dispatch(sentinel_a)
    assert api._dispatch is sentinel_a
    api._bind_dispatch(sentinel_b)
    assert api._dispatch is sentinel_b


# ---------------------------------------------------------------------------
# Gateway.__init__ — wires Dispatch onto the shared api
# ---------------------------------------------------------------------------


def _fake_loop() -> AgentLoop:
    loop = MagicMock(spec=AgentLoop)
    loop.db = MagicMock()
    return loop


def test_gateway_binds_dispatch_onto_shared_api() -> None:
    """After Gateway(...) the shared_api._dispatch is the Dispatch instance."""
    # Seed a shared_api on the registry — emulates load_all having run.
    api = PluginAPI(
        tool_registry=SimpleNamespace(names=lambda: []),
        hook_engine=SimpleNamespace(_hooks={}),
        provider_registry={},
        channel_registry={},
    )
    plugin_registry.shared_api = api
    try:
        gw = Gateway(loop=_fake_loop())
        assert api._dispatch is gw.dispatch
    finally:
        # Don't leak shared_api into other tests.
        plugin_registry.shared_api = None


def test_gateway_with_no_shared_api_does_not_raise() -> None:
    """If plugins haven't loaded yet the binding is a no-op (not an error)."""
    plugin_registry.shared_api = None
    # Construction must not raise.
    gw = Gateway(loop=_fake_loop())
    assert gw.dispatch is not None


# ---------------------------------------------------------------------------
# Discord _reset_session lock-clear branch — now ACTIVE, not silent
# ---------------------------------------------------------------------------


def _load_discord_adapter():
    """Import the Discord adapter under a unique synthetic name (avoids
    sys.modules collision with sibling extension adapters)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "discord_adapter_dispatch_binding_test",
        Path(__file__).resolve().parent.parent
        / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_discord_reset_clears_dispatch_lock_when_bound() -> None:
    """The lock-clear branch must ACTIVATE when shared_api._dispatch is wired.

    Drives ``_reset_session`` directly with a stubbed SessionDB and a
    stubbed Dispatch whose ``_locks`` dict already contains the target
    session id. After the reset, the entry must be gone — proof the
    binding is reachable from inside the plugin.
    """
    mod = _load_discord_adapter()
    DiscordAdapter = mod.DiscordAdapter

    import os
    from unittest.mock import patch

    fake_client = MagicMock()
    fake_state = MagicMock()
    fake_state._command_tree = None
    fake_client._connection = fake_state
    with (
        patch.dict(os.environ, dict(os.environ), clear=True),
        patch("discord.Client", return_value=fake_client),
    ):
        a = DiscordAdapter(config={"bot_token": "fake-token"})

    db_mock = MagicMock()
    db_mock.end_session = MagicMock()
    a._session_db = db_mock

    sid = "deadbeef" * 4  # 32-char-ish placeholder — content doesn't matter.

    # Stub a Dispatch with a populated lock map.
    fake_lock = asyncio.Lock()
    fake_dispatch = SimpleNamespace(_locks={sid: fake_lock})

    api = PluginAPI(
        tool_registry=SimpleNamespace(names=lambda: []),
        hook_engine=SimpleNamespace(_hooks={}),
        provider_registry={},
        channel_registry={},
    )
    api._bind_dispatch(fake_dispatch)
    plugin_registry.shared_api = api
    try:
        msg = a._reset_session(sid)
    finally:
        plugin_registry.shared_api = None

    # end_session was called with the target sid.
    db_mock.end_session.assert_called_once_with(sid)
    # The lock entry is GONE — the binding actually took effect.
    assert sid not in fake_dispatch._locks
    # And the user-facing reply mentions reset.
    assert "reset" in msg.lower()


def test_discord_reset_no_op_when_dispatch_unbound() -> None:
    """When ``_dispatch`` is None (CLI / wire / pre-Gateway) the branch
    silently no-ops — must NOT raise, must NOT clear unrelated state."""
    mod = _load_discord_adapter()
    DiscordAdapter = mod.DiscordAdapter

    import os
    from unittest.mock import patch

    fake_client = MagicMock()
    fake_state = MagicMock()
    fake_state._command_tree = None
    fake_client._connection = fake_state
    with (
        patch.dict(os.environ, dict(os.environ), clear=True),
        patch("discord.Client", return_value=fake_client),
    ):
        a = DiscordAdapter(config={"bot_token": "fake-token"})

    db_mock = MagicMock()
    db_mock.end_session = MagicMock()
    a._session_db = db_mock

    api = PluginAPI(
        tool_registry=SimpleNamespace(names=lambda: []),
        hook_engine=SimpleNamespace(_hooks={}),
        provider_registry={},
        channel_registry={},
    )
    # NO _bind_dispatch call — _dispatch stays None.
    plugin_registry.shared_api = api
    try:
        msg = a._reset_session("any-sid")
    finally:
        plugin_registry.shared_api = None

    # Reset still proceeds — end_session ran, user got a reply.
    db_mock.end_session.assert_called_once()
    assert "reset" in msg.lower()
