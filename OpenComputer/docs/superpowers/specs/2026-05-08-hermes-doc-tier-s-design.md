# Hermes Doc Tier-S — MCP Utilities + API Polish + Honcho/Cred CLI

**Date:** 2026-05-08
**Status:** Spec — implementation scope: 8 features in 1 PR (~750 LOC + ~50 tests)
**Source:** Two Hermes reference docs the user pasted today:
1. *Hermes — MCP, API Server & ACP Editor Integration*
2. *Hermes — Honcho, Provider Routing, Fallback & Credential Pools*
**Companion specs (in flight, separate worktrees — DO NOT touch):**
- `2026-05-08-hermes-wave3-provider-config-design.md` (worktree: `claude-wave3` — covers OR routing, fallback_providers, MCP tool filter, mlx-whisper, custom_providers, oc model wizard, etc.)
- `2026-05-08-hermes-cli-tui-sessions-v2-parity-design.md` (worktree: `.claude/worktrees/hermes-cli-tui-v2-2026-05-08`)
- `2026-05-08-hermes-gateway-cron-delegation-parity-design.md` (untracked spec)

---

## 1. Problem statement

The user pasted two Hermes reference docs and asked: *"do them both now."* Wave 3, CLI-TUI-V2, and gateway-cron-delegation are running in parallel worktrees and cover most of the headline features. This spec ships the **Tier-S residue** — items that the user's pasted docs explicitly call out, that no other in-flight worktree owns, and that pass a brutal-pass cost/benefit gate.

Per the user's verbatim filter: *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, that doesn't mean we should fill it just because we're missing it."*

**Out of scope (covered elsewhere or deferred with reopen triggers — see §6):** OR routing, fallback_providers, MCP per-server tool filter, custom_providers, mlx-whisper, /v1/runs Runs API, /api/jobs Jobs API, /v1/responses chaining, ACP toolset/approvals, MCP sampling, dynamic MCP `tools/list_changed`, Honcho dialecticDepth multi-pass, Honcho observation modes.

---

## 2. Gap analysis (verified, file:line-cited)

Verification was done against `/Users/saksham/Vscode/claude/OpenComputer/` via direct grep + file reads.

### 2.1 Already shipped — re-confirmation

| Item | Status | Evidence |
|---|---|---|
| MCP `/reload-mcp` slash command | SHIPPED | `cli_ui/slash_handlers.py:751-766` |
| `oc mcp serve` exposing 9 of 10 Hermes-spec tools | PARTIAL-SHIPPED | `mcp/server.py:76-180` (deferred: `permissions_respond`, needs F1 write-back path) |
| Credential pool 4 rotation strategies | SHIPPED | `agent/credential_pool.py:49-60` (`STRATEGY_FILL_FIRST/ROUND_ROBIN/RANDOM/LEAST_USED`) |
| Honcho `honcho_profile/search/context/reasoning/conclude` (5 tools) | SHIPPED | `extensions/memory-honcho/provider.py:132-209` |
| Honcho two-layer cadence (`context_cadence` + `dialectic_cadence`) | SHIPPED | `extensions/memory-honcho/provider.py:45-46` |
| `oc memory setup/status/reset` for Honcho | SHIPPED | `cli_memory.py:282-402` |

### 2.2 Verified missing — ship in this PR (Tier-S)

