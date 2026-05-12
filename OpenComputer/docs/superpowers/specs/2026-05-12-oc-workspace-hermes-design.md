# `oc workspace` — Hermes Workspace as a First-Class OC Surface

**Date:** 2026-05-12
**Status:** approved-in-chat → implementing
**Author:** Saksham + Claude (Opus 4.7)

## 1. Intent

Add `oc workspace` as a new CLI command that launches **hermes-workspace** (a Node.js/React SSR app, MIT, by outsourc-e) as a sibling to the existing `oc webui`. The existing `oc webui` stays untouched. `oc workspace` becomes the recommended browser surface for users who want the richer UI: chat, sessions, memory, skills, MCP, terminal, kanban, swarm orchestration.

Production-grade ship: every wire end-to-end, no stubs, no placeholders, no silent fallbacks.

## 2. Why two browser surfaces instead of replacing one

`oc webui` is a Python HTTP server with a tight `_oc_shim/` bridge that aliases Hermes Python modules to OC modules at runtime. It is mature, well-tested (746-line shim test), and ships today.

`oc workspace` is a Node SSR app that:
- Has a richer feature surface (Swarm Mode, Operations, Agent View, Monaco editor, xterm terminal)
- Talks to its backend over HTTP, not Python module aliasing
- Pairs with vanilla upstream Hermes Agent — and with us via an OpenAI-compatible compat layer

Different shape, different audience. Both work. User picks.

## 3. Architecture

```
   user (browser)
        │
        ▼
   http://127.0.0.1:3000             ← hermes-workspace Node SSR
   (chat UI, sessions, memory, etc.)
        │
        │  (HERMES_API_URL / fetch)
        ▼
   http://127.0.0.1:9119             ← OC dashboard FastAPI (existing)
   ├── /v1/chat/completions          ← NEW — OpenAI-compat SSE
   ├── /v1/models                    ← NEW — OpenAI-compat model list
   ├── /v1/health                    ← NEW — OpenAI-compat health
   ├── /api/v1/sessions/...          ← existing (workspace can read)
   ├── /api/v1/skills/...            ← existing
   └── ...
        │
        ▼
   AgentLoop.run_conversation()      ← OC core
        │
        ▼
   provider plugins (anthropic/openai/...)
```

`oc workspace` orchestrates **two processes**:
1. The OC dashboard server (existing `DashboardServer.start()` — in-process thread)
2. The hermes-workspace Node server (`node server-entry.js` — subprocess)

The workspace receives the dashboard's ephemeral session token via `HERMES_API_TOKEN`. All chat traffic from the workspace lands on `POST /v1/chat/completions`, which drives `AgentLoop` and streams OpenAI-format deltas back as SSE.

## 4. Components to build

### 4.1 `opencomputer/dashboard/routes/openai_compat.py` (new)

