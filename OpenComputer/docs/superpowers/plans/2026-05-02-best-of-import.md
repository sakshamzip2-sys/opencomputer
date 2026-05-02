# Best-of-import Implementation Plan (rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 7 carefully-curated capabilities from OpenClaw + Hermes into OpenComputer (audit-rejected 5 duplicates from rev 1; added `oc update` per user request).

**Architecture:** 4 PRs, each independently shippable. PR 1 (Hermes tools) → PR 2 (Providers) → PR 3 (Architectural) → PR 4 (oc update).

**Tech Stack:** Python 3.12+, plugin_sdk, MCP Python SDK, httpx, pytest, ruff.

**Worktree:** `/Users/saksham/.config/superpowers/worktrees/claude/phase-3/OpenComputer`. NEVER touch the main repo.

---

## Pre-Task — Discovery sweep (every PR starts here)

- [ ] **Step 0.1: Verify no collision before writing any new file**

For each new file path the PR introduces, run:

```bash
find opencomputer extensions plugin_sdk -name "<filename>*" 2>/dev/null
```

If a match exists, STOP and re-evaluate. Rev 1's blockers were 100% caused by skipping this step.

- [ ] **Step 0.2: Verify clean baseline test suite on main before branching**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-3/OpenComputer
git checkout main && git pull --ff-only origin main
.venv/bin/python -m pytest -x --tb=short -q 2>&1 | tail -20
```

Expected: all green except `voice/` markers (excluded).

- [ ] **Step 0.3: Branch off main, fresh per PR**

```bash
git checkout -b feat/<pr-name>
```

---

## PR 1 — Hermes tool ports (D2 + D4)

**Branch:** `feat/hermes-tool-ports`. Subagent: opus (D4 is judgment-heavy; D2 is mechanical, but bundle for one PR).

### Task 1.1: D2 SessionSearchTool

**Files:**
- Create: `opencomputer/tools/session_search.py`
- Create: `tests/tools/test_session_search.py`
- Modify: `opencomputer/cli.py` — register tool in default tool registration block (search around line 280-350)

- [ ] **Step 1: Read `SessionDB.search_messages()` signature**

```bash
grep -n "def search_messages\|def search\b" opencomputer/agent/state.py
```

Confirm: `search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]`. Returns dicts with keys including `session_id`, `role`, `timestamp`, `body` or `content`.

Read the actual function body around line 1175 to confirm the dict keys.

- [ ] **Step 2: Write failing test**

```python
# tests/tools/test_session_search.py
import asyncio
import pytest
from unittest.mock import MagicMock
from opencomputer.tools.session_search import SessionSearchTool
from plugin_sdk.core import ToolCall


def _hits():
    return [
        {"session_id": "abc-1234567890", "role": "user", "timestamp": 100, "content": "first hit body"},
        {"session_id": "def-2222222222", "role": "assistant", "timestamp": 200, "content": "second hit body"},
    ]


@pytest.fixture
def tool():
    db = MagicMock()
    db.search_messages.return_value = _hits()
    return SessionSearchTool(db)


def test_returns_dict_keys_not_attrs(tool):
    """Critical regression test for rev 1's `h.session_id` bug."""
    call = ToolCall(id="x", name="SessionSearch", arguments={"query": "first"})
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "abc-1234" in result.content  # truncated session_id
    assert "first hit body" in result.content


def test_empty_query_errors(tool):
    call = ToolCall(id="y", name="SessionSearch", arguments={"query": ""})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "query" in result.content.lower()


