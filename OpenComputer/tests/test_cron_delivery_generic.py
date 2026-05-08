"""Hermes parity: cron delivery generalized to all Platform enum channels.

Pre-fix the existing latent bug — old _deliver called PluginRegistry.instance()
which doesn't exist; the actual singleton is the module-level ``registry``
and channels are stored in ``registry.channels`` as a string-keyed dict.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.cron.scheduler import _deliver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform_str", ["slack", "matrix", "mattermost", "email", "signal", "whatsapp"]
)
async def test_deliver_routes_to_platform_via_registry(platform_str):
    job = {"id": "j1", "name": "n", "notify": f"{platform_str}:#channel-1"}
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(return_value=None)
    with patch.dict(
        "opencomputer.plugins.registry.registry.channels",
        {platform_str: fake_adapter},
        clear=False,
    ):
        err = await _deliver(job, "hello")
    assert err is None, f"unexpected error: {err}"
    fake_adapter.send.assert_awaited_once_with("#channel-1", "hello")


@pytest.mark.asyncio
async def test_deliver_unknown_platform_returns_error():
    job = {"id": "j1", "name": "n", "notify": "made_up_platform:1234"}
    err = await _deliver(job, "hello")
    assert err is not None
    assert "unknown" in err.lower()


@pytest.mark.asyncio
async def test_deliver_missing_adapter_returns_error():
    job = {"id": "j1", "name": "n", "notify": "slack:#x"}
    # Ensure no slack adapter is registered.
    from opencomputer.plugins.registry import registry as plugin_registry
    saved = plugin_registry.channels.pop("slack", None)
    try:
        err = await _deliver(job, "hello")
    finally:
        if saved is not None:
            plugin_registry.channels["slack"] = saved
    assert err is not None
    assert "not enabled" in err.lower()


@pytest.mark.asyncio
async def test_deliver_local_is_noop():
    job = {"id": "j1", "name": "n", "notify": "local"}
    err = await _deliver(job, "hello")
    assert err is None


@pytest.mark.asyncio
async def test_deliver_empty_notify_is_noop():
    job = {"id": "j1", "name": "n", "notify": None}
    err = await _deliver(job, "hello")
    assert err is None
