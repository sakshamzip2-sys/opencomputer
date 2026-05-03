# Hermes Best-of Import — Design Spec

**Date:** 2026-05-03  
**Scope:** 5 serial PRs porting the highest-value Hermes-agent features into OpenComputer  
**Out of scope (deferred):** RL environments, TUI gateway, Gemini CloudCode adapter (OAuth chain too complex)

---

## 1. Context

OpenComputer (OC) and Hermes share a common ancestry. A gap analysis of the 2026-04-23 Hermes snapshot identified 10 items OC lacks. After scoping:

- RL envs / SWE-bench / `rl_cli` — **skip** (require Atropos/Modal/WandB, narrow audience)
- TUI gateway / Ink React frontend — **skip** (no TypeScript frontend to connect)
- Gemini CloudCode adapter — **skip for now** (1,586 LOC helper chain for undocumented proprietary endpoint)
- Docusaurus website / release templates — **out of scope** (content, not code)

**In scope (Approach B):**

| # | Item | Hermes LOC | New OC LOC |
|---|---|---|---|
| PR 1 | Credential pool uplift + `credential_sources.py` | 1,749 | ~1,000 |
| PR 2 | Bedrock adapter + Codex responses adapter | 1,911 | ~1,600 |
| PR 3 | Copilot ACP adapter | 604 | ~650 |
| PR 4 | MCP server: `permissions_list_open` + `permissions_respond` + `attachments_fetch` | 867 | ~200 |
| PR 5 | ACP depth: events + permissions + auth + tools/session uplift | 2,283 | ~1,900 |

---

## 2. Architecture

### 2.1 File Layout

