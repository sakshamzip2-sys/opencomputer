# Hermes Doc Tier-S — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 8 Hermes-doc Tier-S residue items in 1 PR (~750 LOC + ~50 tests): MCP utility tools, API server `/v1/capabilities` + `/health/detailed`, Honcho query-adaptive reasoning + `oc honcho` CLI, credential pool strategy config + 402/401 cooldowns + `oc auth` CLI.

**Architecture:** Single PR, 8 numbered commits. New Typer CLI groups (`oc honcho`, `oc auth`) live in dedicated modules (`cli_honcho.py`, `cli_auth.py`) wired with one-line `add_typer` calls in `cli.py`. MCP utility-tool registration extends existing `MCPClient.discover()` flow. API server endpoints added to existing aiohttp router. Honcho query-adaptive scaling is purely additive on `HonchoConfig`. Credential pool extension adds an optional `classify_failure` callback to `with_retry` (backwards-compat).

**Tech Stack:** Python 3.12+, frozen+slots dataclasses, aiohttp, Typer, pytest, mcp SDK ClientSession.

---

## File map (created / modified)

**Create:**
- `opencomputer/cli_honcho.py` — `oc honcho status / sync / enable / disable / strategy` (T5)
- `opencomputer/cli_auth.py` — `oc auth list / add / remove / reset` (T8)
- `tests/test_mcp_utility_tools.py` (T1)
- `tests/test_api_server_capabilities_health.py` (T2 + T3)
- `tests/test_honcho_query_adaptive.py` (T4)
- `tests/test_oc_honcho_cli.py` (T5)
- `tests/test_credential_pool_strategies_config.py` (T6)
- `tests/test_credential_pool_error_cooldowns.py` (T7)
- `tests/test_oc_auth_cli.py` (T8)

**Modify:**
- `opencomputer/mcp/client.py` — `_register_utility_tools(server_name, session, capabilities, registry)` (T1)
- `extensions/api-server/adapter.py` — add `_handle_capabilities` + `_handle_health_detailed` routes (T2 + T3)
- `extensions/memory-honcho/provider.py` — add `dialectic_reasoning_level` + `reasoning_level_cap` config fields + `_adapt_reasoning_level()` helper, wire into dialectic call (T4)
- `opencomputer/cli.py` — two `app.add_typer` lines (T5 + T8)
- `opencomputer/agent/config.py` — add `credential_pool_strategies: dict[str, str]` to `Config` (T6)
- `opencomputer/agent/config_store.py` — parse + serialize `credential_pool_strategies` (T6)
- `opencomputer/agent/credential_sources.py` — pass strategy from config to pool builder (T6)
- `opencomputer/agent/credential_pool.py` — extend `report_auth_failure(ttl_seconds=None)` + extend `with_retry(classify_failure=None)` + add `EXHAUSTED_TTL_402_SECONDS` constant + add `OAuthRefresher` protocol type for 401 path (T7)

---

## Pre-flight

### Task 0: Worktree + baseline

- [ ] **Step 0.1: Confirm origin/main alignment**
  ```bash
  git -C /Users/saksham/Vscode/claude status --short
  git -C /Users/saksham/Vscode/claude log --oneline origin/main..HEAD
  ```
  Expected: clean tree (modulo new spec/plan files) + empty diff-ahead.

- [ ] **Step 0.2: Create worktree**
  ```bash
  cd /Users/saksham/Vscode/claude
  git worktree add -b feat/hermes-doc-tier-s-2026-05-08 \
    .claude/worktrees/hermes-tier-s-2026-05-08 origin/main
  ```

- [ ] **Step 0.3: Editable install**
  ```bash
  cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-tier-s-2026-05-08
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e ./OpenComputer 2>&1 | tail -5
  ```

- [ ] **Step 0.4: Baseline test pass**
  ```bash
  cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-tier-s-2026-05-08/OpenComputer
  pytest tests/ -x -q --ignore=tests/test_phase12b1_honcho_default.py 2>&1 | tail -5
  ```
  Expected: `===== N passed in Ms =====`. The Honcho-default flake (per `project_honcho_default_test_pollution_flake.md` memory) is excluded for baseline only.

- [ ] **Step 0.5: Move spec + plan files into worktree**
  ```bash
  cp /Users/saksham/Vscode/claude/OpenComputer/docs/superpowers/specs/2026-05-08-hermes-doc-tier-s-design.md \
     /Users/saksham/Vscode/claude/.claude/worktrees/hermes-tier-s-2026-05-08/OpenComputer/docs/superpowers/specs/
  cp /Users/saksham/Vscode/claude/OpenComputer/docs/superpowers/plans/2026-05-08-hermes-doc-tier-s.md \
     /Users/saksham/Vscode/claude/.claude/worktrees/hermes-tier-s-2026-05-08/OpenComputer/docs/superpowers/plans/
  ```

---

## T1 — MCP utility tools

**Files:**
- Modify: `opencomputer/mcp/client.py` — add `_register_utility_tools()` and call it from existing discovery flow (probable location: near line 400 where `session.list_tools()` is called)
- Create: `tests/test_mcp_utility_tools.py`

### Task 1.1: Read mcp/client.py discovery flow

- [ ] **Step 1.1.1: Read MCP client tool-registration site**
  ```bash
  sed -n '380,440p' opencomputer/mcp/client.py
  ```
  Identify: where `session.list_tools()` is invoked, where each tool is registered into `ToolRegistry`, and where the per-server connect/initialize result is held (it should expose server capabilities under `result.capabilities` or similar).

### Task 1.2: Failing test — list_resources tool registers when capability advertised

- [ ] **Step 1.2.1: Write test file `tests/test_mcp_utility_tools.py`**
  ```python
  """T1 — MCP utility tools (list_resources / read_resource / list_prompts / get_prompt)."""

  from __future__ import annotations

  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from opencomputer.agent.config import MCPServerConfig
  from opencomputer.mcp.client import _register_utility_tools


  @pytest.fixture
  def mock_session():
      session = MagicMock()
      session.list_resources = AsyncMock(
          return_value=MagicMock(resources=[
              MagicMock(uri="file:///foo.txt", name="foo", mimeType="text/plain"),
          ])
      )
      session.read_resource = AsyncMock(
          return_value=MagicMock(contents=[MagicMock(uri="file:///foo.txt", text="hello")])
      )
      session.list_prompts = AsyncMock(
          return_value=MagicMock(prompts=[MagicMock(name="welcome", description="A greeting")])
      )
      session.get_prompt = AsyncMock(
          return_value=MagicMock(messages=[{"role": "user", "content": "hello"}])
      )
      return session


  @pytest.fixture
  def mock_registry():
      registry = MagicMock()
      registry.register = MagicMock()
      return registry


  def test_resources_capability_registers_two_tools(mock_session, mock_registry):
      capabilities = {"resources": {}, "prompts": None}  # prompts not advertised
      _register_utility_tools("fs", mock_session, capabilities, mock_registry)
      registered = [c.args[0].name for c in mock_registry.register.call_args_list]
      assert "mcp_fs_list_resources" in registered
      assert "mcp_fs_read_resource" in registered
      assert "mcp_fs_list_prompts" not in registered
      assert "mcp_fs_get_prompt" not in registered


  def test_prompts_capability_registers_two_tools(mock_session, mock_registry):
      capabilities = {"resources": None, "prompts": {}}
      _register_utility_tools("git", mock_session, capabilities, mock_registry)
      registered = [c.args[0].name for c in mock_registry.register.call_args_list]
      assert "mcp_git_list_prompts" in registered
      assert "mcp_git_get_prompt" in registered
      assert "mcp_git_list_resources" not in registered


  def test_no_capabilities_registers_nothing(mock_session, mock_registry):
      _register_utility_tools("empty", mock_session, {}, mock_registry)
      assert mock_registry.register.call_count == 0


  def test_both_capabilities_register_four_tools(mock_session, mock_registry):
      capabilities = {"resources": {}, "prompts": {}}
      _register_utility_tools("full", mock_session, capabilities, mock_registry)
      assert mock_registry.register.call_count == 4


  @pytest.mark.asyncio
  async def test_list_resources_tool_invokes_session(mock_session, mock_registry):
      _register_utility_tools("fs", mock_session, {"resources": {}}, mock_registry)
      list_call = mock_registry.register.call_args_list[0]
      tool_schema = list_call.args[0]
      result = await tool_schema.handler()
      assert mock_session.list_resources.await_count == 1
      assert isinstance(result, list)
      assert result[0]["uri"] == "file:///foo.txt"


  @pytest.mark.asyncio
  async def test_read_resource_tool_invokes_session(mock_session, mock_registry):
      _register_utility_tools("fs", mock_session, {"resources": {}}, mock_registry)
      read_call = mock_registry.register.call_args_list[1]
      tool_schema = read_call.args[0]
      result = await tool_schema.handler(uri="file:///foo.txt")
      assert mock_session.read_resource.await_count == 1
      mock_session.read_resource.assert_awaited_with("file:///foo.txt")


  @pytest.mark.asyncio
  async def test_list_prompts_tool_invokes_session(mock_session, mock_registry):
      _register_utility_tools("git", mock_session, {"prompts": {}}, mock_registry)
      list_call = mock_registry.register.call_args_list[0]
      tool_schema = list_call.args[0]
      result = await tool_schema.handler()
      assert mock_session.list_prompts.await_count == 1
      assert result[0]["name"] == "welcome"


  @pytest.mark.asyncio
  async def test_get_prompt_tool_invokes_session(mock_session, mock_registry):
      _register_utility_tools("git", mock_session, {"prompts": {}}, mock_registry)
      get_call = mock_registry.register.call_args_list[1]
      tool_schema = get_call.args[0]
      result = await tool_schema.handler(name="welcome")
      assert mock_session.get_prompt.await_count == 1
      mock_session.get_prompt.assert_awaited_with("welcome", arguments=None)
  ```