def test_db_failure_returns_tool_error(tool):
    tool._db.search_messages.side_effect = RuntimeError("db locked")
    call = ToolCall(id="z", name="SessionSearch", arguments={"query": "x"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error


def test_limit_passed_through(tool):
    call = ToolCall(id="w", name="SessionSearch", arguments={"query": "x", "limit": 25})
    asyncio.run(tool.execute(call))
    tool._db.search_messages.assert_called_once_with("x", limit=25)
```

- [ ] **Step 3: Run test (expect fail — module not yet created)**

```bash
.venv/bin/python -m pytest tests/tools/test_session_search.py -v
```

Expected: ImportError or ModuleNotFoundError.

- [ ] **Step 4: Implement**

```python
# opencomputer/tools/session_search.py
"""SessionSearchTool — LLM-callable wrapper over SessionDB.search_messages.

Returns up to `limit` FTS5 hits as a compact text block. Use when the agent
needs to recall facts from prior conversations within the user's session history.
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_DEFAULT_LIMIT = 10
_BODY_PREVIEW = 200


class SessionSearchTool(BaseTool):
    parallel_safe = True

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionSearch",
            description=(
                "Full-text search across the user's prior conversations. Returns "
                "matching message snippets from any session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(tool_call_id=call.id, content="missing required argument: query", is_error=True)

        limit = args.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(50, limit))

        try:
            hits = self._db.search_messages(query, limit=limit)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"search failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        if not hits:
            return ToolResult(tool_call_id=call.id, content=f"No matches for '{query}'.")

        lines = [f"Found {len(hits)} match(es) for '{query}':", ""]
        for h in hits:
            sid = (h.get("session_id") or "")[:8]
            role = h.get("role") or "?"
            body = h.get("content") or h.get("body") or h.get("snippet") or ""
            preview = body[:_BODY_PREVIEW] + ("…" if len(body) > _BODY_PREVIEW else "")
            lines.append(f"[{sid}…] {role}: {preview}")
        return ToolResult(tool_call_id=call.id, content="\n".join(lines))


__all__ = ["SessionSearchTool"]
```

- [ ] **Step 5: Run test — expect pass**

- [ ] **Step 6: Wire into CLI tool registration**

In `opencomputer/cli.py`, find the tool registration block (search for existing `MemoryTool` or `RecallTool` registration). Add:

```python
from opencomputer.tools.session_search import SessionSearchTool
tools.append(SessionSearchTool(session_db))
```

Implementer must locate the actual line range.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/tools/session_search.py tests/tools/test_session_search.py opencomputer/cli.py
git commit -m "feat(tools): add SessionSearchTool wrapping SessionDB.search_messages"
```

### Task 1.2: D4 MCPOAuthClient (use MCP SDK's OAuthClientProvider)

**Files:**
- Create: `opencomputer/mcp/oauth.py`
- Create: `tests/mcp/test_oauth.py`
- Modify: `opencomputer/mcp/client.py` — accept optional auth provider

- [ ] **Step 1: Read MCP SDK's OAuthClientProvider + Hermes' wrapper**

```bash
.venv/bin/pip show mcp 2>&1 | head -3 || .venv/bin/pip install "mcp>=1.6"
.venv/bin/python -c "from mcp.client.auth import OAuthClientProvider; help(OAuthClientProvider.__init__)"
sed -n '1,100p' /Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/mcp/oauth.py
```

- [ ] **Step 2: Write failing test**

```python
# tests/mcp/test_oauth.py
import json
import pytest
from opencomputer.mcp.oauth import OCMCPOAuthClient, _tokens_path


def test_tokens_path_is_profile_aware(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    p = _tokens_path()
    assert p == tmp_path / "mcp" / "tokens.json"


def test_load_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    assert OCMCPOAuthClient(server_name="github").load_tokens() == {}


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    c = OCMCPOAuthClient(server_name="github")
    c.save_tokens({"access_token": "tok", "refresh_token": "ref"})
    assert c.load_tokens() == {"access_token": "tok", "refresh_token": "ref"}


def test_save_preserves_other_servers(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    OCMCPOAuthClient(server_name="github").save_tokens({"access_token": "g"})
    OCMCPOAuthClient(server_name="notion").save_tokens({"access_token": "n"})
    saved = json.loads((tmp_path / "mcp" / "tokens.json").read_text())
    assert saved["github"]["access_token"] == "g"
    assert saved["notion"]["access_token"] == "n"


def test_provider_factory_returns_sdk_provider(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    pytest.importorskip("mcp")
    from mcp.client.auth import OAuthClientProvider
    c = OCMCPOAuthClient(server_name="github")
    p = c.as_sdk_provider(
        server_url="https://example.invalid",
        client_metadata={
            "client_name": "OpenComputer",
            "redirect_uris": ["http://localhost:5454/callback"],
        },
    )
    assert isinstance(p, OAuthClientProvider)
```

- [ ] **Step 3: Run test (expect fail)**

- [ ] **Step 4: Implement**

```python
# opencomputer/mcp/oauth.py
"""MCP OAuth 2.1 client — adapter around the MCP SDK's OAuthClientProvider.

Stores tokens per-profile. The MCP Python SDK handles dynamic client
registration, RFC 8414 discovery, PKCE, refresh, step-up — we provide
persistence + profile-aware paths.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _tokens_path() -> Path:
    from opencomputer.agent.config import _home
    return _home() / "mcp" / "tokens.json"


class OCMCPOAuthClient:
    def __init__(self, server_name: str) -> None:
        self.server_name = server_name

    def _all_tokens(self) -> dict[str, Any]:
        path = _tokens_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def load_tokens(self) -> dict[str, Any]:
        return self._all_tokens().get(self.server_name, {})

    def save_tokens(self, tokens: dict[str, Any]) -> None:
        all_t = self._all_tokens()
        all_t[self.server_name] = tokens
        path = _tokens_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_t, indent=2))
        tmp.replace(path)

    def as_sdk_provider(
        self,
        server_url: str,
        client_metadata: dict[str, Any],
        callback_handler: Any | None = None,
    ) -> Any:
        from mcp.client.auth import OAuthClientProvider, OAuthClientMetadata
        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=OAuthClientMetadata(**client_metadata),
            storage=_SDKStorageAdapter(self),
            redirect_handler=callback_handler,
        )


class _SDKStorageAdapter:
    def __init__(self, client: OCMCPOAuthClient) -> None:
        self._client = client

    async def get_tokens(self) -> dict[str, Any] | None:
        toks = self._client.load_tokens()
        return toks if toks else None

    async def set_tokens(self, tokens: Any) -> None:
        if hasattr(tokens, "model_dump"):
            self._client.save_tokens(tokens.model_dump())
        else:
            self._client.save_tokens(dict(tokens))

    async def get_client_info(self) -> dict[str, Any] | None:
        toks = self._client.load_tokens()
        ci = toks.get("client_info") if toks else None
        return ci if ci else None

    async def set_client_info(self, info: Any) -> None:
        existing = self._client.load_tokens()
        existing["client_info"] = info.model_dump() if hasattr(info, "model_dump") else dict(info)
        self._client.save_tokens(existing)


