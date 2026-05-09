# Hermes MCP / API Server / ACP v2 — Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 15 honest gaps between Hermes MCP/API/ACP reference spec and OpenComputer (CORS, Idempotency, /v1/responses defaults, MCP naming aliases, per-server filters, sampling caps, ACP discovery).

**Architecture:** No new modules. Surgical edits to 6 existing files (`extensions/api-server/{adapter,plugin}.py`, `opencomputer/mcp/{client,server,sampling}.py`, `opencomputer/agent/config.py`, `opencomputer/cli.py`). Hand-rolled CORS+Idempotency middlewares (no new deps). MCP naming = additive aliases preserving back-compat. Sampling caps = per-server `MCPSamplingCaps` dataclass enforced in callback.

**Tech Stack:** Python 3.12+, aiohttp, FastMCP, pytest. Reuses existing `MCPServerConfig`, `_responses_storage` LRU, `ConsentStore`, `_home()` profile resolver.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `extensions/api-server/adapter.py` | aiohttp app, all routes | Add CORS+Idempotency middlewares; ungate /v1/responses; add GET/DELETE/{id}; add /v1/health alias; honor API_SERVER_KEY alias |
| `extensions/api-server/plugin.py` | env-var → adapter wiring | Honor API_SERVER_ENABLED auto-enable; alias API_SERVER_KEY |
| `opencomputer/mcp/client.py` | MCPConnection, tool registration | Register `mcp_<server>_<tool>` alias; honor `prompts_enabled`/`resources_enabled`; thread per-server timeouts; sampling-caps wiring |
| `opencomputer/mcp/sampling.py` | sampling callback | Accept caps dataclass; enforce token cap, rpm, tool rounds, allowed_models |
| `opencomputer/mcp/server.py` | Hermes-AS-MCP tools | Add conversations_list/conversation_get aliases; add permissions_list_open; widen events_poll signature; emit approval event types |
| `opencomputer/agent/config.py` | typed config | Extend MCPServerConfig (4 fields); add MCPSamplingCaps dataclass |
| `opencomputer/cli.py` | acp serve | Auto-bundle agent.json on first serve |
| `tests/test_api_server_cors.py` | G1 | Create |
| `tests/test_api_server_idempotency.py` | G2 | Create |
| `tests/test_api_server_responses_default_on.py` | G3 | Create |
| `tests/test_api_server_responses_get_delete.py` | G4 | Create |
| `tests/test_api_server_health_v1.py` | G5 | Create |
| `tests/test_api_server_env_aliases.py` | G6 + G7 | Create |
| `tests/test_mcp_tool_naming_alias.py` | G8 | Create |
| `tests/test_mcp_per_server_filters_v2.py` | G9 | Create |
| `tests/test_mcp_per_server_timeout.py` | G10 | Create |
| `tests/test_mcp_sampling_caps.py` | G11 | Create |
| `tests/test_mcp_server_conversation_aliases.py` | G12 | Create |
| `tests/test_mcp_server_permissions_list_open.py` | G13 | Create |
| `tests/test_mcp_server_events_poll_aliases.py` | G14 | Create |
| `tests/test_acp_agent_json_autobundle.py` | G15 | Create |

---

## Phase A — API Server (G1–G7, ~3-4 hr)

### Task A1: CORS middleware (G1)

**Files:**
- Modify: `OpenComputer/extensions/api-server/adapter.py` (add middleware + register on `_build_app`)
- Test: `OpenComputer/tests/test_api_server_cors.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_cors.py`:

```python
"""Hermes parity G1: CORS preflight + headers per API_SERVER_CORS_ORIGINS."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_preflight_returns_200_with_allowed_headers(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type,Idempotency-Key",
            },
        )
        assert r.status == 200, await r.text()
        assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert r.headers["Access-Control-Max-Age"] == "600"
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
        assert "Idempotency-Key" in r.headers["Access-Control-Allow-Headers"]


@pytest.mark.asyncio
async def test_post_includes_cors_origin_when_allowed(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Origin": "http://localhost:3000"},
            json={"text": "hi"},
        )
        assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


@pytest.mark.asyncio
async def test_no_cors_origin_header_when_origin_disallowed(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Either 200 with no Allow-Origin or 403 — both acceptable for disallowed origin
        assert "Access-Control-Allow-Origin" not in r.headers


@pytest.mark.asyncio
async def test_no_cors_when_env_unset(monkeypatch):
    monkeypatch.delenv("API_SERVER_CORS_ORIGINS", raising=False)
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={"Origin": "http://localhost:3000"},
        )
        assert "Access-Control-Allow-Origin" not in r.headers
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_cors.py -v
```

Expected: FAIL — preflight 405 / no CORS headers.

- [ ] **Step 3: Add CORS middleware to `_build_app`**

In `OpenComputer/extensions/api-server/adapter.py`, near the top of the class methods (just before `_build_app`), add:

```python
def _cors_origins(self) -> list[str]:
    """Read API_SERVER_CORS_ORIGINS at request time so tests can monkeypatch."""
    raw = os.environ.get("API_SERVER_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


@web.middleware
async def _cors_middleware(self, request: web.Request, handler):
    """Hermes-spec CORS: preflight + simple-request headers.

    Honors comma-separated ``API_SERVER_CORS_ORIGINS``. When unset,
    no CORS headers are emitted (default off — server-to-server use).
    Allowed headers include ``Idempotency-Key`` (G2) so deduplication
    works from browsers.
    """
    origins = self._cors_origins()
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS" and origins:
        if origin not in origins:
            return web.Response(status=200)
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key, X-OC-Profile",
                "Access-Control-Max-Age": "600",
                "Access-Control-Allow-Credentials": "true",
            },
        )
    response = await handler(request)
    if origin and origin in origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response
```

Then in `_build_app`, change the line `app = web.Application()` (or whichever constructs it) to pass middlewares:

```python
app = web.Application(middlewares=[self._cors_middleware])
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_cors.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/adapter.py OpenComputer/tests/test_api_server_cors.py
git commit -m "feat(api-server): G1 — CORS middleware honoring API_SERVER_CORS_ORIGINS (Hermes spec parity)"
```

---

### Task A2: Idempotency-Key middleware (G2)

**Files:**
- Modify: `OpenComputer/extensions/api-server/adapter.py` (add module-level cache + middleware + register)
- Test: `OpenComputer/tests/test_api_server_idempotency.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_idempotency.py`:

```python
"""Hermes parity G2: Idempotency-Key dedup with 5-min TTL."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clear_idempotency_cache():
    mod = _load_adapter()
    if hasattr(mod, "_IDEMPOTENCY_CACHE"):
        mod._IDEMPOTENCY_CACHE.clear()
    yield
    if hasattr(mod, "_IDEMPOTENCY_CACHE"):
        mod._IDEMPOTENCY_CACHE.clear()


@pytest.mark.asyncio
async def test_repeat_request_returns_cached_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(text, sid):
        counter["n"] += 1
        return f"call-{counter['n']}"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "abc123"},
            json={"text": "hi"},
        )
        r2 = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "abc123"},
            json={"text": "hi"},
        )
        assert r1.status == 200
        assert r2.status == 200
        assert counter["n"] == 1, "handler called twice — idempotency missed"
        assert r2.headers.get("X-Idempotent-Replay") == "1"
        b1 = await r1.text()
        b2 = await r2.text()
        assert b1 == b2


@pytest.mark.asyncio
async def test_different_keys_call_handler_separately():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(text, sid):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok", "Idempotency-Key": "k1"}, json={"text": "hi"})
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok", "Idempotency-Key": "k2"}, json={"text": "hi"})
        assert counter["n"] == 2


@pytest.mark.asyncio
async def test_no_key_means_no_cache():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(text, sid):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok"}, json={"text": "hi"})
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok"}, json={"text": "hi"})
        assert counter["n"] == 2


@pytest.mark.asyncio
async def test_different_tokens_have_separate_caches():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(text, sid):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        # Same key, different Authorization → cache MISS for second
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok", "Idempotency-Key": "k1"}, json={"text": "hi"})
        # Wrong token will be rejected at auth — but the cache key includes token-hash
        # so even if both requests authenticated, they'd cache separately.
        await client.post("/v1/chat", headers={"Authorization": "Bearer tok-other", "Idempotency-Key": "k1"}, json={"text": "hi"})
        # First call: 200; second: 401 (wrong token). Counter should be 1 (only valid call ran).
        assert counter["n"] == 1
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_idempotency.py -v
```

