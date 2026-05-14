# MCP — Porting OpenClaw's Architecture into OpenComputer

Author: 2026-05-14
Status: Engineering plan, ready for staged implementation
Owner: Saksham

---

## 0. Source-of-truth findings (before any planning)

Read with verified file paths from the OpenClaw checkout at
`/Users/saksham/Vscode/claude/sources/openclaw/`. Cross-referenced against OC's
current implementation at `/Users/saksham/Vscode/claude/OpenComputer/opencomputer/mcp/`.

### What OpenClaw actually has (read every load-bearing file)

OpenClaw uses MCP in **three different roles**, each implemented separately:

**Role 1: Client (consuming external MCP servers).**
Lives in `src/agents/`:
- `pi-bundle-mcp-runtime.ts` (645 LOC) — `SessionMcpRuntime` + `SessionMcpRuntimeManager`. Per-session MCP runtimes with lease-counting, idle TTL eviction, in-flight create dedup.
- `pi-bundle-mcp-materialize.ts` (174 LOC) — converts an MCP `Client.listTools()` catalog into agent-loop tools.
- `pi-bundle-mcp-types.ts` (62 LOC) — strict types: `SessionMcpRuntime`, `McpToolCatalog`, `BundleMcpToolRuntime`.
- `mcp-stdio-transport.ts` (160 LOC) — custom stdio transport with `killProcessTree`, EPIPE handling, OOM-score adjustment on Linux.
- `mcp-transport.ts` (126 LOC) — dispatch on transport type, attach stderr logging.
- `mcp-transport-config.ts` (162 LOC) — config resolution + redacted error logging.
- `pi-bundle-mcp-names.ts` — collision-safe tool name builder (`<server>__<tool>` with sanitization + truncation to 64 chars + collision suffix).

