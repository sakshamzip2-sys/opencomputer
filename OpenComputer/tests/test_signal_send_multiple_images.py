"""Tests for SignalAdapter.send_multiple_images attachments-array RPC.

Wave 5 T11 final closure (Hermes-port 3de8e2168). signal-cli's ``send``
JSON-RPC method accepts an ``attachments`` array — one round-trip
delivers all images bundled with optional message text.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "signal"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_signal_adapter_for_T11", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def signal_adapter_class():
    return _load_adapter().SignalAdapter


def _make_stub_adapter(cls):
    a = cls.__new__(cls)
    a._phone = "+15551234567"
    a._base_url = "http://localhost:8080"
    a._client = MagicMock()
    a.max_message_length = 2000
    ok_resp = MagicMock(status_code=200, json=lambda: {"result": {"timestamp": 1}})
    a._client.post = AsyncMock(return_value=ok_resp)

    async def fake_retry(method, *args, **kwargs):
        return await method(*args, **kwargs)

    a._send_with_retry = fake_retry
    return a


@pytest.mark.asyncio
async def test_empty_list_is_noop(signal_adapter_class):
    a = _make_stub_adapter(signal_adapter_class)
    await a.send_multiple_images("+1555", [])
    a._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_all_missing_is_noop(tmp_path, signal_adapter_class):
    a = _make_stub_adapter(signal_adapter_class)
    await a.send_multiple_images(
        "+1555", [str(tmp_path / "ghost.png")],
    )
    a._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_single_rpc_with_attachments_list(tmp_path, signal_adapter_class):
    a = _make_stub_adapter(signal_adapter_class)
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("+15551112222", paths, caption="batch")
    assert a._client.post.await_count == 1
    payload = a._client.post.await_args.kwargs["json"]
    assert payload["method"] == "send"
    assert payload["params"]["account"] == "+15551234567"
    assert payload["params"]["recipient"] == ["+15551112222"]
    assert len(payload["params"]["attachments"]) == 3
    assert payload["params"]["message"] == "batch"


@pytest.mark.asyncio
async def test_caption_optional(tmp_path, signal_adapter_class):
    a = _make_stub_adapter(signal_adapter_class)
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    await a.send_multiple_images("+15551112222", [str(p)], caption="")
    payload = a._client.post.await_args.kwargs["json"]
    # Empty caption → no "message" key
    assert "message" not in payload["params"]
    assert payload["params"]["attachments"] == [str(p)]


@pytest.mark.asyncio
async def test_missing_files_filtered_present_files_sent(
    tmp_path, signal_adapter_class,
):
    a = _make_stub_adapter(signal_adapter_class)
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    paths = [str(tmp_path / "missing.png"), str(real)]
    await a.send_multiple_images("+1555", paths)
    payload = a._client.post.await_args.kwargs["json"]
    assert payload["params"]["attachments"] == [str(real)]


@pytest.mark.asyncio
async def test_rpc_error_swallowed(tmp_path, signal_adapter_class):
    a = _make_stub_adapter(signal_adapter_class)
    a._client.post = AsyncMock(side_effect=RuntimeError("network blip"))

    async def fake_retry(method, *args, **kwargs):
        return await method(*args, **kwargs)

    a._send_with_retry = fake_retry
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    # Must not raise
    await a.send_multiple_images("+1555", [str(p)])