- [ ] **Step 1.2.2: Run test (expect ImportError)**
  ```bash
  pytest tests/test_mcp_utility_tools.py -v 2>&1 | tail -10
  ```
  Expected: `ImportError: cannot import name '_register_utility_tools'`.

### Task 1.3: Implement `_register_utility_tools` + `_serialize_*`

- [ ] **Step 1.3.1: Read existing `BaseTool` shape**
  ```bash
  grep -n "class BaseTool\|class ToolSchema\|register" plugin_sdk/tool_contract.py opencomputer/tools/registry.py | head -20
  ```

- [ ] **Step 1.3.2: Add to `opencomputer/mcp/client.py`** — append after the existing tool-discovery block (locate via `grep -n "session.list_tools()" opencomputer/mcp/client.py`).

  ```python
  # ─── T1 — MCP utility tools (list_resources / read_resource / list_prompts / get_prompt)

  def _serialize_resource(r: Any) -> dict[str, Any]:
      """Lift an mcp.types.Resource into a JSON-safe dict."""
      return {
          "uri": getattr(r, "uri", None),
          "name": getattr(r, "name", None),
          "description": getattr(r, "description", None),
          "mimeType": getattr(r, "mimeType", None),
      }


  def _serialize_resource_contents(result: Any) -> dict[str, Any]:
      contents = getattr(result, "contents", None) or []
      return {
          "contents": [
              {"uri": getattr(c, "uri", None), "text": getattr(c, "text", None)}
              for c in contents
          ],
      }


  def _serialize_prompt(p: Any) -> dict[str, Any]:
      return {
          "name": getattr(p, "name", None),
          "description": getattr(p, "description", None),
          "arguments": [
              {"name": getattr(a, "name", None), "required": getattr(a, "required", False)}
              for a in (getattr(p, "arguments", None) or [])
          ],
      }


  def _make_list_resources_tool(server_name: str, session: Any) -> ToolSchema:
      async def list_resources() -> list[dict[str, Any]]:
          result = await session.list_resources()
          return [_serialize_resource(r) for r in (result.resources or [])]

      return ToolSchema(
          name=f"mcp_{server_name}_list_resources",
          description=f"List resources exposed by MCP server '{server_name}'.",
          parameters={"type": "object", "properties": {}, "required": []},
          handler=list_resources,
      )


  def _make_read_resource_tool(server_name: str, session: Any) -> ToolSchema:
      async def read_resource(uri: str) -> dict[str, Any]:
          result = await session.read_resource(uri)
          return _serialize_resource_contents(result)

      return ToolSchema(
          name=f"mcp_{server_name}_read_resource",
          description=f"Read a resource by URI from MCP server '{server_name}'.",
          parameters={
              "type": "object",
              "properties": {"uri": {"type": "string", "description": "Resource URI"}},
              "required": ["uri"],
          },
          handler=read_resource,
      )


  def _make_list_prompts_tool(server_name: str, session: Any) -> ToolSchema:
      async def list_prompts() -> list[dict[str, Any]]:
          result = await session.list_prompts()
          return [_serialize_prompt(p) for p in (result.prompts or [])]

      return ToolSchema(
          name=f"mcp_{server_name}_list_prompts",
          description=f"List prompts exposed by MCP server '{server_name}'.",
          parameters={"type": "object", "properties": {}, "required": []},
          handler=list_prompts,
      )


  def _make_get_prompt_tool(server_name: str, session: Any) -> ToolSchema:
      async def get_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
          result = await session.get_prompt(name, arguments=arguments)
          return {"messages": list(getattr(result, "messages", None) or [])}

      return ToolSchema(
          name=f"mcp_{server_name}_get_prompt",
          description=f"Get a prompt by name from MCP server '{server_name}'.",
          parameters={
              "type": "object",
              "properties": {
                  "name": {"type": "string", "description": "Prompt name"},
                  "arguments": {"type": "object", "description": "Prompt template arguments"},
              },
              "required": ["name"],
          },
          handler=get_prompt,
      )


  def _register_utility_tools(
      server_name: str,
      session: Any,
      capabilities: dict[str, Any] | None,
      registry: ToolRegistry,
  ) -> None:
      """Register MCP resource/prompt utility tools, capability-gated.

      Per Hermes-doc reference: when an MCP server's `initialize` reply
      advertises the ``resources`` capability, register two tools
      (``mcp_<name>_list_resources``, ``mcp_<name>_read_resource``).
      Same for ``prompts``. Empty / missing capabilities → nothing.
      """
      if not capabilities:
          return
      if capabilities.get("resources"):
          registry.register(_make_list_resources_tool(server_name, session))
          registry.register(_make_read_resource_tool(server_name, session))
      if capabilities.get("prompts"):
          registry.register(_make_list_prompts_tool(server_name, session))
          registry.register(_make_get_prompt_tool(server_name, session))
  ```

- [ ] **Step 1.3.3: Wire `_register_utility_tools` into discovery flow** — locate the connect/initialize site:
  ```bash
  grep -n "session.initialize\|init_result\|initialize_result\|result.capabilities" opencomputer/mcp/client.py | head -10
  ```
  Add a call after tools are registered, passing the capabilities dict from the initialize result. If the existing code doesn't surface capabilities, capture them: most MCP SDK `ClientSession.initialize()` returns an `InitializeResult` with a `capabilities` attribute.

  Pattern:
  ```python
  init_result = await session.initialize()
  ...
  # existing tool-list registration
  for tool in tool_list.tools:
      registry.register(_build_mcp_tool(server_cfg.name, tool, session))
  # NEW — utility tools
  _register_utility_tools(
      server_cfg.name,
      session,
      getattr(init_result, "capabilities", None) and {
          "resources": getattr(init_result.capabilities, "resources", None),
          "prompts": getattr(init_result.capabilities, "prompts", None),
      },
      registry,
  )
  ```

- [ ] **Step 1.3.4: Run test**
  ```bash
  pytest tests/test_mcp_utility_tools.py -v 2>&1 | tail -15
  ```
  Expected: 8 passed.

### Task 1.4: Commit T1

- [ ] **Step 1.4.1: Stage + commit**
  ```bash
  git add opencomputer/mcp/client.py tests/test_mcp_utility_tools.py
  git commit -m "feat(mcp): register list_resources/read_resource/list_prompts/get_prompt utility tools (capability-gated)"
  ```

---

## T2 + T3 — API server `/v1/capabilities` + `/health/detailed`

**Files:**
- Modify: `extensions/api-server/adapter.py` — add two route handlers + register them in `_build_app`
- Create: `tests/test_api_server_capabilities_health.py`

### Task 2.1: Failing test for /v1/capabilities

