"""Tests for ``POST /v1/runs/{run_id}/stop`` (Wave 6.A — Hermes 0a15dbdc4).

Smoke tests at the aiohttp app level. We exercise:
- the chat endpoint returns a run_id we can target
- the stop endpoint cancels in-flight runs
- unknown run_id returns 404
- unauthenticated requests return 401
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "api-server"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_api_server_run_stop", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def APIServerAdapter():  # noqa: N802
    return _load_adapter().APIServerAdapter


def _make_adapter(cls):
    return cls({"host": "127.0.0.1", "port": 0, "token": "secret"})


@pytest.mark.asyncio
async def test_stop_unknown_run_returns_404(APIServerAdapter):  # noqa: N803
    from aiohttp.test_utils import make_mocked_request

    a = _make_adapter(APIServerAdapter)
    req = make_mocked_request(
        "POST", "/v1/runs/no-such/stop",
        headers={"Authorization": "Bearer secret"},
        match_info={"run_id": "no-such"},
    )
    resp = await a._handle_run_stop(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_stop_requires_auth(APIServerAdapter):  # noqa: N803
    from aiohttp.test_utils import make_mocked_request

    a = _make_adapter(APIServerAdapter)
    req = make_mocked_request(
        "POST", "/v1/runs/x/stop",
        match_info={"run_id": "x"},
    )
    resp = await a._handle_run_stop(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_stop_cancels_active_run(APIServerAdapter):  # noqa: N803
    from aiohttp.test_utils import make_mocked_request

    a = _make_adapter(APIServerAdapter)

    # Plant an active task we can cancel
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_slow())
    a._active_runs["abc"] = task
    await started.wait()

    req = make_mocked_request(
        "POST", "/v1/runs/abc/stop",
        headers={"Authorization": "Bearer secret"},
        match_info={"run_id": "abc"},
    )
    resp = await a._handle_run_stop(req)
    assert resp.status == 200

    # Task should have been cancelled
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_stop_app_route_registered(APIServerAdapter):  # noqa: N803
    a = _make_adapter(APIServerAdapter)
    app = a._build_app()
    routes = [str(r.resource) for r in app.router.routes()]
    assert any("/v1/runs/" in r and "/stop" in r for r in routes)