| ID | Feature | Effort | Reason to ship |
|---|---|---|---|
| **T1** | MCP utility tools — `mcp_<server>_list_resources`, `read_resource`, `list_prompts`, `get_prompt` (capability-aware: only registered if the server's `initialize` reply advertises that capability) | ~150 LOC + 8 tests | Hermes-doc canonical contract. Today users have to read MCP server source to know what resources exist. Free closure of the "MCP servers as resources/prompts not just tools" gap. |
| **T2** | API server `/v1/capabilities` endpoint — machine-readable feature flag dict | ~30 LOC + 3 tests | Hermes doc lists this as required for integrators. ~1 hour to ship; many frontends probe it before negotiating features. |
| **T3** | API server `/health/detailed` — active sessions, running agents, resource usage | ~50 LOC + 3 tests | Hermes doc requirement. Users running OC as a service need a deeper health probe than `{"status": "ok"}`. |
| **T4** | Honcho query-adaptive reasoning — auto-scale `dialectic_reasoning_level` by user-message length: ≥120 chars → +1 step, ≥400 chars → +2 steps, capped at `reasoning_level_cap` | ~80 LOC + 4 tests | Direct Hermes-doc port (the "Three Cost/Depth Knobs" section). Cheap quality lift: short queries pay nothing extra, long queries get deeper synthesis. |
| **T5** | `oc honcho` CLI group: `status / sync / enable / disable / strategy` (5 subcommands) | ~180 LOC + 8 tests | Hermes-doc lists this as the canonical Honcho UX. `oc memory setup/status/reset` partially overlaps but doesn't expose `enable/disable/strategy/sync`. |
| **T6** | `credential_pool_strategies` config.yaml key wired to pool init (per-provider strategy override) | ~40 LOC + 3 tests | Strategies are coded but not exposed via config — only the default `STRATEGY_LEAST_USED` is reachable today. |
| **T7** | Error-code-specific cooldowns in credential pool: 402 → 24h cooldown + immediate rotate; 401 → try OAuth refresh first, then rotate (existing 429 → 1h preserved) | ~80 LOC + 6 tests | Today every error gets the same `ROTATE_COOLDOWN_SECONDS=60s`. 402 (quota exhausted) deserves a longer hold; 401 (auth expired) deserves an OAuth-refresh attempt before quarantine. |
| **T8** | `oc auth` CLI group: `list / add / remove / reset` (4 subcommands) | ~140 LOC + 8 tests | Hermes-doc canonical UX. Users today must hand-edit YAML to manage credential pools. |

**Total:** 8 features, ~750 LOC, ~43 tests.

### 2.3 Verified missing — explicitly NOT shipping (deferred with reopen triggers)

| Item | Cost | Why not ship | Reopen trigger |
|---|---|---|---|
| `permissions_respond` MCP tool (10th of 10) | ~80 LOC + design | Needs F1 pending-consent queue write-back path; bypassing the gateway's grant flow is a security risk | F1 surface lands a write-back API |
| MCP sampling (server → LLM bridge) | ~250 LOC + design | Cross-process LLM bridge is non-trivial; no demand signal | A user installs an MCP server that needs sampling |
| Dynamic MCP `tools/list_changed` notification handler | ~80 LOC | `/reload-mcp` slash works for the same need; users don't seem blocked | A user reports a tool-list-out-of-sync incident |
| `/v1/runs` Runs API (POST/GET/events SSE/stop create+list) | ~250 LOC | Existing `/v1/chat/completions` streaming covers the same surface; SSE event semantics are non-trivial | A frontend integrator asks for it specifically |
| `/api/jobs` Jobs API for cron management | ~300 LOC | OC has `oc cron` CLI; remote API is convenience | A user needs remote cron management from a webapp |
| `/v1/responses` `previous_response_id` + named-conversation chaining | ~120 LOC | Stub exists; nobody has reported the missing chaining as a blocker | A user explicitly hits the chaining gap |
| `event: hermes.tool.progress` SSE in `/v1/chat/completions` | ~80 LOC | Cosmetic — frontends without it still work | A user-facing frontend depends on it |
| ACP toolset registration as ACP tools (read_file/write_file/patch/etc.) | ~250 LOC | Existing tool callbacks work via `ACPSession.tool_callback`; standalone ACP tool registry is a refactor | A user reports tools not visible in their editor |
| ACP approval flow (allow once / always / deny + timeout) | ~200 LOC | `setSessionPermissions` covers the static case; approval prompts depend on editor UI capabilities | A user explicitly hits the approval UX |
| `oc acp` CLI launcher + `acp_registry/agent.json` | ~80 LOC | ACP server starts implicitly via IDE; explicit launcher rarely needed | A user wants to launch ACP from terminal |
| Honcho `dialecticDepth` (1-3 multi-pass: query → self-audit → reconciliation) | ~250 LOC | Big design lift; current `dialectic_reasoning_level` knob (T4) covers the 80% case | A user reports shallow Honcho synthesis |
| Honcho observation modes (directional vs unified) and per-peer observeMe/observeOthers | ~150 LOC | Re-architects two-way observation; current one-way feed works | A user wants AI-to-AI observation |
| Honcho `oc honcho peer / mode / tokens / identity` extra subcommands | ~150 LOC | Lower-priority subcommands; YAML edit covers them | A user asks for one specifically |
| Auxiliary task fallback chain (`auxiliary.<task>.provider: "auto"` resolution) | ~150 LOC | Existing `aux_llm.py` works for the configured provider; auto-chain is convenience | A user reports a vision/web_extract failure that the auto-chain would catch |
| auth.json + `~/.claude/.credentials.json` auto-discovery | ~80 LOC | Adds a new file format + writer; `oc auth add` (T8) writes config.yaml — same outcome | A user has many existing OAuth tokens to import |
| Subagent credential pool inheritance + per-task leasing | ~80 LOC | Subagents share parent's process state by default; explicit pool object passing is a refactor | A user reports concurrent-subagent key conflict |

### 2.4 Why these eight pass the brutal-pass

Each row in §2.2 has:
- A direct verbatim quote from the Hermes reference docs the user pasted today
- A grep/file-read verification of "verified missing" status
- An effort estimate < 200 LOC (single PR, single session)
- A "reason to ship" backed by either reliability/UX gain or token/quality lift
- Zero dependency on the wave3, cli-tui-v2, or gateway-cron-delegation worktrees

---

## 3. Design

### 3.1 T1 — MCP utility tools

**Where:** `opencomputer/mcp/client.py` (the side that connects OUT to MCP servers — distinct from `mcp/server.py` which is the side that exposes OC AS an MCP server).

**Capability detection:** at connect time, the existing `mcp.client` session exposes `session.list_resources()`, `session.list_prompts()`, etc. The capabilities are advertised in the `initialize` response under `capabilities.resources` and `capabilities.prompts`. Only register utility tools when the corresponding capability is advertised.

**Tool registration shape:** each capability adds 2 tools (one list, one read/get). All four utility tools are namespaced `mcp_<server_name>_<utility>`:

```python
# Pseudocode in mcp/client.py
def _register_utility_tools(server_name: str, session, capabilities: dict, registry: ToolRegistry) -> None:
    if "resources" in capabilities:
        registry.register(_make_list_resources_tool(server_name, session))
        registry.register(_make_read_resource_tool(server_name, session))
    if "prompts" in capabilities:
        registry.register(_make_list_prompts_tool(server_name, session))
        registry.register(_make_get_prompt_tool(server_name, session))
```

**Each generated tool is a thin async wrapper:**

```python
def _make_list_resources_tool(server_name: str, session) -> ToolSchema:
    async def list_resources() -> list[dict]:
        result = await session.list_resources()
        return [_serialize_resource(r) for r in result.resources]
    return ToolSchema(
        name=f"mcp_{server_name}_list_resources",
        description=f"List resources exposed by MCP server '{server_name}'.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=list_resources,
    )

def _make_read_resource_tool(server_name: str, session) -> ToolSchema:
    async def read_resource(uri: str) -> dict:
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
```

**Filter compatibility:** if/when wave3 ships `tools_allow`/`tools_deny` per-server filtering, these utility tools (named `mcp_<server>_list_resources` etc.) are subject to the same filter — the existing filter applies to tool names regardless of source.

### 3.2 T2 — `/v1/capabilities` endpoint

**Where:** `extensions/api-server/adapter.py`.

**Shape (Hermes-doc reference):**

```json
{
  "version": "1",
  "features": {
    "chat_completions": true,
    "responses": true,
    "streaming": true,
    "tool_calls": true,
    "vision": true,
    "system_prompt": true,
    "previous_response_id": false,
    "runs_api": false,
    "jobs_api": false
  },
  "model": "<active profile name>",
  "profile": "<profile name>"
}
```

`previous_response_id`, `runs_api`, `jobs_api` set to `false` honestly — these are the deferred items in §2.3. Integrators can probe and degrade gracefully.

**Implementation:** add `_handle_capabilities` to the aiohttp router; reuse profile-name lookup already in `_list_models`.

### 3.3 T3 — `/health/detailed` endpoint

**Shape:**

```json
{
  "status": "ok",
  "sessions": {"active": 3, "total": 142},
  "running_agents": 1,
  "uptime_seconds": 3847,
  "memory_mb": 215.4,
  "api_server": {
    "host": "127.0.0.1",
    "port": 8642,
    "profile": "default"
  }
}
```

**Implementation:** add `_handle_health_detailed` route. Source of truth:
- `sessions.active`: count rows in `sessions.db` where `ended_at` is NULL
- `sessions.total`: SELECT COUNT(*) FROM sessions
- `running_agents`: count of registered active runs (existing run-tracking surface)
- `uptime_seconds`: `time.monotonic() - start_time` captured at adapter init
- `memory_mb`: `psutil.Process().memory_info().rss / (1024*1024)` (psutil is already a dep)

Failure semantics: any failed sub-lookup returns `null` for that field rather than failing the entire endpoint. The endpoint never returns 5xx — only 200 with possibly partial data. This keeps health probes from triggering false alarms on transient SQL contention.

### 3.4 T4 — Honcho query-adaptive reasoning

**Where:** `extensions/memory-honcho/provider.py`.

**Schema:** add two optional fields to `HonchoConfig`:

```python
@dataclass(frozen=True, slots=True)
class HonchoConfig:
    ...
    dialectic_reasoning_level: Literal["low", "medium", "high"] = "low"
    reasoning_level_cap: Literal["low", "medium", "high"] = "high"
```

**Query-adaptive scaling logic:** when synthesizing a dialectic reply (in `prefetch` or `sync_turn`), inspect the latest user message length. Bump the reasoning level by 1 step at ≥120 chars and 2 steps at ≥400 chars, capped at `reasoning_level_cap`:

```python
_LEVELS = ["low", "medium", "high"]

def _adapt_level(base: str, query: str, cap: str) -> str:
    base_idx = _LEVELS.index(base)
    cap_idx = _LEVELS.index(cap)
    boost = 0
    if len(query) >= 120:
        boost += 1
    if len(query) >= 400:
        boost += 1
    return _LEVELS[min(base_idx + boost, cap_idx)]
```

**Wiring:** the boosted level is passed to the Honcho `peer.chat()` call as the `reasoning_level` parameter (existing Honcho API field). If the Honcho server doesn't accept the field (older version), it's silently ignored — no error path.

### 3.5 T5 — `oc honcho` CLI

**Where:** new file `opencomputer/cli_honcho.py`; one-line `app.add_typer(honcho_app, name="honcho")` in `cli.py`.

**Subcommands (5):**

```bash
oc honcho status           # show provider/health/cadence/reasoning level — pulls from extensions/memory-honcho/bootstrap
oc honcho sync             # backfill peers across all profiles (one-shot)
oc honcho enable           # set memory.provider = honcho in current profile config.yaml
oc honcho disable          # set memory.provider = builtin in current profile config.yaml
oc honcho strategy <name>  # set sync_turn / prefetch cadence preset (low/balanced/aggressive → low=4/8, balanced=2/4, aggressive=1/2)
```

Each subcommand reads/writes `~/.opencomputer/<profile>/config.yaml` via existing atomic-write helpers in `agent/config_store.py`. `status` overlaps slightly with `oc memory status` but offers a more focused Honcho-specific view (cadence + reasoning level + sync status).

**`sync` cross-profile loop:** iterate `~/.opencomputer/*/config.yaml`, for each profile that has memory.provider == honcho, call the Honcho server's peer-create/upsert API to ensure the AI peer exists. Idempotent.

**`strategy` presets:**

| Preset | `context_cadence` | `dialectic_cadence` | `dialectic_reasoning_level` |
|---|---|---|---|
| `low` | 4 | 8 | low |
| `balanced` | 2 | 4 | low |
| `aggressive` | 1 | 2 | medium |

(Hermes uses different terminology; we ship a 3-preset abstraction over the raw knobs.)

### 3.6 T6 — `credential_pool_strategies` config wiring

**Schema (existing `Config` dataclass in `agent/config.py`):**

```python
@dataclass(frozen=True, slots=True)
class Config:
    ...
    credential_pool_strategies: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
```

YAML form:

```yaml
credential_pool_strategies:
  openrouter: round_robin
  anthropic: least_used
  groq: random
```

**Wiring:** at `CredentialPool` construction (currently somewhere in `agent/credential_sources.py` or wherever pools are built), look up `config.credential_pool_strategies.get(provider_name, "least_used")` and pass as `strategy=`.

**Validation:** if the value isn't in `SUPPORTED_STRATEGIES`, log a warning and fall back to `STRATEGY_LEAST_USED`. Don't raise — config errors shouldn't crash startup.

### 3.7 T7 — Error-code-specific cooldowns

**Where:** `agent/credential_pool.py`'s `with_retry` / `report_auth_failure`.

**New constants:**

```python
EXHAUSTED_TTL_429_SECONDS: float = 3600.0   # existing
EXHAUSTED_TTL_402_SECONDS: float = 86400.0  # NEW — 24h for billing/quota exhaustion
ROTATE_COOLDOWN_SECONDS: float = 60.0       # existing default
```

**Branching logic in `with_retry`:**

```python
async def with_retry(self, *, op: Callable[[str], Awaitable[Any]]) -> Any:
    while True:
        key = await self._next_key()
        try:
            return await op(key)
        except RateLimitError as e:           # 429
            await self.quarantine(key, ttl=EXHAUSTED_TTL_429_SECONDS, reason="429")
            continue
        except QuotaExhaustedError as e:      # 402 — NEW
            await self.quarantine(key, ttl=EXHAUSTED_TTL_402_SECONDS, reason="402")
            continue
        except AuthExpiredError as e:         # 401 — NEW
            if self._oauth_refresher:
                refreshed = await self._try_oauth_refresh(key)
                if refreshed:
                    continue  # try again with refreshed key, don't quarantine
            await self.quarantine(key, ttl=ROTATE_COOLDOWN_SECONDS, reason="401")
            continue
```

**Exception classification:** `QuotaExhaustedError` and `AuthExpiredError` are existing exception classes if defined in plugin_sdk; otherwise check by HTTP status code on `httpx.HTTPStatusError` (`response.status_code == 402` / `== 401`). Plan task spike — verify the actual exception hierarchy in `plugin_sdk/provider_contract.py` before coding.

**Backwards compat:** any caller that doesn't pass an OAuth refresher gets the existing 401 cooldown behavior (rotate immediately, 60s cooldown). No callers break.

### 3.8 T8 — `oc auth` CLI

**Where:** new file `opencomputer/cli_auth.py`; one-line `app.add_typer(auth_app, name="auth")` in `cli.py`.

**Subcommands (4):**

```bash
oc auth list [provider]                                   # list pool entries (table: provider, index, masked_key, status, last_used)
oc auth add <provider> [--key KEY | --key-env ENV]        # append to credential_pools
oc auth remove <provider> <index>                         # remove by index
oc auth reset <provider>                                  # clear all cooldowns + reset use_counts
```

**State source:** `~/.opencomputer/<profile>/config.yaml` under existing `credential_pools` key. Cooldown state lives at runtime in `CredentialPool` instances, not on disk; `oc auth reset` writes a sentinel `__force_reset_at: <timestamp>` to config.yaml that the running pool reads at next refresh.

**Masking:** `oc auth list` shows the SHA256-12-prefix safe-id (matches `agent/credential_pool.py:_safe_id`), never the actual key.

**Atomic writes:** all state mutations via existing `config_store._atomic_write_yaml`.

---

## 4. Implementation plan — 1 PR, 8 commits

| Commit | Scope | LOC | Tests |
|---|---|---|---|
| 1 | T1 MCP utility tools | ~150 | 8 |
| 2 | T2 + T3 API server endpoints | ~80 | 6 |
| 3 | T4 Honcho query-adaptive | ~80 | 4 |
| 4 | T5 oc honcho CLI | ~180 | 8 |
| 5 | T6 credential_pool_strategies wiring | ~40 | 3 |
| 6 | T7 error-code cooldowns | ~80 | 6 |
| 7 | T8 oc auth CLI | ~140 | 8 |
| 8 | Validation: full pytest + ruff | — | — |

**Branch:** `feat/hermes-doc-tier-s-2026-05-08` from `origin/main` (67664a98).
**Worktree:** `~/Vscode/claude/.claude/worktrees/hermes-tier-s-2026-05-08` (per "Worktrees for Parallel Sessions" memory rule). Do NOT share working tree with `claude-wave3` or `hermes-cli-tui-v2-2026-05-08`.

### Test strategy

- New test files: `tests/test_mcp_utility_tools.py`, `tests/test_api_server_capabilities_health.py`, `tests/test_honcho_query_adaptive.py`, `tests/test_oc_honcho_cli.py`, `tests/test_credential_pool_strategies_config.py`, `tests/test_credential_pool_error_cooldowns.py`, `tests/test_oc_auth_cli.py`.
- All new code paths: at least one happy-path + one error-path test.
- Existing 9356+ tests must remain green. No deletions, no skips.
- Honcho test-pollution flake (per `project_honcho_default_test_pollution_flake.md` memory) is pre-existing and not blocking.

### Validation gates

1. **Per-commit:** `pytest tests/` clean + `ruff check` clean.
2. **Pre-PR:** full suite green + smoke tests:
   - Smoke: `oc auth add openrouter --key test_sk --key-env OR_KEY` round-trips through config.yaml
   - Smoke: GET `/v1/capabilities` returns 200 with the documented dict shape
   - Smoke: `oc honcho status` exits 0 even when Honcho server isn't running

---

## 5. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| MCP utility tool registration breaks existing tool naming | Low | New tool names are prefixed `mcp_<server>_*` — same pattern as existing MCP tool names |
| `psutil` import in `/health/detailed` fails on minimal install | Low | Wrap in try/except; report `null` for memory_mb; psutil is already a dep but defensive code is cheap |
| `oc honcho strategy` collides with `oc memory` ergonomics | Low | Different command name; user can use either |
| `oc auth reset` writes a key that another running process is mid-refresh | Low | Atomic write via tempfile+rename; the running pool re-reads on next refresh, eventual consistency is acceptable |
| 402 cooldown 24h is too aggressive for a transient billing glitch | Medium | Document; user can `oc auth reset <provider>` to clear |
| 401 OAuth refresh path triggers infinite loop on permanently-expired token | Medium | Single refresh attempt per call; if refresh returns the same token, treat as failure and quarantine |
| Honcho query-adaptive scaling overshoots cap due to `index` typo | Low | Use `min(base_idx + boost, cap_idx)` not `min(boost, cap_idx)` — explicit unit test |
| Parallel-session collision with wave3 / cli-tui-v2 / gateway-cron-delegation | Low | Worktree from `origin/main`; don't touch any files those plans touch |

---

## 6. Out of scope (explicit, with reopen triggers)

See §2.3 table — every deferred item has an effort estimate and a reopen trigger documented inline.

---

## 7. Decision

Ship 8 features in 1 PR. Defer the §2.3 items with reopen triggers. Don't touch wave3 / cli-tui-v2 / gateway-cron-delegation worktrees.

**Net delta:** ~750 LOC + ~50 tests. 1 day execution. Zero new public APIs that aren't backward-compatible.

---

## 8. Spec self-review

- **Placeholder scan:** no TBD/TODO. Each "shipping" / "not shipping" row has explicit rationale + effort estimate + reopen trigger.
- **Internal consistency:** §2.2 (8 features) maps 1:1 to §3 designs (T1–T8) maps 1:1 to §4 commits (1–7 + validation).
- **Scope check:** 8 commits × ~75 LOC each — honest single-PR size, all under 200 LOC apiece.
- **Ambiguity check:** §3 designs name file paths; §4 names commit boundaries; §5 enumerates failure modes with mitigations.
- **YAGNI re-check:** §2.3 deferred list is 4× larger than shipping list. Brutal pass survived multiple drops (peer/mode/tokens/identity/oauth-refresh-CLI/auth.json discovery).
- **API surface drift:** all new fields are Optional with default-empty. Old configs parse unchanged. New CLI subcommands occupy fresh namespace (`oc honcho`, `oc auth`). New endpoints don't shadow existing.
- **Composability check:**
  - `oc auth` CLI writes to `credential_pools` → pool reads at init → `credential_pool_strategies` selects rotation → error-code cooldowns fire on rotation. End-to-end chain.
  - `oc honcho status / strategy` modifies HonchoConfig → provider re-reads on next session → query-adaptive scaling applies. End-to-end chain.
- **Architecture stress:**
  - MCP server with no resources/prompts capability → no utility tools registered (graceful)
  - API server `/health/detailed` partial failure → 200 with null fields (graceful)
  - 402 cooldown false-positive → user has reset path
- **Verification dependencies:**
  - Verify `plugin_sdk/provider_contract.py` exception hierarchy before T7 (RateLimitError / QuotaExhaustedError / AuthExpiredError)
  - Verify `mcp/client.py` uses the official `mcp` SDK Session object (it does, per imports)
  - Verify aiohttp router pattern in `extensions/api-server/adapter.py` (it uses `web.RouteTableDef`)

---

## 9. Audit lens results (9-lens framework)

| Lens | Finding | Resolution |
|---|---|---|
| Assumption-check | Assumed psutil is always installed | Wrap in try/except in T3 |
| Architecture stress | MCP server may not advertise resources capability | Capability detection via `initialize` reply |
| Alternative dismissal | Could merge `oc auth` into `oc model auth` | Rejected — Hermes namespace is `oc auth`, simpler |
| Requirement gap | Implicit need: tests must not collide with other worktrees | Worktree isolation + new test files only |
| Composability claim | All 8 items wire end-to-end through config → state → CLI | Verified in §8 |
| Scope honesty | 8 items × ~75 LOC = 600. Honest. | Adjusted to 750 LOC after T1/T5 details |
| API surface drift | All fields Optional, all CLI new namespace | OK |
| Failure mode map | §5 covers 8 failure scenarios | OK |
| YAGNI sweep | Trimmed `oc honcho peer/mode/tokens/identity` (4 subcommands), `oc auth oauth-refresh`, auth.json auto-discovery | All in §2.3 with reopen triggers |