**Role 2: Server (exposing OC's own surface as MCP).**
Lives in `src/mcp/`:
- `channel-bridge.ts` (560 LOC) — bridges OC's gateway/session/approval/event surface AS MCP tools. Includes the **bidirectional Claude permission flow** via MCP notifications.
- `channel-server.ts` (110 LOC) — wires the bridge to a stdio MCP server, handles SIGINT/SIGTERM/EOF shutdown cleanly.
- `channel-tools.ts` (188 LOC) — registers 9 tools (`conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `permissions_list_open`, `permissions_respond`).

**Role 3: Bundle (plugins ship MCP servers, OC auto-mounts).**
Lives in `src/plugins/bundle-mcp.ts` (295 LOC) + `src/agents/embedded-pi-mcp.ts` (23 LOC):
- Plugin's `.mcp.json` or manifest's `mcpServers` field declares one or more servers.
- `${CLAUDE_PLUGIN_ROOT}` placeholder substitution.
- Path absolutization rules (commands resolved against plugin root, args expanded, env vars substituted).
- Merge-patch (RFC 7396) of file-backed + inline configs.

### What OC already has

After reading `opencomputer/mcp/` end-to-end:

| Capability | OC status |
|---|---|
| stdio + sse + streamable-http transports | Yes (`client.py`) |
| Daemon-thread event loop + cross-loop dispatch | Yes (`_run_on_session_loop`, gnarly + correct) |
| Per-server env whitelist (Hermes-parity strict filter) | Yes (`_MCP_SAFE_ENV_KEYS`) |
| OSV vulnerability scan on install | Yes (`osv_check.py`) — neither Hermes nor OpenClaw has this |
| 19 install presets | Yes (`presets.py`) |
| OAuth PKCE flow | Yes (`oauth.py` + `oauth_pkce.py`) |
| Sampling (bidirectional model use) | Yes (`sampling.py`) |
| `notifications/tools/list_changed` live refresh | Yes (`tools_changed_callback`, line 1146+) |
| Per-tool timeout + connect timeout | Yes (`MCPServerConfig.timeout`, `connect_timeout`) |
| `tools_allow` / `tools_deny` allowlist/denylist | Yes (`MCPServerConfig`) |
| Prompts + resources utility-tool toggles | Yes (`prompts_enabled`, `resources_enabled`) |
| OC-as-MCP-server (Role 2) | **Partial.** `server.py` (690 LOC) exposes 10+ tools (sessions, messages, recall, consent, events_poll/wait, permissions_respond). Real implementation. |
| Bundle MCP (Role 3, plugin ships MCP server) | **Stub only.** `PluginManifest.mcp_servers` field exists but it just refers to preset slugs or paths. No `${CLAUDE_PLUGIN_ROOT}` substitution, no auto-mount lifecycle. |
| Per-session MCP runtimes with idle eviction | **No.** OC has process-global MCPs only. |
| Tool-description prompt-injection scanner | **No.** |
| `killProcessTree` on stdio subprocess close | **No** — relies on default cleanup; stdio orphans possible. |
| Schema validation on tool args via Ajv/jsonschema | **No** — args passed to `ClientSession.call_tool` raw. |
| Collision-safe `<server>__<tool>` naming with truncation + collision suffix | **Partial.** Uses `<server>__<tool>` but no length cap / no collision suffix. |
| Plugin `${PLUGIN_ROOT}` placeholder in MCP config | **No.** |
| Stderr capture per-server to its own log | **No** — inherits to parent. |
| Per-server connection-timeout + redacted error logging | **Partial.** Timeout yes; redaction no. |
| Plugin-shipped MCP runtime with auto-load + dispose lifecycle | **No.** |

### The honest gap

OC is competitive with OpenClaw on **Role 1** (the client). OC outright wins on
**OSV scanning**. OC is **partially competitive on Role 2** (`server.py`
exists but is not as deeply wired — no approval-bridge, no Claude permission
flow). OC is **far behind on Role 3** (plugin-shipped bundled MCPs) — that's
the biggest architectural gap.

The plan below targets the three highest-ROI gaps: bundle-MCP (Role 3),
session-scoped runtimes with idle eviction (transferable Role 1 polish), and
the channel-bridge approval flow (Role 2 deepening).

---

## 1. Brainstorm — five candidate approaches

For each: scope, effort (S/M/L/XL), risk, upside, why-not.

### A. Full lift-and-shift port (everything)

**Scope.** Port `bundle-mcp.ts` + `channel-bridge.ts` + `pi-bundle-mcp-runtime.ts` + `pi-bundle-mcp-materialize.ts` + `mcp-stdio-transport.ts` + the names + transport-config files. Rewrite OC's `client.py` to match the per-session runtime model. Build out `server.py` to match `channel-bridge.ts`'s approval flow.

**Effort.** XL — 4-6 weeks for one engineer.

**Risk.** High. OC's current `client.py` (1,959 LOC) has 60+ tests; rewriting it breaks the world. The cross-loop dispatch in `_run_on_session_loop` is the gnarliest 30 lines of code in OC and is tested empirically against real anyio sessions; a rewrite has high chance of subtly breaking it.

**Upside.** Full parity. Cleanest end-state.

**Why not.** The cost-of-rewrite vastly exceeds the marginal benefit. Most of OpenClaw's Role 1 polish (live refresh, per-server timeouts, env whitelist, schema validation, name sanitization) is already in OC or trivially addable. Don't burn months on a rewrite when the gap is plugin-bundle support.

### B. Three-targeted-patches port (recommended)

**Scope.** Three independent, separately-deployable patches:
1. **Bundle MCP** (Role 3) — let plugins ship MCP servers. Plugin manifest gains `bundle_mcp: list[BundleMcpServer]`; loader spawns each at plugin activation, names tools `<plugin_id>__<server>__<tool>`, disposes on plugin disable.
2. **Per-session MCP runtimes with idle eviction** (Role 1 polish) — opt-in flag; legacy process-global path remains default. New `SessionMcpRuntime` class wraps `MCPManager` with session-id keying + lease-counting + idle sweep.
3. **Channel-bridge approval flow** (Role 2 deepening) — add `permissions_list_open` (already in `server.py` lines 402, 594) the bidirectional notifications/claude/channel emission so external MCP clients (Claude Code, Cursor) can drive OC approval prompts.

**Effort.** M+S+M ≈ 3 weeks for one engineer, in three independently-mergeable PRs.

**Risk.** Low-medium. Each patch is independently testable. The bundle MCP patch is the riskiest (new lifecycle code) but lives behind a feature flag (`bundle_mcp_enabled: bool` on `MCPConfig`, default `False` until tests pass).

**Upside.** Closes 80% of the parity gap. Plugin authors gain the highest-leverage missing capability (shipping MCP servers with their plugin). Session-scoped runtimes unlock multi-user later. Channel-bridge approval makes OC useful from inside Claude Code.

**Why this is the pick.** Each patch has independent value. The bundle-MCP patch is the strategically-important one (plugin ecosystem); session-runtimes is forward-leaning architecture; channel-bridge approvals is the existing `server.py`'s next logical step.

### C. Bundle MCP only, defer everything else

**Scope.** Just patch #1 from option B.

**Effort.** S — 4-7 days.

**Risk.** Low.

**Upside.** Closes the single biggest gap. Plugins can ship MCP servers.

**Why not (alone).** Leaves the session-runtime polish on the table, and `server.py` improvements (Role 2) ship at near-zero marginal cost while the engineer is in the MCP module. False economy.

### D. Channel-bridge approval flow only

**Scope.** Just patch #3 from option B. Make `server.py` symmetric with OpenClaw's `channel-bridge.ts`.

**Effort.** S — 3-4 days.

**Risk.** Very low — `server.py` already exists; we're deepening it.

**Upside.** External MCP clients (Claude Code, Cursor, IDE plugins) can drive OC consent prompts and read OC's pending-approval queue. Concrete UX: from inside Claude Code, you ask OC for a tool list, OC asks for permission, Claude Code surfaces a permission prompt to you, you respond inline, OC executes.

**Why not (alone).** Smallest user-visible upside of the three. The bundle MCP gap is more strategically important to close first.

### E. Adopt OpenClaw's bundle-MCP **manifest format** but keep OC's client unchanged

**Scope.** Match OpenClaw's `.mcp.json` convention + `${PLUGIN_ROOT}` placeholder so the same plugin can be installed in either tool. No internal architecture change to OC.

**Effort.** S — 3-5 days.

**Risk.** Low.

**Upside.** Plugin authors target one manifest format and ship to both OC and OpenClaw. Compatibility win without internal rewrite.

**Why not (alone).** It's a strict subset of option B's patch #1. We're doing this anyway as part of option B; not worth doing alone.

### Pick: **Option B** — three-targeted-patches port.

**Why B wins:**

- Highest-value gap (bundle MCP) is included.
- Each patch is independently testable, reviewable, mergeable.
- Bundle MCP includes the manifest-format-compat win from option E for free.
- Channel-bridge approval flow ships under `server.py` deepening, near-zero new infrastructure.
- Per-session runtime is the architectural seam OC needs for future multi-user (Path B from `docs/plans/openhub-mvp.md`). Building it now without committing to multi-user keeps the option open.
- Cost is real (3 weeks) but the highest-cost patch is independently flag-gated.

---

## 2. Audit the design (attack the proposal)

Before planning milestones, find every weak spot in option B.

### Assumption audit

**A1. "Plugins want to ship MCP servers."**
Verifiable. Today plugins ship Python `BaseTool` subclasses via `register(api)`. The whole point of bundling MCP is **language-independence** — a plugin can ship a Go or TypeScript or Rust MCP server. Use case: a plugin author writes their MCP server in Node (because their library is Node-only) and bundles it in the plugin. This is a real demand surface but it's a NEW one for OC.

Risk: we're building infrastructure for demand that may not materialize immediately. Mitigation: build the lifecycle but ship without a single bundled MCP plugin in `extensions/`. Let one third-party plugin try it first. Don't pre-build 5 bundled plugins.

**A2. "Per-session MCP runtimes are needed."**
Today every `oc chat` invocation gets one MCPManager with one set of long-lived connections. Per-session is *interesting* only if (a) we want multi-user, OR (b) different sessions on the same machine want different MCP server sets.

Honest assessment: **today (b) is rare and (a) is multi-user-cloud which is a separate plan.** The lease-counting + idle-eviction is sophisticated engineering for a benefit we don't yet need.

Risk: building Role 1 polish that doesn't pay off. Mitigation: **demote to optional/feature-flagged**. Make per-session runtimes opt-in. Default stays process-global. Only turn on when there's a concrete use case.

**A3. "Channel-bridge approval flow is unlocked by deepening `server.py`."**
Need to verify `server.py` actually has the gateway-event subscription path. Reading lines 277-366 (events_poll, consent_history) and 402-594 (permissions_list_open, messages_send_status, permissions_respond) confirms the surface is there. The MISSING piece vs OpenClaw is the **bidirectional emit** — when an external MCP client requests permission, OC currently has no way to push the result back via MCP notifications. That's the gap to close.

### Edge cases

**E1. Plugin disabled mid-session.** Plugin ships MCP server, user disables plugin. What happens?
- OC must dispose the MCP subprocess (`SIGTERM` then `SIGKILL` on timeout).
- Tools registered by the MCP must be unregistered from `ToolRegistry`.
- Any in-flight tool call from that MCP must error cleanly with a clear message ("plugin X was disabled mid-call").

OpenClaw handles this via `disposeSession` → cascades to `client.close()`. OC needs the same path; today `MCPManager.shutdown_all()` exists but isn't wired to plugin-disable events.

**E2. Bundle MCP subprocess crashes silently.** stdio MCP server exits with non-zero; OC continues, doesn't notice.
- OpenClaw logs the spawn error via `redactErrorUrls` and removes the server from the catalog. Subsequent tool calls error with "bundle-mcp server X is not connected."
- OC currently relies on transport's `onclose` callback; needs verification this triggers actionable error on subsequent calls. (Verified at `client.py:1480` `_on_connection_tools_changed` exists for tool-list deltas but I didn't find an equivalent for spawn-failure cleanup. Could be a real gap.)

**E3. Two plugins ship MCP servers with the same name.** `plugin-A/.mcp.json` declares `github`, `plugin-B/.mcp.json` also declares `github`.
- OpenClaw uses `<plugin_id>__<server>__<tool>` naming with collision suffix — first declared wins the unprefixed slot; second gets `-2`.
- OC's plan: always prefix with `<plugin_id>__<server>__`. No unprefixed slot. Two plugins with same server name = two prefixed namespaces, no collision.

**E4. `${CLAUDE_PLUGIN_ROOT}` injection attacks.** Plugin manifest sets `command: "${CLAUDE_PLUGIN_ROOT}/../../../usr/bin/rm"`.
- Placeholder substitution must happen BEFORE path resolution, and the result must be sanity-checked: command must stay inside the plugin root OR be an absolute path the user explicitly approved at install time.
- OpenClaw resolves via `path.resolve(baseDir, expanded)` which CAN escape baseDir. Mitigation: after substitution, assert the resolved command is inside the plugin root, or warn-and-confirm at install if not.

**E5. Per-session runtime memory.** If we ship per-session runtimes and a user has 50 sessions in 1 day, that's 50 sets of subprocess spawns. Even at 5s spawn time = 4 minutes of cumulative MCP boot.
- Idle eviction (10-min TTL by default in OpenClaw) is the answer.
- Lease counting prevents eviction during an active tool call.
- For OC: default TTL = 5 minutes; min-active-leases = 0.

**E6. Bundle MCP + OSV scan interaction.** OC's OSV scanner runs before spawning any `npx`/`uvx` stdio MCP. If a bundled plugin's MCP server uses npx, OSV should run for it too — but that's a 2-3s network call per spawn. With per-session runtimes that's painful.
- Cache OSV results (already done in OC's `osv_check.py`, verified).
- For bundled MCPs in plugins-from-trusted-sources, allow `osv_check: false` per-server override. Default is on.

**E7. Plugin's MCP server pins different MCP SDK version.** A plugin's server uses MCP SDK 1.30, OC's client uses 1.15. Are they wire-compatible?
- MCP spec is versioned with compatibility guarantees within minor versions. As long as the plugin's SDK and OC's SDK are within one major version, this works.
- Recommend OC stay on the latest stable MCP SDK for Python (currently `mcp>=1.0`).

### Overcomplications

**O1. The lease-counting in `pi-bundle-mcp-runtime.ts` is real engineering** — but OC's first-cut bundle-MCP doesn't need it. A bundled MCP server spawned at plugin activation can stay alive for the process lifetime. **Drop lease-counting from milestone 1.** Add it later when (and if) per-session runtimes ship.

**O2. The `applyMergePatch` (RFC 7396 merge-patch) logic in OpenClaw's `bundle-mcp.ts`** is general-purpose but overkill for our needs. Plugins ship one `.mcp.json` or one `mcpServers:` block in manifest, not five files to merge. **Use simple dict-merge, not full RFC 7396.**

**O3. Sanitized server names with collision suffix** is needed only if we let two plugins coexist with the same server name slot. Since we prefix with `<plugin_id>__`, collision is impossible. **Drop the collision-suffix logic; keep simple sanitization (`[^A-Za-z0-9_-]` → `-`).**

### Fit-check

The three patches in option B compose without conflict:
- **Bundle MCP** writes new code under `opencomputer/mcp/bundle.py` + new `plugin_sdk.core.BundleMcpServer` dataclass + extends `PluginManifest`. No changes to existing `client.py`/`server.py`.
- **Per-session runtimes** wrap `MCPManager` in a new `SessionMcpRuntimeManager` class under `opencomputer/mcp/session_runtime.py`. Existing process-global path is unchanged.
- **Channel-bridge approval** extends `server.py` only. New tools, new notification path; no client-side changes.

No two patches edit the same lines. Each is independently revertable.

### What breaks in production

**B1. Bundle MCP regresses plugin load time.** Plugin activation now waits for `npx -y @some/mcp-server` to spawn + handshake. Cold start: 3-7 seconds per server. With 5 bundled-MCP plugins, plugin activation goes from <1s to 30s.
- Mitigation: bundle-MCP servers spawn **lazily** — only when the first tool from that server is actually called. Plugin activation only registers metadata.

**B2. Session-scoped runtimes leak subprocesses when sessions are abandoned.** Browser tab closes, gateway disconnects, but Python doesn't know. Idle TTL eviction is the only safeguard.
- Mitigation: idle sweep timer + max-sessions cap (default 20). Beyond cap, evict least-recently-used regardless of TTL.

**B3. Channel-bridge approval flow exposes consent-grant state over MCP**, which means any MCP client connected to OC can read who has been granted what. If the user installs a malicious MCP client (e.g., in Claude Code), it can enumerate consent grants.
- Mitigation: the channel-bridge MCP server is **not** registered as an installable MCP automatically. It's opt-in via `oc mcp serve --enable-approvals`. Default deny.

**B4. OSV scan stalls bundle MCP spawn.** A 30-second OSV timeout makes per-session runtimes infeasible.
- Mitigation: OSV check ran at INSTALL time (when plugin is installed), cached; spawn-time is lookup-only. Already done in `osv_check.py:check_package` (verified).

### Resolutions baked into the plan

- Lease-counting → deferred to milestone 4 (optional).
- Lazy spawn → milestone 1 requirement.
- Per-session runtimes → opt-in feature flag from milestone 2.
- Channel-bridge approvals → opt-in via CLI flag.
- Bundle MCP namespace = `<plugin_id>__<server>__<tool>` always (no collision suffix needed).
- `${PLUGIN_ROOT}` path sanity check before spawn.
- Cap concurrent sessions at 20 with LRU eviction.

---

## 3. Plan — milestones

Five milestones. Each independently testable. Each has a clear MVP cut.

Dependencies marked `(after MN)` where present.

### Milestone 1 — Bundle MCP (Role 3, the strategic gap) **— MVP**

Scope: plugins can ship MCP servers, OC auto-mounts them as tools.

**Deliverables.**

1. `plugin_sdk/core.py` — extend `PluginManifest` with `bundle_mcp: tuple[BundleMcpServer, ...] = ()`. Add `BundleMcpServer` dataclass (frozen, slots):
   ```python
   @dataclass(frozen=True, slots=True)
   class BundleMcpServer:
       name: str                          # local to this plugin
       transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
       command: str = ""                  # stdio only
       args: tuple[str, ...] = ()
       env: dict[str, str] = field(default_factory=dict)
       cwd: str = ""                      # if empty, defaults to plugin root
       url: str = ""                      # sse / streamable-http
       headers: dict[str, str] = field(default_factory=dict)
       connection_timeout_seconds: float = 30.0
       lazy: bool = True                  # spawn on first tool call
       tools_allow: tuple[str, ...] | None = None
       tools_deny: tuple[str, ...] = ()
       osv_check: bool = True
   ```

2. `opencomputer/mcp/bundle.py` — new module:
   - `_expand_plugin_root_placeholder(value: str, plugin_root: Path) -> str` — substitute `${PLUGIN_ROOT}` only (NOT `${CLAUDE_PLUGIN_ROOT}` — that's openclaw's name; we use our own).
   - `_resolve_bundle_command(server: BundleMcpServer, plugin_root: Path) -> Path` — expand, resolve, assert resulting absolute path is inside plugin_root OR is an absolute path the user has approved (raise `BundleMcpSafetyError` otherwise).
   - `bundle_mcp_to_mcp_server_config(plugin_id: str, server: BundleMcpServer, plugin_root: Path) -> MCPServerConfig` — produce an `MCPServerConfig` named `<plugin_id>__<server.name>` ready for the existing `MCPManager`.

3. `opencomputer/plugins/loader.py` — at plugin load:
   - Read `manifest.bundle_mcp`.
   - For each server: produce `MCPServerConfig`, register in a new `BundleMcpRegistry` keyed by `plugin_id`. Do NOT spawn (lazy=True default).
   - At plugin DISABLE: dispose all sessions in the registry for that plugin_id.

4. `opencomputer/mcp/client.py` — change `MCPManager.connect_all` to ALSO walk `BundleMcpRegistry.all_servers()` and add them to the connect list. Tool names registered as `<plugin_id>__<server>__<tool>`. Existing `<server>__<tool>` naming for user-configured MCPs unchanged.

5. `opencomputer/cli_mcp.py` — `oc mcp list` shows bundled MCPs grouped under their plugin: `└─ plugin-id`. New subcommand `oc mcp bundles` for bundle-MCP-only view.

**Tests.**

- `tests/test_mcp_bundle.py`:
  - manifest with one stdio server, two tools — both registered as `<plugin>__<server>__<tool>`.
  - `${PLUGIN_ROOT}` substitution works for `command`, `args`, `env` values, `cwd`.
  - command-escape attack: `command="${PLUGIN_ROOT}/../../../bin/rm"` → raises `BundleMcpSafetyError`.
  - lazy=True: subprocess NOT spawned at plugin activation. First tool call triggers spawn.
  - lazy=False: subprocess spawned at plugin activation.
  - plugin DISABLE → subprocess SIGTERMed within 3s, SIGKILLed if no exit.
  - two plugins both declare server named `github` — both load; tools are `pluginA__github__...` and `pluginB__github__...`.
  - In-flight tool call when plugin disabled → returns `ToolResult(is_error=True, error="bundle MCP disposed: plugin X disabled")`.
- `tests/test_plugin_manifest.py`: existing manifest dict roundtrips correctly with new `bundle_mcp` field; old manifests without the field still load.

**Risks.**

- Lazy spawn changes connect timing. Existing tests that assume "MCP is connected after `connect_all`" may need updating. Mitigation: opt-in `BundleMcpServer.lazy=False` for tests that need eager spawn.
- Path-escape detection must use `resolved_path.is_relative_to(plugin_root)` (Python 3.9+). Verify Python 3.12 (the project's minimum) supports this — it does.

**MVP cut.** Milestone 1 alone IS the MVP. Ships independently. Closes the strategic gap.

### Milestone 2 — Per-session MCP runtimes (opt-in)

(after M1)

Scope: opt-in feature flag enables session-scoped MCP runtimes with idle eviction. Default OFF.

**Deliverables.**

1. `opencomputer/mcp/session_runtime.py` — new module:
   - `SessionMcpRuntimeManager` class. Keys: `session_id` → `MCPManager` instance. Each instance has its own background event loop.
   - `get_or_create(session_id, cfg) -> MCPManager` — returns existing or spawns new.
   - `idle_ttl_seconds: float = 300.0` — evict after idle.
   - `max_sessions: int = 20` — LRU-evict beyond.
   - `dispose(session_id)` — explicit retire.
   - Background `asyncio.Task` runs every 60s, sweeps idle.

2. `opencomputer/agent/config.py` — add `MCPConfig.session_scoped: bool = False`. When True, every `AgentLoop` instance routes through `SessionMcpRuntimeManager.get_or_create(self.session_id)`. When False (default), continues using process-global `MCPManager`.

3. `opencomputer/cli_mcp.py` — `oc mcp sessions` lists active session runtimes with last-used / lease-count.

**Tests.**

- `tests/test_mcp_session_runtime.py`:
  - default config: no session manager constructed.
  - `session_scoped=True`: two `AgentLoop` instances with different session_ids → two separate MCPManager instances → distinct subprocess sets.
  - idle eviction: set TTL=5s, run a session, wait 6s, sweep, runtime is gone, subprocesses dead.
  - LRU eviction: cap=3, create 4 sessions sequentially without idle, oldest is evicted.
  - in-flight tool call prevents eviction: simulate active lease, idle TTL elapses, runtime is NOT evicted.

**Risks.**

- Each session-runtime owns its own asyncio event loop. With cap=20 that's 20 background threads. Memory/threading overhead is real on small VMs.
- Mitigation: docs warn that `session_scoped` is for multi-user / multi-tenant scenarios only. Default OFF.

**MVP cut.** Could ship without LRU eviction (just idle TTL). LRU is nice-to-have.

### Milestone 3 — Channel-bridge approval flow (Role 2 deepening)

(after M1, optional after M2)

Scope: external MCP clients can drive OC's permission/approval surface.

**Deliverables.**

1. `opencomputer/mcp/server.py` — verify `permissions_list_open` works against the dispatch queue's pending-approvals view (the surface exists at lines 402, 594).

2. Add `permissions_request_subscribe` tool that long-polls — clients block on this to get new permission requests as they arise. Pattern mirrors existing `events_wait` at line 277.

3. Add MCP **notifications** path. When OC raises a permission prompt, the MCP server emits `notifications/openclaw/permission/requested` to all connected clients. Clients can respond via existing `permissions_respond` tool.

4. `opencomputer/cli_mcp.py` — `oc mcp serve --enable-approvals` flag. Default OFF (security). When ON, advertises permission tools in capabilities.

5. Documentation in `docs/mcp-server-approvals.md` — example workflow with Claude Code: install OC's MCP server in Claude Code's `~/.config/claude-code/settings.json`, ask Claude to "list pending approvals in my OC", grant via Claude inline, OC unblocks.

**Tests.**

- `tests/test_mcp_server_approvals.py`:
  - subscription emits notification when permission request raised.
  - `permissions_respond(decision="allow", id=X)` resolves the pending request in OC's consent queue.
  - `--enable-approvals` off: permission tools not exposed.
  - HMAC-chained audit log records the MCP-driven grant (extension of F1 audit).

**Risks.**

- Two MCP clients respond to the same permission request → first-wins, second gets `409 already_resolved`. Already handled by F1 ConsentGrant uniqueness.
- Audit log must distinguish "granted via MCP client" vs "granted via CLI" for forensics. Add `decision_source: str` to the audit record.

**MVP cut.** Just the `permissions_request_subscribe` long-poll. Notifications can be follow-up.

### Milestone 4 — Lease-counting + bundle-MCP-per-session (deferred)

(after M2 — only if M2 demand materializes)

Scope: bundle MCPs become per-session, with lease-counting. This is the full OpenClaw parity.

**Deliverables.**

1. Add `acquire_lease() -> ReleaseFn` to `MCPManager`.
2. Bundle MCP servers in session-scoped runtimes acquire lease for the duration of each tool call.
3. Idle sweep respects active leases.

**Tests.**

- Tool call holds lease until completion.
- Lease count = 0 + idle TTL elapsed → eviction.
- Lease count > 0 + idle TTL elapsed → NO eviction.

**Why deferred.** Only relevant if M2 ships and we see real multi-user usage. Don't pre-build.

### Milestone 5 — Plugin authoring docs + one reference plugin

(after M1, before public announce)

Scope: tutorial + one bundled-MCP plugin in `extensions/`.

**Deliverables.**

1. `docs/plugin-bundle-mcp.md` — how to author a plugin that ships an MCP server. Cover stdio + http transports, `${PLUGIN_ROOT}` placeholder, lazy vs eager, OSV-check override, tools_allow/deny.

2. One reference plugin in `extensions/` — pick a real one. Suggestion: an Obsidian-vault MCP plugin (writes to user's vault via MCP, useful and self-contained).

3. Update `docs/plans/README.md` to link this plan and its successor "plugin marketplace" plan (out of scope here).

**Tests.**

- Reference plugin's MCP server starts, lists its tools, executes one tool successfully. End-to-end smoke.

---

## 4. Audit the plan (attack it like a critic)

**Critique 1. "M1 still spawns subprocesses lazily — but plugins might break if the first tool call has a 5-second latency penalty for boot."**
True. Mitigation: M1 docs explicitly tell users "if you set `lazy=False`, plugin activation will wait for MCP boot." For UX-sensitive plugins, default lazy=False at the plugin level is fine. Defaults stay lazy=True because that's the right system-wide default.

**Critique 2. "Bundle MCP without bundle MCP **discoverability** is half a feature. How does a user know what's bundled?"**
Addressed in M1 deliverable 5: `oc mcp list` shows bundled servers under their plugin, and `oc mcp bundles` is the bundled-MCP-only view. Plus M5's docs.

**Critique 3. "M2's per-session runtimes are speculative — you're building infrastructure for multi-user before there's a multi-user plan."**
Valid. M2 is OPT-IN (`session_scoped=False` default). It's there as a forward-looking seam — the docs in `~/.claude/projects/-Users-saksham-Vscode-claude/memory/MEMORY.md` show every prior attempt to retrofit session-scoping into a previously-process-global subsystem has been painful. Building the seam now while the surface is small is much cheaper than retrofitting later. The cost of "infrastructure that's not used" is one extra Python file and ~200 LOC.

**Critique 4. "M3 channel-bridge approval is a security hole if not handled carefully."**
Hard agree. Mitigations baked in: opt-in CLI flag, audit-log decision_source, default deny. Plus the existing F1 consent system already enforces per-capability scoping; we're not bypassing it, just adding a new INTERFACE to it.

**Critique 5. "M5's reference plugin — is Obsidian the right choice? It implies users have Obsidian installed."**
Switch the reference to something universal. Better choice: a `~/Downloads` cleanup MCP that lists, categorizes, and offers to archive old downloads. Touches user filesystem (real test of placeholder + sanity-check), self-contained, useful to everyone.

**Critique 6. "Where does this leave the existing 19 install presets?"**
Untouched. Presets remain the path for user-installed MCPs (via `oc mcp install <preset>`). Bundle MCP is for plugin-shipped MCPs. Different lifecycle, different lifecycle owner. Both coexist.

**Critique 7. "What about OAuth for HTTP transports in bundle MCP?"**
A plugin's HTTP MCP server might need OAuth (e.g., a Salesforce MCP server). OC's existing OAuth manager (`oauth.py` + `oauth_pkce.py`) handles this. Bundle MCP just feeds into the same `MCPManager.connect()` path — auth flows are transparent.

**Critique 8. "If a plugin's bundle-MCP server's stderr is noisy, what happens?"**
M1 inherits OC's current behavior (stderr → parent). Future work: per-server stderr capture (the Hermes feature OC lacks). Not blocking. Add to a follow-up.

**Critique 9. "What's the rollout / feature flag story?"**
Bundle MCP is **on by default** in M1 — there's no risk to existing plugins (none ship bundle_mcp manifests today). Per-session runtimes are **off by default** in M2 (`MCPConfig.session_scoped=False`). Channel-bridge approvals are **off by default** in M3 (`--enable-approvals` flag required). Risk is contained to opt-ins.

**Critique 10. "Why not just adopt OpenClaw's exact `bundle-mcp.ts` design?"**
Two reasons:
1. Their `applyMergePatch` + multi-file merge logic is overkill for the single-manifest case.
2. Their `${CLAUDE_PLUGIN_ROOT}` placeholder name is non-portable. We use `${PLUGIN_ROOT}`. Plugins targeting OC also work in OpenClaw if they declare both placeholders, which is fine.

We're **adopting the architecture**, **adapting the surface**. That's the right call when porting between codebases with different conventions.

### Final plan after audit

Plan stands. The five milestones as written are correct. Specific edits from the audit:

- M5's reference plugin → `~/Downloads` cleanup MCP, not Obsidian.
- Per-server stderr capture → follow-up after M5 (not blocking).
- Decision-source audit field → M3 deliverable (was implicit, made explicit).
- Default M1 bundle MCP = on. Default M2 session-scoped = off. Default M3 approvals = off (CLI flag required).

---

## 5. Implementation contract (the part you'll actually code against)

### Files created

```
opencomputer/mcp/bundle.py                    # M1 — bundle MCP loader
opencomputer/mcp/session_runtime.py           # M2 — per-session runtimes
docs/mcp-server-approvals.md                  # M3 — approval flow docs
docs/plugin-bundle-mcp.md                     # M5 — plugin authoring docs
tests/test_mcp_bundle.py                      # M1
tests/test_mcp_session_runtime.py             # M2
tests/test_mcp_server_approvals.py            # M3
extensions/downloads-cleanup-mcp/             # M5 — reference plugin
extensions/downloads-cleanup-mcp/plugin.py
extensions/downloads-cleanup-mcp/plugin.json
extensions/downloads-cleanup-mcp/mcp_server.py
```

### Files modified

```
plugin_sdk/core.py                            # M1 — extend PluginManifest
opencomputer/mcp/client.py                    # M1 — pick up bundle registry
opencomputer/plugins/loader.py                # M1 — register bundle MCPs
opencomputer/agent/config.py                  # M2 — session_scoped flag
opencomputer/mcp/server.py                    # M3 — approvals subscribe
opencomputer/cli_mcp.py                       # M1+M2 — bundle list + sessions
```

### Wire compatibility

- Existing `MCPServerConfig` unchanged. Bundle MCPs produce `MCPServerConfig` instances internally; the `<plugin_id>__<server>__<tool>` prefix prevents collision with user-configured servers.
- Plugin manifests without `bundle_mcp` field load unchanged.
- `oc mcp list` output gains a new section but doesn't break parsers (it appends).
- New CLI subcommand `oc mcp bundles` — pure addition.
- `oc mcp sessions` — pure addition.
- `--enable-approvals` flag on `oc mcp serve` — pure addition.

### Acceptance criteria

Each milestone done when:
- All new tests pass.
- Full `pytest tests/` is green (no regressions).
- `ruff check opencomputer/ plugin_sdk/ extensions/ tests/` clean.
- One independent reviewer (you, in `--review` mode) signs off the diff.
- `oc parity-doctor run` shows OpenClaw parity score increased.

### Order of operations

1. Land M1 (bundle MCP) — ships standalone. Cuts the biggest gap.
2. Land M3 (approvals) — ships next; small, high-leverage.
3. Optionally land M5 (docs + reference plugin) before announcing M1+M3.
4. Land M2 (session runtimes) only if/when multi-user demand materializes.
5. M4 (lease-counting) is contingent on M2 actually being used in anger.

---

## 6. What we're explicitly NOT doing

- **Not rewriting `client.py`.** OC's 1,959-LOC client is good; the cross-loop dispatch is correct; the env whitelist is right. Leave it alone.
- **Not adopting OpenClaw's `applyMergePatch` for manifest merging.** Plain dict merge is fine for our case.
- **Not building a plugin marketplace.** That's a separate plan.
- **Not building per-user MCP key vaults.** That's the multi-tenant plan (`docs/plans/openhub-mvp.md`).
- **Not removing user-installed presets.** They keep working unchanged.
- **Not adopting `${CLAUDE_PLUGIN_ROOT}` placeholder name.** Using `${PLUGIN_ROOT}` because we're OC, not OpenClaw.
- **Not adding the description-injection scanner from Hermes.** Separate, smaller patch — file as a follow-up issue.

---

## 7. Verification ladder before considering this plan "done"

- [ ] Read every M1 file in this checklist back through `Grep` + `Read` after writing.
- [ ] M1 reference test: install one bundled-MCP plugin, list tools, call one, dispose plugin, verify subprocess dies within 3s.
- [ ] M3 reference test: from inside Claude Code, install OC's MCP server, ask "what pending approvals does OC have", grant one inline, verify OC unblocks.
- [ ] M2 reference test: spawn 2 `AgentLoop` instances with different session IDs, verify they have disjoint MCP subprocess sets via `ps`.
- [ ] Full `pytest tests/` green after each milestone.
- [ ] `oc parity-doctor run` delta vs baseline shows +1 to +3 shipped items.

---

## 8. Open questions (file as you hit them)

1. Does the existing `MCPManager` cleanly support being multi-instanced in one process? (M2 needs this — verify before starting M2.)
2. Are there race conditions between plugin-disable and an in-flight bundle-MCP tool call? (Test in M1.)
3. Should `oc mcp install <preset>` get a `--bundle` flag to install into a plugin instead of profile config? (Probably not — different lifecycle owners.)
4. Should bundled MCPs respect global `MCPConfig.deferred=False`? (Yes — but bundle-MCP's own `lazy` flag is the finer control.)

---

## Appendix A — Exact source-of-truth references

**OpenClaw files read (file paths in their checkout):**

- `src/mcp/channel-bridge.ts` — 560 LOC, the bidirectional approval bridge.
- `src/mcp/channel-server.ts` — 110 LOC, stdio MCP server wiring.
- `src/mcp/channel-tools.ts` — 188 LOC, 9 tools registered.
- `src/agents/pi-bundle-mcp-runtime.ts` — 645 LOC, session-scoped runtime.
- `src/agents/pi-bundle-mcp-materialize.ts` — 174 LOC, catalog → AgentTool.
- `src/agents/pi-bundle-mcp-types.ts` — 62 LOC, strict types.
- `src/agents/mcp-stdio-transport.ts` — 160 LOC, custom transport w/ killProcessTree.
- `src/agents/mcp-transport.ts` — 126 LOC, transport dispatch.
- `src/agents/mcp-transport-config.ts` — 162 LOC, config resolution.
- `src/agents/embedded-pi-mcp.ts` — 23 LOC, the embed shim.
- `src/agents/pi-bundle-mcp-names.ts` — name sanitization + collision suffix.
- `src/plugins/bundle-mcp.ts` — 295 LOC, plugin-shipped MCP loader.
- `src/config/types.mcp.ts` — 32 LOC, type defs.

**OC files read (in this repo):**

- `opencomputer/mcp/client.py` — 1,959 LOC.
- `opencomputer/mcp/server.py` — 690 LOC.
- `opencomputer/mcp/presets.py` — 448 LOC.
- `opencomputer/mcp/oauth.py` + `oauth_pkce.py` — 767 LOC combined.
- `opencomputer/mcp/osv_check.py` — 226 LOC.
- `opencomputer/mcp/sampling.py` — 130 LOC.
- `opencomputer/mcp/remote_catalog.py` — 202 LOC.
- `opencomputer/agent/config.py:806-892` — MCPServerConfig + MCPConfig.
- `plugin_sdk/core.py:401-412` — existing PluginManifest.mcp_servers field.

---

End of plan.