- [ ] **Step 2.1.1: Write `tests/test_api_server_capabilities_health.py`**
  ```python
  """T2 + T3 — API server /v1/capabilities + /health/detailed."""

  from __future__ import annotations

  import pytest
  from aiohttp.test_utils import TestClient, TestServer
  from extensions import api_server  # noqa: F401  (registers extension)


  async def _build_test_client(monkeypatch) -> TestClient:
      from extensions.api_server.adapter import APIServerAdapter

      adapter = APIServerAdapter()
      adapter._token = ""  # disable auth for tests
      app = adapter._build_app()
      server = TestServer(app)
      client = TestClient(server)
      await client.start_server()
      return client


  @pytest.mark.asyncio
  async def test_capabilities_returns_feature_dict(monkeypatch):
      client = await _build_test_client(monkeypatch)
      try:
          resp = await client.get("/v1/capabilities")
          assert resp.status == 200
          payload = await resp.json()
          assert payload["version"] == "1"
          features = payload["features"]
          assert features["chat_completions"] is True
          assert features["streaming"] is True
          assert features["tool_calls"] is True
          assert features["previous_response_id"] is False  # honest deferral
          assert features["runs_api"] is False
          assert features["jobs_api"] is False
      finally:
          await client.close()


  @pytest.mark.asyncio
  async def test_capabilities_advertises_active_profile(monkeypatch):
      monkeypatch.setenv("OPENCOMPUTER_PROFILE", "alice")
      client = await _build_test_client(monkeypatch)
      try:
          resp = await client.get("/v1/capabilities")
          payload = await resp.json()
          assert "profile" in payload
      finally:
          await client.close()


  @pytest.mark.asyncio
  async def test_capabilities_no_auth_required():
      """Capabilities endpoint is public (matches Hermes spec)."""
      from extensions.api_server.adapter import APIServerAdapter

      adapter = APIServerAdapter()
      adapter._token = "secret-token"
      app = adapter._build_app()
      server = TestServer(app)
      client = TestClient(server)
      await client.start_server()
      try:
          resp = await client.get("/v1/capabilities")
          assert resp.status == 200
      finally:
          await client.close()


  @pytest.mark.asyncio
  async def test_health_detailed_returns_status_ok(monkeypatch):
      client = await _build_test_client(monkeypatch)
      try:
          resp = await client.get("/health/detailed")
          assert resp.status == 200
          payload = await resp.json()
          assert payload["status"] == "ok"
          assert "uptime_seconds" in payload
          assert "sessions" in payload
          assert "running_agents" in payload
      finally:
          await client.close()


  @pytest.mark.asyncio
  async def test_health_detailed_partial_failure_returns_200(monkeypatch):
      """When a sub-lookup fails (e.g. SQL unavailable), endpoint still 200."""
      from extensions.api_server import adapter as adapter_mod

      def boom():
          raise RuntimeError("simulated SQL contention")

      monkeypatch.setattr(adapter_mod, "_count_active_sessions", boom)
      client = await _build_test_client(monkeypatch)
      try:
          resp = await client.get("/health/detailed")
          assert resp.status == 200
          payload = await resp.json()
          # sessions field present but null
          assert payload["sessions"] is None
      finally:
          await client.close()
  ```

- [ ] **Step 2.1.2: Run test (expect 404)**
  ```bash
  pytest tests/test_api_server_capabilities_health.py -v 2>&1 | tail -10
  ```
  Expected: 5 failures with 404 / ImportError.

### Task 2.2: Implement /v1/capabilities

- [ ] **Step 2.2.1: Read api-server adapter route registration**
  ```bash
  sed -n '378,400p' extensions/api-server/adapter.py
  ```

- [ ] **Step 2.2.2: Add to `extensions/api-server/adapter.py`** — append handler near other `_handle_*` methods, register route in `_build_app`:

  ```python
  # ─── T2 — /v1/capabilities (Hermes-doc spec)

  async def _handle_capabilities(self, request: web.Request) -> web.Response:
      """Machine-readable feature flags for integrators (Hermes-doc parity).

      Public (no auth). Honest about deferred items (runs_api / jobs_api /
      previous_response_id are False until shipped).
      """
      profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
      payload = {
          "version": "1",
          "model": profile,
          "profile": profile,
          "features": {
              "chat_completions": True,
              "responses": True,  # stub exists
              "streaming": True,
              "tool_calls": True,
              "vision": True,
              "system_prompt": True,
              "previous_response_id": False,
              "runs_api": False,
              "jobs_api": False,
          },
      }
      return web.json_response(payload)
  ```

- [ ] **Step 2.2.3: Register route in `_build_app`**
  Locate `app.router.add_get("/v1/models", self._handle_list_models)` and add below:
  ```python
  # T2 — public capabilities probe
  app.router.add_get("/v1/capabilities", self._handle_capabilities)
  ```

- [ ] **Step 2.2.4: Run capabilities tests**
  ```bash
  pytest tests/test_api_server_capabilities_health.py::test_capabilities_returns_feature_dict tests/test_api_server_capabilities_health.py::test_capabilities_no_auth_required -v 2>&1 | tail -10
  ```
  Expected: 2 passed.

### Task 2.3: Implement /health/detailed

- [ ] **Step 2.3.1: Add helper functions + handler to `extensions/api-server/adapter.py`**

  ```python
  # ─── T3 — /health/detailed

  _ADAPTER_START_TIME: float = time.monotonic()


  def _count_active_sessions() -> int | None:
      """SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL.

      Returns None on any failure (SQL not available, schema missing, etc.).
      """
      try:
          import sqlite3
          from pathlib import Path

          db_path = Path.home() / ".opencomputer" / "default" / "sessions.db"
          if not db_path.exists():
              return None
          with sqlite3.connect(str(db_path)) as conn:
              cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL")
              row = cur.fetchone()
              return int(row[0]) if row else 0
      except Exception:
          return None


  def _count_total_sessions() -> int | None:
      try:
          import sqlite3
          from pathlib import Path

          db_path = Path.home() / ".opencomputer" / "default" / "sessions.db"
          if not db_path.exists():
              return None
          with sqlite3.connect(str(db_path)) as conn:
              cur = conn.execute("SELECT COUNT(*) FROM sessions")
              row = cur.fetchone()
              return int(row[0]) if row else 0
      except Exception:
          return None


  def _process_memory_mb() -> float | None:
      try:
          import psutil

          return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
      except Exception:
          return None
  ```

  Inside `APIServerAdapter`:

  ```python
  async def _handle_health_detailed(self, request: web.Request) -> web.Response:
      """Detailed health probe: sessions, agents, uptime, memory.

      Never returns 5xx — partial failures surface as null fields.
      """
      sessions_active = _count_active_sessions()
      sessions_total = _count_total_sessions()
      memory_mb = _process_memory_mb()
      running_agents = len(self._run_handles) if hasattr(self, "_run_handles") else 0
      uptime = max(0.0, time.monotonic() - _ADAPTER_START_TIME)
      payload = {
          "status": "ok",
          "uptime_seconds": round(uptime, 1),
          "sessions": (
              {"active": sessions_active, "total": sessions_total}
              if sessions_active is not None or sessions_total is not None
              else None
          ),
          "running_agents": running_agents,
          "memory_mb": memory_mb,
          "api_server": {
              "host": self._host,
              "port": self._port,
              "profile": os.environ.get("OPENCOMPUTER_PROFILE", "default"),
          },
      }
      return web.json_response(payload)
  ```

  Add `import time` at top if not already present.

- [ ] **Step 2.3.2: Register route in `_build_app`**
  ```python
  app.router.add_get("/health/detailed", self._handle_health_detailed)
  ```

- [ ] **Step 2.3.3: Run all T2+T3 tests**
  ```bash
  pytest tests/test_api_server_capabilities_health.py -v 2>&1 | tail -10
  ```
  Expected: 5 passed.

### Task 2.4: Commit T2 + T3

- [ ] **Step 2.4.1: Stage + commit**
  ```bash
  git add extensions/api-server/adapter.py tests/test_api_server_capabilities_health.py
  git commit -m "feat(api-server): /v1/capabilities + /health/detailed (Hermes-doc parity)"
  ```

---

## T4 — Honcho query-adaptive reasoning

**Files:**
- Modify: `extensions/memory-honcho/provider.py` — add `dialectic_reasoning_level`, `reasoning_level_cap`, `_adapt_reasoning_level()`
- Create: `tests/test_honcho_query_adaptive.py`

### Task 4.1: Failing test

- [ ] **Step 4.1.1: Read existing HonchoConfig + dialectic call site**
  ```bash
  sed -n '40,80p' extensions/memory-honcho/provider.py
  grep -n "peer.chat\|dialectic\|reasoning" extensions/memory-honcho/provider.py | head -15
  ```

- [ ] **Step 4.1.2: Write `tests/test_honcho_query_adaptive.py`**
  ```python
  """T4 — Honcho query-adaptive reasoning level."""

  from __future__ import annotations

  import sys
  from pathlib import Path

  # extensions/ live outside opencomputer/; load by path.
  _EXT = Path(__file__).resolve().parents[1] / "extensions" / "memory-honcho"
  if str(_EXT) not in sys.path:
      sys.path.insert(0, str(_EXT))

  from provider import HonchoConfig, _adapt_reasoning_level  # noqa: E402


  def test_short_query_no_boost():
      assert _adapt_reasoning_level("low", "hi", "high") == "low"


  def test_120_char_query_one_boost():
      query = "x" * 130
      assert _adapt_reasoning_level("low", query, "high") == "medium"


  def test_400_char_query_two_boost():
      query = "x" * 410
      assert _adapt_reasoning_level("low", query, "high") == "high"


  def test_cap_clamps_boost():
      query = "x" * 410
      # base=medium + 2 boost = should clamp to cap=medium
      assert _adapt_reasoning_level("medium", query, "medium") == "medium"


  def test_config_defaults():
      cfg = HonchoConfig()
      assert cfg.dialectic_reasoning_level == "low"
      assert cfg.reasoning_level_cap == "high"
  ```

