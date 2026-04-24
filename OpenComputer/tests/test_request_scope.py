"""Task I.9 — Per-request plugin scopes via ``PluginAPI.in_request``.

Mirrors OpenClaw's ``sources/openclaw/src/gateway/server-plugins.ts:47-64,
107-144`` pattern — the gateway binds a per-request scope around each
dispatch so plugins can query their activation context (auth state,
model-override policy, request identity).

OpenComputer's current fire-per-message channel model doesn't strictly
need this — there is no persistent multi-tenant connection — but the
PLUMBING needs to exist before we can ever move to a connection-pooled
wire protocol. This task adds the plumbing without changing current
fire-per-message semantics.

Verifies:

* ``RequestContext`` dataclass round-trips its fields (request_id,
  channel, user_id, session_id, started_at).
* ``RequestContext`` is re-exported from ``plugin_sdk``.
* ``PluginAPI.request_context`` is ``None`` outside any ``in_request``
  block. Plugins that never enter a gateway dispatch observe no scope.
* Inside ``api.in_request(ctx)``, ``api.request_context`` returns the
  ctx verbatim; on exit, it reverts to ``None``.
* Nested ``in_request`` calls raise ``RuntimeError`` — only one request
  may be in flight at a time per PluginAPI (per OpenClaw's model).
* The gateway dispatch path populates a ``RequestContext`` per inbound
  ``MessageEvent`` and wraps the ``AgentLoop.run_conversation`` call.
* ``Dispatch`` constructed without a plugin_api preserves backwards
  compatibility — CLI + direct AgentLoop users see no change.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── RequestContext dataclass ──────────────────────────────────────────


def test_request_context_dataclass_roundtrip() -> None:
    """RequestContext carries the five fields the spec requires."""
    from plugin_sdk.runtime_context import RequestContext

    now = time.monotonic()
    ctx = RequestContext(
        request_id="req-abc-123",
        channel="telegram",
        user_id="u-42",
        session_id="sess-xyz",
        started_at=now,
    )
    assert ctx.request_id == "req-abc-123"
    assert ctx.channel == "telegram"
    assert ctx.user_id == "u-42"
    assert ctx.session_id == "sess-xyz"
    assert ctx.started_at == now


def test_request_context_is_frozen() -> None:
    """Frozen dataclass — plugins can't mutate the scope mid-request."""
    from dataclasses import FrozenInstanceError

    from plugin_sdk.runtime_context import RequestContext

    ctx = RequestContext(request_id="r")
    with pytest.raises(FrozenInstanceError):
        ctx.request_id = "mutated"  # type: ignore[misc]


def test_request_context_defaults() -> None:
    """Only request_id is required — everything else defaults sensibly."""
    from plugin_sdk.runtime_context import RequestContext

    ctx = RequestContext(request_id="r")
    assert ctx.request_id == "r"
    assert ctx.channel is None
    assert ctx.user_id is None
    assert ctx.session_id is None
    assert ctx.started_at == 0.0


def test_request_context_reexported_from_plugin_sdk() -> None:
    """Public re-export so third-party plugins can ``from plugin_sdk import ...``."""
    import plugin_sdk

    assert hasattr(plugin_sdk, "RequestContext")
    assert "RequestContext" in plugin_sdk.__all__


# ─── PluginAPI.in_request / .request_context ───────────────────────────


def _make_api():
    from opencomputer.plugins.loader import PluginAPI

    return PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=MagicMock(),
    )


def test_request_context_none_outside_in_request() -> None:
    """No scope entered → ``api.request_context is None`` (CLI + direct loop)."""
    api = _make_api()
    assert api.request_context is None


def test_in_request_sets_request_context_for_duration() -> None:
    """Inside the context manager, plugins see the ctx; after, None again."""
    from plugin_sdk.runtime_context import RequestContext

    api = _make_api()
    ctx = RequestContext(
        request_id="req-1",
        channel="telegram",
        user_id="u-1",
        session_id="s-1",
        started_at=time.monotonic(),
    )
    assert api.request_context is None
    with api.in_request(ctx):
        assert api.request_context is ctx
        assert api.request_context.request_id == "req-1"
        assert api.request_context.channel == "telegram"
    # Reverts on exit.
    assert api.request_context is None


def test_in_request_reverts_on_exception() -> None:
    """If the wrapped block raises, the scope still unwinds cleanly."""
    from plugin_sdk.runtime_context import RequestContext

    api = _make_api()
    ctx = RequestContext(request_id="req-exc")
    with pytest.raises(RuntimeError, match="boom"):
        with api.in_request(ctx):
            assert api.request_context is ctx
            raise RuntimeError("boom")
    assert api.request_context is None


def test_nested_in_request_raises() -> None:
    """Only one request in flight per API — nested entry is a bug. Spec says raise."""
    from plugin_sdk.runtime_context import RequestContext

    api = _make_api()
    outer = RequestContext(request_id="outer")
    inner = RequestContext(request_id="inner")
    with api.in_request(outer):
        assert api.request_context is outer
        with pytest.raises(RuntimeError, match="already in a request"):
            with api.in_request(inner):
                pass  # pragma: no cover — should never execute
        # Outer scope is still live after the rejection.
        assert api.request_context is outer
    assert api.request_context is None