OpenAI-compatible HTTP endpoints under the `/v1` prefix (separate from `/api/v1` which is OC's native format).

| Endpoint | Method | Behaviour |
|---|---|---|
| `/v1/health` | GET | `{"status": "ok", "version": <oc-version>}`. Public. |
| `/v1/models` | GET | OpenAI list shape: `{"object": "list", "data": [{"id": str, "object": "model", "owned_by": str}]}`. Aggregates models from every loaded provider via `ProviderRegistry`. Public. |
| `/v1/chat/completions` | POST | OpenAI chat-completions shape. Bearer-token gated (dashboard session token). Streams when `stream: true`. Backed by `AgentLoop`. |

**Streaming format** (`stream: true`):
- `Content-Type: text/event-stream`
- One `data: {...}\n\n` per chunk
- Each chunk shape: `{"id": str, "object": "chat.completion.chunk", "created": int, "model": str, "choices": [{"index": 0, "delta": {"content": str}, "finish_reason": null | "stop"}]}`
- Final chunk has `delta: {}` and `finish_reason: "stop"`, then `data: [DONE]\n\n` sentinel.

**Non-streaming format**: standard `chat.completion` object with a single choice.

**Conversation handling**: each call is treated as a one-shot. The `messages[]` array is used as the prior transcript (system + user + assistant pairs), and the LAST user message drives a fresh `AgentLoop.run_conversation()` call. Sessions are NOT persisted by default — hermes-workspace already manages its own conversation history client-side. Optional: a custom `oc_session_id` extension field can reuse an existing OC session if present.

**Tool calls**: scoped OUT of v1. The workspace will see the agent's FINAL text response. Tool calls happen invisibly during AgentLoop execution; their UI rendering in workspace is deferred to a follow-up (would require translating OC `tool_use` blocks to OpenAI `tool_calls` deltas).

**Auth**: same Bearer-token model as the rest of the dashboard. Token sourced from `app.state.session_token`.

**Error model**: OpenAI-style error envelope:
```json
{"error": {"message": "...", "type": "invalid_request_error", "code": "..."}}
```
HTTP status codes match OpenAI conventions (400, 401, 404, 500, 503).

### 4.2 `opencomputer/workspace/` (new package)

```
opencomputer/workspace/
├── __init__.py
├── discovery.py     ← locate the hermes-workspace dir on disk
├── prerequisites.py ← detect node, pnpm, version checks
├── builder.py       ← run `pnpm install` + `pnpm build` with caching
├── launcher.py      ← spawn `node server-entry.js` + lifecycle
└── lifecycle.py     ← coordinate dashboard + workspace boot order
```

**Discovery order** (mirrors `oc webui`):
1. `--workspace-dir` CLI flag
2. `$OC_WORKSPACE_DIR` env var
3. `<profile_home>/workspace/`
4. `~/.opencomputer/workspace/`
5. `/Users/saksham/Vscode/claude/sources/hermes-workspace` (dev-only sibling)
6. Fail loud with searched paths

**Prerequisites**: node ≥ 22, pnpm ≥ 9. Clear remediation hints on failure.

**Build cache**: skip `pnpm install` + `pnpm build` when `dist/server/server.js` and `node_modules/` are both present AND newer than `package.json`. Force rebuild via `oc workspace build --force`.

**Launcher**: `subprocess.Popen(["node", "server-entry.js"], env=enriched_env, cwd=workspace_dir)`. Env enrichment:
- `HERMES_API_URL=http://127.0.0.1:<dashboard_port>`
- `HERMES_API_TOKEN=<dashboard_session_token>`
- `PORT=<workspace_port>` (default 3000)
- `HOST=127.0.0.1`
- `NODE_ENV=production`
- `OPENCOMPUTER_HOME=<profile_home>` (for any future Python helpers)

**Lifecycle**:
1. Resolve workspace dir (fail loud if missing)
2. Check prerequisites (fail loud if missing node/pnpm)
3. Run build if needed (cached)
4. Start OC dashboard in background thread on `:9119` (or chosen port). Capture session token.
5. Health-check `/api/health` until 200 OK or 30s timeout
6. Spawn workspace Node subprocess
7. Health-check `http://127.0.0.1:<workspace_port>/` until 200 OK or 60s timeout
8. Open browser (unless `--no-browser`)
9. Block on subprocess; forward SIGINT/SIGTERM to clean both down
10. On exit, stop dashboard thread

### 4.3 `opencomputer/cli_workspace.py` (new)

Four subcommands under `oc workspace`:

| Command | Purpose |
|---|---|
| `oc workspace` (bare) / `oc workspace run` | Launch dashboard + workspace, open browser |
| `oc workspace install <repo-url>` | Clone hermes-workspace into `~/.opencomputer/workspace/` |
| `oc workspace build [--force]` | Run `pnpm install` + `pnpm build` |
| `oc workspace doctor` | Print prerequisite status, paths resolved, build state |

Flags on `run`:
- `--host` (default 127.0.0.1)
- `--port` (default 3000)
- `--dashboard-port` (default 9119)
- `--no-browser` (skip auto-open)
- `--foreground` (run in-process, no detach)
- `--workspace-dir <path>` (override discovery)

## 5. Env-var contract

| Var | Purpose | Default |
|---|---|---|
| `OC_WORKSPACE_DIR` | Override discovery | (unset; uses search order) |
| `OC_WORKSPACE_PORT` | Workspace bind port | 3000 |
| `OC_WORKSPACE_HOST` | Workspace bind host | 127.0.0.1 |
| `OC_DASHBOARD_PORT` | Backend dashboard port | 9119 |
| `HERMES_API_URL` | (set by launcher) | computed |
| `HERMES_API_TOKEN` | (set by launcher) | computed |

## 6. Failure modes — what happens when X breaks

| Failure | Behaviour |
|---|---|
| node missing | `oc workspace` exits with code 1, prints install link. `oc workspace doctor` shows `node: MISSING`. |
| pnpm missing | Same: exit 1, install link. |
| node version < 22 | Exit 1, version mismatch error. |
| Workspace dir not found | List all searched paths; suggest `oc workspace install`. |
| Build fails | Surface pnpm stderr verbatim; exit with pnpm's exit code. |
| Dashboard fails to start | Exit 1 with the captured uvicorn error. Don't launch Node. |
| Workspace Node process exits non-zero before health-check | Capture stderr, print, exit with Node's exit code. |
| User Ctrl+C | SIGINT propagates: kill Node child cleanly (5s grace + SIGKILL), stop dashboard thread, exit 130. |
| Provider with no API key configured | `/v1/chat/completions` returns 503 with OpenAI error envelope: `{error: {type: "provider_unavailable"}}`. |
| Malformed request body | 400 with OpenAI error envelope `{error: {type: "invalid_request_error"}}`. |
| Missing `messages` field | 400. |
| Empty `messages` array | 400. |
| Unknown model | 404 OpenAI error envelope. |
| AgentLoop raises mid-stream | SSE channel sends an `error` chunk then closes; HTTP status is already 200, so error is in-band. |

## 7. Security posture

- Default bind is `127.0.0.1` for both dashboard AND workspace. Non-loopback binds require explicit `--host` flag.
- `/v1/chat/completions` requires Bearer auth (dashboard session token).
- `/v1/health` and `/v1/models` are public (matches OpenAI behaviour for `/v1/models`).
- The session token is passed to the Node subprocess via env var (NEVER on the command line, NEVER logged).
- The OpenAI-compat routes log at `INFO` for request method + path + status, `WARN` on errors, `ERROR` on internal failures. No prompt content logged.
- No shell injection: subprocess uses argv list, not `shell=True`. No string interpolation of user input into env or argv.

## 8. Testing

`tests/test_dashboard_openai_compat.py`:
- GET `/v1/health` → 200, shape
- GET `/v1/models` → 200, OpenAI list shape, contains expected provider models
- POST `/v1/chat/completions` (stream=false) → 200, OpenAI completion shape
- POST `/v1/chat/completions` (stream=true) → SSE with `data:` prefixed JSON chunks, final `[DONE]` sentinel
- POST with missing `messages` → 400 OpenAI error envelope
- POST with empty `messages` → 400
- POST with unknown model → 404
- POST without Bearer token → 401
- POST with wrong token → 401
- POST when AgentLoop raises → in-band SSE error chunk
- Adversarial: 10MB body → 413; non-JSON body → 400; `messages: null` → 400

`tests/test_workspace_discovery.py`:
- `--workspace-dir` valid → resolves
- `--workspace-dir` invalid → exits 1, lists searched paths
- `$OC_WORKSPACE_DIR` honored
- Discovery order: profile-local → global → sources/ fallback
- Symlink target resolution

`tests/test_workspace_prerequisites.py`:
- node missing → returns missing-detail dataclass
- node < 22 → version-fail detail
- pnpm missing → detail
- All present → ok=True

`tests/test_workspace_builder.py`:
- Build cache: skip when dist/ + node_modules/ newer than package.json
- Force rebuild flag
- pnpm failure surfaces exit code

`tests/test_workspace_launcher.py`:
- Env-var enrichment includes HERMES_API_URL, HERMES_API_TOKEN, PORT
- Token never appears in argv
- SIGINT cleanup

`tests/test_cli_workspace.py`:
- `oc workspace --help`
- `oc workspace doctor` prints status
- `oc workspace install` clones (mock subprocess.run)
- `oc workspace build` invokes pnpm (mock)

## 9. Out of scope (followups)

- Tool-call rendering in workspace (OpenAI `tool_calls` translation)
- `/api/sessions` shape parity with hermes-agent dashboard (workspace can read existing OC `/api/v1/sessions` but the response shapes differ; mapping is a follow-up)
- Swarm Mode wiring (requires hermes-agent's swarm endpoints; defer)
- Workspace's `/conductor` endpoint (placeholder behaviour upstream; defer)
- Electron desktop build (`pnpm electron:build`)

## 10. Migration / coexistence

- `oc webui` unchanged — same CLI, same code, same tests.
- `oc workspace` is additive.
- Both can run simultaneously on different ports (`:8787` and `:3000`).
- Both read the same OC profile via `OPENCOMPUTER_HOME`.

## 11. Open questions resolved

- **Q: Vendor hermes-workspace into the OC wheel?**
  A: No. ~500MB of node_modules, MIT but better managed via clone + `oc workspace install`. Discovery order accommodates dev (sources/), per-profile install, and global install.
- **Q: Auto-launch dashboard?**
  A: Yes — `oc workspace` ensures the dashboard is running before launching the Node server. If the user already has `oc dashboard` running, `oc workspace` reuses it (port collision detection skips start).
- **Q: Build artifacts in git?**
  A: No. `dist/` is built on first `oc workspace run` and cached locally.