- [ ] **Step 4.1.3: Run test (expect ImportError on `_adapt_reasoning_level`)**
  ```bash
  pytest tests/test_honcho_query_adaptive.py -v 2>&1 | tail -10
  ```
  Expected: ImportError.

### Task 4.2: Implement adapter

- [ ] **Step 4.2.1: Modify `extensions/memory-honcho/provider.py`** — extend `HonchoConfig`:

  Find `class HonchoConfig:` (around line 46) and add two fields. Field literal types match the docstring:

  ```python
  @dataclass(frozen=True, slots=True)
  class HonchoConfig:
      """Provider-side config loaded from ~/.opencomputer/honcho/.env or env vars."""

      base_url: str = _DEFAULT_BASE_URL
      api_key: str = ""
      workspace: str = "opencomputer"
      host_key: str = "opencomputer"
      context_cadence: int = 1
      dialectic_cadence: int = 3
      # T4 — Hermes-doc query-adaptive reasoning
      dialectic_reasoning_level: Literal["low", "medium", "high"] = "low"
      reasoning_level_cap: Literal["low", "medium", "high"] = "high"
  ```

- [ ] **Step 4.2.2: Add `_adapt_reasoning_level` helper** — after the dataclass, before the `_HonchoState` class:

  ```python
  _LEVELS: tuple[str, ...] = ("low", "medium", "high")


  def _adapt_reasoning_level(base: str, query: str, cap: str) -> str:
      """Boost the dialectic reasoning level by query length (Hermes-doc heuristic).

      Rules: ≥120 chars → +1 step, ≥400 chars → +2 steps. Clamped at cap.
      Unknown ``base`` or ``cap`` falls back to ``base``.
      """
      try:
          base_idx = _LEVELS.index(base)
          cap_idx = _LEVELS.index(cap)
      except ValueError:
          return base
      boost = 0
      if len(query) >= 120:
          boost += 1
      if len(query) >= 400:
          boost += 1
      return _LEVELS[min(base_idx + boost, cap_idx)]
  ```

- [ ] **Step 4.2.3: Wire into dialectic call site** — locate where `peer.chat(...)` is invoked (likely inside `prefetch` or `sync_turn`). Inject the adapted level:

  ```python
  # Pattern (locate exact call site via grep first):
  reasoning_level = _adapt_reasoning_level(
      self._config.dialectic_reasoning_level,
      latest_user_message,
      self._config.reasoning_level_cap,
  )
  # Pass to httpx call as reasoning_level field; if Honcho server rejects
  # the field (older version), it's silently ignored — best-effort fwd.
  body = {..., "reasoning_level": reasoning_level}
  ```

  If no `peer.chat` call exists yet (the adapter only stores `dialectic_cadence`), this step is design-only; the field still ships and a follow-up wave wires it. Document inline.

- [ ] **Step 4.2.4: Run test**
  ```bash
  pytest tests/test_honcho_query_adaptive.py -v 2>&1 | tail -10
  ```
  Expected: 5 passed.

### Task 4.3: Commit T4

- [ ] **Step 4.3.1: Stage + commit**
  ```bash
  git add extensions/memory-honcho/provider.py tests/test_honcho_query_adaptive.py
  git commit -m "feat(honcho): query-adaptive dialectic reasoning level (length-scaled)"
  ```

---

## T5 — `oc honcho` CLI

**Files:**
- Create: `opencomputer/cli_honcho.py`
- Modify: `opencomputer/cli.py` — one `app.add_typer` line
- Create: `tests/test_oc_honcho_cli.py`

### Task 5.1: Failing test

- [ ] **Step 5.1.1: Write `tests/test_oc_honcho_cli.py`**
  ```python
  """T5 — oc honcho CLI subcommand group."""

  from __future__ import annotations

  import pytest
  from typer.testing import CliRunner

  from opencomputer.cli_honcho import honcho_app


  @pytest.fixture
  def runner():
      return CliRunner()


  def test_status_runs_without_honcho(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["status"])
      assert result.exit_code == 0
      assert "Honcho" in result.stdout or "honcho" in result.stdout.lower()


  def test_enable_writes_provider_to_config(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["enable"])
      assert result.exit_code == 0
      cfg = (tmp_path / "config.yaml").read_text(encoding="utf-8")
      assert "memory:" in cfg
      assert "provider: honcho" in cfg


  def test_disable_writes_provider_builtin(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(honcho_app, ["enable"])
      result = runner.invoke(honcho_app, ["disable"])
      assert result.exit_code == 0
      cfg = (tmp_path / "config.yaml").read_text(encoding="utf-8")
      assert "provider: builtin" in cfg


  def test_strategy_balanced_writes_cadence(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["strategy", "balanced"])
      assert result.exit_code == 0
      cfg = (tmp_path / "config.yaml").read_text(encoding="utf-8")
      assert "context_cadence: 2" in cfg
      assert "dialectic_cadence: 4" in cfg


  def test_strategy_aggressive_sets_medium_reasoning(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["strategy", "aggressive"])
      assert result.exit_code == 0
      cfg = (tmp_path / "config.yaml").read_text(encoding="utf-8")
      assert "context_cadence: 1" in cfg
      assert "dialectic_cadence: 2" in cfg
      assert "dialectic_reasoning_level: medium" in cfg


  def test_strategy_invalid_name_exits_nonzero(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["strategy", "ludicrous"])
      assert result.exit_code != 0
      assert "preset" in result.stdout.lower() or "low" in result.stdout.lower()


  def test_sync_runs_without_honcho_server(runner, monkeypatch, tmp_path):
      """sync should not crash if Honcho server is offline."""
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(honcho_app, ["sync"])
      # exit 0 (best-effort) OR exit 1 with clear message — both acceptable
      assert result.exit_code in (0, 1)


  def test_status_shows_cadence_and_provider(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(honcho_app, ["enable"])
      runner.invoke(honcho_app, ["strategy", "balanced"])
      result = runner.invoke(honcho_app, ["status"])
      assert "honcho" in result.stdout.lower()
      assert "balanced" in result.stdout.lower() or "context_cadence" in result.stdout.lower()
  ```

- [ ] **Step 5.1.2: Run test (expect ImportError on `honcho_app`)**
  ```bash
  pytest tests/test_oc_honcho_cli.py -v 2>&1 | tail -10
  ```
  Expected: ImportError on `from opencomputer.cli_honcho import honcho_app`.

### Task 5.2: Implement `cli_honcho.py`