__all__ = ["OCMCPOAuthClient", "_tokens_path"]
```

- [ ] **Step 5: Run test — expect pass**

- [ ] **Step 6: Commit**

```bash
git add opencomputer/mcp/oauth.py tests/mcp/test_oauth.py
git commit -m "feat(mcp): OAuth 2.1 token store + SDK provider adapter"
```

### Task 1.3: Push PR 1

- [ ] Run full suite + ruff. Push. Open PR. Watch CI green. Merge.

```bash
.venv/bin/python -m pytest -x --tb=short -q --ignore=tests/voice 2>&1 | tail -10
.venv/bin/ruff check opencomputer extensions plugin_sdk tests 2>&1 | tail -5
git push -u origin feat/hermes-tool-ports
gh pr create --title "feat(tools+mcp): SessionSearch + MCP OAuth (Hermes ports)" --body "..."
```

---

## PR 2 — Provider plugins (B1 + B2)

**Branch:** `feat/ollama-groq-providers`. Subagent: sonnet (mechanical port).

### Plugin convention reference (CRITICAL — read first)

OC plugins follow a specific layout that the rev-1 plan got wrong. Verified from `extensions/openai-provider/` and `extensions/anthropic-provider/`:

- **Directory name is HYPHENATED** on disk: `extensions/ollama-provider/` — NOT `ollama_provider/`.
- **NO `__init__.py`** — the plugin loader puts the plugin root on `sys.path` so `plugin.py` can `from provider import X` directly.
- **`plugin.json`** (NOT `PluginManifest` dataclass) — JSON manifest with id/name/version/description/kind/entry/setup.
- **`plugin.py`** uses dual-import pattern: try `from provider import X` first (plugin-loader mode), fall back to `from extensions.<name>_provider.provider import X` (package mode for tests).
- **Provider class attribute is `name = "ollama"`** — NOT `provider_id`. Optional `default_model`, `_api_key_env`.
- **`register(api)` signature is `api.register_provider("ollama", OllamaProvider)`** — name + class, NOT just class.
- **Tests import via underscore alias** (`extensions.ollama_provider.provider`). The hyphen→underscore aliasing is wired in `tests/conftest.py` via `_register_extension_alias()` — every new plugin needs a per-plugin registration helper there.

### Task 2.1: B1 ollama-provider

**Files:**
- Create: `extensions/ollama-provider/plugin.json`
- Create: `extensions/ollama-provider/plugin.py`
- Create: `extensions/ollama-provider/provider.py`
- Create: `tests/extensions/test_ollama_provider.py`
- Modify: `tests/conftest.py` — add `_OLLAMA_PROVIDER_DIR` + `_register_ollama_provider_alias()` + invocation

- [ ] **Step 1: Read existing openai-provider + anthropic-provider as template**

```bash
ls extensions/openai-provider/
cat extensions/openai-provider/plugin.json
cat extensions/openai-provider/plugin.py
sed -n '160,210p' extensions/openai-provider/provider.py   # provider class shape
sed -n '195,260p' tests/conftest.py                         # alias registration pattern
```

Confirm:
- The dir is `extensions/openai-provider/` (hyphen)
- `plugin.json` has `id`, `kind: "provider"`, `entry: "plugin"`, `setup.providers[]`
- `plugin.py` uses the `try: from provider import X / except: from extensions.openai_provider.provider import X` dual-import
- `OpenAIProvider.name = "openai"` (class attribute)
- `register(api)` calls `api.register_provider("openai", OpenAIProvider)`
- conftest has `_register_openai_provider_alias()` calling `_register_extension_alias("openai_provider", _OPENAI_PROVIDER_DIR, submodules=("provider", ...))`

- [ ] **Step 2: Write failing test**

```python
# tests/extensions/test_ollama_provider.py
"""Ollama provider tests.

Imports via the underscore alias (extensions.ollama_provider) — the
hyphen→underscore aliasing is wired in tests/conftest.py.
"""
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from extensions.ollama_provider.provider import OllamaProvider
from plugin_sdk.core import Message


@pytest.fixture
def provider():
    return OllamaProvider(api_key=None, base_url="http://localhost:11434/v1")


def test_provider_name_is_class_attribute():
    """register() uses the class attribute as the provider name; must be 'ollama'."""
    assert OllamaProvider.name == "ollama"


def test_default_base_url_uses_local_ollama():
    p = OllamaProvider()
    assert p._base_url == "http://localhost:11434/v1"


def test_env_var_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://other:9999/v1")
    p = OllamaProvider()
    assert p._base_url == "http://other:9999/v1"


@pytest.mark.asyncio
async def test_complete_returns_provider_response(provider):
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "id": "cmpl-x",
        "model": "llama3",
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    })
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        resp = await provider.complete(model="llama3", messages=[Message(role="user", content="hi")])
    assert resp.message.content == "hello"
    assert resp.message.role == "assistant"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 1


