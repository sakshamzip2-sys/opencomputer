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
    bedrock_adapter.py          ← CREATE (~900 LOC)
    codex_responses_adapter.py  ← CREATE (~700 LOC)
    copilot_acp_client.py       ← CREATE (~600 LOC)
    model_resolver.py           ← MODIFY (register new provider slugs)
  agent/
    loop.py                     ← MODIFY (add tool_callback param to run() + _dispatch_tool_calls)
  acp/
    events.py                   ← CREATE (~190 LOC)
    permissions.py              ← CREATE (~80 LOC)
    auth.py                     ← CREATE (~30 LOC)
    tools.py                    ← MODIFY (16 → ~200 LOC)
    server.py                   ← MODIFY (add _send_notification + auth middleware + permission hooks)
    session.py                  ← MODIFY (add event_queue + emit_event)
  mcp/
    server.py                   ← MODIFY (add permissions_list_open + permissions_respond + attachments_fetch)

tests/
  test_credential_pool.py       ← MODIFY (extend)
  test_credential_sources.py    ← CREATE
  test_bedrock_adapter.py       ← CREATE
  test_codex_responses_adapter.py ← CREATE
  test_copilot_acp_client.py    ← CREATE
  test_mcp_serve.py             ← CREATE
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

### PR 2 — Bedrock + Codex Adapters

Both expose an OpenAI-compatible `.chat.completions.create()` facade consumed identically by `loop.py`.

**Bedrock adapter (`bedrock_adapter.py`):**
- Uses `boto3` Converse API (already in `pyproject.toml` as `bedrock` optional dep)
- Strips `hermes_constants` + `hermes_cli.auth` — replaces with OC `config_store` for region/profile
- Format conversion: OpenAI `messages[]` + `tools[]` → Bedrock `contentBlocks[]` + `toolConfig`
- Response normalization: Bedrock `output.message` → OpenAI `choices[0].message`
- Streaming: `converseStream` → yield OpenAI-shaped delta chunks
- Dynamic model discovery via `list_foundation_models()` control plane call
- Provider slug: `bedrock/<model-id>` (e.g. `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0`)

**Codex adapter (`codex_responses_adapter.py`):**
- Pure format-conversion; zero external deps
- OpenAI Chat `messages[]` → Responses API `input[]` with `input_text` / `input_image` parts
- Tool calls: `tool_calls[]` → Responses API `function_call` items
- Inlines `DEFAULT_AGENT_IDENTITY` constant (removes `agent.prompt_builder` import)
- Provider slug: `codex/<model-id>` (e.g. `codex/codex-mini-latest`)

**`model_resolver.py`:** Register `bedrock/` and `codex/` prefixes → return appropriate adapter instance.

**Tests:** Format-round-trip unit tests (no live AWS/OpenAI calls). Mock `boto3.client` for Bedrock tests.

**Deps:** `boto3` (already optional). No new deps for Codex.

---

### PR 3 — Copilot ACP Adapter

**What it does:** Spawns `copilot --acp --stdio` subprocess, speaks JSON-RPC to it, presents OpenAI-compatible facade to OC's agent loop.

**Hermes-internal imports inlined (~15 LOC each):**
- `agent.file_safety.get_read_block_error` / `is_write_denied` → path-check helpers checking `~/.opencomputer/blocked_paths.txt` (or `OC_BLOCKED_PATHS` env var)
- `agent.redact.redact_sensitive_text` → regex scrubber stripping `Bearer <token>` / `sk-...` patterns from logged strings

**Config:**
- `OC_COPILOT_ACP_COMMAND` env var (was `HERMES_COPILOT_ACP_COMMAND`)
- `OC_COPILOT_ACP_ARGS` env var (was `HERMES_COPILOT_ACP_ARGS`)
- Defaults: `copilot --acp --stdio` (requires `copilot` CLI in `$PATH`)

**Provider slug:** `acp://copilot` registered in `model_resolver.py`.

**Tests:** Mock subprocess with `unittest.mock.patch`. Test JSON-RPC encode/decode, tool call round-trip, timeout handling.

**Deps:** No new deps. `subprocess` + `threading` from stdlib.

---

### PR 4 — MCP Serve (complete the existing server)

**Context:** `opencomputer/mcp/server.py` already exists (470 LOC) with 10 tools: `sessions_list`, `session_get`, `messages_read`, `recall_search`, `consent_history`, `channels_list`, `events_poll`, `messages_send`, `messages_send_status`, `events_wait`. The existing file honestly defers `permissions_respond` (needs F1 write-back path). PR 4 closes that gap and adds `attachments_fetch`.

**Changes to `opencomputer/mcp/server.py`:**
- **`permissions_list_open(limit=50)`** — query `consent/store.py` for pending approval requests; returns list of `{id, capability, description, requested_at}`
- **`permissions_respond(request_id, outcome)`** — write consent decision (`once | always | deny`) back via `consent/gate.py`; validates `outcome` enum before writing
- **`attachments_fetch(session_id, message_id)`** — reads the file path stored in a message's attachment field; returns base64-encoded content with MIME type

**No CLI changes needed** — `opencomputer mcp serve` already works via `cli_mcp.py:354`.

**Tests:** Add 3 new test functions to `tests/test_mcp_serve.py`; mock `consent/store.py` and `consent/gate.py`.

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