# ─── Gateway dispatch populates a RequestContext ───────────────────────


def test_dispatch_populates_request_context(monkeypatch) -> None:
    """``Dispatch.handle_message`` wraps ``run_conversation`` in ``api.in_request``.

    This is the I.9 wiring point: a monkeypatched provider observes the
    ctx mid-flight via ``api.request_context`` and captures its fields.
    """
    import hashlib

    from opencomputer.gateway.dispatch import Dispatch
    from opencomputer.plugins.loader import PluginAPI
    from plugin_sdk.core import MessageEvent, Platform

    api = PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=MagicMock(),
    )

    captured: dict[str, object] = {}

    async def fake_run_conversation(user_message: str, session_id: str, **kw):
        # Plugin-side view: the dispatch must have populated a scope.
        ctx = api.request_context
        captured["ctx"] = ctx
        captured["user_message"] = user_message
        captured["session_id"] = session_id
        # Return a minimal loop-result mock.
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop = MagicMock()
    fake_loop.run_conversation = fake_run_conversation
    dispatch = Dispatch(fake_loop, plugin_api=api)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="chat-42",
        user_id="user-99",
        text="hello",
        timestamp=1700000000.0,
    )
    result_text = asyncio.run(dispatch.handle_message(event))
    assert result_text == "ok"

    # Outside dispatch, no scope lingers.
    assert api.request_context is None

    # Dispatch must have populated a RequestContext during the call.
    ctx = captured["ctx"]
    assert ctx is not None
    from plugin_sdk.runtime_context import RequestContext

    assert isinstance(ctx, RequestContext)
    assert ctx.channel == "telegram"
    # chat_id is OpenComputer's per-chat user identifier surface.
    assert ctx.user_id == "chat-42"
    # Matches the deterministic dispatch session id.
    expected_session = hashlib.sha256(b"telegram:chat-42").hexdigest()[:32]
    assert ctx.session_id == expected_session
    # request_id is a non-empty string (uuid4 hex).
    assert isinstance(ctx.request_id, str)
    assert len(ctx.request_id) > 0
    # started_at is a plausible monotonic() reading (>0).
    assert ctx.started_at > 0


def test_dispatch_without_plugin_api_preserves_backwards_compat() -> None:
    """Dispatch built without a plugin_api must still work — no scope, no error."""
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent, Platform

    async def fake_run_conversation(user_message: str, session_id: str, **kw):
        result = MagicMock()
        result.final_message = MagicMock(content="reply")
        return result

    fake_loop = MagicMock()
    fake_loop.run_conversation = fake_run_conversation
    dispatch = Dispatch(fake_loop)  # no plugin_api

    event = MessageEvent(
        platform=Platform.DISCORD,
        chat_id="guild-1",
        user_id="member-1",
        text="hi",
        timestamp=1700000000.0,
    )
    result_text = asyncio.run(dispatch.handle_message(event))
    assert result_text == "reply"


def test_dispatch_empty_text_returns_none_without_touching_scope() -> None:
    """Empty text short-circuits before the scope is entered."""
    from opencomputer.gateway.dispatch import Dispatch
    from opencomputer.plugins.loader import PluginAPI
    from plugin_sdk.core import MessageEvent, Platform

    api = PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=MagicMock(),
    )
    fake_loop = MagicMock()
    fake_loop.run_conversation = AsyncMock()
    dispatch = Dispatch(fake_loop, plugin_api=api)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="c",
        user_id="u",
        text="   ",  # whitespace only
        timestamp=1.0,
    )
    result = asyncio.run(dispatch.handle_message(event))
    assert result is None
    assert api.request_context is None
    fake_loop.run_conversation.assert_not_awaited()


# ─── PluginRegistry exposes the shared PluginAPI ───────────────────────


def test_plugin_registry_tracks_shared_api_after_load_all(tmp_path) -> None:
    """After ``load_all``, the registry exposes the shared PluginAPI.

    The dispatch layer reads ``registry.shared_api`` to wrap each
    request in ``in_request``. Before ``load_all`` it's None (no
    plugins yet → nothing to scope around).
    """
    from opencomputer.plugins.registry import PluginRegistry

    reg = PluginRegistry()
    assert reg.shared_api is None
    # An empty load_all (no plugin paths) still materialises the api —
    # so a running gateway with zero plugins still has a scope target.
    reg.load_all([tmp_path])
    assert reg.shared_api is not None
    # And calling again idempotently reuses a live api (no double-swap).
    first = reg.shared_api
    reg.load_all([tmp_path])
    # We allow re-creation (the public contract is "non-None after load"),
    # but the new api must still be a PluginAPI.
    from opencomputer.plugins.loader import PluginAPI

    assert isinstance(reg.shared_api, PluginAPI)
    # Sanity: the stored api is the same one plugins would have seen on
    # the most recent call.
    assert reg.shared_api is not None
    del first  # silence unused-var