All agent-layer modules land in `opencomputer/agent/` (OC's existing flat layout). ACP additions go into `opencomputer/acp/`. MCP serve completes `opencomputer/mcp/server.py` (already wired to `opencomputer mcp serve` CLI) by adding 3 missing tools.

```
opencomputer/
  agent/
    credential_pool.py          ← MODIFY (144 → ~600 LOC)
    credential_sources.py       ← CREATE (~400 LOC)
    loop.py                     ← MODIFY (add tool_callback param to run() + _dispatch_tool_calls)
  acp/
    events.py                   ← CREATE (~190 LOC)
    permissions.py              ← CREATE (~80 LOC)
    auth.py                     ← CREATE (~30 LOC)
    tools.py                    ← MODIFY (16 → ~200 LOC)
    server.py                   ← MODIFY (add _send_notification + auth middleware + permission hooks)
    session.py                  ← MODIFY (add event_queue + emit_event)
  mcp/
    server.py                   ← MODIFY (add attachments_fetch; permissions_respond stays deferred)

extensions/
  bedrock-provider/
    plugin.py                   ← CREATE (register(api) → api.register_provider("bedrock", BedrockProvider))
    bedrock_adapter.py          ← CREATE (format-conversion, ~900 LOC)
  codex-provider/
    plugin.py                   ← CREATE (register(api) → api.register_provider("codex", CodexProvider))
    codex_responses_adapter.py  ← CREATE (~700 LOC)
  copilot-acp-provider/
    plugin.py                   ← CREATE (register(api) → api.register_provider("copilot-acp", CopilotACPProvider))
    copilot_acp_client.py       ← CREATE (~600 LOC)

tests/
  test_credential_pool.py       ← MODIFY (extend)
  test_credential_sources.py    ← CREATE
  test_bedrock_provider.py      ← CREATE (mocks boto3, tests format round-trip)
  test_codex_provider.py        ← CREATE (tests format conversion)
  test_copilot_acp_provider.py  ← CREATE (mocks subprocess)
  test_mcp_serve.py             ← MODIFY (add attachments_fetch test)
  test_acp_events.py            ← CREATE
  test_acp_permissions.py       ← CREATE
```

---

## 3. PR-by-PR Detail

### PR 1 — Credential Pool Uplift

**Problem:** OC's `credential_pool.py` (144 LOC) supports only `least_used` rotation and flat 60s quarantine. Hermes's version (1,349 LOC) adds multi-strategy rotation, OAuth/JWT refresh, and `reset_at` from 429 headers.

**Changes:**

- **`credential_pool.py`** — Keep existing `CredentialPool` public interface (`acquire()`, `with_retry()`) intact. Add:
  - `strategy` param: `fill_first | round_robin | random | least_used` (default: `least_used`, no regression)
  - `reset_at` support: parse `Retry-After` / `x-ratelimit-reset` headers from 429 responses; use that timestamp as quarantine expiry instead of flat cooldown
  - JWT-aware quarantine: if a credential is a short-lived JWT (detected by decoding header without verification), auto-refresh before expiry via a pluggable `refresher` callback instead of quarantining
  - Constants: `EXHAUSTED_TTL_429_SECONDS = 3600`, `STRATEGY_*` literals

- **`credential_sources.py`** (new) — Loads credential lists from:
  1. Numbered env vars: `OPENAI_API_KEY_1`, `OPENAI_API_KEY_2`, … (stops at first gap)
  2. Config YAML `credential_pools:` block
  3. OS keyring (via `keyring` package, already in OC deps)
  - Returns a `list[str]` for `CredentialPool(keys=...)` — callers never construct pools manually

**Tests:** Single-key pool behaves identically to no-pool (regression). Each strategy covered. `reset_at` correctly overrides flat cooldown. JWT detection stub (no real JWT decode needed for tests).

**Deps:** No new deps. `keyring` already in OC.

---

### PR 2 — Bedrock + Codex Provider Plugins

Both are **new plugins in `extensions/`**, following the exact pattern of `extensions/anthropic-provider/` and `extensions/openai-provider/`. Each plugin has a `plugin.py` with `register(api)` and an adapter module. Plugins may only import from `plugin_sdk/` (enforced by `tests/test_phase6a.py`) — the format-conversion logic is self-contained inside each plugin directory.

**`extensions/bedrock-provider/`:**
- `bedrock_adapter.py` — format conversion module (~900 LOC):
  - OC `Message[]` + `Tool[]` → Bedrock Converse API `contentBlocks[]` + `toolConfig` and back
  - Lazy `boto3` import (`pip install opencomputer[bedrock]`)
  - Strips all `hermes_constants` + `hermes_cli.auth` refs; reads region/profile from env vars directly
  - Streaming: `converseStream` event-stream → yield `StreamEvent` objects
- `plugin.py` — `BedrockProvider(BaseProvider)`:
  - Implements `complete()` + `stream_complete()` delegating to adapter
  - Reads `AWS_PROFILE` / `AWS_REGION` / `AWS_ACCESS_KEY_ID` from env
  - `register(api)`: `api.register_provider("bedrock", BedrockProvider)`

**`extensions/codex-provider/`:**
- `codex_responses_adapter.py` — pure format conversion (~700 LOC):
  - OC `Message[]` + `Tool[]` → OpenAI Responses API `input[]` items and back
  - Inlines `DEFAULT_AGENT_IDENTITY` string constant; zero non-stdlib deps
  - Streaming via `httpx` AsyncClient (already in OC deps)
- `plugin.py` — `CodexProvider(BaseProvider)`:
  - Implements `complete()` + `stream_complete()`
  - Reads `OPENAI_API_KEY` from env
  - `register(api)`: `api.register_provider("codex", CodexProvider)`

**Tests:** `tests/test_bedrock_provider.py` + `tests/test_codex_provider.py` — format round-trip unit tests (no live API calls). Mock `boto3.client` for Bedrock. No `model_resolver.py` changes needed.

**Deps:** `boto3` (already optional dep in `pyproject.toml`). `httpx` (already core). No new deps.

---

### PR 3 — Copilot ACP Provider Plugin

New plugin in `extensions/copilot-acp-provider/`. Spawns `copilot --acp --stdio` subprocess, speaks JSON-RPC to it, wraps as a `BaseProvider`.

**`extensions/copilot-acp-provider/`:**
- `copilot_acp_client.py` (~600 LOC):
  - Subprocess management: spawn `copilot --acp --stdio`, read/write JSON-RPC over stdio
  - Drops `agent.file_safety` import (Hermes-internal); replaces with a simple `OC_BLOCKED_PATHS` env-var check (~10 LOC inline)
  - Drops `agent.redact` import; replaces with `re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', ...)` inline
  - Renames env vars: `OC_COPILOT_ACP_COMMAND` (was `HERMES_COPILOT_ACP_COMMAND`), `OC_COPILOT_ACP_ARGS`
  - Defaults: `copilot --acp --stdio` (requires `copilot` CLI in `$PATH`)
- `plugin.py` — `CopilotACPProvider(BaseProvider)`:
  - Wraps `copilot_acp_client.py` as `complete()` + `stream_complete()`
  - `register(api)`: `api.register_provider("copilot-acp", CopilotACPProvider)`

**Tests:** `tests/test_copilot_acp_provider.py` — mock subprocess. Test JSON-RPC encode/decode, tool call round-trip, timeout.

**Deps:** No new deps. `subprocess` + `threading` from stdlib.

---

### PR 4 — MCP Server: `attachments_fetch`

**Context:** `opencomputer/mcp/server.py` already exists (470 LOC) with 10 tools already shipping. The file honestly defers `permissions_respond` because pending consent requests are in-memory in the live agent process and not accessible from the MCP subprocess. That deferral stays.

**Single addition to `opencomputer/mcp/server.py`:**
- **`attachments_fetch(session_id, message_id)`** — reads the `attachments` JSON column (added 2026-04-27) from the `messages` table; deserializes as `list[str]` of file paths; returns base64-encoded content + MIME type for each path. Returns `[]` if message has no attachments or paths no longer exist.

**No CLI changes needed** — `opencomputer mcp serve` already works via `cli_mcp.py:354`.

**Tests:** Add 1 new test to `tests/test_mcp_serve.py`; uses an in-memory SQLite DB.

**Deps:** No new deps.

---

### PR 5 — ACP Depth

**Critical adaptation:** Hermes's `acp_adapter/` uses an external `acp` Python library (`import acp`). OC's `acp/` is entirely self-contained JSON-RPC. All ports must adapt to OC's protocol, not import the external library.

**`events.py` (new):**
- Factories returning callbacks wired to OC's agent loop via a new `tool_callback` parameter (see `loop.py` change below)
- Each callback pushes a JSON-RPC notification to the ACP client via a new `ACPServer._send_notification(session_id, method, params)` method (added to `server.py`)
- Uses `asyncio.run_coroutine_threadsafe()` since agent loop runs in worker thread
- Notification methods: `session/toolStart`, `session/toolComplete`, `session/toolError`, `session/contentDelta`, `session/thinkingDelta`

**`loop.py` change (required for events.py):**
- Add `tool_callback: Callable[[str, str, Any, Any], None] | None = None` parameter to `AgentLoop.run()` and `_dispatch_tool_calls()`
- Fire `tool_callback("start", tool_name, tool_id, args)` before tool execution; `tool_callback("complete", tool_name, tool_id, result)` after
- Existing callers pass `None` (default) — no regression possible

**`permissions.py` (new):**
- `make_approval_callback(session_id, server)` → returns `approval_callback(command, description) -> str`
- Bridges ACP `request_permission` JSON-RPC method to OC's `consent/gate.py` `request_approval()`
- Timeout 60s: auto-deny on timeout (same as Hermes)
- Maps ACP `allow_once / allow_always / reject_once / reject_always` → OC consent `once / always / deny`

**`auth.py` (new):**
- `detect_provider() -> Optional[str]`: reads active provider from OC's `config_store.load_config()`
- `has_provider() -> bool`: convenience wrapper
- Surfaced in `initialize` response as `serverCapabilities.provider`

**`tools.py` uplift (16 → ~200 LOC):**
- Add full tool event schema builders: `build_tool_start(tool_name, tool_id, args)`, `build_tool_complete(tool_id, result)`, `build_tool_error(tool_id, error)`
- Add `make_tool_call_id()` UUID generator
- Keep existing 3 stubs, extend rather than replace

**`server.py` uplift:**
- Wire `auth.py` into `initialize` handler response
- Add `request_permission` method handler, delegating to `permissions.py`
- Inject event callbacks from `events.py` into `ACPSession` on `newSession`

**`session.py` uplift:**
- Add `event_queue: asyncio.Queue` for buffered event delivery
- Add `emit_event(method, params)` method consumed by `events.py` callbacks

**Tests:** `test_acp_events.py` — mock loop callbacks, assert notification JSON shape. `test_acp_permissions.py` — mock consent gate, assert `allow_once` maps correctly.

---

## 4. Sequencing & Dependencies

```
PR 1 (credential pool)   ──► independent, merge first
PR 2 (Bedrock + Codex)   ──► independent of PR 1, can merge in parallel
PR 3 (Copilot ACP)       ──► independent, can merge with PR 2
PR 4 (mcp serve)         ──► soft dep on PR 5 (events_poll stubs gracefully without it)
PR 5 (ACP depth)         ──► independent of PRs 1-3; merge before or after PR 4
```

Recommended merge order: **1 → 2 → 3 → 5 → 4** (ACP depth before MCP serve so `events_poll` is real, not a stub).

---

## 5. Testing Strategy

- All PRs must pass `pytest -x` + `ruff check` locally before push
- No admin-merge bypasses (per `feedback_no_push_without_deep_testing.md`)
- Each PR ships its own tests; full suite run before every push
- Live provider calls (AWS Bedrock, `copilot` CLI) mocked in unit tests; integration tests gated by `pytest.mark.integration` + env var presence
- Credential pool: single-key regression test must remain green throughout

---

## 6. Out of Scope (this round)

- Gemini CloudCode adapter + `google_oauth` / `gemini_schema` / `google_code_assist` helpers
- RL environments (Atropos/SWE-bench/`rl_cli`)
- TUI gateway (JSON-RPC bridge for TypeScript Ink frontend)
- Docusaurus documentation site
- Release template formatting