- [ ] **Step 5.2.1: Create `opencomputer/cli_honcho.py`**
  ```python
  """T5 — `oc honcho` CLI subcommand group.

  Mirrors Hermes-doc canonical UX:
  - status   — show Honcho provider state (cadence / reasoning level)
  - sync     — backfill Honcho peers across all profiles (one-shot)
  - enable   — set memory.provider = honcho in current profile
  - disable  — set memory.provider = builtin in current profile
  - strategy — preset cadence + reasoning level (low / balanced / aggressive)
  """

  from __future__ import annotations

  import os
  from pathlib import Path
  from typing import Any

  import typer
  import yaml
  from rich.console import Console

  console = Console()
  honcho_app = typer.Typer(
      name="honcho",
      help="Manage the Honcho memory provider (Hermes-doc parity).",
      no_args_is_help=True,
  )

  _PRESETS: dict[str, dict[str, Any]] = {
      "low": {
          "context_cadence": 4,
          "dialectic_cadence": 8,
          "dialectic_reasoning_level": "low",
      },
      "balanced": {
          "context_cadence": 2,
          "dialectic_cadence": 4,
          "dialectic_reasoning_level": "low",
      },
      "aggressive": {
          "context_cadence": 1,
          "dialectic_cadence": 2,
          "dialectic_reasoning_level": "medium",
      },
  }


  def _profile_home() -> Path:
      """Return the active profile config directory."""
      override = os.environ.get("OPENCOMPUTER_HOME")
      if override:
          return Path(override)
      profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
      return Path.home() / ".opencomputer" / profile


  def _config_path() -> Path:
      return _profile_home() / "config.yaml"


  def _load_config() -> dict[str, Any]:
      path = _config_path()
      if not path.exists():
          return {}
      try:
          return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
      except yaml.YAMLError:
          console.print(f"[red]Could not parse {path} — refusing to overwrite.[/red]")
          raise typer.Exit(code=1) from None


  def _save_config(data: dict[str, Any]) -> None:
      path = _config_path()
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(
          yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
          encoding="utf-8",
      )


  def _set_memory_provider(provider: str) -> None:
      cfg = _load_config()
      cfg.setdefault("memory", {})
      cfg["memory"]["provider"] = provider
      _save_config(cfg)


  @honcho_app.command("status")
  def status() -> None:
      """Show Honcho provider state — cadence, reasoning level, enabled flag."""
      cfg = _load_config()
      memory = cfg.get("memory", {}) or {}
      provider = memory.get("provider", "builtin")
      cadence_ctx = memory.get("context_cadence", "unset")
      cadence_dial = memory.get("dialectic_cadence", "unset")
      level = memory.get("dialectic_reasoning_level", "unset")
      preset = _detect_preset(memory)
      console.print("Honcho status:")
      console.print(f"  provider: [bold]{provider}[/bold]")
      console.print(f"  context_cadence: {cadence_ctx}")
      console.print(f"  dialectic_cadence: {cadence_dial}")
      console.print(f"  dialectic_reasoning_level: {level}")
      console.print(f"  preset: [bold]{preset}[/bold]")


  def _detect_preset(memory: dict[str, Any]) -> str:
      for name, preset in _PRESETS.items():
          if all(memory.get(k) == v for k, v in preset.items()):
              return name
      return "custom"


  @honcho_app.command("enable")
  def enable() -> None:
      """Set memory.provider = honcho in this profile's config."""
      _set_memory_provider("honcho")
      console.print("[green]Honcho enabled[/green]. Run 'oc honcho status' to verify.")


  @honcho_app.command("disable")
  def disable() -> None:
      """Set memory.provider = builtin in this profile's config."""
      _set_memory_provider("builtin")
      console.print("[yellow]Honcho disabled[/yellow] — built-in memory active.")


  @honcho_app.command("strategy")
  def strategy(name: str = typer.Argument(..., help="Preset: low / balanced / aggressive")) -> None:
      """Apply a cadence + reasoning-level preset to this profile."""
      if name not in _PRESETS:
          console.print(
              f"[red]Unknown preset '{name}'.[/red] Choose one of: "
              f"{', '.join(_PRESETS.keys())}"
          )
          raise typer.Exit(code=1)
      cfg = _load_config()
      cfg.setdefault("memory", {})
      cfg["memory"].update(_PRESETS[name])
      _save_config(cfg)
      console.print(f"[green]Applied '{name}' preset[/green]:")
      for k, v in _PRESETS[name].items():
          console.print(f"  {k}: {v}")


  @honcho_app.command("sync")
  def sync() -> None:
      """Backfill Honcho peers across all profiles (one-shot, idempotent).

      Best-effort — silently skips profiles where Honcho server is unreachable.
      """
      home_root = Path.home() / ".opencomputer"
      if not home_root.exists():
          console.print("[dim]No profiles found at ~/.opencomputer[/dim]")
          return
      profiles = [p.name for p in home_root.iterdir() if p.is_dir()]
      if not profiles:
          console.print("[dim]No profiles found.[/dim]")
          return
      console.print(f"Found {len(profiles)} profile(s): {', '.join(profiles)}")
      synced = 0
      skipped = 0
      for prof in profiles:
          ok = _sync_one_profile(prof)
          if ok:
              synced += 1
          else:
              skipped += 1
      console.print(f"[green]Sync complete[/green]: {synced} synced, {skipped} skipped.")


  def _sync_one_profile(profile: str) -> bool:
      """Best-effort: import the Honcho bootstrap and ensure the AI peer exists.

      Returns True if synced, False on any failure (silent — sync is best-effort).
      """
      try:
          import importlib.util
          import sys

          repo_root = Path(__file__).resolve().parents[1]
          bootstrap_py = repo_root / "extensions" / "memory-honcho" / "bootstrap.py"
          if not bootstrap_py.exists():
              return False
          spec = importlib.util.spec_from_file_location("_honcho_bootstrap", bootstrap_py)
          if spec is None or spec.loader is None:
              return False
          mod = importlib.util.module_from_spec(spec)
          sys.modules["_honcho_bootstrap"] = mod
          spec.loader.exec_module(mod)
          ensure_peer = getattr(mod, "honcho_ensure_peer", None)
          if ensure_peer is None:
              return False
          return bool(ensure_peer(profile=profile))
      except Exception:
          return False
  ```

- [ ] **Step 5.2.2: Wire into `cli.py`** — find the existing `app.add_typer(memory_app, name="memory")` line:
  ```bash
  grep -n "app.add_typer.*memory_app" opencomputer/cli.py
  ```
  Add directly below it:
  ```python
  from opencomputer.cli_honcho import honcho_app  # noqa: E402

  app.add_typer(honcho_app, name="honcho")
  ```

- [ ] **Step 5.2.3: Run T5 tests**
  ```bash
  pytest tests/test_oc_honcho_cli.py -v 2>&1 | tail -15
  ```
  Expected: 8 passed (the `test_sync_runs_without_honcho_server` accepts both 0 and 1 exit).

### Task 5.3: Commit T5

- [ ] **Step 5.3.1: Stage + commit**
  ```bash
  git add opencomputer/cli_honcho.py opencomputer/cli.py tests/test_oc_honcho_cli.py
  git commit -m "feat(cli): oc honcho status/sync/enable/disable/strategy (Hermes-doc parity)"
  ```

---

## T6 — credential_pool_strategies config wiring

**Files:**
- Modify: `opencomputer/agent/config.py` — add `credential_pool_strategies` to `Config`
- Modify: `opencomputer/agent/config_store.py` — round-trip the field (both load + save)
- Modify: `opencomputer/agent/credential_sources.py` — read strategy when building pools
- Create: `tests/test_credential_pool_strategies_config.py`

### Task 6.1: Failing test

- [ ] **Step 6.1.1: Write `tests/test_credential_pool_strategies_config.py`**
  ```python
  """T6 — credential_pool_strategies config knob."""

  from __future__ import annotations

  import yaml
  import pytest

  from opencomputer.agent.config import default_config
  from opencomputer.agent.config_store import _apply_overrides


  def test_default_config_has_empty_strategies():
      cfg = default_config()
      assert cfg.credential_pool_strategies == {}


  def test_yaml_loads_strategies():
      cfg = default_config()
      yaml_data = yaml.safe_load("""
  credential_pool_strategies:
    openrouter: round_robin
    anthropic: least_used
  """)
      out = _apply_overrides(cfg, yaml_data)
      assert out.credential_pool_strategies["openrouter"] == "round_robin"
      assert out.credential_pool_strategies["anthropic"] == "least_used"


  def test_resolve_strategy_falls_back_to_least_used():
      from opencomputer.agent.credential_sources import resolve_pool_strategy

      cfg = default_config()
      assert resolve_pool_strategy(cfg, "openrouter") == "least_used"


  def test_resolve_strategy_unknown_value_warns_and_falls_back(monkeypatch):
      from opencomputer.agent.credential_sources import resolve_pool_strategy
      from opencomputer.agent.config import Config

      cfg = default_config()
      cfg = type(cfg)(**{
          **{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()},
          "credential_pool_strategies": {"openrouter": "made_up_strategy"},
      })
      assert resolve_pool_strategy(cfg, "openrouter") == "least_used"
  ```

- [ ] **Step 6.1.2: Run test (expect AttributeError on `credential_pool_strategies`)**
  ```bash
  pytest tests/test_credential_pool_strategies_config.py -v 2>&1 | tail -10
  ```

### Task 6.2: Add field to Config

- [ ] **Step 6.2.1: Read existing `Config` field shape**
  ```bash
  grep -n "^class Config:\|hooks: tuple" opencomputer/agent/config.py
  sed -n '699,750p' opencomputer/agent/config.py
  ```

- [ ] **Step 6.2.2: Add field to `Config` dataclass**
  Inside `class Config`, add:
  ```python
  # T6 — Hermes-doc credential pool rotation strategy per provider
  credential_pool_strategies: dict[str, str] = field(
      default_factory=dict,
      compare=False,
      hash=False,
  )
  ```

- [ ] **Step 6.2.3: Verify the YAML loader round-trips the field**
  Read `_apply_overrides` in `config_store.py` (already inspected — it handles `dict` fields generically). No changes should be needed; the test will confirm.

- [ ] **Step 6.2.4: Add to `_to_yaml_dict` so `oc config set` round-trips**
  Locate in `config_store.py`:
  ```bash
  grep -n "_to_yaml_dict\|hooks_by_event" opencomputer/agent/config_store.py
  ```
  Add to the result dict construction (near the other top-level fields like `tools`, `gateway`):
  ```python
  if cfg.credential_pool_strategies:
      result["credential_pool_strategies"] = dict(cfg.credential_pool_strategies)
  ```

### Task 6.3: Resolver helper in credential_sources