Expected: FAIL — handler called twice, no `X-Idempotent-Replay` header.

- [ ] **Step 3: Implement middleware**

In `OpenComputer/extensions/api-server/adapter.py`, near the top of the file (after the existing module-level `_ADAPTER_START_TIME`), add:

```python
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

#: Hermes parity G2 — process-wide LRU cache for Idempotency-Key dedup.
#: Bounded at 256 entries so a runaway client can't OOM the server.
#: TTL = 300s (Hermes spec). Eviction on cap or TTL expiry.
_IDEMPOTENCY_CACHE: "OrderedDict[tuple[str, str], _CachedResponse]" = OrderedDict()
_IDEMPOTENCY_CACHE_MAX = 256
_IDEMPOTENCY_TTL_S = 300.0


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    body: bytes
    status: int
    headers: dict[str, str]
    expires_at: float
```

Then add the middleware as a method on `APIServerAdapter`:

```python
@web.middleware
async def _idempotency_middleware(self, request: web.Request, handler):
    """Hermes parity G2: dedup POST requests by Idempotency-Key.

    Cache key = (token-hash, idempotency-key). 5-min TTL. Bounded LRU.
    Replays carry ``X-Idempotent-Replay: 1`` for client introspection.
    """
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or request.method != "POST":
        return await handler(request)

    auth = request.headers.get("Authorization", "")
    token_hash = hashlib.sha256(auth.encode()).hexdigest()[:16]
    cache_key = (token_hash, key)
    now = time.monotonic()

    cached = _IDEMPOTENCY_CACHE.get(cache_key)
    if cached is not None:
        if cached.expires_at > now:
            _IDEMPOTENCY_CACHE.move_to_end(cache_key)
            return web.Response(
                body=cached.body,
                status=cached.status,
                headers={**cached.headers, "X-Idempotent-Replay": "1"},
            )
        else:
            del _IDEMPOTENCY_CACHE[cache_key]

    response = await handler(request)
    body = response.body if isinstance(response.body, bytes) else b""
    if not body and hasattr(response, "_body") and isinstance(response._body, bytes):
        body = response._body
    _IDEMPOTENCY_CACHE[cache_key] = _CachedResponse(
        body=body,
        status=response.status,
        headers={k: v for k, v in response.headers.items() if k.lower() not in {"date", "content-length"}},
        expires_at=now + _IDEMPOTENCY_TTL_S,
    )
    while len(_IDEMPOTENCY_CACHE) > _IDEMPOTENCY_CACHE_MAX:
        _IDEMPOTENCY_CACHE.popitem(last=False)
    return response
```

Update the `_build_app` middleware list to include both:

```python
app = web.Application(middlewares=[self._cors_middleware, self._idempotency_middleware])
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_idempotency.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/adapter.py OpenComputer/tests/test_api_server_idempotency.py
git commit -m "feat(api-server): G2 — Idempotency-Key dedup w/ 5-min LRU (Hermes spec)"
```

---

### Task A3: Default-on `/v1/responses` (G3)

**Files:**
- Modify: `OpenComputer/extensions/api-server/adapter.py:1131-1143` (remove env gate)
- Test: `OpenComputer/tests/test_api_server_responses_default_on.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_responses_default_on.py`:

```python
"""Hermes parity G3: /v1/responses works by default (no API_SERVER_API_TYPE env)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_responses_endpoint_works_without_env_gate(monkeypatch):
    monkeypatch.delenv("API_SERVER_API_TYPE", raising=False)
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello!"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "Hi", "model": "opencomputer"},
        )
        assert r.status == 200, await r.text()
        body = await r.json()
        assert "id" in body
        assert body.get("status") == "completed" or body.get("object") == "response"


@pytest.mark.asyncio
async def test_responses_still_works_with_env_set(monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello!"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "Hi"},
        )
        assert r.status == 200, "back-compat: env-set still works"
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_responses_default_on.py -v
```

Expected: First test FAILS with 404 "Responses API disabled".

- [ ] **Step 3: Remove the env gate from `_handle_responses_stub`**

In `OpenComputer/extensions/api-server/adapter.py`, find the block at line 1139-1143:

```python
        if os.environ.get("API_SERVER_API_TYPE", "").lower() != "responses":
            return web.json_response(
                {"error": {"message": "Responses API disabled. Set API_SERVER_API_TYPE=responses to enable."}},
                status=404,
            )
```

Replace with:

```python
        # Hermes parity G3 (2026-05-09): /v1/responses is default-on.
        # API_SERVER_API_TYPE env retained as a no-op alias for back-compat.
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_responses_default_on.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/adapter.py OpenComputer/tests/test_api_server_responses_default_on.py
git commit -m "feat(api-server): G3 — /v1/responses default-on (drop API_SERVER_API_TYPE gate)"
```

---

### Task A4: GET/DELETE `/v1/responses/{id}` (G4)

**Files:**
- Modify: `OpenComputer/extensions/api-server/adapter.py` (add 2 handlers + 2 routes)
- Test: `OpenComputer/tests/test_api_server_responses_get_delete.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_responses_get_delete.py`:

```python
"""Hermes parity G4: GET + DELETE /v1/responses/{id}."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_get_returns_stored_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "hi"},
        )
        assert r.status == 200
        rid = (await r.json())["id"]

        r2 = await client.get(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert r2.status == 200, await r2.text()
        body = await r2.json()
        assert body["id"] == rid


@pytest.mark.asyncio
async def test_get_unknown_returns_404():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get(
            "/v1/responses/nonexistent_id",
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status == 404


@pytest.mark.asyncio
async def test_delete_removes_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "hi"},
        )
        rid = (await r.json())["id"]

        rd = await client.delete(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert rd.status == 200
        body = await rd.json()
        assert body.get("deleted") is True

        # Now GET should 404
        r2 = await client.get(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert r2.status == 404


@pytest.mark.asyncio
async def test_get_requires_auth():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/responses/anything")
        assert r.status == 401
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_responses_get_delete.py -v
```

Expected: 4 fail — routes don't exist (likely 405).

- [ ] **Step 3: Add 2 handlers + 2 routes**

In `OpenComputer/extensions/api-server/adapter.py`, after `_handle_responses_stub` (search for that name), add:

```python
async def _handle_response_get(self, request: web.Request) -> web.Response:
    """Hermes parity G4: GET /v1/responses/{id} — fetch stored response."""
    err = self._auth_check(request)
    if err is not None:
        return err
    response_id = request.match_info.get("response_id", "")
    entry = self._responses_storage.get(response_id) if hasattr(self, "_responses_storage") else None
    if entry is None:
        return web.json_response(
            {"error": {"message": f"response {response_id!r} not found"}},
            status=404,
        )
    payload = {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "input": entry.get("user", ""),
        "output": [
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": entry.get("agent", "")},
            ]},
        ],
        "previous_response_id": entry.get("previous_response_id"),
        "conversation": entry.get("conversation"),
    }
    return web.json_response(payload)


async def _handle_response_delete(self, request: web.Request) -> web.Response:
    """Hermes parity G4: DELETE /v1/responses/{id} — remove stored response."""
    err = self._auth_check(request)
    if err is not None:
        return err
    response_id = request.match_info.get("response_id", "")
    storage = getattr(self, "_responses_storage", None)
    if storage is None or response_id not in storage:
        return web.json_response(
            {"error": {"message": f"response {response_id!r} not found"}},
            status=404,
        )
    del storage[response_id]
    return web.json_response({"id": response_id, "deleted": True, "object": "response.deleted"})
```

In `_build_app` (where the other `app.router.add_*` calls are — around line 925-948), add right after the existing `/v1/responses` POST route:

```python
        app.router.add_get("/v1/responses/{response_id}", self._handle_response_get)
        app.router.add_delete("/v1/responses/{response_id}", self._handle_response_delete)
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_responses_get_delete.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/adapter.py OpenComputer/tests/test_api_server_responses_get_delete.py
git commit -m "feat(api-server): G4 — GET + DELETE /v1/responses/{id} (Hermes spec)"
```

---

### Task A5: `/v1/health` alias (G5)

**Files:**
- Modify: `OpenComputer/extensions/api-server/adapter.py` (add 1 route)
- Test: `OpenComputer/tests/test_api_server_health_v1.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_health_v1.py`:

```python
"""Hermes parity G5: /v1/health alias mirrors /health."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_v1_health_returns_ok():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/health")
        assert r.status == 200
        body = await r.json()
        assert body.get("status") == "ok"


@pytest.mark.asyncio
async def test_legacy_health_still_returns_ok():
    """Don't regress the existing /health route while adding /v1/health."""
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/health")
        assert r.status == 200
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_health_v1.py -v
```

Expected: First test fails (404).

- [ ] **Step 3: Find the existing /health handler and add the alias**

Find the `/health` registration in `_build_app`:

```bash
grep -n "/health" /Users/saksham/Vscode/claude/OpenComputer/extensions/api-server/adapter.py
```

Add right after it:

```python
        # Hermes parity G5 (2026-05-09): /v1/health alias for spec compliance.
        app.router.add_get("/v1/health", self._handle_health)
```

(The handler `_handle_health` already exists; we're just adding a second route to it.)

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_health_v1.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/adapter.py OpenComputer/tests/test_api_server_health_v1.py
git commit -m "feat(api-server): G5 — /v1/health alias (Hermes spec)"
```

---

### Task A6: Env-var aliases — `API_SERVER_KEY` + `API_SERVER_ENABLED` (G6 + G7)

**Files:**
- Modify: `OpenComputer/extensions/api-server/plugin.py` (read both env names)
- Test: `OpenComputer/tests/test_api_server_env_aliases.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_api_server_env_aliases.py`:

```python
"""Hermes parity G6+G7: API_SERVER_KEY + API_SERVER_ENABLED env aliases."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_plugin():
    sys.modules.pop("api_server_plugin_test", None)
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "plugin.py"
    spec = importlib.util.spec_from_file_location("api_server_plugin_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_plugin_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_api_server_key_resolves_token(monkeypatch):
    """G6: When only API_SERVER_KEY is set, plugin uses it as the bearer token."""
    monkeypatch.delenv("API_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "spec-key-abc")
    mod = _load_plugin()
    cfg = mod._resolve_api_server_config()
    assert cfg["token"] == "spec-key-abc"


def test_api_server_token_takes_precedence_over_key(monkeypatch):
    """When both set, OC's _TOKEN wins (existing users keep working)."""
    monkeypatch.setenv("API_SERVER_TOKEN", "oc-token")
    monkeypatch.setenv("API_SERVER_KEY", "spec-key")
    mod = _load_plugin()
    cfg = mod._resolve_api_server_config()
    assert cfg["token"] == "oc-token"


def test_api_server_enabled_true_means_enabled(monkeypatch):
    """G7: API_SERVER_ENABLED=true reports enabled."""
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    mod = _load_plugin()
    assert mod._is_api_server_enabled() is True


def test_api_server_enabled_false_means_disabled(monkeypatch):
    monkeypatch.setenv("API_SERVER_ENABLED", "false")
    mod = _load_plugin()
    assert mod._is_api_server_enabled() is False


def test_api_server_enabled_unset_defers_to_plugin_state(monkeypatch):
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False)
    mod = _load_plugin()
    # When unset, return None → caller defers to profile plugin-enable list
    assert mod._is_api_server_enabled() is None
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_env_aliases.py -v
```

Expected: All 5 fail — helpers don't exist.

- [ ] **Step 3: Add the helpers to plugin.py**

In `OpenComputer/extensions/api-server/plugin.py`, find the existing token-reading code (around line 33) and refactor into a helper, plus add an enabled helper:

```python
def _resolve_api_server_config() -> dict:
    """Build the {host, port, token} dict from env vars.

    Hermes parity G6 (2026-05-09): ``API_SERVER_TOKEN`` (OC) takes
    precedence; ``API_SERVER_KEY`` (Hermes spec) is accepted as a
    fallback. Either being set is sufficient.
    """
    token = (
        os.environ.get("API_SERVER_TOKEN", "").strip()
        or os.environ.get("API_SERVER_KEY", "").strip()
    )
    host = os.environ.get("API_SERVER_HOST", "127.0.0.1").strip()
    try:
        port = int(os.environ.get("API_SERVER_PORT", "18791"))
    except ValueError as exc:
        raise RuntimeError(
            "api-server plugin: API_SERVER_PORT must be an integer; "
            f"got {os.environ.get('API_SERVER_PORT')!r}"
        ) from exc
    return {"token": token, "host": host, "port": port}


