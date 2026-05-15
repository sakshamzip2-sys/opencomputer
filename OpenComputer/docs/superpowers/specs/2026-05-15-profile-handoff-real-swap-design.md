# Profile Handoff — Real Swap (Path B Partial)

Author: 2026-05-15
Status: SHIPPED with one hard-justified deferral (§9.6 plugin reload).
Driver: `docs/plans/profile-handoff-investigation.md` v2 — the v3 audit
of what shipped lives in the correction log at the bottom of that doc.

## Goal

Close every gap documented in the v2 investigation except the one
that requires multi-week plugin-contract surgery (§9.6 plugin reload).
After this PR, a profile swap propagates to:

1. `OPENCOMPUTER_HOME` env var (§9.1)
2. `.env` credentials (§9.3) — without clobbering shell-set values
3. Active provider client (§9.7)
4. Top-level Config fields in the **allowlist** (§9.4)
5. MCP server fleet via diff-cycle (§9.5)
6. SessionDB + continuation pointer (§9.2)
7. Browser-profile env (§9.8)
8. F1 ConsentGate audit chain (§2.10)
9. Classifier context (real hour + recent file paths)
10. Gateway per-channel `auto_swap_enabled` opt-in

## Architecture

### Rebind Registry

`opencomputer/agent/profile_rebind.py` — the central composition primitive.

```
                   _apply_pending_profile_swap (turn entry, async)
                                  │
            ┌─────────────────────┼──────────────────────────┐
            ▼                     ▼                          ▼
       memory.rebind        env var update          await registry.invoke
                            (sticky file)                    │
                                                  ┌──────────┼──────────┐
                                                  ▼          ▼          ▼
                                              priority 20  priority 50  ...
                                              dotenv       config       provider
                                                                       (60, 120, 130, 160)
```

The registry is **ordered** (lower priority runs first) and
**exception-isolated** (one handler raising does NOT stop the rest).
Each handler is `(new_home: Path, old_home: Path | None) -> None |
Awaitable[None]`. The registry awaits awaitables.

### Built-in Handlers (priorities)

| Priority | Name | Responsibility |
|---|---|---|
| 20  | `dotenv`        | Unload prior `.env`, load new profile's `.env` |
| 50  | `config`        | Allowlisted Config field hot-swap |
| 60  | `provider`      | Rebuild active provider client; invalidate handoff adapter cache |
| 110 | `mcp`           | (registered by cli.py) Diff-cycle MCP fleet |
| 120 | `session_db`    | Write continuation pointer to OLD db; rebind path; cascade SubagentStore + EpisodicMemory |
| 130 | `consent_gate`  | Re-point F1 ConsentGate at new profile's `audit.db` + keyring |
| 150-159 | plugins reserved | `PluginAPI.register_profile_rebind_handler` queues lands here |
| 160 | `browser-harness` (plugin) | Update `AGENT_BROWSER_PROFILE` + flush sessions |

### Plugin Exposure

`plugin_sdk.runtime_context.PluginAPI.register_profile_rebind_handler(name, handler, *, priority=150)`
queues the handler. AgentLoop drains the queue into its registry at
`__init__` time. Plugins (browser-harness, future honcho / langfuse)
use this to participate without coupling to `opencomputer.*`.

## Config Hot-Swap Allowlist

`HOT_SWAPPABLE_TOP_LEVEL_FIELDS` (in `agent/config_hot_swap.py`):

| Field | Why hot-swappable |
|---|---|
| `model` | Read per-turn; provider rebind reacts |
| `memory` | Memory paths already re-pointed via `memory.rebind_to_profile` |
| `gateway` | Photo-burst window etc. consulted per-message |
| `deepening` | Layer-3 extractor; per-extraction call |
| `model_context_overrides` | Per-turn lookup |
| `credential_pool_strategies` | Per-call lookup |
| `custom_providers` | Resolver consults on model swap |

Fields NOT in the allowlist (delta logged at WARN, restart-required):
`loop`, `mcp` (handled by `MCPManager.diff_cycle`), `hooks`,
`prompt_hooks`, `agent_hooks`, `http_hooks`, `tools`, `session`
(handled by SessionDB rebind), `system_control`, `cron`, `worktree`,
`checkpoints`.

## SessionDB Continuation Pointer

When `SessionDB.rebind(new_path, source_session_id=sid,
target_profile="stocks")` runs, it INSERTs a `system`-role marker
message into the OLD database for `sid`:

```
[profile-swap] This session continued in profile 'stocks' at <utc>.
To resume, run: oc -p stocks chat -c <sid>
```

