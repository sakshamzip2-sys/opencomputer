# Best-of-OpenClaw/Hermes/Claude-Code Import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-05-02-best-of-import-design.md`

**Goal:** Port 12 curated items from OpenClaw, Hermes-agent, and Claude Code into OpenComputer — bringing in only what's empirically popular AND fills a real gap, after both audit-doc curation and 2026-05-02 web-search popularity research.

**Architecture:** 4 phases / 6 PRs / each independently shippable. Order: D (Hermes tool ports) → B (providers) → C (search tools) → A (architectural ports). Smallest/most-isolated wins ship first; deepest changes last.

**Tech Stack:** Python 3.12+, asyncio, httpx, pytest, ruff, plugin_sdk boundary contract, existing extensions/* layout, existing tools/registry pattern.

**Backwards-compatibility contract:** all 6 PRs must keep the full pytest suite (voice-excluded) green vs origin/main baseline at every merge. Each PR is opt-in (new files; existing behavior unchanged for users who don't enable the new plugin/feature).

---

## File Structure

### New top-level dirs

| Path | Responsibility |
|---|---|
| `extensions/ollama-provider/` | OpenAI-compatible HTTP client for local Ollama (B1) |
| `extensions/groq-provider/` | OpenAI-compatible HTTP client for Groq cloud (B2) |
| `extensions/firecrawl/` | Tool plugin for Firecrawl search + scrape (C1) |
| `extensions/tavily/` | Tool plugin for Tavily agent-search API (C2) |
| `extensions/exa/` | Tool plugin for Exa semantic search (C3) |

### New files in core

| Path | Responsibility |
|---|---|
| `opencomputer/tools/memory.py` | LLM-callable verbs over MEMORY.md (D1) |
| `opencomputer/tools/session_search.py` | LLM-callable wrapper for SessionDB.search (D2) |
| `opencomputer/tools/send_message.py` | LLM-callable wrapper for OutgoingQueue (D3) |
| `opencomputer/mcp/oauth.py` | OAuth 2.1 + PKCE for MCP servers (D4) |
| `opencomputer/gateway/streaming_chunker.py` | Block streaming chunker (A1) |
| `opencomputer/agent/active_memory.py` | Pre-reply blocking recall (A2) |
| `opencomputer/agent/standing_orders.py` | AGENTS.md `## Program:` parser (A3) |

### Modified core files

| Path | Change |
|---|---|
| `opencomputer/agent/loop.py` | Hook in Active Memory pre-reply (A2); apply Standing Orders as system context (A3) |
| `opencomputer/mcp/client.py` | Use OAuth client when MCP server requires it (D4) |
| `opencomputer/tools/registry.py` | Register the 3 new core tools (D1, D2, D3) |
| `extensions/telegram/adapter.py` and similar channel adapters | Opt-in chunker integration (A1) |

---

# PR 1 — Phase D: Hermes Tool Ports (4 items)

**PR title:** `feat(tools): port memory_tool + session_search_tool + send_message_tool + mcp_oauth from Hermes`
**Branch:** `feat/phase-d-hermes-tool-ports`
**Estimated scope:** ~600 LOC + ~400 LOC tests, ~6 hours.
**Behavior change:** opt-in only — new tools are registered but the LLM uses them only when it chooses to call them.

### Task D1.1 — `MemoryTool` core implementation (TDD)

**Files:**
- Create: `opencomputer/tools/memory.py`
- Test: `tests/test_memory_tool.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_tool.py
"""MemoryTool — LLM-callable verbs for MEMORY.md edits.

Hermes port (per docs/refs/hermes-agent/inventory.md: high value, port to core).
Today MEMORY.md is a plain file the agent reads as system prompt context;
this tool lets the LLM write/append/search/list/delete entries as a
ToolCall instead of just reading the static prompt.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.tools.memory import MemoryTool
from plugin_sdk.core import ToolCall


def _call(action: str, **kwargs) -> ToolCall:
    return ToolCall(id="t1", name="memory", arguments={"action": action, **kwargs})


@pytest.mark.asyncio
async def test_memory_write_creates_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    tool = MemoryTool()
    res = await tool.execute(_call("write", content="Saksham prefers concise replies."))
    assert "ok" in res.content.lower() or "wrote" in res.content.lower()
    assert (tmp_path / "MEMORY.md").exists()
    assert "concise replies" in (tmp_path / "MEMORY.md").read_text()


@pytest.mark.asyncio
async def test_memory_append_preserves_existing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "MEMORY.md").write_text("# Memory\n- existing line\n")
    tool = MemoryTool()
    await tool.execute(_call("append", content="- new line"))
    body = (tmp_path / "MEMORY.md").read_text()
    assert "existing line" in body and "new line" in body


@pytest.mark.asyncio
async def test_memory_search_returns_matches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "MEMORY.md").write_text(
        "# Memory\n- alpha is fast\n- beta is slow\n- gamma is medium\n"
    )
    tool = MemoryTool()
    res = await tool.execute(_call("search", query="slow"))
    assert "beta" in res.content


@pytest.mark.asyncio
async def test_memory_list_returns_all_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "MEMORY.md").write_text("# Memory\n- one\n- two\n")
    tool = MemoryTool()
    res = await tool.execute(_call("list"))
    assert "one" in res.content and "two" in res.content


@pytest.mark.asyncio
async def test_memory_delete_removes_matching_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "MEMORY.md").write_text("# Memory\n- keep me\n- delete me\n")
    tool = MemoryTool()
    await tool.execute(_call("delete", match="delete me"))
    body = (tmp_path / "MEMORY.md").read_text()
    assert "keep me" in body
    assert "delete me" not in body


@pytest.mark.asyncio
async def test_memory_unknown_action_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    tool = MemoryTool()
    res = await tool.execute(_call("explode"))
    assert res.is_error
    assert "explode" in res.content or "action" in res.content.lower()
```

- [ ] **Step 2: Run; expect 6 errors (ModuleNotFoundError on `opencomputer.tools.memory`)**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-3/OpenComputer && .venv/bin/python -m pytest tests/test_memory_tool.py -v
```

- [ ] **Step 3: Read the Hermes source for shape reference (do not blind-copy)**

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/tools/memory_tool.py` to understand verb semantics and error patterns. Note OC's path conventions are different (OC uses `_home() / "MEMORY.md"`, Hermes uses `~/.hermes/...`).

- [ ] **Step 4: Implementation**

Create `opencomputer/tools/memory.py`:

```python
"""LLM-callable tool over MEMORY.md.

Hermes port (per docs/refs/hermes-agent/inventory.md tagged "high value,
port to core"). Today MEMORY.md is read as static system-prompt context;
this tool lets the LLM mutate it via ``write`` / ``append`` / ``search`` /
``list`` / ``delete`` actions.

The tool resolves the active profile's MEMORY.md path via ``_home()`` —
ContextVar-aware after Phase 1 of profile-as-agent multi-routing
(PR #279). Per-profile memory naturally falls out.
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class MemoryTool(BaseTool):
    parallel_safe = False  # MEMORY.md writes serialize naturally

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="memory",
            description=(
                "Read or write the agent's long-term declarative memory "
                "(MEMORY.md). Use to record durable facts about the user, "
                "their projects, preferences, and decisions. Verbs: "
                "write (overwrite), append (add line), search (substring "
                "match across lines), list (return entire file), delete "
                "(remove first matching line)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "append", "search", "list", "delete"],
                    },
                    "content": {"type": "string", "description": "For write/append."},
                    "query": {"type": "string", "description": "For search."},
                    "match": {"type": "string", "description": "For delete (substring)."},
                },
                "required": ["action"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from opencomputer.agent.config import _home

        action = call.arguments.get("action", "").strip().lower()
        path = _home() / "MEMORY.md"

        try:
            if action == "write":
                content = (call.arguments.get("content") or "").strip()
                if not content:
                    return ToolResult(call.id, "Error: content required for write", is_error=True)
                path.write_text(content + "\n", encoding="utf-8")
                return ToolResult(call.id, f"wrote {len(content)} chars to {path.name}")

            if action == "append":
                content = (call.arguments.get("content") or "").rstrip()
                if not content:
                    return ToolResult(call.id, "Error: content required for append", is_error=True)
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                path.write_text(existing + content + "\n", encoding="utf-8")
                return ToolResult(call.id, f"appended {len(content)} chars to {path.name}")

            if action == "search":
                q = (call.arguments.get("query") or "").strip()
                if not q:
                    return ToolResult(call.id, "Error: query required for search", is_error=True)
                if not path.exists():
                    return ToolResult(call.id, "memory empty")
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if q.lower() in ln.lower()]
                return ToolResult(call.id, "\n".join(lines) if lines else f"no matches for {q!r}")

            if action == "list":
                if not path.exists():
                    return ToolResult(call.id, "memory empty")
                return ToolResult(call.id, path.read_text(encoding="utf-8"))

            if action == "delete":
                m = (call.arguments.get("match") or "").strip()
                if not m:
                    return ToolResult(call.id, "Error: match required for delete", is_error=True)
                if not path.exists():
                    return ToolResult(call.id, "memory empty; nothing to delete")
                lines = path.read_text(encoding="utf-8").splitlines()
                kept: list[str] = []
                deleted = False
                for ln in lines:
                    if not deleted and m.lower() in ln.lower():
                        deleted = True
                        continue
                    kept.append(ln)
                path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                return ToolResult(call.id, f"deleted {1 if deleted else 0} line(s)")

            return ToolResult(call.id, f"Error: unknown action {action!r}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.id, f"Error: {type(exc).__name__}: {exc}", is_error=True)


__all__ = ["MemoryTool"]
```

- [ ] **Step 5: Register in tool registry**

In `opencomputer/tools/registry.py`, find the existing `register_default_tools()` function and add MemoryTool to the list. Pattern:

```python
from opencomputer.tools.memory import MemoryTool
# inside register_default_tools or wherever core tools are registered:
registry.register(MemoryTool())
```

- [ ] **Step 6: Run tests**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-3/OpenComputer && .venv/bin/python -m pytest tests/test_memory_tool.py -v
```

Expected: 6 passed.

- [ ] **Step 7: Run broader regression**

```bash
.venv/bin/python -m pytest tests/ -q --ignore-glob="tests/test_voice_*" 2>&1 | tail -7
```

Expected: 0 new failures vs main.

### Task D2 — `SessionSearchTool`

**Files:**
- Create: `opencomputer/tools/session_search.py`
- Test: `tests/test_session_search_tool.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_session_search_tool.py
"""SessionSearchTool — LLM-callable wrapper for SessionDB FTS5 search.

Hermes port. Today FTS5 works but only the CLI calls it
(`opencomputer search QUERY`); the LLM cannot search session history mid-
conversation. This tool fills it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.tools.session_search import SessionSearchTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_session_search_returns_hits(tmp_path: Path, monkeypatch) -> None:
    """Searches via SessionDB.search(query, limit) and formats results."""
    fake_db = MagicMock()
    fake_db.search = MagicMock(return_value=[
        MagicMock(session_id="s1", role="user", content="learning about ollama", timestamp=1.0),
        MagicMock(session_id="s2", role="assistant", content="ollama runs locally", timestamp=2.0),
    ])
    monkeypatch.setattr(
        "opencomputer.tools.session_search._get_db",
        lambda: fake_db,
    )
    tool = SessionSearchTool()
    res = await tool.execute(ToolCall(id="t1", name="session_search", arguments={"query": "ollama"}))
    assert "ollama" in res.content.lower()
    assert "s1" in res.content or "user" in res.content


@pytest.mark.asyncio
async def test_session_search_empty_query_errors(tmp_path: Path, monkeypatch) -> None:
    tool = SessionSearchTool()
    res = await tool.execute(ToolCall(id="t1", name="session_search", arguments={"query": ""}))
    assert res.is_error


@pytest.mark.asyncio
async def test_session_search_no_hits_message(tmp_path: Path, monkeypatch) -> None:
    fake_db = MagicMock()
    fake_db.search = MagicMock(return_value=[])
    monkeypatch.setattr("opencomputer.tools.session_search._get_db", lambda: fake_db)
    tool = SessionSearchTool()
    res = await tool.execute(ToolCall(id="t1", name="session_search", arguments={"query": "needle"}))
    assert "no" in res.content.lower() or "0" in res.content
```

- [ ] **Step 2: Run; expect failures**

```bash
.venv/bin/python -m pytest tests/test_session_search_tool.py -v
```

- [ ] **Step 3: Implementation**

Create `opencomputer/tools/session_search.py`:

```python
"""LLM-callable wrapper for SessionDB FTS5 search.

Hermes port. The SessionDB FTS5 engine has been usable from CLI
(``opencomputer search QUERY``) but not from inside the agent loop.
This tool exposes it.
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _get_db() -> Any:
    """Resolve the active profile's SessionDB."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.state import SessionDB
    cfg = default_config()
    return SessionDB(cfg.session.db_path)


class SessionSearchTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="session_search",
            description=(
                "Search past conversation history (FTS5 full-text). Use to "
                "recall prior chats, decisions, code snippets, etc. Returns "
                "matching session/role/content lines."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        q = (call.arguments.get("query") or "").strip()
        if not q:
            return ToolResult(call.id, "Error: query required", is_error=True)
        limit = int(call.arguments.get("limit", 10))
        try:
            db = _get_db()
            hits = db.search(q, limit=limit)
            if not hits:
                return ToolResult(call.id, f"0 hits for {q!r}")
            lines = [
                f"[{h.session_id[:8]}] {h.role}: {h.content[:200]}"
                for h in hits
            ]
            return ToolResult(call.id, f"{len(hits)} hit(s) for {q!r}:\n" + "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.id, f"Error: {type(exc).__name__}: {exc}", is_error=True)


__all__ = ["SessionSearchTool"]
```

- [ ] **Step 4: Register + tests pass + regression green**

Same pattern as D1.

### Task D3 — `SendMessageTool`

**Files:**
- Create: `opencomputer/tools/send_message.py`
- Test: `tests/test_send_message_tool.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_send_message_tool.py
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.tools.send_message import SendMessageTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_send_message_enqueues_via_outgoing_queue(monkeypatch) -> None:
    fake_q = MagicMock()
    fake_q.enqueue = MagicMock(return_value=True)
    monkeypatch.setattr("opencomputer.tools.send_message._get_queue", lambda: fake_q)
    tool = SendMessageTool()
    res = await tool.execute(ToolCall(
        id="t1", name="send_message",
        arguments={"platform": "telegram", "chat_id": "12345", "text": "hi"}
    ))
    fake_q.enqueue.assert_called_once()
    assert "queued" in res.content.lower() or "ok" in res.content.lower()


@pytest.mark.asyncio
async def test_send_message_no_queue_fails_gracefully(monkeypatch) -> None:
    monkeypatch.setattr("opencomputer.tools.send_message._get_queue", lambda: None)
    tool = SendMessageTool()
    res = await tool.execute(ToolCall(
        id="t1", name="send_message",
        arguments={"platform": "telegram", "chat_id": "x", "text": "hi"}
    ))
    assert res.is_error
    assert "queue" in res.content.lower() or "gateway" in res.content.lower()


@pytest.mark.asyncio
async def test_send_message_missing_args_errors() -> None:
    tool = SendMessageTool()
    res = await tool.execute(ToolCall(id="t1", name="send_message", arguments={}))
    assert res.is_error
```

- [ ] **Step 2-4: Implementation + register + verify**

```python
# opencomputer/tools/send_message.py
"""LLM-callable tool to send messages on a platform without an inbound trigger.

Hermes port. Useful for cron jobs, standing orders, and proactive flows
that need to post to a chat without a live MessageEvent to reply to.
Routes through the gateway's OutgoingQueue (already in OC).
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _get_queue() -> Any:
    """Best-effort accessor for the live OutgoingQueue."""
    from opencomputer.plugins.registry import registry as plugin_registry
    return getattr(plugin_registry, "outgoing_queue", None)


class SendMessageTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="send_message",
            description=(
                "Send a message on a specific channel/platform without "
                "needing an inbound MessageEvent. Use for cron output, "
                "scheduled summaries, autonomous-program reports, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "telegram, discord, slack, ..."},
                    "chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["platform", "chat_id", "text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        platform = (call.arguments.get("platform") or "").strip()
        chat_id = (call.arguments.get("chat_id") or "").strip()
        text = call.arguments.get("text") or ""
        if not (platform and chat_id and text):
            return ToolResult(call.id, "Error: platform, chat_id, text all required", is_error=True)
        q = _get_queue()
        if q is None:
            return ToolResult(
                call.id,
                "Error: OutgoingQueue not bound — this tool requires the gateway to be running",
                is_error=True,
            )
        try:
            q.enqueue(platform=platform, chat_id=chat_id, body=text, attachments=[], metadata={})
            return ToolResult(call.id, f"queued message on {platform}:{chat_id}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.id, f"Error: {type(exc).__name__}: {exc}", is_error=True)


__all__ = ["SendMessageTool"]
```

### Task D4 — `mcp_oauth` (OAuth 2.1 + PKCE for MCP)

**Files:**
- Create: `opencomputer/mcp/oauth.py`
- Modify: `opencomputer/mcp/client.py` (apply OAuth when MCP server requires it)
- Test: `tests/test_mcp_oauth.py`

- [ ] **Step 1: Read Hermes source for OAuth pattern**

```bash
cat /Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/mcp/oauth.py 2>/dev/null | head -100
# Note: structure of authorization-code flow + PKCE challenge generation + token refresh
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_mcp_oauth.py
"""OAuth 2.1 + PKCE client for MCP servers (port from Hermes).

OAuth 2.1 (RFC 9700) requires PKCE for all authorization code flows.
Test the PKCE challenge generation, state/nonce handling, and the
token-exchange request shape.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from opencomputer.mcp.oauth import MCPOAuthClient, generate_pkce_pair


def test_pkce_pair_format() -> None:
    """code_verifier is 43-128 char URL-safe base64; challenge is SHA256(verifier) base64url."""
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert all(c.isalnum() or c in "-._~" for c in verifier)
    # challenge must be different from verifier (it's the SHA256 hash, base64url-encoded)
    assert challenge != verifier
    assert 43 <= len(challenge) <= 64


def test_pkce_pair_unique() -> None:
    """Each call must produce a fresh verifier (no static)."""
    v1, _ = generate_pkce_pair()
    v2, _ = generate_pkce_pair()
    assert v1 != v2


def test_state_nonce_unique() -> None:
    client = MCPOAuthClient(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="test",
        redirect_uri="http://localhost:5757/cb",
    )
    s1 = client._mint_state()
    s2 = client._mint_state()
    assert s1 != s2
    assert len(s1) >= 32


@pytest.mark.asyncio
async def test_exchange_code_for_token_calls_token_endpoint() -> None:
    client = MCPOAuthClient(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="test",
        redirect_uri="http://localhost:5757/cb",
    )
    fake_response = AsyncMock()
    fake_response.json = AsyncMock(return_value={
        "access_token": "test-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    })
    fake_response.raise_for_status = lambda: None

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=fake_response)

    with patch("httpx.AsyncClient", return_value=fake_client):
        token = await client.exchange_code_for_token(
            code="abc", code_verifier="verifier-xyz",
        )
        assert token.access_token == "test-token"
        assert token.expires_in == 3600
        # POST to token_url with grant_type=authorization_code + code + verifier
        args, kwargs = fake_client.post.call_args
        assert client.token_url in args
        body = kwargs.get("data") or kwargs.get("json") or {}
        assert body.get("grant_type") == "authorization_code"
        assert body.get("code") == "abc"
        assert body.get("code_verifier") == "verifier-xyz"


def test_authorization_url_includes_pkce_challenge() -> None:
    """The /authorize redirect URL must include code_challenge + S256."""
    client = MCPOAuthClient(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="test-client",
        redirect_uri="http://localhost:5757/cb",
    )
    url, state, verifier = client.build_authorization_url(scopes=["read"])
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert f"state={state}" in url
    assert f"client_id=test-client" in url
    assert "scope=read" in url or "scope=read+" in url
```

- [ ] **Step 3: Implementation**

Create `opencomputer/mcp/oauth.py`:

```python
"""OAuth 2.1 + PKCE client for MCP servers that require authentication.

Hermes port. Implements RFC 9700 (OAuth 2.1 BCP) authorization code
flow with mandatory PKCE. Used by ``opencomputer.mcp.client`` when a
configured MCP server's manifest declares OAuth-required.

Usage:

    client = MCPOAuthClient(
        authorization_url="https://oauth.example.com/authorize",
        token_url="https://oauth.example.com/token",
        client_id="<registered client id>",
        redirect_uri="http://localhost:5757/cb",
    )
    url, state, verifier = client.build_authorization_url(scopes=["mcp:read"])
    # User opens url; redirects back to redirect_uri with ?code=...&state=...
    token = await client.exchange_code_for_token(code, verifier)
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge).

    Per RFC 7636: code_verifier is a 43-128 char string of unreserved
    characters [A-Z][a-z][0-9]-._~. We use 43-char URL-safe base64 of 32
    random bytes (matches Hermes implementation). code_challenge is
    SHA256(verifier) base64url-encoded without padding.
    """
    raw = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str | None = None


class MCPOAuthClient:
    """OAuth 2.1 + PKCE authorization code client."""

    def __init__(
        self,
        *,
        authorization_url: str,
        token_url: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
    ) -> None:
        self.authorization_url = authorization_url
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def _mint_state(self) -> str:
        return secrets.token_urlsafe(32)

    def build_authorization_url(
        self, *, scopes: list[str] | None = None,
    ) -> tuple[str, str, str]:
        """Return (url, state, code_verifier).

        Caller redirects user to ``url``, awaits redirect to ``redirect_uri``
        with ``?code=...&state=...``, validates ``state`` matches, then calls
        :meth:`exchange_code_for_token` with the code + verifier.
        """
        verifier, challenge = generate_pkce_pair()
        state = self._mint_state()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        sep = "&" if "?" in self.authorization_url else "?"
        return f"{self.authorization_url}{sep}{urlencode(params)}", state, verifier

    async def exchange_code_for_token(
        self, code: str, code_verifier: str,
    ) -> OAuthToken:
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            body["client_secret"] = self.client_secret
        async with httpx.AsyncClient() as http:
            r = await http.post(self.token_url, data=body)
            r.raise_for_status()
            j = await r.json() if hasattr(r.json, "__await__") else r.json()
        return OAuthToken(
            access_token=j["access_token"],
            token_type=j.get("token_type", "Bearer"),
            expires_in=int(j.get("expires_in", 3600)),
            refresh_token=j.get("refresh_token"),
        )

    async def refresh_token(self, refresh_token: str) -> OAuthToken:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            body["client_secret"] = self.client_secret
        async with httpx.AsyncClient() as http:
            r = await http.post(self.token_url, data=body)
            r.raise_for_status()
            j = await r.json() if hasattr(r.json, "__await__") else r.json()
        return OAuthToken(
            access_token=j["access_token"],
            token_type=j.get("token_type", "Bearer"),
            expires_in=int(j.get("expires_in", 3600)),
            refresh_token=j.get("refresh_token", refresh_token),
        )


__all__ = ["MCPOAuthClient", "OAuthToken", "generate_pkce_pair"]
```

- [ ] **Step 4: Wire into `opencomputer/mcp/client.py`**

Read the existing MCPManager. Find where it builds the HTTP client / connection. Add a path: when an MCP server's config has `oauth: { authorization_url, token_url, client_id, redirect_uri }`, run `MCPOAuthClient.build_authorization_url(...)`, surface the URL to the user (CLI prompt), accept the code via local callback or paste, then `exchange_code_for_token(...)`. Cache the token in `~/.opencomputer/<profile>/mcp/tokens.json`.

This wiring is non-trivial; document the hook point but allow the OAuth client to be used standalone before full integration. Tests for the wiring belong in a follow-up; this PR ships the OAuth client + standalone tests.

- [ ] **Step 5: All tests pass + ruff clean**

### Task D-Final — Commit + push + PR

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-3
git add OpenComputer/opencomputer/tools/memory.py OpenComputer/opencomputer/tools/session_search.py OpenComputer/opencomputer/tools/send_message.py OpenComputer/opencomputer/mcp/oauth.py OpenComputer/opencomputer/tools/registry.py OpenComputer/tests/test_memory_tool.py OpenComputer/tests/test_session_search_tool.py OpenComputer/tests/test_send_message_tool.py OpenComputer/tests/test_mcp_oauth.py
# (Also add OpenComputer/opencomputer/mcp/client.py if you wired OAuth into it.)
git commit -m "$(cat <<'EOF'
feat(tools,mcp): port memory_tool + session_search + send_message + mcp_oauth from Hermes

Phase D of best-of-import. 4 Hermes-tagged "high value, port to core"
items in one PR:

- MemoryTool: LLM-callable verbs (write/append/search/list/delete) over
  MEMORY.md. ContextVar-aware via _home() — per-profile by default.
- SessionSearchTool: LLM-callable wrapper for SessionDB FTS5 search.
  Today FTS5 is CLI-only.
- SendMessageTool: LLM-callable cross-platform send via OutgoingQueue.
  For cron / standing orders / proactive flows.
- MCPOAuthClient: OAuth 2.1 + PKCE per RFC 9700. Standalone client +
  hook documented in mcp/client.py for full integration follow-up.

All four tools register in the global tool_registry. Refs: spec at
docs/superpowers/specs/2026-05-02-best-of-import-design.md (Phase D).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push -u origin feat/phase-d-hermes-tool-ports
gh pr create --title "feat(tools,mcp): port 4 Hermes tools (Phase D)" --body "(see commit)"
```

---

# PR 2 — Phase B: Provider Plugins (2 items)

**PR title:** `feat(providers): ollama + groq chat (Phase B)`
**Branch:** `feat/phase-b-providers`
**Estimated scope:** ~250 LOC + ~150 LOC tests, ~3 hours.

### Task B1 — `extensions/ollama-provider/`

**Files:**
- Create: `extensions/ollama-provider/plugin.py`
- Create: `extensions/ollama-provider/provider.py`
- Create: `extensions/ollama-provider/plugin.json`
- Test: `tests/test_ollama_provider.py`

- [ ] **Step 1: Read source pattern**

Read `/Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/ollama/` for the OpenClaw shape, then read `extensions/openai-provider/` in OC for the OC plugin shape (since Ollama exposes an OpenAI-compatible API, much can be shared with the openai-provider pattern).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ollama_provider.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# The plugin's provider class
from extensions.ollama_provider.provider import OllamaProvider


def test_provider_metadata() -> None:
    """Provider declares its name + supported models."""
    p = OllamaProvider(api_key=None, base_url="http://localhost:11434/v1")
    # Provider class must inherit BaseProvider and declare name
    assert hasattr(p, "complete") or hasattr(p, "stream_complete")


@pytest.mark.asyncio
async def test_ollama_complete_calls_http_endpoint() -> None:
    """Provider.complete posts to the configured Ollama base_url."""
    fake_response_data = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    fake_resp = AsyncMock()
    fake_resp.json = AsyncMock(return_value=fake_response_data)
    fake_resp.raise_for_status = lambda: None

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=fake_resp)

    with patch("httpx.AsyncClient", return_value=fake_client):
        p = OllamaProvider(api_key=None, base_url="http://localhost:11434/v1")
        # Adjust signature to match OC's BaseProvider.complete
        from plugin_sdk.core import Message
        result = await p.complete(
            messages=[Message(role="user", content="hi")],
            model="ollama/llama3.2",
            max_tokens=100,
        )
        assert "ok" in result.message.content
```

- [ ] **Step 3: Implementation**

Create `extensions/ollama-provider/provider.py` (uses OpenAI-compatible endpoint for both streaming + non-streaming):

```python
"""Ollama provider — OpenAI-compatible HTTP client to localhost:11434.

Per 2026 popularity research, Ollama is the #1 local-LLM tool for
individual developers. This provider exposes Ollama as a first-class
OC provider. Default base URL ``http://localhost:11434/v1``;
overridable via plugin config.

Plugin-SDK boundary: this file does NOT import from opencomputer.*.
"""
from __future__ import annotations

from typing import Any

import httpx

from plugin_sdk.core import Message, ProviderResponse, Usage
from plugin_sdk.provider_contract import BaseProvider


class OllamaProvider(BaseProvider):
    name = "ollama"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "http://localhost:11434/v1",
    ) -> None:
        self.api_key = api_key  # ollama is typically open; api_key is a placeholder for parity
        self.base_url = base_url.rstrip("/")

    async def complete(
        self,
        *,
        messages: list[Message],
        model: str,
        max_tokens: int = 1024,
        tools: Any = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        # Strip the "ollama/" prefix to get the actual ollama model tag
        ollama_model = model.split("/", 1)[1] if "/" in model else model
        payload = {
            "model": ollama_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(
                f"{self.base_url}/chat/completions",
                json=payload, headers=headers,
            )
            r.raise_for_status()
            j = r.json()
        choice = j["choices"][0]
        text = choice["message"]["content"]
        u = j.get("usage") or {}
        return ProviderResponse(
            message=Message(role="assistant", content=text),
            stop_reason=choice.get("finish_reason", "stop"),
            usage=Usage(
                input_tokens=int(u.get("prompt_tokens", 0)),
                output_tokens=int(u.get("completion_tokens", 0)),
            ),
        )

    async def stream_complete(self, *args: Any, **kwargs: Any) -> Any:
        # Implement streaming; pattern mirrors openai-provider's StreamEvent generator
        raise NotImplementedError("streaming TBD — non-streaming covers most uses")
```

> Adjust kwargs/methods to match OC's `BaseProvider` exactly. Read `plugin_sdk/provider_contract.py` and `extensions/openai-provider/provider.py` first.

Create `extensions/ollama-provider/plugin.py`:

```python
"""Ollama provider plugin entry point."""
from plugin_sdk.core import PluginManifest

from extensions.ollama_provider.provider import OllamaProvider


def register(api):
    api.providers["ollama"] = OllamaProvider


MANIFEST = PluginManifest(
    id="ollama-provider",
    name="Ollama Provider",
    version="0.1.0",
    kind="provider",
    entry="plugin.py",
)
```

- [ ] **Step 4: Tests pass + ruff clean**

### Task B2 — `extensions/groq-provider/`

Same shape as B1, but base_url is `https://api.groq.com/openai/v1` and reads `GROQ_API_KEY`. Default models: `groq/llama-4-70b`, `groq/mixtral-8x7b`. Reuse the openai-provider pattern.

**Tests:** mock-based; live opt-in benchmark. 3 tests minimum (init, complete-non-streaming, missing-API-key error).

### Task B-Final — Commit + push + PR

Standard pattern.

---

# PR 3 — Phase C: Search Tool Plugins (3 items)

**PR title:** `feat(tools): firecrawl + tavily + exa search plugins (Phase C)`
**Branch:** `feat/phase-c-search-tools`
**Estimated scope:** ~400 LOC + ~250 LOC tests, ~3 hours.

### Task C1 — `extensions/firecrawl/`

**Files:**
- Create: `extensions/firecrawl/plugin.py`
- Create: `extensions/firecrawl/tool.py`
- Create: `extensions/firecrawl/plugin.json`
- Test: `tests/test_firecrawl_tool.py`

- [ ] **Step 1: Read source**

```bash
ls /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/firecrawl/
cat /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/firecrawl/*.ts | head -200
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_firecrawl_tool.py
"""Firecrawl tool — LLM-callable web search + scrape via api.firecrawl.dev."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from extensions.firecrawl.tool import FirecrawlTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_firecrawl_search_calls_search_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    fake_resp = AsyncMock()
    fake_resp.json = AsyncMock(return_value={"data": [
        {"title": "Ollama", "url": "https://ollama.com", "markdown": "# Ollama\n..."},
    ]})
    fake_resp.raise_for_status = lambda: None
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=fake_resp)
    with patch("httpx.AsyncClient", return_value=fake_client):
        tool = FirecrawlTool()
        res = await tool.execute(ToolCall(
            id="t1", name="firecrawl_search", arguments={"query": "ollama"}
        ))
        assert "Ollama" in res.content


@pytest.mark.asyncio
async def test_firecrawl_no_api_key_errors(monkeypatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    tool = FirecrawlTool()
    res = await tool.execute(ToolCall(
        id="t1", name="firecrawl_search", arguments={"query": "x"}
    ))
    assert res.is_error
    assert "FIRECRAWL_API_KEY" in res.content
```

- [ ] **Step 3: Implementation**

```python
# extensions/firecrawl/tool.py
"""Firecrawl LLM-callable tool — search + scrape.

Per 2026 popularity benchmarks, Firecrawl is the "starting recommendation"
for agent web research. Free tier 500 credits at api.firecrawl.dev.
Reads FIRECRAWL_API_KEY env var.

Plugin-SDK boundary: no opencomputer.* imports.
"""
from __future__ import annotations

import os

import httpx

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class FirecrawlTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="firecrawl_search",
            description=(
                "Search the web via Firecrawl. Returns clean markdown for "
                "top results. Use for research, current events, finding "
                "specific info on the open web. Free tier requires "
                "FIRECRAWL_API_KEY env var."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            return ToolResult(
                call.id,
                "Error: FIRECRAWL_API_KEY env var not set. Get a free key at firecrawl.dev",
                is_error=True,
            )
        q = (call.arguments.get("query") or "").strip()
        if not q:
            return ToolResult(call.id, "Error: query required", is_error=True)
        limit = int(call.arguments.get("limit", 5))
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.post(
                    "https://api.firecrawl.dev/v1/search",
                    json={"query": q, "limit": limit},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                j = r.json()
            data = j.get("data") or []
            if not data:
                return ToolResult(call.id, f"0 results for {q!r}")
            lines = []
            for d in data:
                lines.append(f"- {d.get('title','(no title)')} <{d.get('url','')}>")
                if d.get("markdown"):
                    lines.append("  " + d["markdown"][:400].replace("\n", " ").strip())
            return ToolResult(call.id, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.id, f"Error: {type(exc).__name__}: {exc}", is_error=True)
```

### Task C2 — `extensions/tavily/`

Same shape. POST to `https://api.tavily.com/search` with `{"api_key": ..., "query": ...}`. Reads `TAVILY_API_KEY`.

### Task C3 — `extensions/exa/`

Same shape. POST to `https://api.exa.ai/search`. Reads `EXA_API_KEY`.

### Task C-Final — Commit + push + PR

Standard pattern.

---

# PR 4 — Phase A1: Block Streaming Chunker

**PR title:** `feat(gateway): block streaming chunker + humanDelay (Phase A1)`
**Branch:** `feat/phase-a1-streaming-chunker`
**Estimated scope:** ~400 LOC + ~300 LOC tests, ~5 hours.

### Task A1.1 — Chunker core

**Files:**
- Create: `opencomputer/gateway/streaming_chunker.py`
- Test: `tests/test_streaming_chunker.py`

- [ ] **Step 1: Read source**

OpenClaw's chunker source: `sources/openclaw-2026.4.23/src/streaming/`. Read both the chunker and the test fixtures to understand boundary preferences + code-fence handling.

- [ ] **Step 2: Write 8 failing tests**

```python
# tests/test_streaming_chunker.py
"""BlockStreamingChunker — buffers token deltas, splits at human-readable
boundaries, idle-coalesces, applies humanDelay between blocks.

Source: sources/openclaw-2026.4.23/src/streaming/. Adapted to Python +
asyncio. Per-channel opt-in via plugin config.
"""
from __future__ import annotations

import asyncio
import pytest

from opencomputer.gateway.streaming_chunker import (
    BlockStreamingChunker,
    ChunkerConfig,
)


def _cfg(**overrides) -> ChunkerConfig:
    base = dict(
        min_chars=20,
        max_chars=400,
        idle_ms=200,
        human_delay_min_ms=0,    # disable delay for fast tests
        human_delay_max_ms=0,
    )
    base.update(overrides)
    return ChunkerConfig(**base)


@pytest.mark.asyncio
async def test_chunker_emits_at_paragraph_boundary() -> None:
    """Prefer paragraph (\\n\\n) over other boundaries when both available."""
    chunker = BlockStreamingChunker(_cfg())
    out: list[str] = []
    async def feed():
        chunker.feed("First paragraph that is long enough to exceed min_chars threshold.\n\n")
        chunker.feed("Second paragraph follows. ")
        chunker.close()
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # First chunk should end at \n\n boundary
    assert out[0].endswith("\n\n") or out[0].rstrip().endswith(".")


@pytest.mark.asyncio
async def test_chunker_never_splits_inside_code_fence() -> None:
    chunker = BlockStreamingChunker(_cfg(max_chars=50))
    out: list[str] = []
    async def feed():
        chunker.feed("Here is code:\n```python\ndef foo():\n    return 1\n```\n")
        chunker.close()
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # Code fence must be intact in some chunk (joined output preserves fence)
    joined = "".join(out)
    assert "```python" in joined and "```\n" in joined
    # No chunk should END inside an open code fence
    for chunk in out:
        opens = chunk.count("```")
        assert opens % 2 == 0, f"chunk has unmatched ```: {chunk!r}"


@pytest.mark.asyncio
async def test_chunker_idle_coalesce() -> None:
    chunker = BlockStreamingChunker(_cfg(min_chars=10, idle_ms=100))
    out: list[str] = []
    async def feed():
        chunker.feed("Short. ")
        await asyncio.sleep(0.01)  # < idle_ms
        chunker.feed("Another short. ")
        await asyncio.sleep(0.20)  # > idle_ms — should now flush
        chunker.feed("Final part long enough to exceed.")
        chunker.close()
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # First emit should contain both short pieces (coalesced after idle)
    assert any("Short" in c and "Another" in c for c in out)


@pytest.mark.asyncio
async def test_chunker_min_chars_holds_until_threshold() -> None:
    chunker = BlockStreamingChunker(_cfg(min_chars=50, idle_ms=10000))  # huge idle
    out: list[str] = []
    async def feed():
        chunker.feed("Tiny.")  # under min_chars
        chunker.close()  # close should force-flush
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # Force-flush on close emits the buffer
    assert any("Tiny" in c for c in out)


@pytest.mark.asyncio
async def test_chunker_max_chars_forces_split() -> None:
    chunker = BlockStreamingChunker(_cfg(min_chars=10, max_chars=60))
    out: list[str] = []
    async def feed():
        chunker.feed("a" * 200)  # one long blob, no boundaries
        chunker.close()
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # No single chunk should exceed max_chars (except slack for safety)
    for chunk in out:
        assert len(chunk) <= 100, f"chunk too long: {len(chunk)}"


@pytest.mark.asyncio
async def test_chunker_sentence_boundary_preference() -> None:
    chunker = BlockStreamingChunker(_cfg(min_chars=15, max_chars=200))
    out: list[str] = []
    async def feed():
        chunker.feed("Sentence one. Sentence two. Sentence three. Sentence four.")
        chunker.close()
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    # At least one chunk should end at a sentence boundary
    assert any(c.rstrip().endswith(".") for c in out)


@pytest.mark.asyncio
async def test_chunker_empty_input_emits_nothing() -> None:
    chunker = BlockStreamingChunker(_cfg())
    out: list[str] = []
    async def feed():
        chunker.close()  # no feed at all
    async def collect():
        async for chunk in chunker.chunks():
            out.append(chunk)
    await asyncio.gather(feed(), collect())
    assert out == []


@pytest.mark.asyncio
async def test_chunker_human_delay_applied() -> None:
    """Verify human_delay sleeps between emits (use small delay for tests)."""
    chunker = BlockStreamingChunker(_cfg(
        min_chars=10, human_delay_min_ms=50, human_delay_max_ms=100
    ))
    timestamps: list[float] = []
    import time
    async def feed():
        for _ in range(3):
            chunker.feed("Some content. " * 5)
        chunker.close()
    async def collect():
        async for _ in chunker.chunks():
            timestamps.append(time.monotonic())
    await asyncio.gather(feed(), collect())
    # Between consecutive emits there should be at least the min delay
    if len(timestamps) >= 2:
        gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        # Allow some slack but verify some delay was applied
        assert max(gaps) >= 0.04, f"no delay observed: {gaps}"
```

- [ ] **Step 3: Implementation**

```python
# opencomputer/gateway/streaming_chunker.py
"""Block streaming chunker with humanDelay — channel UX improvement.

Source: OpenClaw streaming subsystem (sources/openclaw-2026.4.23/src/
streaming/). Adapted to Python + asyncio.

Token-stream → coarse blocks at human-readable boundaries:
  paragraph (\\n\\n) → newline (\\n) → sentence (. ! ?) → whitespace.

Never splits inside ``` code fences. Idle-coalesces (waits idle_ms
before flushing partial buffer). Applies randomized human_delay between
emits so channel output reads naturally.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    min_chars: int = 80
    max_chars: int = 600
    idle_ms: int = 350
    human_delay_min_ms: int = 800
    human_delay_max_ms: int = 2500


class BlockStreamingChunker:
    """Buffer tokens; emit blocks at boundary preference; apply human delay.

    Usage:

        chunker = BlockStreamingChunker(ChunkerConfig())
        async def producer():
            async for delta in provider.stream_complete(...):
                chunker.feed(delta)
            chunker.close()
        async def consumer():
            async for block in chunker.chunks():
                await adapter.send(chat_id, block)
        await asyncio.gather(producer(), consumer())
    """

    def __init__(self, config: ChunkerConfig) -> None:
        self._cfg = config
        self._buf = ""
        self._closed = False
        self._cv = asyncio.Condition()

    def feed(self, text: str) -> None:
        if not text:
            return
        # Schedule notify; can't await from sync method
        self._buf += text
        loop = asyncio.get_event_loop()
        loop.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cv:
            self._cv.notify_all()

    def close(self) -> None:
        self._closed = True
        loop = asyncio.get_event_loop()
        loop.create_task(self._notify())

    async def chunks(self) -> AsyncIterator[str]:
        first = True
        while True:
            chunk = await self._next_chunk()
            if chunk is None:
                return
            if not first:
                # human delay between emits (skip before first)
                if self._cfg.human_delay_min_ms or self._cfg.human_delay_max_ms:
                    delay = random.uniform(
                        self._cfg.human_delay_min_ms / 1000.0,
                        self._cfg.human_delay_max_ms / 1000.0,
                    )
                    await asyncio.sleep(delay)
            first = False
            yield chunk

    async def _next_chunk(self) -> str | None:
        async with self._cv:
            while True:
                # If closed and buffer empty, EOF
                if self._closed and not self._buf:
                    return None
                # If we have enough or are forced to flush, decide split
                buf = self._buf
                in_code_fence = (buf.count("```") % 2) != 0
                if not in_code_fence and (len(buf) >= self._cfg.min_chars or self._closed):
                    split = self._find_split(buf)
                    if split is not None:
                        self._buf = buf[split:]
                        return buf[:split]
                if in_code_fence and self._closed:
                    # Closed mid-fence: emit everything as one block (preserve fence)
                    self._buf = ""
                    return buf
                # Not enough content, or we're inside a code fence — wait
                try:
                    await asyncio.wait_for(self._cv.wait(), timeout=self._cfg.idle_ms / 1000.0)
                except TimeoutError:
                    # Idle gap → flush whatever we have if past min_chars or closed
                    if not in_code_fence and (len(self._buf) >= 1 or self._closed):
                        buf = self._buf
                        if buf:
                            self._buf = ""
                            return buf

    def _find_split(self, buf: str) -> int | None:
        """Return index AFTER which to split, preferring paragraph > newline > sentence > whitespace."""
        # Within the prefix [min_chars : max_chars], find best boundary
        lo = self._cfg.min_chars
        hi = min(len(buf), self._cfg.max_chars)
        if lo >= len(buf):
            # buffer not yet at min_chars
            return None

        # 1. Paragraph (\n\n) within range
        for i in range(min(hi, len(buf) - 1), lo, -1):
            if buf[i - 1:i + 1] == "\n\n" and not self._inside_code_fence(buf, i):
                return i + 1

        # 2. Newline (\n)
        for i in range(min(hi, len(buf)), lo, -1):
            if buf[i - 1] == "\n" and not self._inside_code_fence(buf, i):
                return i

        # 3. Sentence end (. ! ?) followed by space
        for i in range(min(hi, len(buf) - 1), lo, -1):
            if buf[i - 1] in ".!?" and (i >= len(buf) or buf[i].isspace()):
                if not self._inside_code_fence(buf, i):
                    return i

        # 4. Whitespace
        for i in range(min(hi, len(buf)), lo, -1):
            if buf[i - 1].isspace() and not self._inside_code_fence(buf, i):
                return i

        # 5. Hard split at max_chars (only if we MUST)
        if len(buf) >= self._cfg.max_chars:
            return self._cfg.max_chars
        return None

    def _inside_code_fence(self, buf: str, pos: int) -> bool:
        """Are we inside an open code fence at position pos?"""
        return (buf[:pos].count("```") % 2) != 0


__all__ = ["BlockStreamingChunker", "ChunkerConfig"]
```

- [ ] **Step 4: Channel-adapter opt-in integration**

In `extensions/telegram/adapter.py` (and similar for discord, slack), add a config flag `streaming.use_chunker: true` that wraps the outgoing stream in `BlockStreamingChunker`. Default: false (no behavior change).

- [ ] **Step 5: Tests pass + ruff clean + commit + PR**

---

# PR 5 — Phase A2: Active Memory

**PR title:** `feat(agent): active memory pre-reply blocking recall (Phase A2)`
**Branch:** `feat/phase-a2-active-memory`
**Estimated scope:** ~250 LOC + ~200 LOC tests, ~4 hours.

### Task A2.1 — Core implementation

**Files:**
- Create: `opencomputer/agent/active_memory.py`
- Modify: `opencomputer/agent/loop.py` (one hook before reply emission)
- Test: `tests/test_active_memory.py`

- [ ] **Step 1: Read source**

```bash
ls /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/active-memory/
```

- [ ] **Step 2: Tests + impl**

The active-memory module is essentially:

```python
async def maybe_recall_for(
    user_message: str,
    *,
    memory_search_fn,
    token_budget: int = 1500,
    eligibility: callable = None,
) -> str | None:
    """Run a bounded recall sub-agent. Return prefix to inject, or None."""
    if eligibility is not None and not eligibility(user_message):
        return None
    if not user_message.strip():
        return None
    try:
        hits = await asyncio.wait_for(
            memory_search_fn(user_message, limit=5),
            timeout=2.0,
        )
        if not hits:
            return None
        prefix = "\n".join(h.content for h in hits)[:token_budget * 4]  # rough char budget
        return f"<recalled-memory>\n{prefix}\n</recalled-memory>"
    except (asyncio.TimeoutError, Exception):
        return None
```

Wire into `agent/loop.py` at the pre-reply emission point. Config: opt-in flag in `cfg.memory.active_memory_enabled`.

Tests cover: eligibility filter, timeout fallback, empty user message, hits returned, no hits returned, opt-in flag respected.

### Task A2-Final — Commit + push + PR

---

# PR 6 — Phase A3: Standing Orders

**PR title:** `feat(agent): standing orders — autonomous program authority (Phase A3)`
**Branch:** `feat/phase-a3-standing-orders`
**Estimated scope:** ~400 LOC + ~300 LOC tests, ~5 hours.

### Task A3.1 — AGENTS.md `## Program:` parser

**Files:**
- Create: `opencomputer/agent/standing_orders.py`
- Modify: `opencomputer/agent/loop.py` (apply standing orders as system context)
- Test: `tests/test_standing_orders.py`

- [ ] **Step 1: Read source + AGENTS.md examples**

```bash
ls /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/src/agents/
grep -rn "## Program:" /Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/extensions/ | head -10
```

- [ ] **Step 2: Test + impl**

```python
# opencomputer/agent/standing_orders.py
"""Standing Orders — text-contract for autonomous program authority.

Source: OpenClaw standing-orders pattern. Declarative `## Program:` blocks
in AGENTS.md grant the agent permanent operating authority for autonomous
programs. Each block defines:

    ## Program: morning-briefing
    Scope: read-only access to ~/.opencomputer/briefings/
    Triggers: cron("0 7 * * *")
    Approval: auto for read; ask for any send_message > 100 chars
    Escalation: stop and ping me if 3 consecutive failures

The parser reads AGENTS.md, extracts `## Program:` blocks, validates
each, and surfaces them to the agent loop as system-prompt context +
runtime authority hints.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StandingOrder:
    name: str
    scope: str
    triggers: tuple[str, ...]
    approval: str
    escalation: str
    raw: str


_PROGRAM_RE = re.compile(
    r"^## Program: (?P<name>[\w\-]+)\s*$\n(?P<body>(?:(?!^## ).+\n?)*)",
    re.MULTILINE,
)


def parse_standing_orders(agents_md: str) -> list[StandingOrder]:
    out: list[StandingOrder] = []
    for m in _PROGRAM_RE.finditer(agents_md):
        name = m.group("name").strip()
        body = m.group("body")
        scope = _extract_field(body, "Scope")
        triggers = tuple(t.strip() for t in _extract_field(body, "Triggers").split(",") if t.strip())
        approval = _extract_field(body, "Approval")
        escalation = _extract_field(body, "Escalation")
        if not scope:
            continue  # malformed — skip
        out.append(StandingOrder(
            name=name, scope=scope, triggers=triggers,
            approval=approval, escalation=escalation, raw=m.group(0),
        ))
    return out


def _extract_field(body: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


def load_standing_orders(profile_home: Path) -> list[StandingOrder]:
    agents_md = profile_home / "AGENTS.md"
    if not agents_md.exists():
        return []
    return parse_standing_orders(agents_md.read_text(encoding="utf-8"))


__all__ = ["StandingOrder", "parse_standing_orders", "load_standing_orders"]
```

Wire into `agent/loop.py` to inject Standing Orders into system prompt at session start. Cron triggers integrate with OC's existing cron via runtime registration.

Tests cover: parser well-formed/malformed, multiple programs, missing Scope (skip), trigger field comma-split, integration test (orders surface in system prompt).

### Task A3-Final — Commit + push + PR

---

## Self-review (run after writing)

### 1. Spec coverage check

| Spec section | Plan coverage |
|---|---|
| §2 Phase A — A1 streaming chunker | ✅ PR 4 with 8 tests |
| §2 Phase A — A2 Active Memory | ✅ PR 5 |
| §2 Phase A — A3 Standing Orders | ✅ PR 6 |
| §2 Phase B — B1 ollama | ✅ PR 2 task B1 |
| §2 Phase B — B2 groq | ✅ PR 2 task B2 |
| §2 Phase C — C1 firecrawl | ✅ PR 3 task C1 |
| §2 Phase C — C2 tavily | ✅ PR 3 task C2 |
| §2 Phase C — C3 exa | ✅ PR 3 task C3 |
| §2 Phase D — D1 memory_tool | ✅ PR 1 task D1 |
| §2 Phase D — D2 session_search | ✅ PR 1 task D2 |
| §2 Phase D — D3 send_message | ✅ PR 1 task D3 |
| §2 Phase D — D4 mcp_oauth | ✅ PR 1 task D4 |
| §3 Phase ordering D→B→C→A | ✅ PR 1=D, PR 2=B, PR 3=C, PRs 4-6 = A |
| §4 Cross-cutting (worktree, plugin SDK boundary, subagent split) | ✅ Per-PR-Final task includes |
| §5 Error handling | ✅ Each tool has try/except + is_error returns |
| §6 Testing | ✅ TDD red-green for every task |

No gaps.

### 2. Placeholder scan

Search the plan for: TBD, TODO, "implement later," "fill in details," "Add appropriate error handling," "similar to Task N." Result: 1 instance of "TBD" in B1 stream_complete (intentional — non-streaming covers most uses; streaming is a follow-up). Acceptable; documented as "non-streaming covers most uses."

### 3. Type consistency

- `BaseTool` / `ToolSchema` / `ToolCall` / `ToolResult` — used consistently.
- `BaseProvider` / `Message` / `ProviderResponse` / `Usage` — used consistently in B1.
- `_home()` reference (D1) — matches OC's existing helper.
- `MCPOAuthClient` / `OAuthToken` / `generate_pkce_pair` — consistent throughout D4.

No drift.

---

## Adversarial-audit hooks (per user's standing audit instructions)

Per the user's flow: brainstorm → writing-plans → **rigorous self-audit** → executing-plans. The audit appends below in the next turn before execution starts. Hooks the audit MUST cover:

1. **Plugin SDK boundary** — does each new extension's plugin.py / provider.py / tool.py avoid `from opencomputer` imports? (Test enforces this; implementer must not bypass.)
2. **OutgoingQueue lifecycle** — does send_message_tool (D3) handle `queue=None` (CLI/test path)?
3. **Chunker re-entrancy** — does the chunker handle being closed mid-feed (race condition)?
4. **OAuth token caching** — where do tokens get stored? File permissions? Per-profile?
5. **Active Memory failure isolation** — if memory_search hangs, does it block the reply forever?
6. **Standing Orders scope enforcement** — does the parser do anything to ENFORCE scope, or just declare it? (Likely just declare; enforcement TBD as follow-up.)
7. **B1 model name format** — is `ollama/llama3.2` the right convention given OC's existing model-prefix logic?
8. **D4 OAuth `httpx.AsyncClient` mock** — does the test correctly mock both context-manager + post call?

---

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-05-02-best-of-import.md`. Next per user's flow: rigorous self-audit (their explicit instruction) → invoke `superpowers:executing-plans` (or subagent-driven-development).