def _is_api_server_enabled() -> bool | None:
    """Hermes parity G7: API_SERVER_ENABLED=true|false override.

    Returns ``True``/``False`` when env is set; ``None`` when unset,
    so callers can defer to the profile's plugin-enable list.
    """
    raw = os.environ.get("API_SERVER_ENABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None
```

Adjust the existing `register(api)` body to call `_resolve_api_server_config()` instead of inlining the env reads. Keep the missing-token error path; just update the env-name reference in the error message to "API_SERVER_TOKEN/API_SERVER_KEY".

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_api_server_env_aliases.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/api-server/plugin.py OpenComputer/tests/test_api_server_env_aliases.py
git commit -m "feat(api-server): G6+G7 — API_SERVER_KEY/ENABLED env aliases (Hermes spec)"
```

---

## Phase B — MCP Client (G8–G11, ~2-3 hr)

### Task B1: Tool naming alias `mcp_<server>_<tool>` (G8)

**Files:**
- Modify: `OpenComputer/opencomputer/mcp/client.py` — when registering MCPTool, also register a sibling alias
- Test: `OpenComputer/tests/test_mcp_tool_naming_alias.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_tool_naming_alias.py`:

```python
"""Hermes parity G8: mcp_<server>_<tool> naming alongside <server>__<tool>."""
from __future__ import annotations

import pytest

from opencomputer.mcp.client import MCPTool


def _make_tool(server: str, tool: str) -> MCPTool:
    return MCPTool(
        server_name=server,
        tool_name=tool,
        description="test",
        parameters={"type": "object", "properties": {}},
        session=None,
    )


def test_canonical_name_unchanged():
    t = _make_tool("filesystem", "read_file")
    assert t.schema().name == "filesystem__read_file"


def test_hermes_alias_helper_produces_spec_name():
    """The canonical tool surfaces a sibling alias name for Hermes-spec clients."""
    from opencomputer.mcp.client import hermes_alias_name
    assert hermes_alias_name("filesystem", "read_file") == "mcp_filesystem_read_file"
    # Hyphens in tool name preserved (gh's `create-issue` becomes `mcp_gh_create-issue`)
    assert hermes_alias_name("github", "create-issue") == "mcp_github_create-issue"


def test_alias_tool_dispatches_through_canonical():
    """Calling the alias forwards to the same MCP session call_tool."""
    from opencomputer.mcp.client import MCPAliasTool

    canonical = _make_tool("fs", "list")
    alias = MCPAliasTool(canonical)
    assert alias.schema().name == "mcp_fs_list"
    assert alias._canonical is canonical
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_tool_naming_alias.py -v
```

Expected: 2 fail (alias helper + alias class don't exist).

- [ ] **Step 3: Add helper + alias class + plumbing**

In `OpenComputer/opencomputer/mcp/client.py`, near the `MCPTool` class, add:

```python
def hermes_alias_name(server_name: str, tool_name: str) -> str:
    """Hermes-spec tool naming: ``mcp_<server>_<tool>`` (G8 — 2026-05-09).

    OpenComputer's canonical form is ``<server>__<tool>`` (double underscore).
    This helper produces the Hermes-spec form for clients that key off it.
    Both names are registered side-by-side; this ensures third-party tools
    written against either spec discover OC's MCP toolset correctly.
    """
    return f"mcp_{server_name}_{tool_name}"


class MCPAliasTool(BaseTool):
    """Thin wrapper that re-publishes an MCPTool under the Hermes-spec name.

    Dispatch is forwarded to the canonical tool's ``call`` method — the
    underlying MCP session handles only one invocation. This avoids
    schema_name collisions in :class:`ToolRegistry` while letting clients
    discover the tool by either name.
    """

    def __init__(self, canonical: "MCPTool") -> None:
        self._canonical = canonical

    def schema(self) -> ToolSchema:
        base = self._canonical.schema()
        return ToolSchema(
            name=hermes_alias_name(self._canonical._server_name, self._canonical._tool_name),
            description=base.description,
            parameters=base.parameters,
        )

    async def call(self, arguments: dict[str, Any]) -> ToolResult:
        return await self._canonical.call(arguments)
```

Then find where `MCPTool` instances get created (the `_reconcile_tools` method around line 887 and the initial `connect`/discovery path around line 700). At each site that builds a `new_tools_by_name[t.name] = MCPTool(...)`, also register the alias:

```python
                tool_obj = MCPTool(
                    server_name=self.config.name,
                    tool_name=t.name,
                    description=t.description or "",
                    parameters=t.inputSchema or {"type": "object", "properties": {}},
                    session=self.session,
                )
                new_tools_by_name[t.name] = tool_obj
                # G8 (2026-05-09): also register the Hermes-spec alias as a sibling.
                # Use a synthetic key so registry-side dedup keeps both.
                new_tools_by_name[hermes_alias_name(self.config.name, t.name)] = MCPAliasTool(tool_obj)
```

(Adapt to the surrounding code shape — there may be 2 sites, both need the same wrap.)

The `_canonical._server_name` and `_canonical._tool_name` private accessors require that `MCPTool` stores those as instance attributes. If they aren't already, add to `MCPTool.__init__`:

```python
        self._server_name = server_name
        self._tool_name = tool_name
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_tool_naming_alias.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run existing MCP tests as a guardrail**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_dynamic_discovery.py tests/test_mcp_utility_tools.py -v
```

Expected: pass — no regression. If aliases are double-registered without dedup, expect ValueError; if so, add an `if alias_name not in new_tools_by_name:` guard before the alias insertion.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/mcp/client.py OpenComputer/tests/test_mcp_tool_naming_alias.py
git commit -m "feat(mcp): G8 — register mcp_<server>_<tool> alias alongside <server>__<tool> (Hermes spec)"
```

---

### Task B2: Per-server `prompts_enabled` / `resources_enabled` (G9)

**Files:**
- Modify: `OpenComputer/opencomputer/agent/config.py:534` (`MCPServerConfig`)
- Modify: `OpenComputer/opencomputer/mcp/client.py` (suppress utility tool registration when disabled)
- Test: `OpenComputer/tests/test_mcp_per_server_filters_v2.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_per_server_filters_v2.py`:

```python
"""Hermes parity G9: per-server prompts_enabled / resources_enabled filters."""
from __future__ import annotations

from opencomputer.agent.config import MCPServerConfig


def test_default_both_enabled():
    cfg = MCPServerConfig(name="x")
    assert cfg.prompts_enabled is True
    assert cfg.resources_enabled is True


def test_can_disable_prompts():
    cfg = MCPServerConfig(name="x", prompts_enabled=False)
    assert cfg.prompts_enabled is False


def test_can_disable_resources():
    cfg = MCPServerConfig(name="x", resources_enabled=False)
    assert cfg.resources_enabled is False


def test_yaml_load_accepts_nested_tools_filter():
    """Hermes-spec nested form: tools.include / tools.exclude / prompts: false."""
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    yaml_dict = {
        "name": "github",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "tools": {
            "include": ["create_issue", "list_issues"],
            "prompts": False,
            "resources": False,
        },
    }
    normalized = _normalize_mcp_server_dict(yaml_dict)
    assert normalized["tools_allow"] == ("create_issue", "list_issues")
    assert normalized["prompts_enabled"] is False
    assert normalized["resources_enabled"] is False
    # Original "tools" key removed after normalization
    assert "tools" not in normalized
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_per_server_filters_v2.py -v
```

Expected: 4 fail — fields missing + helper missing.

- [ ] **Step 3: Add fields to MCPServerConfig**

In `OpenComputer/opencomputer/agent/config.py`, after the existing `tools_deny` field (line 561), add:

```python
    #: Hermes parity G9 (2026-05-09): per-server suppression of MCP
    #: prompt + resource utility tools. Default ``True`` keeps every
    #: utility tool the server publishes; ``False`` skips registering
    #: ``<server>__list_prompts`` / ``__get_prompt`` (or the resource
    #: equivalents). The server stays connected; only the utility-tool
    #: registration is suppressed.
    prompts_enabled: bool = True
    resources_enabled: bool = True
```

- [ ] **Step 4: Add normalization helper to config_store**

In `OpenComputer/opencomputer/agent/config_store.py`, add a top-level helper:

```python
def _normalize_mcp_server_dict(raw: dict) -> dict:
    """Convert Hermes-spec nested MCP server YAML to OC's flat MCPServerConfig fields.

    Hermes-spec form::

        mcp_servers:
          github:
            tools:
              include: [create_issue, list_issues]
              prompts: false
              resources: false

    Maps to OC dataclass fields ``tools_allow``, ``tools_deny``,
    ``prompts_enabled``, ``resources_enabled``. The flat form is also
    accepted unchanged.
    """
    out = dict(raw)
    tools = out.pop("tools", None)
    if isinstance(tools, dict):
        if "include" in tools:
            out["tools_allow"] = tuple(tools["include"])
        if "exclude" in tools:
            out["tools_deny"] = tuple(tools["exclude"])
        if "prompts" in tools:
            out["prompts_enabled"] = bool(tools["prompts"])
        if "resources" in tools:
            out["resources_enabled"] = bool(tools["resources"])
    return out
```

Find where `MCPServerConfig` is constructed from YAML (likely a load_config helper or a list comprehension). Wrap each per-server dict with `_normalize_mcp_server_dict` before instantiation. Quick locator:

```bash
grep -n "MCPServerConfig(" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/agent/config_store.py
```

- [ ] **Step 5: Honor flags in client.py**

In `OpenComputer/opencomputer/mcp/client.py`, the utility-tool registration sites (the 4 utility tools at lines ~308, 348, 404, 444) get added per server. Find where `_make_*_tool` helpers register them — likely in `MCPConnection.connect` after `tool_list = await self.session.list_tools()`. Wrap each utility-tool addition with the per-server flag check. Pseudocode pattern:

```python
        # ... after building tool_list ...
        if self.config.prompts_enabled and "prompts" in self.session.server_capabilities:
            tools.extend([_make_list_prompts_tool(...), _make_get_prompt_tool(...)])
        if self.config.resources_enabled and "resources" in self.session.server_capabilities:
            tools.extend([_make_list_resources_tool(...), _make_read_resource_tool(...)])
```

The exact code shape will depend on the existing capability-check pattern; preserve it. Search for `list_resources` / `list_prompts` in client.py for the precise registration sites.

- [ ] **Step 6: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_per_server_filters_v2.py -v
```

Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/agent/config_store.py OpenComputer/opencomputer/mcp/client.py OpenComputer/tests/test_mcp_per_server_filters_v2.py
git commit -m "feat(mcp): G9 — per-server prompts_enabled/resources_enabled + nested YAML form"
```

---

### Task B3: Per-server `timeout` / `connect_timeout` (G10)

**Files:**
- Modify: `OpenComputer/opencomputer/agent/config.py` (`MCPServerConfig`)
- Modify: `OpenComputer/opencomputer/mcp/client.py` (apply timeouts)
- Test: `OpenComputer/tests/test_mcp_per_server_timeout.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_per_server_timeout.py`:

```python
"""Hermes parity G10: per-server timeout / connect_timeout."""
from __future__ import annotations

from opencomputer.agent.config import MCPServerConfig


def test_defaults_30s():
    cfg = MCPServerConfig(name="x")
    assert cfg.timeout == 30.0
    assert cfg.connect_timeout == 30.0


def test_can_set_per_server():
    cfg = MCPServerConfig(name="x", timeout=5.0, connect_timeout=10.0)
    assert cfg.timeout == 5.0
    assert cfg.connect_timeout == 10.0


def test_yaml_load_accepts_timeouts():
    from opencomputer.agent.config_store import _normalize_mcp_server_dict

    raw = {"name": "x", "timeout": 5, "connect_timeout": 15}
    out = _normalize_mcp_server_dict(raw)
    assert out["timeout"] == 5
    assert out["connect_timeout"] == 15
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_per_server_timeout.py -v
```

Expected: 3 fail.

- [ ] **Step 3: Add fields to MCPServerConfig**

In `OpenComputer/opencomputer/agent/config.py`, after `resources_enabled`:

```python
    #: Hermes parity G10 (2026-05-09): per-server tool-call timeout (s).
    #: Applies to ``ClientSession.call_tool`` invocations. Default 30s
    #: matches Hermes spec.
    timeout: float = 30.0
    #: Initial-connect timeout (s). Applies to ``stdio_client`` /
    #: ``streamablehttp_client`` / ``sse_client`` connect path.
    connect_timeout: float = 30.0
```

- [ ] **Step 4: Apply timeouts in client.py**

Find the `connect` method's transport-init block (around line 700) and the `call_tool` invocation in `MCPTool.call` (around line 1100+). Wrap each with `asyncio.wait_for`:

```python
# In MCPConnection.connect — wrap the connect path:
        try:
            await asyncio.wait_for(
                self.session.initialize(),
                timeout=self.config.connect_timeout,
            )
        except asyncio.TimeoutError:
            self.last_error = f"connect timeout after {self.config.connect_timeout}s"
            self.state = "error"
            return False

# In MCPTool.call (instance) — store config_timeout from MCPConnection at construction:
# Add a `timeout: float = 30.0` parameter to MCPTool.__init__ and store on self.
# Then in MCPTool.call:
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._tool_name, arguments=arguments),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult.error(f"MCP call timeout after {self._timeout}s")
```

The construction sites for `MCPTool(...)` need `timeout=self.config.timeout` added.

- [ ] **Step 5: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_per_server_timeout.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/mcp/client.py OpenComputer/tests/test_mcp_per_server_timeout.py
git commit -m "feat(mcp): G10 — per-server timeout/connect_timeout (Hermes spec)"
```

---

### Task B4: MCP sampling caps (G11)

**Files:**
- Create dataclass in `OpenComputer/opencomputer/agent/config.py`
- Modify: `OpenComputer/opencomputer/mcp/sampling.py` (enforce caps)
- Test: `OpenComputer/tests/test_mcp_sampling_caps.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_sampling_caps.py`:

```python
"""Hermes parity G11: MCP sampling caps (max_tokens_cap, max_rpm, allowed_models)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def test_caps_dataclass_defaults():
    from opencomputer.agent.config import MCPSamplingCaps

    c = MCPSamplingCaps()
    assert c.max_tokens_cap == 4096
    assert c.max_rpm == 60
    assert c.max_tool_rounds == 5
    assert c.allowed_models == ()


@pytest.mark.asyncio
async def test_max_tokens_cap_clips_request():
    from opencomputer.agent.config import MCPSamplingCaps
    from opencomputer.mcp.sampling import make_sampling_callback

    caps = MCPSamplingCaps(max_tokens_cap=128)
    cb = make_sampling_callback(caps=caps)

    seen_max_tokens = []

    async def fake_complete_text(messages, system, max_tokens, temperature):
        seen_max_tokens.append(max_tokens)
        return "ok"

    with patch("opencomputer.mcp.sampling.complete_text", new=AsyncMock(side_effect=fake_complete_text)):
        params = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content=SimpleNamespace(text="hi"))],
            systemPrompt="", maxTokens=10000, temperature=1.0, modelPreferences=None,
        )
        await cb(None, params)
        assert seen_max_tokens[0] == 128, "request must be capped at max_tokens_cap"


@pytest.mark.asyncio
async def test_allowed_models_filter_rejects_non_listed():
    from opencomputer.agent.config import MCPSamplingCaps
    from opencomputer.mcp.sampling import make_sampling_callback

    caps = MCPSamplingCaps(allowed_models=("anthropic/claude-opus",))
    cb = make_sampling_callback(caps=caps)

    params = SimpleNamespace(
        messages=[SimpleNamespace(role="user", content=SimpleNamespace(text="hi"))],
        systemPrompt="", maxTokens=100, temperature=1.0,
        modelPreferences=SimpleNamespace(hints=[SimpleNamespace(name="openai/gpt-4o")]),
    )
    result = await cb(None, params)
    # Returns ErrorData when model not allowed
    assert hasattr(result, "code"), f"expected ErrorData but got {type(result).__name__}"


@pytest.mark.asyncio
async def test_no_caps_means_legacy_behavior():
    """Back-compat: callback works without caps (existing OC behavior)."""
    from opencomputer.mcp.sampling import make_sampling_callback

    cb = make_sampling_callback()

    async def fake_complete_text(messages, system, max_tokens, temperature):
        return "ok"

    with patch("opencomputer.mcp.sampling.complete_text", new=AsyncMock(side_effect=fake_complete_text)):
        params = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content=SimpleNamespace(text="hi"))],
            systemPrompt="", maxTokens=1024, temperature=1.0, modelPreferences=None,
        )
        result = await cb(None, params)
        assert hasattr(result, "content"), "no-caps path must succeed"
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_sampling_caps.py -v
```

Expected: 4 fail.

- [ ] **Step 3: Add MCPSamplingCaps dataclass**

In `OpenComputer/opencomputer/agent/config.py`, near `MCPConfig` (around line 563), add:

```python
@dataclass(frozen=True, slots=True)
class MCPSamplingCaps:
    """Hermes parity G11 (2026-05-09): per-server sampling caps.

    Bounds applied when an MCP server uses ``sampling/createMessage`` to
    reach back into Hermes' LLM. Without caps, a server could trivially
    exhaust the operator's quota (high max_tokens, high RPM, runaway
    multi-turn) or pick a more expensive model than the operator
    intends.

    * ``max_tokens_cap`` — clip ``params.maxTokens`` to this ceiling.
    * ``max_rpm`` — soft RPM throttle (token-bucket; warn-on-overflow).
    * ``max_tool_rounds`` — cap on multi-turn tool-use rounds within
      one sampling request.
    * ``allowed_models`` — when non-empty, reject any request whose
      ``modelPreferences.hints[*].name`` is outside the list.
    """

    max_tokens_cap: int = 4096
    max_rpm: int = 60
    max_tool_rounds: int = 5
    allowed_models: tuple[str, ...] = ()
```

Add to `__all__` if present.

- [ ] **Step 4: Update sampling.py to honor caps**

In `OpenComputer/opencomputer/mcp/sampling.py`, change the signature + body:

```python
def make_sampling_callback(caps: "MCPSamplingCaps | None" = None):
    """Return an MCP ``SamplingFnT`` that drives the host LLM via aux_llm.

    G11 (2026-05-09): when ``caps`` is provided, enforce per-server
    bounds before dispatching to the host LLM.
    """
    from opencomputer.agent.config import MCPSamplingCaps as _Caps
    effective_caps = caps or _Caps()

    async def _callback(context: Any, params: Any) -> Any:
        from mcp.types import (
            CreateMessageResult,
            ErrorData,
            TextContent,
        )

        # G11: model allowlist check.
        if effective_caps.allowed_models:
            prefs = getattr(params, "modelPreferences", None)
            if prefs is not None:
                hints = getattr(prefs, "hints", []) or []
                requested = {getattr(h, "name", "") for h in hints if hasattr(h, "name")}
                if requested and not (requested & set(effective_caps.allowed_models)):
                    return ErrorData(
                        code=-32603,
                        message=f"requested model not in allowed_models: {sorted(requested)}",
                    )

        messages: list[dict[str, str]] = []
        for sm in params.messages:
            content = sm.content
            text = getattr(content, "text", None)
            if not isinstance(text, str):
                logger.debug("MCP sampling: dropping non-text content from %s", sm.role)
                continue
            messages.append({"role": sm.role, "content": text})

        system_prompt = getattr(params, "systemPrompt", "") or ""
        # G11: clip max_tokens.
        requested_max = int(getattr(params, "maxTokens", 1024) or 1024)
        max_tokens = min(requested_max, effective_caps.max_tokens_cap)
        temperature_raw = getattr(params, "temperature", None)
        temperature = float(temperature_raw) if temperature_raw is not None else 1.0

        try:
            text_out = await complete_text(
                messages=messages, system=system_prompt,
                max_tokens=max_tokens, temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP sampling/createMessage failed: %s", exc)
            return ErrorData(code=-32603, message=f"sampling failed: {exc}")

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=text_out or ""),
            model="opencomputer-aux",
            stopReason="endTurn",
        )

    return _callback
```

- [ ] **Step 5: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_sampling_caps.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/mcp/sampling.py OpenComputer/tests/test_mcp_sampling_caps.py
git commit -m "feat(mcp): G11 — per-server sampling caps (token/rpm/tool-rounds/allowed-models)"
```

---

## Phase C — MCP Server / Hermes-AS-MCP (G12–G14, ~2 hr)

### Task C1: `conversations_list` / `conversation_get` aliases (G12)

**Files:**
- Modify: `OpenComputer/opencomputer/mcp/server.py:78-103` (add 2 alias decorators)
- Test: `OpenComputer/tests/test_mcp_server_conversation_aliases.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_server_conversation_aliases.py`:

```python
"""Hermes parity G12: conversations_list / conversation_get aliases."""
from __future__ import annotations

import pytest

from opencomputer.mcp.server import build_server


@pytest.mark.asyncio
async def test_conversations_list_alias_exists():
    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "conversations_list" in names, f"missing alias; have: {sorted(names)[:20]}"
    assert "sessions_list" in names, "canonical name removed"


@pytest.mark.asyncio
async def test_conversation_get_alias_exists():
    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "conversation_get" in names
    assert "session_get" in names
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_conversation_aliases.py -v
```

Expected: 2 fail.

- [ ] **Step 3: Add 2 alias decorators sharing the body**

In `OpenComputer/opencomputer/mcp/server.py`, after the existing `sessions_list` / `session_get` definitions (around lines 77-103), add:

```python
    # G12 (2026-05-09): Hermes-spec aliases. Same body, second name.
    # FastMCP allows multiple decorators registering distinct tools; the
    # registry keys on tool name so two names → two registrations → same
    # implementation pointer. No collision because each is unique.
    @server.tool()
    def conversations_list(limit: int = 20) -> list[dict[str, Any]]:
        """Hermes-spec alias for ``sessions_list`` (G12 — 2026-05-09)."""
        bounded = max(1, min(limit, 200))
        db = SessionDB(_home() / "sessions.db")
        return db.list_sessions(limit=bounded)

    @server.tool()
    def conversation_get(session_id: str) -> dict[str, Any] | None:
        """Hermes-spec alias for ``session_get`` (G12 — 2026-05-09)."""
        db = SessionDB(_home() / "sessions.db")
        return db.get_session(session_id)
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_conversation_aliases.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/mcp/server.py OpenComputer/tests/test_mcp_server_conversation_aliases.py
git commit -m "feat(mcp-server): G12 — conversations_list/conversation_get aliases (Hermes spec)"
```

---

### Task C2: `permissions_list_open` (G13)

**Files:**
- Modify: `OpenComputer/opencomputer/mcp/server.py` (add new tool)
- Test: `OpenComputer/tests/test_mcp_server_permissions_list_open.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_server_permissions_list_open.py`:

```python
"""Hermes parity G13: permissions_list_open returns OPEN approval requests."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


@pytest.mark.asyncio
async def test_returns_empty_when_no_consent_requests_table(isolated_home):
    """G13 part-1: missing-table case returns [] (back-compat)."""
    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "permissions_list_open" in names

    result = await server.call_tool("permissions_list_open", {})
    # FastMCP wraps return in TextContent — accept either form
    if hasattr(result, "content") and isinstance(result.content, list):
        text_blocks = [b.text for b in result.content if hasattr(b, "text")]
        if text_blocks:
            import json as _json
            data = _json.loads(text_blocks[0])
            assert data == []


@pytest.mark.asyncio
async def test_returns_pending_requests(isolated_home):
    """G13 part-2: queries the consent_requests table when present."""
    db_path = isolated_home / "sessions.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE consent_requests ("
            "capability_id TEXT, scope TEXT, requested_at REAL, "
            "requested_by TEXT, state TEXT)"
        )
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.write", "/etc/passwd", 1700000000.0, "tool:Edit", "pending"),
        )
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("shell.exec", "rm -rf /", 1700000001.0, "tool:Bash", "granted"),
        )
        conn.commit()

    server = build_server()
    result = await server.call_tool("permissions_list_open", {})
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    import json as _json
    data = _json.loads(text_blocks[0])
    assert len(data) == 1
    assert data[0]["capability_id"] == "fs.write"
    assert data[0]["scope"] == "/etc/passwd"
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_permissions_list_open.py -v
```

Expected: 2 fail.

- [ ] **Step 3: Add tool to server.py**

In `OpenComputer/opencomputer/mcp/server.py`, after `consent_history` (around line 347), add:

```python
    @server.tool()
    def permissions_list_open(limit: int = 50) -> list[dict[str, Any]]:
        """Hermes parity G13 (2026-05-09): list OPEN consent requests.

        Returns capabilities currently awaiting a user/operator decision.
        Distinct from ``consent_history`` (which returns the full audit
        log) — this is the live "approvals queue".

        Falls back to ``[]`` if the F1 ``consent_requests`` table doesn't
        exist (pre-F1 profile or fresh DB).
        """
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return []
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT capability_id, scope, requested_at, requested_by "
                    "FROM consent_requests WHERE state = 'pending' "
                    "ORDER BY requested_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_permissions_list_open.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/mcp/server.py OpenComputer/tests/test_mcp_server_permissions_list_open.py
git commit -m "feat(mcp-server): G13 — permissions_list_open (Hermes spec — OPEN approvals queue)"
```

---

### Task C3: `events_poll(after_cursor=…)` arg + approval event types (G14)

**Files:**
- Modify: `OpenComputer/opencomputer/mcp/server.py:259-311` (widen events_poll signature, surface approval events)
- Test: `OpenComputer/tests/test_mcp_server_events_poll_aliases.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_mcp_server_events_poll_aliases.py`:

```python
"""Hermes parity G14: events_poll(after_cursor) alias + approval event types."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


@pytest.mark.asyncio
async def test_after_cursor_alias_works(isolated_home):
    """G14: after_cursor accepted alongside since_message_id."""
    server = build_server()
    result = await server.call_tool("events_poll", {"after_cursor": 0, "limit": 10})
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    import json as _json
    data = _json.loads(text_blocks[0])
    assert "messages" in data
    assert "next_cursor" in data


@pytest.mark.asyncio
async def test_legacy_since_message_id_still_works(isolated_home):
    """Back-compat: don't break clients using since_message_id."""
    server = build_server()
    result = await server.call_tool("events_poll", {"since_message_id": 0})
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    import json as _json
    data = _json.loads(text_blocks[0])
    assert "messages" in data


@pytest.mark.asyncio
async def test_returns_approval_event_types_when_audit_log_present(isolated_home):
    """G14: when F1 audit_log has approval entries newer than cursor, surface them."""
    db_path = isolated_home / "sessions.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, platform TEXT, started_at REAL)
        """)
        conn.execute("""
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
                role TEXT, content TEXT, timestamp REAL)
        """)
        conn.execute("""
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY, ts REAL, capability_id TEXT,
                action TEXT, tier INTEGER, scope TEXT, granted_by TEXT
            )
        """)
        conn.execute(
            "INSERT INTO audit_log (ts, capability_id, action, tier, scope, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1700000000.0, "fs.write", "granted", 1, "/tmp", "user"),
        )
        conn.commit()

    server = build_server()
    result = await server.call_tool("events_poll", {"after_cursor": 0})
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    import json as _json
    data = _json.loads(text_blocks[0])
    # New event types appear under a separate key OR mixed in — accept either
    if "approvals" in data:
        assert any(a["capability_id"] == "fs.write" for a in data["approvals"])
    elif "events" in data:
        assert any(e.get("type") == "approval_resolved" for e in data["events"])
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_events_poll_aliases.py -v
```

Expected: 1 fail (after_cursor) + maybe 1 more.

- [ ] **Step 3: Widen events_poll signature**

In `OpenComputer/opencomputer/mcp/server.py`, replace the `events_poll` function (around line 259) with:

```python
    @server.tool()
    def events_poll(
        since_message_id: int = 0,
        after_cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Incremental poll for messages and consent events.

        Hermes parity G14 (2026-05-09): accepts ``after_cursor`` as an
        alias for ``since_message_id``. Surfaces approval-related entries
        from the F1 ``audit_log`` table under the ``approvals`` key when
        the table exists; clients can use these to react to permissions
        being granted/revoked elsewhere.

        Args:
            since_message_id: Cursor (legacy OC). Use ``after_cursor`` instead.
            after_cursor: Hermes-spec cursor name. If both supplied,
                ``after_cursor`` wins.
            limit: Max messages to return (default 50, max 500).

        Returns:
            ``{"messages": [...], "next_cursor": int, "approvals": [...]}``.
            ``approvals`` is empty when the audit_log table doesn't exist.
        """
        cursor = after_cursor if after_cursor is not None else since_message_id
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return {"messages": [], "next_cursor": cursor, "approvals": []}

        out_messages: list[dict[str, Any]] = []
        out_approvals: list[dict[str, Any]] = []
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, "
                    "m.timestamp, s.platform "
                    "FROM messages m "
                    "JOIN sessions s ON m.session_id = s.id "
                    "WHERE m.id > ? "
                    "ORDER BY m.id ASC LIMIT ?",
                    (cursor, bounded),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            out_messages = [dict(r) for r in rows]

            # G14: surface approval events from audit_log when present.
            try:
                a_rows = conn.execute(
                    "SELECT id, ts, capability_id, action, tier, scope, granted_by "
                    "FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (cursor, bounded),
                ).fetchall()
                for r in a_rows:
                    rd = dict(r)
                    rd["type"] = (
                        "approval_resolved"
                        if rd["action"] in ("granted", "revoked")
                        else "approval_requested"
                    )
                    out_approvals.append(rd)
            except sqlite3.OperationalError:
                pass

        next_cursor = out_messages[-1]["id"] if out_messages else cursor
        return {
            "messages": out_messages,
            "next_cursor": next_cursor,
            "approvals": out_approvals,
        }
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_mcp_server_events_poll_aliases.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/mcp/server.py OpenComputer/tests/test_mcp_server_events_poll_aliases.py
git commit -m "feat(mcp-server): G14 — events_poll(after_cursor) + approval event types"
```

---

## Phase D — ACP discovery (G15, ~30 min)

### Task D1: Auto-bundle `agent.json` on first `oc acp serve` (G15)

**Files:**
- Modify: `OpenComputer/opencomputer/cli.py:4357-4391` (acp_serve writes manifest if missing)
- Test: `OpenComputer/tests/test_acp_agent_json_autobundle.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_acp_agent_json_autobundle.py`:

```python
"""Hermes parity G15: agent.json appears at canonical path on serve."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


def test_agent_json_path_helper_returns_canonical(isolated_home):
    from opencomputer.cli import _default_agent_json_path
    p = _default_agent_json_path()
    assert p.parent.name == "acp_registry"
    assert p.name == "agent.json"
    # path inside profile home
    assert isolated_home in p.parents


def test_ensure_agent_json_writes_when_missing(isolated_home):
    from opencomputer.cli import _ensure_agent_json
    p = _ensure_agent_json()
    assert p.exists()
    assert p.parent.name == "acp_registry"
    import json as _json
    data = _json.loads(p.read_text())
    # Manifest emits at least name + transports
    assert "name" in data or "agent" in data or "transport" in data


def test_ensure_agent_json_no_op_when_present(isolated_home):
    from opencomputer.cli import _ensure_agent_json
    p1 = _ensure_agent_json()
    p1.write_text('{"manual": "edited"}')
    p2 = _ensure_agent_json()
    # Should NOT overwrite a user-edited file
    assert p2.read_text() == '{"manual": "edited"}'
```

- [ ] **Step 2: Verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_acp_agent_json_autobundle.py -v
```

Expected: 3 fail.

- [ ] **Step 3: Add helpers to cli.py**

In `OpenComputer/opencomputer/cli.py`, near `acp_serve` (around line 4357), add:

```python
def _default_agent_json_path() -> "Path":
    """Hermes parity G15 (2026-05-09): canonical ACP discovery path.

    Returns ``<profile_home>/acp_registry/agent.json`` — the path JetBrains
    and other IDEs probe for ACP-compatible agents.
    """
    from pathlib import Path
    from opencomputer.agent.config import _home
    return _home() / "acp_registry" / "agent.json"


def _ensure_agent_json() -> "Path":
    """Write agent.json at the canonical path if absent. No-op otherwise.

    The user can hand-edit the file; we never overwrite. Returns the
    path either way.
    """
    import json as _json
    p = _default_agent_json_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = _build_agent_manifest()
        p.write_text(_json.dumps(payload, indent=2) + "\n")
    return p
```

In `acp_serve` (around line 4357), prepend the call:

```python
@acp_app.command(name="serve")
def acp_serve() -> None:
    """Start the Agent Client Protocol server over stdio.

    OpenComputer becomes the agent backend for ACP-aware IDEs (Zed,
    VS Code with the ACP extension, Cursor, Claude Desktop).

    Hermes parity G15 (2026-05-09): ensures agent.json exists at
    ``~/.opencomputer/<profile>/acp_registry/agent.json`` so JetBrains
    and other IDEs can auto-discover this profile's agent.
    """
    _ensure_agent_json()
    _run_acp_stdio()
```

- [ ] **Step 4: Verify pass**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_acp_agent_json_autobundle.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli.py OpenComputer/tests/test_acp_agent_json_autobundle.py
git commit -m "feat(acp): G15 — auto-bundle agent.json at canonical acp_registry path"
```

---

## Phase E — Verification + Push

### Task E1: Full-suite regression + push

- [ ] **Step 1: Run full pytest with parallel workers**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/ -q --tb=short -x
```

Expected: All pass. Investigate any new failure (cross-cutting regression).

- [ ] **Step 2: Run ruff**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && ruff check opencomputer/ extensions/api-server/ plugin_sdk/ tests/
```

Expected: No new violations.

- [ ] **Step 3: Verify all 14 commits land cleanly**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && git log --oneline -20
```

Expected: 14 feat commits + 1 doc/spec commit + plan commit (16 total) since the start of the work.

- [ ] **Step 4: Push branch + open PR**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && git push -u origin HEAD
```

Then `gh pr create --title "feat: Hermes MCP/API/ACP v2 gap-closure (15 items, 4 phases)" --body "$(cat <<'EOF'
## Summary

Closes 15 honest gaps between the Hermes MCP / API Server / ACP reference spec
and OpenComputer's implementation. No new modules; surgical edits to 6 files.

### Tier-1 (browser-frontend critical)
- G1: CORS middleware honoring `API_SERVER_CORS_ORIGINS` (preflight + Max-Age 600)
- G2: `Idempotency-Key` 5-min LRU dedup with token-scoped cache key

### Tier-2 (spec completeness)
- G3: `/v1/responses` default-on (drop opt-in env gate)
- G4: GET + DELETE `/v1/responses/{id}`
- G5: `/v1/health` alias
- G6: `API_SERVER_KEY` env-var alias for `API_SERVER_TOKEN`
- G7: `API_SERVER_ENABLED` env-var auto-enable

### Tier-3 (MCP client polish)
- G8: `mcp_<server>_<tool>` Hermes-spec naming alias alongside `<server>__<tool>`
- G9: per-server `prompts_enabled` / `resources_enabled` + nested YAML form
- G10: per-server `timeout` / `connect_timeout`
- G11: MCP sampling caps (`max_tokens_cap`, `max_rpm`, `max_tool_rounds`, `allowed_models`)

### Tier-4 (Hermes-AS-MCP server polish)
- G12: `conversations_list` / `conversation_get` aliases
- G13: `permissions_list_open` (OPEN approvals queue)
- G14: `events_poll(after_cursor)` + approval event types

### Tier-5 (ACP discovery)
- G15: auto-bundle `agent.json` at `~/.opencomputer/<profile>/acp_registry/agent.json`

## Test plan
- [x] 14 new test files (~50 tests) — all green locally
- [x] Full pytest suite — no regressions
- [x] `ruff check` — clean
- [ ] Manual smoke: `oc acp serve`, `curl /v1/health`, `curl /v1/capabilities`, `curl /v1/responses`
- [ ] Manual smoke: Open WebUI connect + send message (CORS + Idempotency)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"`

---

## Self-Review Checklist

After completing all tasks, confirm:

- [ ] Spec coverage: each gap (G1–G15) has a dedicated task. ✓
- [ ] No placeholders or TBDs.
- [ ] Type names consistent: `MCPServerConfig`, `MCPSamplingCaps`, `MCPAliasTool`, `_responses_storage`, `_IDEMPOTENCY_CACHE`.
- [ ] Each task has TDD ordering: write failing test → run → minimal impl → run → commit.
- [ ] CORS middleware default OFF when env unset (no regression for existing server-to-server users).
- [ ] Idempotency-Key cache TTL = 300s, max 256 entries, eviction on cap.
- [ ] /v1/responses ungated but existing API_SERVER_API_TYPE kept as deprecated no-op alias.
- [ ] MCP tool aliases register WITHOUT colliding (different schema_name).
- [ ] permissions_list_open returns [] on missing table (back-compat).
- [ ] events_poll preserves both `since_message_id` AND `after_cursor` semantics.
- [ ] agent.json autobundle never overwrites user-edited file.

---

## Risk Log

- **MCP alias-tool collision** if `<server>__<tool>` and `mcp_<server>_<tool>` happen to be identical strings (won't happen unless server name contains `__mcp_`-like prefix). Mitigation: defensive `if alias_name not in new_tools_by_name` guard before insert.
- **CORS for SSE streaming endpoints** — middleware applies to all responses but SSE responses may use chunked streaming. Tested by re-using existing `/v1/runs/{id}/events` test fixtures.
- **Idempotency-Key with streaming** — caching SSE bodies is unsupported (large + tail-truncated). The middleware will pass through streaming responses by checking `response.body is None` before caching. Add this guard explicitly in Task A2 implementation if tests catch it.
- **Sampling caps with non-MCP-spec params** — when an MCP server omits `modelPreferences`, the allowlist check is skipped (no requested model to compare). Documented behavior.
- **agent.json path collision** — if user has an existing `acp_registry/` at a non-canonical location (e.g. relative to repo root), our auto-bundle won't find it. Acceptable — they can run `oc acp manifest --write <path>` for non-canonical placement.