- [ ] **Step 6.3.1: Add `resolve_pool_strategy` to `opencomputer/agent/credential_sources.py`**
  ```python
  # Append at module bottom:

  from opencomputer.agent.credential_pool import (
      STRATEGY_LEAST_USED,
      SUPPORTED_STRATEGIES,
  )


  def resolve_pool_strategy(cfg: Any, provider: str) -> str:
      """Return the rotation strategy for *provider* per config.yaml.

      Falls back to ``STRATEGY_LEAST_USED`` if unset or unknown.
      """
      strategies = getattr(cfg, "credential_pool_strategies", {}) or {}
      candidate = strategies.get(provider)
      if candidate is None:
          return STRATEGY_LEAST_USED
      if candidate not in SUPPORTED_STRATEGIES:
          import logging

          logging.getLogger(__name__).warning(
              "credential_pool_strategies[%s] = %r is unsupported; falling back to %s",
              provider,
              candidate,
              STRATEGY_LEAST_USED,
          )
          return STRATEGY_LEAST_USED
      return candidate
  ```

  Add `from typing import Any` at the top if missing.

- [ ] **Step 6.3.2: Run T6 tests**
  ```bash
  pytest tests/test_credential_pool_strategies_config.py -v 2>&1 | tail -10
  ```
  Expected: 4 passed.

### Task 6.4: Commit T6

- [ ] **Step 6.4.1: Stage + commit**
  ```bash
  git add opencomputer/agent/config.py opencomputer/agent/config_store.py \
          opencomputer/agent/credential_sources.py \
          tests/test_credential_pool_strategies_config.py
  git commit -m "feat(credential-pool): credential_pool_strategies config knob (per-provider rotation)"
  ```

---

## T7 — Error-code-specific cooldowns (402, 401-OAuth-refresh)

**Files:**
- Modify: `opencomputer/agent/credential_pool.py` — extend `report_auth_failure(ttl_seconds=None)` + extend `with_retry(classify_failure=None)` + add `EXHAUSTED_TTL_402_SECONDS` + add `OAuthRefresher` protocol
- Create: `tests/test_credential_pool_error_cooldowns.py`

### Task 7.1: Failing test

- [ ] **Step 7.1.1: Write `tests/test_credential_pool_error_cooldowns.py`**
  ```python
  """T7 — Error-code-specific cooldowns + OAuth refresh path."""

  from __future__ import annotations

  import time

  import pytest

  from opencomputer.agent.credential_pool import (
      EXHAUSTED_TTL_402_SECONDS,
      EXHAUSTED_TTL_429_SECONDS,
      ROTATE_COOLDOWN_SECONDS,
      CredentialPool,
  )


  @pytest.mark.asyncio
  async def test_429_uses_one_hour_cooldown():
      pool = CredentialPool(keys=["a", "b"])
      await pool.report_auth_failure("a", reason="429", ttl_seconds=EXHAUSTED_TTL_429_SECONDS)
      stats = pool.stats()
      a_state = next(k for k in stats["keys"] if k["key_preview"].endswith(":") or "0" in k["key_preview"])
      assert 3500 <= a_state["quarantine_remaining_s"] <= 3700


  @pytest.mark.asyncio
  async def test_402_uses_24h_cooldown():
      pool = CredentialPool(keys=["a", "b"])
      await pool.report_auth_failure("a", reason="402", ttl_seconds=EXHAUSTED_TTL_402_SECONDS)
      stats = pool.stats()
      a = stats["keys"][0]
      assert a["quarantine_remaining_s"] >= 86000


  @pytest.mark.asyncio
  async def test_default_cooldown_unchanged():
      pool = CredentialPool(keys=["a", "b"])
      await pool.report_auth_failure("a", reason="401")
      stats = pool.stats()
      a = stats["keys"][0]
      assert ROTATE_COOLDOWN_SECONDS - 5 <= a["quarantine_remaining_s"] <= ROTATE_COOLDOWN_SECONDS + 5


  @pytest.mark.asyncio
  async def test_with_retry_classify_failure_402_quarantines_24h():
      from httpx import HTTPStatusError, Response, Request

      pool = CredentialPool(keys=["a", "b"])
      req = Request("POST", "https://example.com")
      err_402 = HTTPStatusError(
          "billing", request=req, response=Response(402, request=req)
      )

      attempts = {"n": 0}

      async def op(key: str):
          attempts["n"] += 1
          if attempts["n"] == 1:
              raise err_402
          return "ok"

      def is_auth_failure(exc):
          return isinstance(exc, HTTPStatusError) and exc.response.status_code in (401, 402, 429)

      def classify(exc) -> float | None:
          if isinstance(exc, HTTPStatusError):
              code = exc.response.status_code
              if code == 402:
                  return EXHAUSTED_TTL_402_SECONDS
              if code == 429:
                  return EXHAUSTED_TTL_429_SECONDS
          return None

      result = await pool.with_retry(op, is_auth_failure=is_auth_failure, classify_failure=classify)
      assert result == "ok"
      assert attempts["n"] == 2
      stats = pool.stats()
      a = stats["keys"][0]
      assert a["quarantine_remaining_s"] >= 86000


  @pytest.mark.asyncio
  async def test_oauth_refresh_succeeds_no_quarantine():
      from httpx import HTTPStatusError, Request, Response

      pool = CredentialPool(keys=["expired_token"], oauth_refresher=lambda k: "fresh_token")

      attempts = {"n": 0}

      async def op(key: str):
          attempts["n"] += 1
          if attempts["n"] == 1:
              req = Request("POST", "https://example.com")
              raise HTTPStatusError(
                  "auth expired", request=req, response=Response(401, request=req)
              )
          return f"ok with {key}"

      def is_auth_failure(exc):
          return isinstance(exc, HTTPStatusError) and exc.response.status_code == 401

      result = await pool.with_retry(op, is_auth_failure=is_auth_failure)
      # After OAuth refresh, second attempt uses the refreshed token.
      assert "fresh_token" in result
      stats = pool.stats()
      # Original expired_token slot should now hold fresh_token, not be quarantined.
      assert stats["keys"][0]["quarantined"] is False


  @pytest.mark.asyncio
  async def test_backwards_compat_default_with_retry_unchanged():
      """A caller that doesn't pass classify_failure or oauth_refresher works as before."""
      pool = CredentialPool(keys=["a", "b"])

      async def op(key: str):
          return f"ok-{key}"

      result = await pool.with_retry(op, is_auth_failure=lambda e: False)
      assert result.startswith("ok-")
  ```

- [ ] **Step 7.1.2: Run test (expect failures on EXHAUSTED_TTL_402_SECONDS, oauth_refresher kwarg, classify_failure kwarg)**
  ```bash
  pytest tests/test_credential_pool_error_cooldowns.py -v 2>&1 | tail -15
  ```

### Task 7.2: Implement extensions

- [ ] **Step 7.2.1: Add 402 constant + protocol type**
  Modify `opencomputer/agent/credential_pool.py` near the existing `EXHAUSTED_TTL_429_SECONDS` constant:

  ```python
  EXHAUSTED_TTL_429_SECONDS: float = 3600.0
  EXHAUSTED_TTL_402_SECONDS: float = 86400.0  # T7 — 24h for billing/quota exhaustion
  ```

  And add to `__all__`:
  ```python
  __all__ = [
      ...,
      "EXHAUSTED_TTL_402_SECONDS",
      ...,
  ]
  ```

- [ ] **Step 7.2.2: Extend `report_auth_failure` to accept `ttl_seconds`**
  Locate the method at `opencomputer/agent/credential_pool.py:219`. Modify:

  ```python
  async def report_auth_failure(
      self,
      key: str,
      *,
      reason: str = "401",
      reset_at: float | None = None,
      ttl_seconds: float | None = None,  # T7 — explicit TTL override
  ) -> None:
      async with self._lock:
          now = time.time()
          for idx, s in enumerate(self._states):
              if s.key == key:
                  if reset_at is not None and reset_at > now:
                      s.quarantined_until = reset_at
                  elif ttl_seconds is not None and ttl_seconds > 0:
                      s.quarantined_until = now + ttl_seconds
                  else:
                      s.quarantined_until = now + self._cooldown
                  s.last_failure_reason = reason
                  logger.warning(
                      "credential_pool: quarantined key %s for %.0fs (reason: %s)",
                      _safe_id(key, idx),
                      s.quarantined_until - now,
                      reason,
                  )
                  self._write_state_file()
                  return
          logger.warning(
              "credential_pool: report_auth_failure for unknown key %s",
              _safe_id(key, pool_index=-1),
          )
  ```

- [ ] **Step 7.2.3: Extend `__init__` to accept `oauth_refresher`**
  Modify the `__init__` signature (locate at line ~106):

  ```python
  def __init__(
      self,
      keys: Sequence[str],
      *,
      rotate_cooldown_seconds: float = ROTATE_COOLDOWN_SECONDS,
      strategy: str = STRATEGY_LEAST_USED,
      max_rotation_attempts: int = 5,
      jwt_refresher: Callable[[str], Awaitable[str]] | None = None,
      oauth_refresher: Callable[[str], Awaitable[str] | str] | None = None,  # T7
      state_file: str | None = None,
  ) -> None:
      ...
      self._oauth_refresher = oauth_refresher
  ```

  (Match existing arg layout — read the file first.)

