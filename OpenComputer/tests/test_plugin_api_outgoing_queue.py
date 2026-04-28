"""Tests for PluginAPI.outgoing_queue accessor (Hermes PR 2, amendment §A.3).

Verifies that webhook-style plugins can enqueue outbound messages via
``api.outgoing_queue.enqueue(...)`` WITHOUT importing
``opencomputer.gateway.outgoing_queue`` directly. This preserves the
plugin_sdk → opencomputer one-way boundary while letting plugins schedule
async sends.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from opencomputer.plugins.loader import PluginAPI


def _build_api(outgoing_queue=None) -> PluginAPI:
    return PluginAPI(
        tool_registry=object(),
        hook_engine=object(),
        provider_registry={},
        channel_registry={},
        outgoing_queue=outgoing_queue,
    )


def test_outgoing_queue_default_is_none() -> None:
    """No queue bound (CLI / direct AgentLoop runs) → property returns None."""
    api = _build_api()
    assert api.outgoing_queue is None


def test_outgoing_queue_returns_bound_object() -> None:
    """Constructor-bound queue surfaces via the property."""
    sentinel = object()
    api = _build_api(outgoing_queue=sentinel)
    assert api.outgoing_queue is sentinel


def test_bind_outgoing_queue_late_binding() -> None:
    """Gateway binds the live queue after construction; plugins see it."""
    api = _build_api()
    assert api.outgoing_queue is None
    sentinel = object()
    api._bind_outgoing_queue(sentinel)
    assert api.outgoing_queue is sentinel


def test_plugin_can_enqueue_via_api_without_importing_opencomputer(tmp_path: Path) -> None:
    """End-to-end: simulate a plugin calling api.outgoing_queue.enqueue(...).

    The plugin code is given ONLY the ``api`` handle — no opencomputer
    import — and successfully writes a row into the queue. Mirrors the
    real webhook adapter usage (PR 6) without coupling this test to it.
    """
    from opencomputer.gateway.outgoing_queue import OutgoingQueue

    queue = OutgoingQueue(tmp_path / "sessions.db")
    api = _build_api(outgoing_queue=queue)

    # Pretend this is plugin code — closes only over ``api``, not over
    # any opencomputer module:
    def plugin_handler(api_arg) -> str:
        msg = api_arg.outgoing_queue.enqueue(
            platform="webhook",
            chat_id="https://example.com/hook",
            body="hello from plugin",
        )
        return msg.id

    msg_id = plugin_handler(api)
    assert msg_id

    # Confirm the row landed.
    fetched = queue.get(msg_id)
    assert fetched is not None
    assert fetched.platform == "webhook"
    assert fetched.body == "hello from plugin"


def test_outgoing_queue_property_is_readonly_setter_only_via_bind() -> None:
    """The public surface is a property — plugins can't reassign ``api.outgoing_queue``."""
    api = _build_api()
    # ``outgoing_queue`` is a property without a setter; attempting to
    # assign through the public name raises AttributeError. Plugins must
    # use ``_bind_outgoing_queue`` (and only the gateway should) — but
    # we don't enforce naming here, only that the read-only contract
    # holds.
    with pytest.raises(AttributeError):
        api.outgoing_queue = object()  # type: ignore[misc]


def test_plugin_sdk_does_not_directly_import_outgoing_queue() -> None:
    """Sanity: the PluginAPI source itself doesn't import OutgoingQueue.

    The accessor MUST stay duck-typed so plugin_sdk modules referencing
    PluginAPI don't transitively pull opencomputer.gateway. The
    boundary check in test_phase6a covers plugin_sdk/*; this is the
    symmetric check on the loader side.
    """
    src = inspect.getsource(PluginAPI)
    assert "from opencomputer.gateway.outgoing_queue" not in src
    assert "import opencomputer.gateway.outgoing_queue" not in src