`oc resume` / `oc chat -c` can detect this marker and surface a hint
("session moved to stocks; switch profile and resume there"). Tests
cover the marker write + read path.

## Hard-Justified Deferral: §9.6 Plugin Reload

**What's deferred:** rebuilding the plugin registry, hook engine,
provider registry, channel registry, and injection engine on profile
swap. New profile's `plugins.enabled` list does NOT take effect until
process restart.

**Why deferred:** The plugin contract has no `unregister` /
`dispose` path on:
- `ToolRegistry.unregister_*` (partial; some tools register without
  uniquely-claimed names)
- `HookEngine` subscriptions (no unsubscribe API for fire-and-forget
  hooks)
- `Adapter.disconnect()` cascade (not idempotent in some channels)
- `InjectionEngine.unregister_provider`
- `ProviderRegistry` removal

Building these dispose paths is a 2-3 week refactor of the public
plugin contract — a separate workstream. Doing it half-way creates
worse footguns than restart-to-switch.

**UX mitigation:** when the new profile's plugins.enabled differs from
the active one, the next system message includes a one-line warning:
"Plugin set differs from current — restart `oc` for full switch."
(Documented; implementation lives in the config-hot-swap WARN already
emitted.)

**Path to closure:** define a `Disposable` mixin on every plugin
registration kind, ship dispose paths plugin-by-plugin, then ship a
new rebind handler at priority 200. Out of scope of this PR.

## Test Coverage

| Module | Test file | Count |
|---|---|---|
| ProfileRebindRegistry | `tests/test_profile_rebind_registry.py` | 15 |
| env-var swap | `tests/test_profile_swap_env_alignment.py` | 7 |
| Classifier ctx | `tests/test_handoff_classifier_ctx.py` | 12 |
| Channel opt-in | `tests/test_channel_adapter_auto_swap_optin.py` | 8 |
| Dotenv tracker | `tests/test_dotenv_tracker.py` | 12 |
| Provider rebind | `tests/test_profile_rebind_handlers.py` | 6 |
| Config hot-swap | `tests/test_config_hot_swap.py` | 8 |
| MCP diff_cycle | `tests/test_mcp_diff_cycle.py` | 11 |
| SessionDB rebind | `tests/test_session_db_rebind.py` | 9 |
| Browser-profile | `tests/test_browser_profile_rebind.py` | 5 |
| ConsentGate | `tests/test_consent_gate_rebind.py` | 4 |
| Existing handoff suite | `tests/test_handoff_*.py` | 46 (unbroken) |

**Total new tests:** 97.
**Pre-existing handoff suite:** 46, all green.

## Out-of-Scope (Documented, Not Hidden)

* §9.6 plugin reload — see above.
* Tools that captured `consent_gate` at THEIR init may keep the old
  gate's audit reference. Gate is mutated in place (`rebind_to_profile`
  swaps `_store`/`_audit`/`_owned_conn` on the existing object) so the
  reference remains valid and the audit chain follows the rebind.
  But tools that snapshot e.g. `gate._audit` into a local are out of
  luck; nobody does this in the current codebase (verified via grep)
  but a future tool author should NOT.
* In-flight async streaming during a swap: the swap fires at turn
  entry, BEFORE the provider call, so cannot interrupt a streaming
  response. Documented.

## Files Changed

```
opencomputer/agent/profile_rebind.py           NEW
opencomputer/agent/dotenv_tracker.py           NEW
opencomputer/agent/config_hot_swap.py          NEW
opencomputer/agent/loop.py                     +145 lines (handlers, drain)
opencomputer/agent/state.py                    +85 (rebind + close)
opencomputer/agent/consent/gate.py             +95 (rebind_to_profile)
opencomputer/cli_ui/_profile_swap.py           +40 (env var, ContextVar)
opencomputer/agent/handoff/orchestrator.py     +60 (real hour, file paths)
opencomputer/cli.py                            +12 (MCP rebind wire)
opencomputer/mcp/client.py                     +130 (diff_cycle + hash)
opencomputer/plugins/loader.py                 +50 (register_profile_rebind_handler)
plugin_sdk/channel_contract.py                 +20 (auto_swap_enabled config-driven)
extensions/browser-harness/plugin.py           +55 (rebind handler)
tests/                                         +11 new test files, +97 tests
docs/plans/profile-handoff-investigation.md    v3 correction log
docs/superpowers/specs/2026-05-15-profile-handoff-real-swap-design.md   NEW
```