- [ ] **Step 7.2.4: Extend `with_retry` with `classify_failure` + OAuth refresh path**
  Modify the method at line ~248:

  ```python
  async def with_retry(
      self,
      fn,
      *,
      is_auth_failure,
      classify_failure: Callable[[Exception], float | None] | None = None,  # T7
  ):
      attempts = 0
      last_exc: Exception | None = None
      while attempts < self._max_rotation_attempts:
          key = await self.acquire()
          try:
              return await fn(key)
          except Exception as exc:
              if is_auth_failure(exc):
                  ttl = classify_failure(exc) if classify_failure else None
                  refreshed = await self._try_oauth_refresh(key, exc)
                  if refreshed is not None:
                      # Refresh succeeded — replace key in-place + retry without quarantining
                      await self._replace_key(key, refreshed)
                      attempts += 1
                      continue
                  await self.report_auth_failure(
                      key, reason=type(exc).__name__, ttl_seconds=ttl
                  )
                  last_exc = exc
                  attempts += 1
                  continue
              raise
      raise CredentialPoolExhausted(
          f"Exhausted {self._max_rotation_attempts} rotation attempts; "
          f"last failure: {last_exc!r}"
      ) from last_exc

  async def _try_oauth_refresh(self, key: str, exc: Exception) -> str | None:
      """If a refresher is configured AND this looks like a 401, attempt refresh.

      Returns the refreshed token on success, None otherwise.
      """
      if self._oauth_refresher is None:
          return None
      # Heuristic: only attempt on 401-ish errors. Caller's is_auth_failure
      # already gated us; if classify_failure returned a 24h TTL (402), don't refresh.
      try:
          import inspect
          if inspect.iscoroutinefunction(self._oauth_refresher):
              new_token = await self._oauth_refresher(key)
          else:
              new_token = self._oauth_refresher(key)
      except Exception:
          return None
      if not new_token or new_token == key:
          return None
      return new_token

  async def _replace_key(self, old_key: str, new_key: str) -> None:
      """Atomically replace ``old_key`` with ``new_key`` in the pool."""
      async with self._lock:
          for s in self._states:
              if s.key == old_key:
                  s.key = new_key
                  s.quarantined_until = 0.0
                  s.last_failure_reason = None
                  break
  ```

  Note: `_KeyState.key` was a frozen field originally; if the dataclass is `frozen`, change to mutable or use `object.__setattr__`. **Read the dataclass first to confirm.**

  ```bash
  grep -n "@dataclass\|class _KeyState" opencomputer/agent/credential_pool.py
  ```

  If `_KeyState` is `@dataclass` (not frozen), direct assignment works. If frozen, use `object.__setattr__(s, "key", new_key)`.

- [ ] **Step 7.2.5: Run T7 tests**
  ```bash
  pytest tests/test_credential_pool_error_cooldowns.py -v 2>&1 | tail -15
  ```
  Expected: 6 passed.

### Task 7.3: Commit T7

- [ ] **Step 7.3.1: Stage + commit**
  ```bash
  git add opencomputer/agent/credential_pool.py tests/test_credential_pool_error_cooldowns.py
  git commit -m "feat(credential-pool): error-code cooldowns (402→24h, 401→OAuth refresh) + classify_failure"
  ```

---

## T8 — `oc auth` CLI

**Files:**
- Create: `opencomputer/cli_auth.py`
- Modify: `opencomputer/cli.py` — one `app.add_typer` line
- Create: `tests/test_oc_auth_cli.py`

### Task 8.1: Failing test

- [ ] **Step 8.1.1: Write `tests/test_oc_auth_cli.py`**
  ```python
  """T8 — oc auth CLI subcommand group."""

  from __future__ import annotations

  import pytest
  import yaml
  from typer.testing import CliRunner

  from opencomputer.cli_auth import auth_app


  @pytest.fixture
  def runner():
      return CliRunner()


  def test_list_empty_pool(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(auth_app, ["list"])
      assert result.exit_code == 0
      assert "no credential" in result.stdout.lower() or "empty" in result.stdout.lower()


  def test_add_with_inline_key(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-or-v1-aaa"])
      assert result.exit_code == 0
      cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
      assert "sk-or-v1-aaa" in cfg["credential_pools"]["openrouter"]


  def test_add_with_key_env(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      monkeypatch.setenv("MY_OR_KEY", "sk-or-v1-bbb")
      result = runner.invoke(auth_app, ["add", "openrouter", "--key-env", "MY_OR_KEY"])
      assert result.exit_code == 0
      cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
      # key-env preserves the indirection by env-var name
      assert "${MY_OR_KEY}" in cfg["credential_pools"]["openrouter"] or "sk-or-v1-bbb" in cfg["credential_pools"]["openrouter"]


  def test_add_no_key_or_env_errors(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      result = runner.invoke(auth_app, ["add", "openrouter"])
      assert result.exit_code != 0


  def test_list_after_add_shows_masked(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-or-v1-aaa"])
      result = runner.invoke(auth_app, ["list"])
      assert result.exit_code == 0
      assert "openrouter" in result.stdout
      # Masked — full key should NOT appear in output
      assert "sk-or-v1-aaa" not in result.stdout


  def test_remove_by_index(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
      runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-2"])
      result = runner.invoke(auth_app, ["remove", "openrouter", "0"])
      assert result.exit_code == 0
      cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
      assert cfg["credential_pools"]["openrouter"] == ["sk-2"]


  def test_remove_invalid_index(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
      result = runner.invoke(auth_app, ["remove", "openrouter", "99"])
      assert result.exit_code != 0


  def test_reset_writes_force_reset_marker(runner, monkeypatch, tmp_path):
      monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
      runner.invoke(auth_app, ["add", "openrouter", "--key", "sk-1"])
      result = runner.invoke(auth_app, ["reset", "openrouter"])
      assert result.exit_code == 0
      cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
      assert "credential_pool_reset_at" in cfg
      assert "openrouter" in cfg["credential_pool_reset_at"]
  ```

- [ ] **Step 8.1.2: Run test (expect ImportError)**
  ```bash
  pytest tests/test_oc_auth_cli.py -v 2>&1 | tail -15
  ```

### Task 8.2: Implement `cli_auth.py`

- [ ] **Step 8.2.1: Create `opencomputer/cli_auth.py`**
  ```python
  """T8 — `oc auth` CLI subcommand group.

  Mirrors Hermes-doc canonical UX:
  - list   — show pool entries (table: provider, index, masked_key, status)
  - add    — append key to credential_pools[provider]
  - remove — remove by index
  - reset  — clear cooldowns by writing a force-reset marker
  """

  from __future__ import annotations

  import hashlib
  import os
  import time
  from pathlib import Path
  from typing import Any

  import typer
  import yaml
  from rich.console import Console
  from rich.table import Table

  console = Console()
  auth_app = typer.Typer(
      name="auth",
      help="Manage credential pools (Hermes-doc parity).",
      no_args_is_help=True,
  )


  def _profile_home() -> Path:
      override = os.environ.get("OPENCOMPUTER_HOME")
      if override:
          return Path(override)
      profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
      return Path.home() / ".opencomputer" / profile


  def _config_path() -> Path:
      return _profile_home() / "config.yaml"


  def _load_config() -> dict[str, Any]:
      path = _config_path()
      if not path.exists():
          return {}
      try:
          return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
      except yaml.YAMLError:
          console.print(f"[red]Could not parse {path}.[/red]")
          raise typer.Exit(code=1) from None


  def _save_config(data: dict[str, Any]) -> None:
      path = _config_path()
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(
          yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
          encoding="utf-8",
      )


  def _safe_id(key: str, idx: int) -> str:
      """Stable masked id (matches credential_pool._safe_id pattern)."""
      if not key:
          return f"[{idx}]:empty"
      digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
      return f"[{idx}]:{digest}"


  @auth_app.command("list")
  def list_keys(provider: str | None = typer.Argument(None, help="Filter by provider")) -> None:
      """List credential pool entries."""
      cfg = _load_config()
      pools = (cfg.get("credential_pools") or {}) if isinstance(cfg, dict) else {}
      if provider:
          pools = {provider: pools.get(provider, [])} if provider in pools else {}
      if not pools or not any(pools.values()):
          console.print("[dim]No credential pools configured (empty).[/dim]")
          return
      table = Table(title="Credential pools")
      table.add_column("Provider")
      table.add_column("Index")
      table.add_column("Masked key")
      for prov_name, keys in pools.items():
          for idx, k in enumerate(keys or []):
              # Resolve env-var indirection for masking
              if isinstance(k, str) and k.startswith("${") and k.endswith("}"):
                  display = k  # show env-var name, not the secret
              else:
                  display = _safe_id(str(k), idx)
              table.add_row(prov_name, str(idx), display)
      console.print(table)


  @auth_app.command("add")
  def add(
      provider: str = typer.Argument(..., help="Provider name (e.g. openrouter)"),
      key: str | None = typer.Option(None, "--key", help="Inline API key"),
      key_env: str | None = typer.Option(None, "--key-env", help="Env var name holding the key"),
  ) -> None:
      """Append a credential to credential_pools[provider]."""
      if not key and not key_env:
          console.print("[red]Either --key or --key-env is required.[/red]")
          raise typer.Exit(code=2)
      stored = key if key else f"${{{key_env}}}"
      cfg = _load_config()
      cfg.setdefault("credential_pools", {})
      cfg["credential_pools"].setdefault(provider, [])
      cfg["credential_pools"][provider].append(stored)
      _save_config(cfg)
      console.print(f"[green]Added[/green] credential to {provider} pool (index {len(cfg['credential_pools'][provider]) - 1}).")


  @auth_app.command("remove")
  def remove(
      provider: str = typer.Argument(..., help="Provider name"),
      index: int = typer.Argument(..., help="Pool index to remove (0-based)"),
  ) -> None:
      """Remove a credential by index."""
      cfg = _load_config()
      keys = (cfg.get("credential_pools") or {}).get(provider, [])
      if not 0 <= index < len(keys):
          console.print(f"[red]Index {index} out of range for {provider} (size {len(keys)}).[/red]")
          raise typer.Exit(code=2)
      removed = keys.pop(index)
      cfg["credential_pools"][provider] = keys
      _save_config(cfg)
      masked = removed if (isinstance(removed, str) and removed.startswith("${")) else _safe_id(str(removed), index)
      console.print(f"[yellow]Removed[/yellow] {masked} from {provider}.")


  @auth_app.command("reset")
  def reset(provider: str = typer.Argument(..., help="Provider name")) -> None:
      """Clear all cooldowns for *provider* (writes a force-reset marker the running pool reads)."""
      cfg = _load_config()
      cfg.setdefault("credential_pool_reset_at", {})
      cfg["credential_pool_reset_at"][provider] = time.time()
      _save_config(cfg)
      console.print(f"[green]Reset[/green] cooldowns for {provider}. Running processes pick up on next refresh.")
  ```

