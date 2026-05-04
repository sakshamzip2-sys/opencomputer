"""Tests for SlackAdapter.send_multiple_images per-file upload override.

Wave 5 T11 closure — Hermes-port (3de8e2168). Slack's UX bundles a series
of file uploads under a single ``initial_comment`` on the first call;
this override loops ``files.upload`` per image, attaching the comment
only to the first one.

Per-file approach (vs the modern files.uploadV2 two-step) was chosen for
implementation simplicity; can be upgraded later without changing the
public API.
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
        / "slack"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_slack_adapter_for_T11", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def slack_adapter_class():
    return _load_adapter().SlackAdapter


def _make_stub_adapter(cls):
    """SlackAdapter without running real __init__ — only fields the
    override touches need to be set."""
    a = cls.__new__(cls)
    a._client = MagicMock()
    a._client.post = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"ok": True}),
    )
    return a


@pytest.mark.asyncio
async def test_empty_list_is_noop(slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    await a.send_multiple_images("C123", [])
    a._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_no_client_is_noop(slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    a._client = None
    await a.send_multiple_images("C123", ["/x.png"])
    # No exception, no calls (we'd need _client.post to call anything)


@pytest.mark.asyncio
async def test_each_image_one_post(tmp_path, slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("C123", paths, caption="hello")
    assert a._client.post.await_count == 3


@pytest.mark.asyncio
async def test_initial_comment_on_first_only(tmp_path, slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    paths = []
    for i in range(2):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("C123", paths, caption="first comment")
    first_data = a._client.post.await_args_list[0].kwargs["data"]
    second_data = a._client.post.await_args_list[1].kwargs["data"]
    assert first_data.get("initial_comment") == "first comment"
    assert "initial_comment" not in second_data


@pytest.mark.asyncio
async def test_missing_file_skipped_not_raised(tmp_path, slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    real = tmp_path / "exists.png"
    real.write_bytes(b"\x89PNG")
    paths = [str(tmp_path / "missing.png"), str(real)]
    await a.send_multiple_images("C123", paths, caption="x")
    # Only the existing file uploads
    assert a._client.post.await_count == 1


@pytest.mark.asyncio
async def test_per_file_error_continues_to_next(tmp_path, slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    # First call raises, second returns ok
    a._client.post.side_effect = [
        RuntimeError("rate limited"),
        MagicMock(status_code=200, json=lambda: {"ok": True}),
    ]
    paths = []
    for i in range(2):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    # Should not raise — per-file resilience
    await a.send_multiple_images("C123", paths, caption="x")
    # Both attempted (first raised, second succeeded)
    assert a._client.post.await_count == 2


@pytest.mark.asyncio
async def test_filename_passed_in_data(tmp_path, slack_adapter_class):
    a = _make_stub_adapter(slack_adapter_class)
    p = tmp_path / "specific-name.png"
    p.write_bytes(b"\x89PNG")
    await a.send_multiple_images("C123", [str(p)])
    data = a._client.post.await_args.kwargs["data"]
    assert data["filename"] == "specific-name.png"
    assert data["channels"] == "C123"