@pytest.mark.asyncio
async def test_stream_complete_yields_text_delta_then_done(provider):
    """Critical: stream_complete MUST yield StreamEvent objects (not the
    rev-1 fictional StreamDelta), and finish with a `done` event carrying
    the full ProviderResponse — that's what the agent loop unwraps.
    """
    async def fake_lines():
        for line in [
            'data: {"choices":[{"delta":{"content":"hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[{"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]:
            yield line

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = fake_lines

    class _CM:
        async def __aenter__(self_): return mock_resp
        async def __aexit__(self_, *a): return None

    with patch("httpx.AsyncClient.stream", MagicMock(return_value=_CM())):
        events = []
        async for e in provider.stream_complete(model="llama3", messages=[Message(role="user", content="hi")]):
            events.append(e)
    text_chunks = [e.text for e in events if e.kind == "text_delta"]
    assert "".join(text_chunks) == "hello"
    # Final event must be `done` with a complete ProviderResponse
    assert events[-1].kind == "done"
    assert events[-1].response is not None
    assert events[-1].response.message.content == "hello"
```

- [ ] **Step 3: Run test (expect fail — `extensions.ollama_provider` not yet aliased)**

- [ ] **Step 4: Implement provider — copy openai-provider's stream pattern**

```python
# extensions/ollama-provider/provider.py
"""Ollama provider — local LLM via Ollama's OpenAI-compatible API.

Default endpoint: http://localhost:11434/v1. Reads OLLAMA_BASE_URL override.

Differs from openai-provider with OPENAI_BASE_URL=http://localhost:11434/v1 by:
- Cleaner config UX (defaults right out of the box, no env fiddling)
- Logical home for Ollama-specific extensions later (modelfile mgmt, etc.)
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

DEFAULT_BASE_URL = "http://localhost:11434/v1"

# OpenAI finish_reason → OC stop_reason vocabulary
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: "end_turn",
}


class OllamaProvider(BaseProvider):
    name = "ollama"
    default_model = "llama3"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # Ollama doesn't require auth by default but accepts arbitrary tokens.
        self._api_key = (api_key or "ollama").strip()
        self._base_url = (
            (base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        body = self._build_body(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, stream=False,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
            data = r.json()
        return self._parse_response(data)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self._build_body(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, stream=True,
        )
        content_parts: list[str] = []
        finish_reason: str | None = None
        usage = Usage()
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if text := delta.get("content"):
                        content_parts.append(text)
                        yield StreamEvent(kind="text_delta", text=text)
                    if fr := choices[0].get("finish_reason"):
                        finish_reason = fr
                    if u := chunk.get("usage"):
                        usage = Usage(
                            input_tokens=u.get("prompt_tokens", 0),
                            output_tokens=u.get("completion_tokens", 0),
                        )
        # Final event — done, carrying the full ProviderResponse
        final_msg = Message(role="assistant", content="".join(content_parts))
        yield StreamEvent(
            kind="done",
            response=ProviderResponse(
                message=final_msg,
                stop_reason=_STOP_MAP.get(finish_reason, "end_turn"),
                usage=usage,
            ),
        )

    def _build_body(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str,
        tools: list[ToolSchema] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg(m) for m in messages)
        body: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = [t.to_openai_format() for t in tools]
        return body

    def _parse_response(self, data: dict) -> ProviderResponse:
        choice = data["choices"][0]
        msg_data = choice["message"]
        finish = choice.get("finish_reason")
        u = data.get("usage") or {}
        return ProviderResponse(
            message=Message(
                role="assistant",
                content=msg_data.get("content") or "",
                tool_calls=msg_data.get("tool_calls") or None,
            ),
            stop_reason=_STOP_MAP.get(finish, "end_turn"),
            usage=Usage(
                input_tokens=u.get("prompt_tokens", 0),
                output_tokens=u.get("completion_tokens", 0),
            ),
        )

    @staticmethod
    def _msg(m: Message) -> dict:
        d = {"role": m.role, "content": m.content or ""}
        if getattr(m, "tool_calls", None):
            d["tool_calls"] = m.tool_calls
        if getattr(m, "tool_call_id", None):
            d["tool_call_id"] = m.tool_call_id
        return d
```

- [ ] **Step 5: Implement plugin entry (dual-import + register)**

```python
# extensions/ollama-provider/plugin.py
"""Ollama provider plugin — entry point.

Flat layout: plugin.py is the entry, sibling provider.py is importable
via plain name because the plugin loader puts the plugin root on sys.path.
"""
from __future__ import annotations

try:
    from provider import OllamaProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.ollama_provider.provider import OllamaProvider  # package mode


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("ollama", OllamaProvider)
```

- [ ] **Step 6: Implement plugin manifest**

```json
// extensions/ollama-provider/plugin.json
{
  "id": "ollama-provider",
  "name": "Ollama Provider",
  "version": "0.1.0",
  "description": "Local LLM via Ollama's OpenAI-compatible API.",
  "author": "OpenComputer",
  "license": "MIT",
  "kind": "provider",
  "entry": "plugin",
  "tool_names": [],
  "model_support": {
    "model_prefixes": ["llama", "mistral", "phi", "gemma", "qwen", "codellama", "deepseek"]
  },
  "setup": {
    "providers": [
      {
        "id": "ollama",
        "auth_methods": ["none"],
        "env_vars": ["OLLAMA_BASE_URL"],
        "label": "Ollama (local)",
        "default_model": "llama3",
        "signup_url": "https://ollama.ai/download"
      }
    ]
  }
}
```

- [ ] **Step 7: Wire conftest alias for tests**

In `tests/conftest.py`, add (mirror the openai-provider pattern):

```python
# Around the other _XXX_PROVIDER_DIR constants near top
_OLLAMA_PROVIDER_DIR = _REPO_ROOT / "extensions" / "ollama-provider"


# After _register_anthropic_provider_alias()
def _register_ollama_provider_alias() -> None:
    """Eager-exec + parent-binding for the Ollama provider."""
    _register_extension_alias(
        "ollama_provider", _OLLAMA_PROVIDER_DIR,
        submodules=("provider", "plugin"),
    )


# In the registration block at the bottom (alphabetical position)
_register_ollama_provider_alias()
```

The implementer reads the actual conftest.py and inserts at the matching positions.

- [ ] **Step 8: Run tests + verify import**

```bash
.venv/bin/python -m pytest tests/extensions/test_ollama_provider.py -v
.venv/bin/python -c "
import sys
sys.path.insert(0, 'extensions/ollama-provider')
from provider import OllamaProvider
print(OllamaProvider.name, '=', 'ollama')
"
```

- [ ] **Step 9: Commit**

```bash
git add extensions/ollama-provider/ tests/extensions/test_ollama_provider.py tests/conftest.py
git commit -m "feat(providers): ollama-provider plugin (OpenAI-compatible local LLM)"
```

### Task 2.2: B2 groq-provider

**Files:** mirror Task 2.1 with these changes:
- Dir: `extensions/groq-provider/` (HYPHEN)
- Provider class attribute: `name = "groq"`, `default_model = "llama-3.3-70b-versatile"`
- Default base URL: `https://api.groq.com/openai/v1`
- API key REQUIRED — raise `RuntimeError` if `GROQ_API_KEY` missing in `__init__`
- `_api_key_env: str = "GROQ_API_KEY"` class attribute
- conftest: add `_GROQ_PROVIDER_DIR` + `_register_groq_provider_alias()` + invocation
- Add test:
  ```python
  def test_missing_api_key_raises(monkeypatch):
      monkeypatch.delenv("GROQ_API_KEY", raising=False)
      with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
          GroqProvider()
  ```

`plugin.json` setup section uses `"auth_methods": ["api_key"]`, `"env_vars": ["GROQ_API_KEY"]`, `"label": "Groq (fast inference)"`, `"signup_url": "https://console.groq.com/keys"`.

- [ ] **All steps + commit**

```bash
git add extensions/groq-provider/ tests/extensions/test_groq_provider.py tests/conftest.py
git commit -m "feat(providers): groq-provider plugin (276-1500 t/s inference)"
```

### Task 2.3: Push PR 2

- [ ] Same shape as Task 1.3.

---

## PR 3 — Architectural ports (A1 + A3)

**Branch:** `feat/chunker-and-standing-orders`. Subagent: opus (judgment-heavy).

### Task 3.1: A1 BlockStreamingChunker

**Files:**
- Create: `opencomputer/gateway/streaming_chunker.py`
- Create: `tests/gateway/test_streaming_chunker.py`
- Modify: telegram channel adapter (one channel as proof; document pattern)

- [ ] **Step 1: Read OpenClaw's discord chunker as canonical reference**

```bash
sed -n '1,200p' /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/discord/src/chunk.ts
```

Note key behaviors: paragraph→newline→sentence→whitespace boundary preference, code-fence-safe, idle-coalesce, humanDelay 800-2500ms.

- [ ] **Step 2: Write failing tests**

```python
# tests/gateway/test_streaming_chunker.py
import asyncio
import pytest
from opencomputer.gateway.streaming_chunker import BlockStreamingChunker, ChunkerConfig


@pytest.mark.asyncio
async def test_paragraph_boundary_preferred():
    chunks = []
    async def collect(c): chunks.append(c)
    chunker = BlockStreamingChunker(emit=collect, config=ChunkerConfig(human_delay_ms=0))
    await chunker.feed("First paragraph.\n\nSecond paragraph.\n\nThird.")
    await chunker.close()
    assert any("First paragraph" in c for c in chunks)
    assert any("Second paragraph" in c for c in chunks)


@pytest.mark.asyncio
async def test_no_split_inside_code_fence():
    chunks = []
    async def collect(c): chunks.append(c)
    chunker = BlockStreamingChunker(emit=collect, config=ChunkerConfig(human_delay_ms=0))
    text = "Look:\n\n```python\ndef foo():\n    return 1\n```\n\nDone."
    await chunker.feed(text)
    await chunker.close()
    assembled = "".join(chunks)
    assert "```python" in assembled and "Done." in assembled


@pytest.mark.asyncio
async def test_close_flushes_remaining():
    chunks = []
    async def collect(c): chunks.append(c)
    chunker = BlockStreamingChunker(emit=collect, config=ChunkerConfig(human_delay_ms=0))
    await chunker.feed("no boundary")
    await chunker.close()
    assert "no boundary" in "".join(chunks)


@pytest.mark.asyncio
async def test_idle_coalesce_flushes():
    chunks = []
    async def collect(c): chunks.append(c)
    chunker = BlockStreamingChunker(emit=collect, config=ChunkerConfig(idle_ms=50, human_delay_ms=0))
    await chunker.feed("partial")
    await asyncio.sleep(0.15)
    await chunker.close()
    assert "".join(chunks).startswith("partial")
```

- [ ] **Step 3: Implement**

```python
# opencomputer/gateway/streaming_chunker.py
"""Block streaming chunker — humanlike pacing for chat-channel adapters.

Async-only API: feed() and close() MUST be called from coroutine context.
Reference: openclaw-2026.4.23/extensions/discord/src/chunk.ts.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)
EmitFn = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    idle_ms: int = 250
    human_delay_min_ms: int = 800
    human_delay_max_ms: int = 2500
    human_delay_ms: int | None = None  # test override
    min_emit_chars: int = 1


