"""Tests for MattermostAdapter.send_multiple_images native multi-attach.

Wave 5 T11 final closure (Hermes-port 3de8e2168). Mattermost flow:
1. POST /api/v4/files per image → file id
2. POST /api/v4/posts with file_ids list → single threaded post
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
        / "mattermost"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_mattermost_adapter_for_T11", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mattermost_adapter_class():
    return _load_adapter().MattermostAdapter


def _make_stub_adapter(cls):
    a = cls.__new__(cls)
    a._client = MagicMock()
    a._base_url = "https://mm.example.com"

    files_resp = MagicMock(
        status_code=201,
        json=lambda: {"file_infos": [{"id": "f-1"}]},
    )
    posts_resp = MagicMock(status_code=201, json=lambda: {"id": "p-1"})

    async def post_router(url, *args, **kwargs):
        if url.endswith("/api/v4/files"):
            return files_resp
        return posts_resp

    a._client.post = AsyncMock(side_effect=post_router)
    return a


@pytest.mark.asyncio
async def test_empty_list_is_noop(mattermost_adapter_class):
    a = _make_stub_adapter(mattermost_adapter_class)
    await a.send_multiple_images("ch-1", [])
    a._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_no_client_is_noop(mattermost_adapter_class):
    a = _make_stub_adapter(mattermost_adapter_class)
    a._client = None
    await a.send_multiple_images("ch-1", ["/a.png"])  # no exception


@pytest.mark.asyncio
async def test_each_image_uploaded_then_one_post(tmp_path, mattermost_adapter_class):
    a = _make_stub_adapter(mattermost_adapter_class)
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("ch-1", paths, caption="hi")
    # 3 file uploads + 1 post
    calls = a._client.post.await_args_list
    file_calls = [c for c in calls if "/api/v4/files" in c.args[0]]
    post_calls = [c for c in calls if "/api/v4/posts" in c.args[0]]
    assert len(file_calls) == 3
    assert len(post_calls) == 1
    assert post_calls[0].kwargs["json"]["message"] == "hi"
    assert post_calls[0].kwargs["json"]["file_ids"] == ["f-1", "f-1", "f-1"]


@pytest.mark.asyncio
async def test_missing_files_skipped(tmp_path, mattermost_adapter_class):
    a = _make_stub_adapter(mattermost_adapter_class)
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    paths = [str(tmp_path / "missing.png"), str(real)]
    await a.send_multiple_images("ch-1", paths, caption="x")
    calls = a._client.post.await_args_list
    file_calls = [c for c in calls if "/api/v4/files" in c.args[0]]
    assert len(file_calls) == 1


@pytest.mark.asyncio
async def test_no_uploads_means_no_post(tmp_path, mattermost_adapter_class):
    """If every file is missing, skip the post entirely."""
    a = _make_stub_adapter(mattermost_adapter_class)
    await a.send_multiple_images("ch-1", [str(tmp_path / "ghost.png")])
    a._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_upload_error_continues_to_next(tmp_path, mattermost_adapter_class):
    a = _make_stub_adapter(mattermost_adapter_class)
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    real2 = tmp_path / "real2.png"
    real2.write_bytes(b"\x89PNG")

    files_ok = MagicMock(
        status_code=201,
        json=lambda: {"file_infos": [{"id": "f-1"}]},
    )
    posts_ok = MagicMock(status_code=201, json=lambda: {"id": "p-1"})

    async def post_router(url, *args, **kwargs):
        if url.endswith("/api/v4/files"):
            # First file raises, second succeeds
            if post_router.first:
                post_router.first = False
                raise RuntimeError("transient")
            return files_ok
        return posts_ok

    post_router.first = True
    a._client.post = AsyncMock(side_effect=post_router)

    await a.send_multiple_images("ch-1", [str(real), str(real2)], caption="x")
    # Both file uploads attempted, one succeeded, one post issued with the single id
    calls = a._client.post.await_args_list
    post_calls = [c for c in calls if "/api/v4/posts" in c.args[0]]
    assert len(post_calls) == 1
    assert post_calls[0].kwargs["json"]["file_ids"] == ["f-1"]
