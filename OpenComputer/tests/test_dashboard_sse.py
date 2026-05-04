"""Tests for the dashboard SSE stream helpers (Wave 6.D-β)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencomputer.dashboard import build_app
from opencomputer.dashboard._sse import encode_keepalive, encode_sse, mtime_watch

# ---- encode_sse ----


def test_encode_sse_format():
    out = encode_sse("change", {"k": 1})
    assert out.startswith(b"event: change\n")
    assert b'"k": 1' in out
    assert out.endswith(b"\n\n")


def test_encode_keepalive_is_comment():
    out = encode_keepalive()
    assert out.startswith(b":")
    assert out.endswith(b"\n\n")


# ---- mtime_watch ----


@pytest.mark.asyncio
async def test_mtime_watch_emits_initial(tmp_path: Path):
    """initial_emit=True should produce one event without waiting."""
    f = tmp_path / "watched.yaml"
    f.write_text("a: 1\n")

    gen = mtime_watch(f, poll_interval=0.05, initial_emit=True)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert b"event: change" in first
    await gen.aclose()


@pytest.mark.asyncio
async def test_mtime_watch_emits_on_change(tmp_path: Path):
    """Touch the file → next iteration should yield."""
    f = tmp_path / "watched.yaml"
    f.write_text("a: 1\n")

    gen = mtime_watch(f, poll_interval=0.05, initial_emit=False)
    # Bump mtime
    await asyncio.sleep(0.06)
    new_mtime = time.time() + 1
    import os
    os.utime(f, (new_mtime, new_mtime))
    msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert b"event: change" in msg
    await gen.aclose()


@pytest.mark.asyncio
async def test_mtime_watch_payload_fn(tmp_path: Path):
    f = tmp_path / "watched.yaml"
    f.write_text("a: 1\n")

    async def _payload(p):
        return {"custom": "yes"}

    gen = mtime_watch(f, poll_interval=0.05, initial_emit=True, payload_fn=_payload)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    decoded = first.decode()
    # Find data: line
    for line in decoded.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[len("data:"):].strip())
            assert payload == {"custom": "yes"}
            break
    await gen.aclose()


# ---- /stream routes ----


@pytest.fixture()
def home_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(home_dir: Path) -> TestClient:
    app = build_app(enable_pty=False)
    c = TestClient(app)
    c._token = app.state.session_token
    return c


def test_management_stream_requires_token(client: TestClient):
    """Stream route must reject missing token like the rest of the API."""
    r = client.get("/api/plugins/management/stream")
    assert r.status_code == 401


def test_models_stream_requires_token(client: TestClient):
    r = client.get("/api/plugins/models/stream")
    assert r.status_code == 401


def test_streaming_routes_mounted(client: TestClient):
    """Both SSE routes appear on the OpenAPI surface (verifies they
    were registered without trying to actually stream against
    Starlette's TestClient, which doesn't cancel the generator
    cleanly when used in unit-test mode)."""
    spec = client.get("/openapi.json").json()
    paths = set(spec.get("paths", {}).keys())
    assert "/api/plugins/management/stream" in paths
    assert "/api/plugins/models/stream" in paths