class BlockStreamingChunker:
    def __init__(self, emit: EmitFn, *, config: ChunkerConfig | None = None) -> None:
        self._emit = emit
        self._cfg = config or ChunkerConfig()
        self._buf: list[str] = []
        self._fence_open = False
        self._idle_task: asyncio.Task | None = None
        self._closed = False

    async def feed(self, text: str) -> None:
        if self._closed:
            return
        self._buf.append(text)
        for _ in self._scan_fence(text):
            self._fence_open = not self._fence_open
        await self._maybe_emit()
        self._reset_idle()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        if self._buf:
            await self._do_emit("".join(self._buf))
            self._buf.clear()

    def _scan_fence(self, text: str) -> list[int]:
        out, i = [], 0
        while True:
            j = text.find("```", i)
            if j < 0:
                return out
            out.append(j)
            i = j + 3

    async def _maybe_emit(self) -> None:
        if self._fence_open:
            return
        s = "".join(self._buf)
        b = self._find_boundary(s)
        if b < 0:
            return
        chunk = s[:b]
        rest = s[b:]
        if len(chunk.strip()) < self._cfg.min_emit_chars:
            return
        self._buf = [rest] if rest else []
        await self._do_emit(chunk)

    def _find_boundary(self, s: str) -> int:
        idx = s.rfind("\n\n")
        if idx >= 0:
            return idx + 2
        idx = s.rfind("\n")
        if idx >= 0:
            return idx + 1
        for end in (". ", "? ", "! "):
            idx = s.rfind(end)
            if idx >= 0:
                return idx + len(end)
        return -1

    async def _do_emit(self, chunk: str) -> None:
        delay = self._cfg.human_delay_ms
        if delay is None:
            delay = random.randint(self._cfg.human_delay_min_ms, self._cfg.human_delay_max_ms)
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)
        try:
            await self._emit(chunk)
        except Exception:
            logger.exception("chunker emit failed; reinserting buffer")
            self._buf.insert(0, chunk)

    def _reset_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_task = loop.create_task(self._idle_flush())

    async def _idle_flush(self) -> None:
        try:
            await asyncio.sleep(self._cfg.idle_ms / 1000.0)
        except asyncio.CancelledError:
            return
        if self._buf and not self._fence_open:
            await self._do_emit("".join(self._buf))
            self._buf.clear()