- [ ] **Step 8.2.2: Wire into `cli.py`**
  Add below the `app.add_typer(memory_app, name="memory")` line:
  ```python
  from opencomputer.cli_auth import auth_app  # noqa: E402

  app.add_typer(auth_app, name="auth")
  ```

- [ ] **Step 8.2.3: Run T8 tests**
  ```bash
  pytest tests/test_oc_auth_cli.py -v 2>&1 | tail -15
  ```
  Expected: 8 passed.

### Task 8.3: Commit T8

- [ ] **Step 8.3.1: Stage + commit**
  ```bash
  git add opencomputer/cli_auth.py opencomputer/cli.py tests/test_oc_auth_cli.py
  git commit -m "feat(cli): oc auth list/add/remove/reset (Hermes-doc parity)"
  ```

---

## Validation

### Task 9: Full suite + ruff

- [ ] **Step 9.1: Full pytest run (excluding the known-flake)**
  ```bash
  pytest tests/ -q --ignore=tests/test_phase12b1_honcho_default.py 2>&1 | tail -10
  ```
  Expected: all green except possibly the known Honcho-default test pollution flake. Compare count to pre-flight baseline.

- [ ] **Step 9.2: Ruff check**
  ```bash
  ruff check opencomputer/ extensions/api-server/ extensions/memory-honcho/ plugin_sdk/ tests/ 2>&1 | tail -5
  ```
  Expected: clean.

- [ ] **Step 9.3: Smoke — capabilities endpoint**
  ```bash
  python -c "
  import asyncio
  from aiohttp.test_utils import TestServer, TestClient
  from extensions.api_server.adapter import APIServerAdapter
  async def main():
      adapter = APIServerAdapter()
      adapter._token = ''
      app = adapter._build_app()
      async with TestClient(TestServer(app)) as client:
          r = await client.get('/v1/capabilities')
          print(r.status, await r.json())
  asyncio.run(main())
  "
  ```
  Expected: `200 {'version': '1', ...}`.

- [ ] **Step 9.4: Smoke — `oc auth add`/`list`/`remove` round-trip**
  ```bash
  TMPHOME=$(mktemp -d)
  OPENCOMPUTER_HOME=$TMPHOME python -m opencomputer.cli auth add openrouter --key sk-test-aaa
  OPENCOMPUTER_HOME=$TMPHOME python -m opencomputer.cli auth list
  OPENCOMPUTER_HOME=$TMPHOME python -m opencomputer.cli auth remove openrouter 0
  rm -rf $TMPHOME
  ```

- [ ] **Step 9.5: Smoke — `oc honcho status` exits 0 without Honcho server**
  ```bash
  TMPHOME=$(mktemp -d)
  OPENCOMPUTER_HOME=$TMPHOME python -m opencomputer.cli honcho status
  echo "exit: $?"
  rm -rf $TMPHOME
  ```
  Expected: exit 0.

### Task 10: Push branch + open PR

- [ ] **Step 10.1: Push**
  ```bash
  git push -u origin feat/hermes-doc-tier-s-2026-05-08
  ```

- [ ] **Step 10.2: Open PR via gh**
  ```bash
  gh pr create \
    --title "feat: Hermes-doc Tier-S residue (MCP utilities + API polish + Honcho/cred CLI)" \
    --body "Closes the Tier-S Hermes-doc residue not covered by wave3 / cli-tui-v2 / gateway-cron-delegation worktrees: 8 features in 1 PR.

  - **T1** MCP utility tools — \`mcp_<server>_list_resources/read_resource/list_prompts/get_prompt\` (capability-gated)
  - **T2** API server \`/v1/capabilities\`
  - **T3** API server \`/health/detailed\`
  - **T4** Honcho query-adaptive reasoning level (length-scaled)
  - **T5** \`oc honcho\` CLI: status / sync / enable / disable / strategy
  - **T6** \`credential_pool_strategies\` config knob wiring
  - **T7** Error-code-specific cooldowns in credential pool: 402 → 24h, 401 → OAuth refresh first
  - **T8** \`oc auth\` CLI: list / add / remove / reset

  Spec: \`docs/superpowers/specs/2026-05-08-hermes-doc-tier-s-design.md\`
  Plan: \`docs/superpowers/plans/2026-05-08-hermes-doc-tier-s.md\`

  ~750 LOC + ~50 tests. Honest deferrals (MCP sampling, Runs/Jobs API, ACP toolset, Honcho dialecticDepth multi-pass, etc.) documented in spec §2.3 with reopen triggers.

  🤖 Generated with [Claude Code](https://claude.com/claude-code)"
  ```

### Task 11: Cleanup post-merge

- [ ] **Step 11.1: After PR merges**
  ```bash
  git -C /Users/saksham/Vscode/claude worktree remove .claude/worktrees/hermes-tier-s-2026-05-08
  git -C /Users/saksham/Vscode/claude branch -D feat/hermes-doc-tier-s-2026-05-08
  ```

---

## Self-review

- **Spec coverage:** all 8 features (T1–T8) → at least one task each. Validation in Task 9. ✓
- **Placeholder scan:** no TBD/TODO. Each step has concrete code or commands. The "locate via grep" steps are real research blockers, not placeholders. ✓
- **Type consistency:**
  - `_register_utility_tools(server_name, session, capabilities, registry)` — same signature in spec §3.1 and Task 1.3.2 ✓
  - `_adapt_reasoning_level(base, query, cap)` — same signature in spec §3.4 and Task 4.2.2 ✓
  - `EXHAUSTED_TTL_402_SECONDS`, `EXHAUSTED_TTL_429_SECONDS` constants ✓
  - `report_auth_failure(key, *, reason, reset_at, ttl_seconds)` extends existing ✓
  - `with_retry(fn, *, is_auth_failure, classify_failure)` extends existing ✓
- **Backwards compat:** `with_retry` adds `classify_failure=None`; existing callers untouched. `report_auth_failure` adds `ttl_seconds=None`; existing callers untouched. `Config.credential_pool_strategies` adds dict default-empty; old YAMLs parse. ✓
- **Test coverage:** every implementation step is preceded by a failing test. ✓
- **Open execution-time research:**
  - MCP `init_result.capabilities` shape — verify by reading `mcp.types.InitializeResult` (used by `mcp` SDK)
  - Honcho `peer.chat()` call site — may not exist yet; T4 ships the field + helper, wiring may be design-only
  - `_KeyState.key` mutation — verify dataclass mutability before writing `_replace_key`
- **Risk:** T7's `_replace_key` requires `_KeyState` to allow mutation. If frozen, use `object.__setattr__`. Documented in Task 7.2.4.