__all__ = ["BlockStreamingChunker", "ChunkerConfig"]
```

- [ ] **Step 4: Run tests (expect green)**

- [ ] **Step 5: Wire into telegram adapter (proof of integration)**

Find the telegram outbound emit point. Add opt-in flag in plugin config. Document the integration in adapter README so other channels can follow.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/gateway/streaming_chunker.py tests/gateway/test_streaming_chunker.py extensions/telegram-channel/...
git commit -m "feat(gateway): block streaming chunker for human-paced channel emits"
```

### Task 3.2: A3 Standing Orders

**Files:**
- Create: `opencomputer/agent/standing_orders.py`
- Create: `tests/agent/test_standing_orders.py`
- Modify: `opencomputer/agent/loop.py` — apply parsed orders as system context per turn

- [ ] **Step 1: Write failing tests (line-state-machine + adjacent-H2 regression)**

```python
# tests/agent/test_standing_orders.py
import pytest
from opencomputer.agent.standing_orders import parse_agents_md, StandingOrder


def test_single_well_formed_program():
    text = """# Project notes

## Program: weekly-summary
Scope: opencomputer/
Triggers: cron weekly
Approval Gates: human-confirm before send
Escalation: notify Saksham

## Other section
unrelated
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert orders[0].name == "weekly-summary"
    assert orders[0].scope == "opencomputer/"
    assert "human-confirm" in orders[0].approval_gates


def test_two_adjacent_program_blocks_dont_merge():
    """Critical regression test for rev 1's regex bug."""
    text = """## Program: alpha
Scope: a-only
Triggers: x

## Program: beta
Scope: b-only
Triggers: y
"""
    orders = parse_agents_md(text)
    assert len(orders) == 2
    assert orders[0].name == "alpha" and orders[0].scope == "a-only"
    assert orders[1].name == "beta" and orders[1].scope == "b-only"


def test_malformed_block_skipped_not_crashed():
    text = """## Program: bad
Scope:
(no other fields)

## Program: good
Scope: ok
Triggers: cron daily
"""
    orders = parse_agents_md(text)
    assert any(o.name == "good" for o in orders)


def test_empty_file_returns_empty_list():
    assert parse_agents_md("") == []
    assert parse_agents_md("# Just a heading") == []
```

- [ ] **Step 2: Implement parser as line-state-machine**

```python
# opencomputer/agent/standing_orders.py
"""Standing Orders — parsed `## Program:` blocks from AGENTS.md.

Parser is a line-state-machine to avoid regex pitfalls.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StandingOrder:
    name: str
    scope: str = ""
    triggers: str = ""
    approval_gates: str = ""
    escalation: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)


_HEADER_RE = re.compile(r"^##\s+Program:\s+(?P<name>[\w\-]+)\s*$")
_FIELD_RE = re.compile(r"^(?P<key>[A-Z][\w\s]*?):\s*(?P<val>.*)$")
_OTHER_H2_RE = re.compile(r"^##\s+(?!Program:)")


def parse_agents_md(text: str) -> list[StandingOrder]:
    if not text:
        return []
    out: list[StandingOrder] = []
    state = "OUTSIDE"
    current: StandingOrder | None = None
    cur_key: str | None = None
    cur_lines: list[str] = []

    def commit_field() -> None:
        nonlocal cur_key, cur_lines
        if current is None or cur_key is None:
            return
        val = "\n".join(cur_lines).strip()
        norm = cur_key.strip().lower().replace(" ", "_")
        current.raw_fields[norm] = val
        if norm == "scope":
            current.scope = val
        elif norm == "triggers":
            current.triggers = val
        elif norm == "approval_gates":
            current.approval_gates = val
        elif norm == "escalation":
            current.escalation = val
        cur_key = None
        cur_lines = []

    def commit_block() -> None:
        nonlocal current
        if current is None:
            return
        commit_field()
        if current.triggers:
            out.append(current)
        else:
            logger.warning("standing-order %r missing 'Triggers' — skipped", current.name)
        current = None

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            commit_block()
            current = StandingOrder(name=m.group("name"))
            state = "IN_BLOCK"
            continue
        if _OTHER_H2_RE.match(line):
            commit_block()
            state = "OUTSIDE"
            continue
        if state != "IN_BLOCK":
            continue
        fm = _FIELD_RE.match(line)
        if fm and not line.startswith((" ", "\t")):
            commit_field()
            cur_key = fm.group("key")
            cur_lines = [fm.group("val")]
            continue
        if cur_key is not None:
            cur_lines.append(line)

    commit_block()
    return out


__all__ = ["parse_agents_md", "StandingOrder"]
```

- [ ] **Step 3: Run tests (expect green)**

- [ ] **Step 4: Wire into agent loop**

In `opencomputer/agent/loop.py`, find per-turn system-context construction. Add: read `<workspace>/AGENTS.md`, parse, prepend orders as system context.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/standing_orders.py tests/agent/test_standing_orders.py opencomputer/agent/loop.py
git commit -m "feat(agent): standing orders parser + loop integration"
```

### Task 3.3: Push PR 3

- [ ] Same shape as Task 1.3.

---

## PR 4 — `oc update` + banner integration (E1)

**Branch:** `feat/oc-update-command`. Subagent: opus (subprocess + git edge cases).

### Task 4.1: `check_for_updates()` + cmd_update

**Files:**
- Create: `opencomputer/cli/update.py`
- Create: `tests/cli/test_update.py`
- Modify: `opencomputer/cli.py` — add `oc update` subcommand, call prefetch on startup
- Modify: `opencomputer/cli/banner.py` — render commits-behind line if positive

- [ ] **Step 1: Read Hermes' implementation**

```bash
sed -n '120,250p' /Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/hermes_cli/banner.py
sed -n '5425,5650p' /Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/hermes_cli/main.py
```

- [ ] **Step 2: Write failing tests**

```python
# tests/cli/test_update.py
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from opencomputer.cli.update import check_for_updates, cmd_update, _UPDATE_CHECK_CACHE_SECONDS


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_returns_none_when_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: None)
    monkeypatch.setattr("opencomputer.cli.update._cache_path", lambda: tmp_path / ".update_check")
    assert check_for_updates() is None


def test_uses_cache_when_fresh(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    cache = tmp_path / ".update_check"
    cache.write_text(json.dumps({"ts": time.time(), "behind": 7}))
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: repo)
    monkeypatch.setattr("opencomputer.cli.update._cache_path", lambda: cache)
    fake_run = MagicMock()
    with patch("subprocess.run", fake_run):
        result = check_for_updates()
    assert result == 7
    fake_run.assert_not_called()


def test_runs_git_commands_when_cache_stale(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    cache = tmp_path / ".update_check"
    cache.write_text(json.dumps({"ts": time.time() - _UPDATE_CHECK_CACHE_SECONDS - 1, "behind": 0}))
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: repo)
    monkeypatch.setattr("opencomputer.cli.update._cache_path", lambda: cache)

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            return MagicMock(returncode=0)
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="3\n")
        return MagicMock(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        result = check_for_updates()
    assert result == 3
    assert json.loads(cache.read_text())["behind"] == 3


def test_check_handles_failure_gracefully(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: repo)
    monkeypatch.setattr("opencomputer.cli.update._cache_path", lambda: tmp_path / "cache")
    with patch("subprocess.run", side_effect=Exception("network error")):
        assert check_for_updates() is None


def test_cmd_update_pip_install_fallback(monkeypatch, capsys):
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: None)
    rc = cmd_update()
    assert rc == 1
    assert "pip install --upgrade" in capsys.readouterr().out


def test_cmd_update_already_up_to_date(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("opencomputer.cli.update._resolve_repo_dir", lambda: repo)
    monkeypatch.setattr("opencomputer.cli.update._cache_path", lambda: tmp_path / "cache")

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return MagicMock(returncode=0, stderr="")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout="main\n")
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="0\n")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_update()
    assert rc == 0
    assert "Already up to date" in capsys.readouterr().out
```

- [ ] **Step 3: Implement**

```python
# opencomputer/cli/update.py
"""`oc update` command + banner update-check.

Mirrors Hermes' design: prefetch → 6h cache → banner display → cmd_update.
Reference: sources/hermes-agent-2026.4.23/hermes_cli/{banner.py,main.py}.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600
_PREFETCH_THREAD: threading.Thread | None = None


def _cache_path() -> Path:
    from opencomputer.agent.config import _home
    return _home() / ".update_check"


def _resolve_repo_dir() -> Path | None:
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / ".git").exists():
        return project_root
    from opencomputer.agent.config import _home
    candidate = _home() / "opencomputer"
    if (candidate / ".git").exists():
        return candidate
    return None


def check_for_updates() -> int | None:
    repo = _resolve_repo_dir()
    if repo is None:
        return None
    cache = _cache_path()
    now = time.time()
    try:
        if cache.exists():
            data = json.loads(cache.read_text())
            if now - data.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS:
                return data.get("behind")
    except (OSError, json.JSONDecodeError):
        pass
    behind: int | None = None
    try:
        subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            capture_output=True, timeout=10, cwd=str(repo),
        )
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, timeout=5, cwd=str(repo),
        )
        if result.returncode == 0:
            behind = int(result.stdout.strip())
    except Exception as e:  # noqa: BLE001 — soft-fail offline / errored repo
        logger.debug("update check failed: %s", e)
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ts": now, "behind": behind}))
    except OSError:
        pass
    return behind


def prefetch_update_check() -> None:
    global _PREFETCH_THREAD
    if _PREFETCH_THREAD and _PREFETCH_THREAD.is_alive():
        return
    _PREFETCH_THREAD = threading.Thread(target=check_for_updates, daemon=True)
    _PREFETCH_THREAD.start()


def get_update_result(timeout: float = 0.5) -> int | None:
    if _PREFETCH_THREAD and _PREFETCH_THREAD.is_alive():
        _PREFETCH_THREAD.join(timeout=timeout)
    cache = _cache_path()
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text()).get("behind")
    except (OSError, json.JSONDecodeError):
        return None


def cmd_update() -> int:
    repo = _resolve_repo_dir()
    if repo is None:
        print("✗ Not a git checkout. To upgrade a pip install, run:")
        print("   pip install --upgrade opencomputer")
        return 1

    print("⚕ Updating OpenComputer...")
    try:
        print("→ Fetching updates...")
        r = subprocess.run(["git", "fetch", "origin"], cwd=str(repo), capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or "").splitlines()
            print(f"✗ Fetch failed: {err[0] if err else 'unknown error'}")
            return 1

        cur = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        stashed = False
        if cur != "main":
            print(f"  ⚠ On branch '{cur}' — stashing and switching to main...")
            stash = subprocess.run(
                ["git", "stash", "push", "-u", "-m", "oc update auto-stash"],
                cwd=str(repo), capture_output=True, text=True,
            )
            stashed = stash.returncode == 0 and "No local changes" not in (stash.stdout + stash.stderr)
            subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)

        n = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip())

        if n == 0:
            print("✓ Already up to date!")
            _invalidate_cache()
            if cur != "main":
                subprocess.run(["git", "checkout", cur], cwd=str(repo), check=False, capture_output=True)
                if stashed:
                    subprocess.run(["git", "stash", "pop"], cwd=str(repo), check=False, capture_output=True)
            return 0

        print(f"→ Found {n} new commit(s)")
        print("→ Pulling updates...")
        pull = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            cwd=str(repo), capture_output=True, text=True,
        )
        if pull.returncode != 0:
            err = (pull.stderr or "").splitlines()
            print("✗ Pull failed (local diverged from origin):")
            print(f"  {err[0] if err else 'unknown error'}")
            return 1

        print(f"✓ Updated to latest main (+{n} commits)")
        _invalidate_cache()
        if cur != "main":
            subprocess.run(["git", "checkout", cur], cwd=str(repo), check=False, capture_output=True)
            if stashed:
                pop = subprocess.run(["git", "stash", "pop"], cwd=str(repo), capture_output=True, text=True)
                if pop.returncode != 0:
                    print("  ⚠ Stash pop failed; your changes remain in stash list.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"✗ git command failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"✗ Update failed: {type(e).__name__}: {e}")
        return 1


def _invalidate_cache() -> None:
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "check_for_updates",
    "prefetch_update_check",
    "get_update_result",
    "cmd_update",
]
```

- [ ] **Step 4: Run tests (expect green)**

- [ ] **Step 5: Wire into CLI entry**

- Add `oc update` subcommand calling `cmd_update()` and exiting with its code.
- At CLI startup (banner-mode): `from opencomputer.cli.update import prefetch_update_check; prefetch_update_check()`.
- In banner: `behind = get_update_result(timeout=0.5); if behind: print(f"⚠ {behind} commits behind — run 'oc update' to update")`.

Implementer locates the actual entry points before editing.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli/update.py tests/cli/test_update.py opencomputer/cli.py opencomputer/cli/banner.py
git commit -m "feat(cli): oc update command + banner behind-count display"
```

### Task 4.2: Push PR 4

- [ ] Same shape as Task 1.3.

---

## Final verification (before declaring done)

- [ ] All 4 PRs merged to main with green CI
- [ ] Full pytest suite green (voice-excluded)
- [ ] `oc --help` shows `update` subcommand
- [ ] `oc update` runs cleanly on the worktree
- [ ] Banner shows commits-behind count when behind > 0
- [ ] No regressions in existing memory_tool, send_message, active_memory, search backends

---

## Self-review (post-write inline check)

- ✅ Spec rev 2 coverage: every of 7 items has a task; 5 cut items not duplicated.
- ✅ Pre-Task 0.1 enforces discovery sweep — would have prevented all 3 rev-1 collisions.
- ✅ D2 uses `dict["key"]` access + `search_messages()` (rev 1 used `h.session_id` which would AttributeError).
- ✅ D4 uses MCP SDK's `OAuthClientProvider` (Hermes' actual approach, not from-scratch).
- ✅ B1 implements `stream_complete` (not abstract NotImplementedError).
- ✅ B1/B2 use underscore dir names so Python imports work.
- ✅ A1 chunker: async-only API; uses `get_running_loop()`; tests cover code-fence safety + idle-coalesce + close-flush.
- ✅ A3 standing orders: line-state-machine parser, NOT regex; adjacent-H2 regression test included.
- ✅ E1 update: 4 layers (prefetch → cache → banner → cmd) with pip-install fallback.

**Lessons encoded:** discovery-sweep is mandatory; verify return-type contracts before writing consumers; SDK wrappers beat re-implementations.
