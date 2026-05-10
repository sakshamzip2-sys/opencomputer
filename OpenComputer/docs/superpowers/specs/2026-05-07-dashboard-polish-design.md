# Dashboard + TUI Full Port (Hermes-shape, OC-functionality) — Design Spec

**Date:** 2026-05-07
**Driver docs:** `docs/refs/hermes-agent/2026-05-06-deep-comparison.md` Tier A1 + the user's 2026-05-07 directive: ship full Hermes parity (12 pages, ~67 routes) AND port the Hermes Ink+React TUI shell wired to OC's existing functionality.
**Companion plan:** `docs/superpowers/plans/2026-05-07-dashboard-polish.md`
**Worktree:** `.claude/worktrees/dashboard-polish-2026-05-07` on `feat/dashboard-polish-2026-05-07`. Zero file overlap with the parallel `pr-a-steer-wake-acp` worktree (verified — that work touches `agent/`, `voice/`, `acp/`; this work touches `dashboard/`, `ui-web/`, `ui-tui/`, plus additive entries to `gateway/wire_server.py` + `gateway/protocol_v2.py`).

---

## 0. Scope (corrected after user direction on 2026-05-07)

The user explicitly directed:

1. **Full 12-page Hermes parity** for the dashboard — not the 5-page YAGNI cut.
2. **Match Hermes's route set** (~67 routes) — not the 14-route minimum.
3. **Track B (Ink+React TUI) IS in scope** — but with the constraint "use Hermes's shell, keep OC's functionality." Vendor `hermes-agent/ui-tui/` + `packages/hermes-ink/` source, then re-wire every action to dispatch into OC's existing wire_server / slash commands / tools / sessions / skills hub. The visible TUI looks like Hermes; every action is OC's.

This pivots the prior 5-page minimum-viable v1 (~7.5d) to a comprehensive port (~27d, 11 PRs). Trade-off accepted because (a) the deep-comparison doc identifies dashboard polish as the only major Hermes-shaped gap, (b) TUI port is what the user requested, (c) the parallel `pr-a-steer-wake-acp` session is already shipping operational hardening (S1/S2/S3) so we don't deprioritize that.

---

## 1. Ground truth

### 1.1 OC dashboard today

| File | LOC | Notes |
|---|---|---|
| `opencomputer/dashboard/server.py` | 505 | FastAPI host + SPA shell + PTY-WS + plugin router auto-discover + ephemeral session token + loopback gate + CSP headers. Pattern is sound. |
| `opencomputer/dashboard/_auth.py` | 51 | Token check, used when bound non-loopback. Reuse. |
| `opencomputer/dashboard/_sse.py` | 126 | SSE encoder + mtime-watch helper. Reuse. |
| `opencomputer/dashboard/pty_bridge.py` | 233 | POSIX-only PTY bridge. Don't break. |
| `opencomputer/dashboard/plugins/{kanban,management,models}/plugin_api.py` | — | Existing dashboard-plugin router pattern (kanban has 17 routes already). Pattern to mimic, not break. |
| `opencomputer/dashboard/static/{index,plugins,models,llm-calls}.html` | — | Stub static pages. Will be superseded by Vite SPA mounted at `/`; legacy paths get HTTP redirects to SPA equivalents. |
| `opencomputer/cli_dashboard.py` | 98 | `oc dashboard --host --port --wire-url`, defaults `127.0.0.1:9119` + `ws://127.0.0.1:18789`. Already shipped. |

Existing dashboard tests live at flat `OpenComputer/tests/test_dashboard_*.py` (NOT in `tests/dashboard/` subdir). New tests follow the same flat layout — `test_dashboard_routes_<domain>.py`.

### 1.2 OC gateway / wire server

`opencomputer/gateway/`:
- `wire_server.py` — WS JSON-RPC at `ws://127.0.0.1:18789`. Existing methods: `hello`, `chat`, `sessions.list`, `search`, `skills.list`, `steer.submit`. Streams events: `turn.begin`, `assistant_message`, `error`, `turn.end`.
- `protocol_v2.py` — Pydantic-typed METHOD constants + WireRequest/WireResponse/WireEvent. Per-method param schemas.

Architecture rule (load-bearing): dashboard does NOT proxy wire. Browser opens its own WS to port 18789 for live chat. Dashboard's REST surface is read-mostly state.

### 1.3 OC's existing slash command + cli_ui infrastructure

- `opencomputer/agent/slash_commands_impl/` — 30+ commands (`/auto`, `/bell`, `/branch`, `/btw`, `/capabilities`, `/copy`, `/display_toggles`, `/fast`, `/history`, `/mode`, `/persona_mode`, `/platforms`, `/profile_suggest`, `/queue_mode`, `/reasoning`, `/save`, `/scrape`, plus more).
- `opencomputer/cli_ui/` — prompt_toolkit-based input loop, slash completer, reasoning store/view, paste folder, file completer, ask-user-question handler.
- The TUI port dispatches through OC's same slash registry — single source of truth for slash semantics.

### 1.4 Hermes web/ frontend reference

`/Users/saksham/.hermes/hermes-agent/web/`:
- React 19 + Vite + Tailwind v4 + `@nous-research/ui@^0.10` + `lucide-react` + react-router v7 + `@xterm/xterm` + `@react-three/fiber` + `@observablehq/plot`.
- 12 pages: Analytics, Chat, Config, Cron, Docs, Env, Logs, Models, Plugins, Profiles, Sessions, Skills.
- i18n EN+ZH, JS-side dashboard-plugin tab registry.

`@nous-research/ui` IS public on npm (latest 0.12.0, verified `npm view @nous-research/ui dist-tags`). Hermes's `package.json:6` does `npm run sync-assets` predev/prebuild to copy fonts + ds-assets out of node_modules/. Pattern reused verbatim.

### 1.5 Hermes ui-tui reference

`/Users/saksham/.hermes/hermes-agent/ui-tui/`:
- React 19 + Ink 6 + `@hermes/ink` (workspace fork at `./packages/hermes-ink`).
- 21 components in `src/components/`: agentsOverlay, appChrome, appLayout, appOverlays, branding, fpsOverlay, helpHint, markdown, maskedPrompt, messageLine, modelPicker, overlayControls, prompts, queuedMessages, sessionPicker, skillsHub, streamingAssistant, streamingMarkdown, textInput, themed, thinking, todoPanel.
- `entry.tsx` + `app.tsx` + `gatewayClient.ts` (stdio JSON-RPC).
- Build: tsc → babel-react-compiler → chmod +x dist/entry.js.

`@hermes/ink` is NOT on public npm (verified `npm view @hermes/ink` → 404). It's a `file:./packages/hermes-ink` workspace dep. We vendor `packages/hermes-ink/` as well, renamed to `oc-ink`.

---

## 2. Track A — Dashboard (12 pages, ~58 new routes)

### 2.1 Architecture

```
                          OC process tree
┌──────────────────────────────────────────────────────────┐
│  oc dashboard (cli_dashboard.py — already shipped)       │
│    ↓                                                     │
│  DashboardServer (uvicorn @ 9119, FastAPI)               │
│    ├─ /                  → static/spa/index.html         │
│    ├─ /assets/*          → static/spa/assets/*           │
│    ├─ /static/*          → existing legacy HTML pages    │
│    ├─ /api/health        → existing                      │
│    ├─ /api/llm-calls/recent → existing                   │
│    ├─ /api/gateway/restart  → existing                   │
│    ├─ /api/pty           → existing PTY-WS bridge        │
│    ├─ /api/plugins/<n>/* → existing dashboard plugins    │
│    └─ /api/v1/*          → NEW: ~58 routes + 2 SSE       │
│                                                          │
│  WireServer (websockets @ 18789)                         │
│    └─ JSON-RPC: existing 6 methods + 2 NEW               │
│       (slash.list, slash.dispatch — used by both         │
│        dashboard ChatPage and TUI for slash palette)     │
└──────────────────────────────────────────────────────────┘

                          Browser
┌──────────────────────────────────────────────────────────┐
│  React SPA at http://127.0.0.1:9119                      │
│   ├─ REST → /api/v1/* (read-most + write-some)            │
│   ├─ EventSource → /api/v1/events + /api/v1/logs (SSE)    │
│   ├─ WS → ws://127.0.0.1:18789 (live chat — ChatPage)     │
│   └─ WS → /api/pty (terminal embed where used)            │
└──────────────────────────────────────────────────────────┘
```

### 2.2 Frontend stack — Hermes look matched

```jsonc
{
  "dependencies": {
    "@nous-research/ui": "^0.12.0",
    "@observablehq/plot": "^0.6.17",
    "@xterm/xterm": "^6.0.0",
    "@xterm/addon-fit": "^0.11.0",
    "@xterm/addon-unicode11": "^0.9.0",
    "@xterm/addon-web-links": "^0.12.0",
    "@xterm/addon-webgl": "^0.19.0",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "lucide-react": "^0.577.0",
    "react": "^19.2.4",
    "react-dom": "^19.2.4",
    "react-router-dom": "^7.14.1",
    "tailwind-merge": "^3.5.0",
    "tailwindcss": "^4.2.1",
    "@tailwindcss/vite": "^4.2.1"
  }
}
```

Drops vs Hermes: `@react-three/fiber`, `gsap`, `unicode-animations`, `leva` — flourish, not load-bearing. YAGNI.

`sync-assets` script copied verbatim from Hermes for the @nous-research/ui font + ds-assets bundling.

### 2.3 Frontend layout — `OpenComputer/ui-web/`

```
OpenComputer/ui-web/
├── package.json
├── vite.config.ts                  # builds to ../opencomputer/dashboard/static/spa/
├── tsconfig.{json,app.json,node.json}
├── eslint.config.js
├── index.html
├── public/
│   ├── fonts/                      # synced from @nous-research/ui at build time
│   └── ds-assets/                  # synced ditto
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── index.css                   # Tailwind v4 import + theme @theme block
    ├── i18n/
    │   ├── context.tsx
    │   ├── en.ts
    │   ├── zh.ts
    │   ├── index.ts
    │   └── types.ts
    ├── lib/
    │   ├── api.ts                  # typed REST client; reads token from <meta>
    │   ├── wire.ts                 # WS JSON-RPC client to wire server
    │   ├── events.ts               # EventSource helper
    │   └── theme.ts                # CSS-var theme tokens
    ├── hooks/
    │   ├── useApi.ts
    │   ├── useWire.ts              # connect ws://127.0.0.1:18789 with reconnect
    │   ├── useEvent.ts             # SSE subscription
    │   ├── useStatus.ts
    │   └── useTheme.ts
    ├── contexts/
    │   ├── SystemActionsContext.tsx
    │   ├── PageHeaderProvider.tsx
    │   └── ToastProvider.tsx
    ├── components/
    │   ├── Sidebar.tsx
    │   ├── ChatSidebar.tsx
    │   ├── SidebarFooter.tsx
    │   ├── SidebarStatusStrip.tsx
    │   ├── StatusBar.tsx
    │   ├── ConnectionIndicator.tsx
    │   ├── ThemeSwitcher.tsx
    │   ├── LanguageSwitcher.tsx
    │   ├── Markdown.tsx
    │   ├── ToolCall.tsx
    │   ├── SlashPopover.tsx
    │   ├── ModelPickerDialog.tsx
    │   ├── OAuthLoginModal.tsx
    │   ├── OAuthProvidersCard.tsx
    │   ├── ModelInfoCard.tsx
    │   ├── PlatformsCard.tsx
    │   ├── DeleteConfirmDialog.tsx
    │   ├── AutoField.tsx
    │   ├── NouiTypography.tsx
    │   ├── Toast.tsx
    │   ├── Backdrop.tsx
    │   └── Terminal.tsx            # xterm.js wrapper for PTY embed
    ├── pages/
    │   ├── ChatPage.tsx
    │   ├── SessionsPage.tsx
    │   ├── SkillsPage.tsx
    │   ├── PluginsPage.tsx
    │   ├── CronPage.tsx
    │   ├── LogsPage.tsx
    │   ├── ModelsPage.tsx
    │   ├── ProfilesPage.tsx
    │   ├── EnvPage.tsx
    │   ├── ConfigPage.tsx
    │   ├── AnalyticsPage.tsx
    │   └── DocsPage.tsx
    └── plugins/
        ├── PluginPage.tsx
        ├── registry.ts
        ├── slots.ts
        ├── types.ts
        ├── usePlugins.ts
        └── index.ts
```

Build artifact lives at `OpenComputer/opencomputer/dashboard/static/spa/`, shipped in the wheel via hatch package_data.

### 2.4 Backend layout — `opencomputer/dashboard/routes/`

```
opencomputer/dashboard/routes/
├── __init__.py             # ALL_ROUTERS list
├── _common.py              # clamp_limit, get_session_db, audit_log helpers
├── _auth_dep.py            # FastAPI Depends wrappers (loopback bypass + token + confirm header)
├── status.py               # 1 route
├── sessions.py             # 5 routes
├── logs.py                 # SSE
├── models.py               # 4 routes
├── providers_oauth.py      # 5 routes
├── profiles.py             # 7 routes
├── skills.py               # 3 routes
├── plugins.py              # 5 routes
├── cron.py                 # 8 routes
├── config.py               # 6 routes
├── env.py                  # 4 routes (reveal requires X-OC-Confirm: yes + audit)
├── analytics.py            # 3 routes
├── tools.py                # 1 route
├── dashboard_meta.py       # 4 routes (themes / dashboard plugins)
├── oc_update.py            # 2 routes
├── actions.py              # 1 route
└── events.py               # SSE multiplex over TypedEventBus
```

### 2.5 Route inventory (close to Hermes's 67)

**Status (1):** GET `/api/v1/status` → profile + wire URL + version

**Sessions (5):**
- GET `/api/v1/sessions?limit&offset&channel` → `SessionDB.list_sessions(limit)`
- GET `/api/v1/sessions/{id}` → `SessionDB.get_session(id)`
- GET `/api/v1/sessions/{id}/messages?since_seq&limit` → `SessionDB.get_messages(id, limit)`
- GET `/api/v1/sessions/search?q&limit` → `SessionDB.search_messages(q, limit)`
- DELETE `/api/v1/sessions/{id}` (consent-gated)

**Logs (1 SSE):** GET `/api/v1/logs?level&since` → ring-buffer log handler → SSE

**Models (4):**
- GET `/api/v1/models` → `cli_models.list_providers_with_models()`
- GET `/api/v1/models/info` → current
- GET `/api/v1/models/auxiliary` → aux client config
- POST `/api/v1/models/set` → `cli_model_picker.set_default(provider, model)`

**Providers OAuth (5):**
- GET `/api/v1/providers/oauth` → list provider OAuth state
- POST `/api/v1/providers/oauth/{id}/start` → kick off; returns session_id
- POST `/api/v1/providers/oauth/{id}/submit` → finalize with code
- GET `/api/v1/providers/oauth/{id}/poll/{session_id}` → poll
- DELETE `/api/v1/providers/oauth/{id}` → revoke
- (DELETE `/api/v1/providers/oauth/sessions/{session_id}` to cancel a flow)

**Profiles (7):**
- GET `/api/v1/profiles` → list
- POST `/api/v1/profiles` → create
- DELETE `/api/v1/profiles/{name}` → delete
- GET `/api/v1/profiles/{name}/setup-command` → returns `eval "$(oc setup --profile {name})"` snippet
- POST `/api/v1/profiles/{name}/open-terminal` → spawn terminal pinned to that profile (loopback-only, consent)
- GET/PUT `/api/v1/profiles/{name}/persona` (Hermes calls it `/soul`; OC name aligned)

**Skills (3):**
- GET `/api/v1/skills` → list
- PUT `/api/v1/skills/toggle` → enable/disable
- GET `/api/v1/skills/{name}` → details

**Plugins (5):**
- GET `/api/v1/plugins` → list
- POST `/api/v1/plugins/{name}/enable`
- POST `/api/v1/plugins/{name}/disable`
- POST `/api/v1/plugins/install` → install from URL/path
- POST `/api/v1/plugins/dashboard/install` → dashboard-side plugin install

**Cron (8):**
- GET `/api/v1/cron/jobs`, GET `/api/v1/cron/jobs/{id}`, POST, PUT, POST `/jobs/{id}/{pause,resume,trigger}`, DELETE.

**Config (6):**
- GET/PUT `/api/v1/config`
- GET/PUT `/api/v1/config/raw`
- GET `/api/v1/config/defaults`
- GET `/api/v1/config/schema`

**Env (4):**
- GET `/api/v1/env` (keys + redacted hints `(N chars)`)
- PUT `/api/v1/env`
- DELETE `/api/v1/env?key=X`
- POST `/api/v1/env/reveal` (consent-gated, audit-logged, requires `X-OC-Confirm: yes`)

**Analytics (3):**
- GET `/api/v1/analytics/usage?days=30`
- GET `/api/v1/analytics/models?days=30`
- GET `/api/v1/analytics/tools?days=30`

**Tools (1):** GET `/api/v1/tools/toolsets`

**Dashboard meta (4):** GET/PUT `/api/v1/dashboard/themes`; GET `/api/v1/dashboard/plugins`; POST `/api/v1/dashboard/plugins/rescan`

**OC update (2):** POST `/api/v1/oc/update`; GET `/api/v1/oc/version`

**Actions (1):** GET `/api/v1/actions/{name}/status` (poll long-running ops)

**Events (1 SSE):** GET `/api/v1/events?topics=glob` — multiplex `TypedEventBus`

**TOTAL NEW: 58 + 2 SSE = 60 endpoints.** Adding existing 8 ≈ 68 ≈ Hermes's 67.

### 2.6 Page → API mapping (12 pages)

| Page | Reads | Mutates | Live |
|---|---|---|---|
| ChatPage | wire `chat`, `/api/v1/sessions` | wire `chat`, wire `steer.submit` | WS to wire |
| SessionsPage | `/api/v1/sessions{,/{id},/messages,/search}` | DELETE | none |
| SkillsPage | `/api/v1/skills` | `/skills/toggle` | events `skills.changed` |
| PluginsPage | `/api/v1/plugins` + `/api/v1/dashboard/plugins` | enable/disable/install | events `plugins.changed` |
| CronPage | `/api/v1/cron/jobs` | full CRUD | events `cron.changed` |
| LogsPage | `/api/v1/logs` (SSE) | none | SSE |
| ModelsPage | `/api/v1/models{,/info,/auxiliary}` + OAuth list | `/models/set` + OAuth flow | none |
| ProfilesPage | `/api/v1/profiles` | create/delete/persona/setup-command/open-terminal | events `profile.changed` |
| EnvPage | `/api/v1/env` | put/delete/reveal | none |
| ConfigPage | `/api/v1/config{,/raw,/defaults,/schema}` | put/put-raw | none |
| AnalyticsPage | `/api/v1/analytics/*` | none | events `usage.tick` |
| DocsPage | renders bundled markdown (CLAUDE.md / README / extension READMEs) | none | none |

### 2.7 Auth, CSP, theming, i18n

- **Auth:** existing session-token; SPA reads `<meta name="oc-session-token">`. Loopback bypass; non-loopback enforces. Reveal endpoints require `X-OC-Confirm: yes` header.
- **CSP:** existing CSP allows `connect-src 'self' ws://127.0.0.1:* wss://127.0.0.1:* …` — works for ChatPage's WS to port 18789. ✅
- **Theming:** CSS-var-driven; default = Nous's dark theme; switcher writes profile config.
- **i18n:** EN + ZH (mirror Hermes); switcher persists to `localStorage` + profile config.
- **Plugin tab registry:** stub structure ships; v2 wires backend hooks.

---

## 3. Track B — TUI port (Hermes shell, OC backend)

### 3.1 Architecture rule

Saksham: "let's take theirs only right just keep the same functioning of what ours do." Translation:

- **Visuals + interaction model = Hermes.** Vendor `ui-tui/src/*` and `ui-tui/packages/hermes-ink/*` from Hermes verbatim, then adjust import paths + branding only.
- **All actions wire into OC.** Replace Hermes's `gatewayClient.ts` (stdio JSON-RPC) with a WS client speaking OC's `protocol_v2`. Replace Hermes-specific menu items with OC's. Replace Hermes branding with OC's banner art.

### 3.2 Layout — `OpenComputer/ui-tui/`

```
OpenComputer/ui-tui/
├── package.json                # vendored, name = "oc-tui"
├── tsconfig.{json,build.json}
├── babel.compiler.config.cjs
├── vitest.config.ts
├── eslint.config.mjs
├── packages/oc-ink/            # vendored from packages/hermes-ink, renamed
│   ├── package.json
│   └── src/                    # forked Ink components
└── src/
    ├── entry.tsx               # vendored, Hermes-specific code stripped
    ├── app.tsx                 # vendored
    ├── theme.ts                # rebrand to OC palette
    ├── gatewayClient.ts        # REWRITTEN — WS client to ws://127.0.0.1:18789
    ├── components/             # all 21 vendored:
    │   ├── agentsOverlay.tsx
    │   ├── appChrome.tsx
    │   ├── appLayout.tsx
    │   ├── appOverlays.tsx
    │   ├── branding.tsx        # rebranded to OC banner ASCII
    │   ├── fpsOverlay.tsx
    │   ├── helpHint.tsx
    │   ├── markdown.tsx
    │   ├── maskedPrompt.tsx
    │   ├── messageLine.tsx
    │   ├── modelPicker.tsx     # reads OC models via REST
    │   ├── overlayControls.tsx
    │   ├── prompts.tsx
    │   ├── queuedMessages.tsx
    │   ├── sessionPicker.tsx   # reads OC sessions via wire `sessions.list`
    │   ├── skillsHub.tsx       # reads OC skills via wire `skills.list`
    │   ├── streamingAssistant.tsx
    │   ├── streamingMarkdown.tsx
    │   ├── textInput.tsx
    │   ├── themed.tsx
    │   ├── thinking.tsx
    │   └── todoPanel.tsx
    └── hooks/
```

### 3.3 Wire client rewrite

Hermes's stdio JSON-RPC → OC's WebSocket JSON-RPC. New `gatewayClient.ts`:

```ts
class OCWireClient {
  ws: WebSocket;
  pending: Map<string, {resolve, reject}>;
  events: EventEmitter;

  async hello(): Promise<HelloResult> { return this.call("hello", {}); }
  async chat(msg: string, sid?: string): Promise<void> { /* streams via events */ }
  async sessionsList(limit=50): Promise<Session[]> { ... }
  async search(q: string): Promise<MessageRow[]> { ... }
  async skillsList(): Promise<Skill[]> { ... }
  async slashList(): Promise<SlashCommand[]> { return (await this.call("slash.list", {})).commands; }
  async slashDispatch(name: string, args: string): Promise<SlashResult> { ... }
  async steerSubmit(text: string): Promise<void> { ... }

  // For non-wire reads, fall back to dashboard REST at /api/v1/*
  async modelsList() { return fetch("http://127.0.0.1:9119/api/v1/models").then(r => r.json()); }
}
```

Reconnect: exponential backoff (1s, 2s, 4s, 8s, max 30s). On reconnect, re-issue `hello` to restore session capability flags.

### 3.4 New wire methods

Add to `gateway/protocol_v2.py` METHODS + `wire_server.py` dispatch:

- `slash.list` → returns `{commands: [{name, description, aliases}]}` from OC's slash registry.
- `slash.dispatch` → body `{name, args, session_id}` → invokes OC's slash dispatcher → `{output, side_effects}`.

Both reusable by dashboard ChatPage.

### 3.5 CLI flag

New `opencomputer/cli_tui.py`:

```python
@tui_app.callback(invoke_without_command=True)
def run(
    wire_url: str = "ws://127.0.0.1:18789",
    dashboard_url: str = "http://127.0.0.1:9119",
) -> None:
    import importlib.resources, os
    entry = importlib.resources.files("opencomputer").joinpath("ui-tui/dist/entry.js")
    env = os.environ.copy()
    env["OC_WIRE_URL"] = wire_url
    env["OC_DASHBOARD_URL"] = dashboard_url
    os.execvpe("node", ["node", str(entry)], env)
```

`oc tui` boots the Ink TUI against the running wire server + dashboard.

### 3.6 Build pipeline

Mirror Hermes:
```
tsc -p tsconfig.build.json && \
  babel dist --out-dir dist --config-file ./babel.compiler.config.cjs --extensions .js --keep-file-extension && \
  chmod +x dist/entry.js
```

CI runs `cd OpenComputer/ui-tui && npm ci && npm run build`. Wheel ships `opencomputer/ui-tui/dist/` via hatch `force-include`.

### 3.7 Branding & licensing

- Hermes source license verified before vendoring (Phase 0 task).
- Keep their copyright headers; add `// Adapted for OpenComputer 2026-05-07 (oc-tui)` to each modified file.
- Banner: replace Hermes brand strings with OC's `cli_banner_art.py` ASCII.
- `package.json` name: `oc-tui`. `@hermes/ink` workspace dep renamed to `oc-ink`.
- Windows: TUI is best-effort — Node + tsx works; PTY semantics differ — document limitation.

---

## 4. The 9-lens audit (post-brainstorm, pre-plan)

### Lens 1 — Assumption-check (what design decisions are unvalidated assertions?)

| Assumption | Status | Mitigation |
|---|---|---|
| `@nous-research/ui` public on npm | ✅ verified — latest 0.12.0 | install as-is |
| `@hermes/ink` is a vendored workspace package | ✅ verified — `file:./packages/hermes-ink` | vendor too as `oc-ink` |
| OC SessionDB exposes `list_sessions/get_session/get_messages/search_messages/delete_session` | ⚠ partial | Phase 0 audit captures actual API + adapts |
| `TypedEventBus` has `subscribe/publish` + a default-bus singleton | ⚠ partial | Phase 0 audit |
| `cli_models`, `cli_plugins`, `cli_profiles`, `cli_cron`, `cli_skills` exports match assumed shapes | ⚠ partial | Phase 0 grep |
| OAuth flows can be driven via existing extensions | ⚠ each extension self-contained; needs adapter | PR4 designs adapter |
| OC slash command registry is enumerable | ⚠ exists but introspection API unknown | Phase 0 audit reads `agent/slash_commands.py` |
| Hermes TUI source license permits vendoring | ⚠ verify | Phase 0 reads `LICENSE` |
| `oc tui` execvpe to node + dist/entry.js works on macOS/Linux | ✅ Hermes does this | — |
| `oc tui` works on Windows | ❌ likely needs `node.exe` adjustment | document limitation |

### Lens 2 — Architecture stress (does it handle obvious edge cases?)

| Edge case | Handled? | Mitigation |
|---|---|---|
| Two OC dashboards on same host | ✅ port collision via `--port` | — |
| Wire server restart while ChatPage open | ⚠ partial | wire client adds backoff reconnect; on reconnect, re-attach to same `session_id` |
| Browser reveals secret then closes tab | ✅ token via `<meta>`, never in URL fragments | — |
| User clicks "delete profile" + "delete sessions" rapidly | ⚠ race | mutation endpoints take a profile-wide flock |
| TUI invoked while wire server not running | ⚠ TUI hangs | TUI shows "Wire server not reachable" + retry every 2s |
| Long-running OAuth flow times out | ✅ session_id-keyed flow with 5min TTL | — |
| Logs SSE buffer overflow | ✅ ring buffer caps at 5000 records | — |
| 100k sessions list query | ✅ pagination | — |
| Config edit corrupts YAML | ⚠ partial | PUT `/config/raw` makes `.bak` first; PUT `/config` validates schema |
| Env reveal accidentally hit by SPA | ⚠ | requires `X-OC-Confirm: yes` + audit-logged |

### Lens 3 — Alternative dismissal (was the chosen approach picked on merit?)

- TUI Hermes-fork vs from-scratch: vendor Hermes per directive. ✅
- `@nous-research/ui` vs shadcn/ui: `@nous-research/ui` per directive (match Hermes look). ✅
- Wire methods for slash commands vs REST: chose wire because TUI uses wire; dashboard ChatPage gets bonus reuse. ✅
- Dashboard does NOT proxy wire — ChatPage opens direct WS. Avoids duplication. ✅
- `oc tui` package-internal vs top-level: package-internal so `pip install` ships TUI. ✅
- `ui-web/` top-level vs `dashboard/web/`: top-level mirrors Hermes's structure. ✅

### Lens 4 — Requirement gap

- **Mobile responsive?** Tablet ok; phone not optimized.
- **Dashboard works without wire?** Yes — REST endpoints don't depend on wire.
- **TUI works without dashboard?** No — TUI calls dashboard REST for non-streaming reads. Either both run (recommended) or only chat-over-wire works.
- **Multi-profile session isolation?** All routes operate on `default_config()` — profile-bound by env var `OPENCOMPUTER_HOME`. SPA shows current profile in StatusBar.
- **A11y?** `@nous-research/ui` ships ARIA-correct. We don't regress.

### Lens 5 — Composability claim

- Routes + plugins compose ✅ — different prefixes.
- SSE multiplex over TypedEventBus ✅ — pattern proven by `acp/session.py::emit_event`.
- Wire `slash.list` + `slash.dispatch` compose with OC's existing dispatcher — verified Phase 0. ⚠
- TUI gatewayClient over WS + REST ✅ — symmetric with dashboard.
- `@nous-research/ui` styling + Tailwind v4 ✅ — Hermes does this.
- `hermes-ink` → `oc-ink` rename: file/package + every `from '@hermes/ink'` import. Mechanical. ✅

### Lens 6 — Scope honesty (sub-tasks honestly sized?)

| Sub-task | Size | Note |
|---|---|---|
| Vite scaffold + sync-assets + index.html shell | 1d | one-time |
| Each REST page (12) | 0.75d avg | reads-only ~9d; mutation-heavy (Cron, Config, Env, OAuth) ~15d |
| Wire server adds (`slash.list`, `slash.dispatch`) | 0.5d | ~50 LOC each + tests |
| Events SSE multiplex | 1d | `_sse.py` + new TypedEventBus subscribe |
| Sidebar / StatusBar / ConnectionIndicator | 1d | one-time |
| i18n EN + ZH | 1d | string sweep |
| Theme system (CSS-vars) | 0.5d | Hermes's pattern |
| Backend route auth tests | 1d | shared dep |
| Wheel package_data + CI smoke | 0.5d | hatch config |
| **Track A total** | **~17d** | 7 PRs |
| TUI vendor + branding rename | 1d | mechanical |
| TUI gatewayClient rewrite | 2d | WS client + reconnect + tests |
| TUI components wiring (21) | 4d | most components vendored as-is; only need wiring |
| TUI build pipeline + node ship | 1d | tsc + babel + chmod |
| TUI CLI flag + binary discovery | 0.5d | execvpe |
| TUI tests + docs | 1d | vitest |
| **Track B total** | **~10d** | 4 PRs |
| **GRAND TOTAL** | **~27d** | 11 PRs |

### Lens 7 — API surface drift

All new routes prefixed `/api/v1/`. Wire methods `slash.list` + `slash.dispatch` are additive. `hello` capability flags advertise new methods; old clients (existing TUI/IDE bridges) continue working.

### Lens 8 — Failure mode map

| Failure | Manifestation | Mitigation |
|---|---|---|
| `@nous-research/ui` install fails | CI red | npm ci retries 3x in CI |
| Vite build fails | Wheel can't ship | scripts/build-dashboard.sh fails fast |
| Wheel missing SPA / TUI | runtime ImportError | wheel-content unit test |
| `_DashboardLogHandler` registers multiple times in tests | duplicate log lines | de-dup guard in `_ensure_handler_attached` |
| Wire server crash mid-chat | ChatPage shows disconnected | exponential backoff |
| OAuth flow leaks tokens to logs | secret leak | reveal endpoint requires confirm header + audit log scrubs payload |
| Config-edit corrupts YAML | broken profile | `.bak` saved on write; `oc doctor` detects |
| TUI dispatcher races with parallel session work | possible | TUI changes ONLY add wire methods; loop.py unchanged; rebase before each push |
| Hermes TUI vendoring conflicts on rebase | merge conflicts | code is in `ui-tui/` directory disjoint from anything OC owns |

### Lens 9 — YAGNI sweep (per directive: less aggressive than initial draft)

✂ Drop:
- `@react-three/fiber` + `gsap` — Hermes 3D banner flourishes
- Vercel deploy template
- Plugin tab registry runtime (keep stub for forward-compat only)
- `unicode-animations`, `leva` — flourish
- TUI Atropos / RL hooks (Hermes-specific)
- TUI Daytona/Singularity/Modal terminal backends (won't-do)

✓ Keep (per directive):
- All 12 dashboard pages
- All ~58 REST routes
- TUI port (vendored)
- i18n EN+ZH
- @nous-research/ui (Hermes look)
- xterm.js (terminal embed)
- @observablehq/plot (analytics)

---

## 5. After-audit refinements (deltas baked into design above)

1. Phase 0 audit captures actual SessionDB / TypedEventBus / cli_* APIs.
2. New wire methods `slash.list` + `slash.dispatch` for unified slash semantics.
3. Reveal endpoints require `X-OC-Confirm: yes` + audit log.
4. Wire client exponential-backoff reconnect (1s → 30s).
5. Config raw edit makes `.bak`.
6. TUI on Windows is best-effort, documented.
7. Drop @react-three/fiber + gsap + leva + unicode-animations from `ui-web/package.json`.

---

## 6. Ship arc (11 PRs)

### Track A — Dashboard (7 PRs, ~17d)

- **PR1 — Foundation.** Vite scaffold + `@nous-research/ui` install + sync-assets + Sidebar shell + StatusBar + ConnectionIndicator + `/api/v1/status` + tests. ~3d.
- **PR2 — Sessions + Logs + Models.** SessionsPage + LogsPage + ModelsPage + 5+1+4 routes + Events SSE multiplex. ~3d.
- **PR3 — Plugins + Profiles + Skills + Tools.** 4 pages + 5+7+3+1 routes. ~3d.
- **PR4 — OAuth + Cron.** ProvidersOAuth in ModelsPage + CronPage + 5+8 routes. ~3d.
- **PR5 — Config + Env.** ConfigPage + EnvPage + 6+4 routes (consent gates, `.bak`, audit). ~2d.
- **PR6 — Chat + Analytics + Docs + i18n.** ChatPage (live wire WS) + AnalyticsPage + DocsPage + EN/ZH bundles. ~3d.
- **PR7 — Dashboard meta + theming + polish + integration tests + CI smoke + wheel verification.** ~2d.

### Track B — TUI (4 PRs, ~10d)

- **PR8 — TUI scaffold.** Vendor `ui-tui/` + `packages/oc-ink/`. Rename hermes→oc. Update copyright headers. Build pipeline. ~2d.
- **PR9 — Wire client + entry.** Replace `gatewayClient.ts` with WS client. Wire `hello` + first message round-trip. Reconnect logic. ~2d.
- **PR10 — Component wiring.** Each of 21 components: reconcile vendored Hermes-side behavior with OC backend. New wire methods (`slash.list`, `slash.dispatch`). ~4d.
- **PR11 — `oc tui` CLI + tests + docs + CI.** ~2d.

PRs are mergeable independently; each stands alone with green tests.

---

## 7. Risks & mitigations

1. **Parallel session collision.** Active `pr-a-steer-wake-acp` worktree touches `agent/loop.py`, `agent/steer.py`, `gateway/dispatch.py`. Track A touches `dashboard/`, `gateway/protocol_v2.py` (additive), `gateway/wire_server.py` (additive). Track B touches new `ui-tui/`. **Risk: low.** Mitigation: rebase before each push.
2. **`@nous-research/ui` API drift.** They're at 0.12 (Hermes uses 0.10). Pin to 0.12 + lockfile.
3. **Hermes TUI source license.** Verify in Phase 0.
4. **CI Node setup.** First time we add Node to CI. `actions/setup-node@v4` + cache.
5. **Wheel size bloat.** Vite + TUI artifact ~5-10 MB. Acceptable.
6. **Browser cache stale SPA after upgrade.** Vite hashes assets; `index.html` cached `Cache-Control: no-store`.

---

## 8. Verification gates (per-PR)

Each PR must pass:
- `ruff check` + `ruff format --check`
- `pytest OpenComputer/tests/` (relevant flat tests + new flat tests)
- For Track B PRs: `cd OpenComputer/ui-tui && npm ci && npm run build && npm test`
- For Track A PRs: `cd OpenComputer/ui-web && npm ci && npm run build` produces `opencomputer/dashboard/static/spa/index.html`
- `oc dashboard` boots, browser shows new SPA, no console errors
- Existing PTY bridge intact
- After PR8+: `python -m build` produces wheel containing both SPA + TUI artifacts

---

## 9. Out-of-scope (deliberately deferred)

- Plugin-tab JS registry runtime (stub only ships)
- Vercel deploy template
- Atropos / RL training UI
- Asia-region channel UIs
- Marketplace pricing/payments
- Mobile-native apps
- @react-three/fiber 3D banner

---

## 10. Phase 0 audit (captured 2026-05-07)

Verified APIs that the plan code depends on. Source = grep against the worktree at `feat/dashboard-polish-2026-05-07` (tip: `387a5b49`).

### 10.1 SessionDB — `opencomputer/agent/state.py`

```
l.877  def create_session(...)
l.963  def get_session(self, session_id: str) -> dict[str, Any] | None
l.968  def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]
l.977  def get_session_title(self, session_id: str) -> str | None
l.1026 def delete_session(self, session_id: str) -> bool
l.1107 def get_session_vibe(self, session_id: str) -> tuple[str | None, float | None]
l.1159 def get_session_goal(self, session_id: str)
l.1445 def get_messages(self, session_id: str) -> list[Message]
l.1736 def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]
```

**Notes for routes:**
- `get_messages(session_id)` does NOT take a limit arg. Routes that paginate must slice client-side.
- `search_messages(query, limit=10)` is FTS5-backed.
- `delete_session` returns bool (True if existed, False if not). Route maps to 204 on True, 404 on False.
- `Message` is `plugin_sdk.core.Message`, not a dict.

### 10.2 TypedEventBus — `opencomputer/ingestion/bus.py` + SignalEvent in `plugin_sdk/ingestion.py`

```
class TypedEventBus (l.145):
  def subscribe(event_type: str | None, handler, policy=BLOCK) -> Subscription
  def subscribe_pattern(pattern: str, handler, policy=BLOCK) -> Subscription
  def publish(event: SignalEvent) -> str  # returns event_id
  def subscribers(event_type=None) -> list[Subscription]

class Subscription (l.95):
  def unsubscribe() -> None  # idempotent

def get_default_bus() -> TypedEventBus  # process-wide singleton (l.501)

class SignalEvent (plugin_sdk/ingestion.py:73):
  event_type: str = ""
  event_id: str = uuid4
  timestamp: float = time()
  session_id: str | None
  source: str
  metadata: Mapping[str, Any]
```

**Notes for routes:**
- `subscribe_pattern("*", handler)` is the wildcard subscription for SSE multiplex.
- `event_id` per event is auto-generated.
- `SignalEvent` is a frozen dataclass (slots=True).

### 10.3 OC CLI exports

| Module | Real exports | Notes |
|---|---|---|
| `cli_models` | `models_add(...)`, `models_list(...)` | Note: NO `list_providers_with_models()`. Routes call `models_list()` and adapt return shape. |
| `cli_model_picker` | `_grouped_models()`, `_pick_provider()`, `_pick_model()`, `model_picker()` | Use `_grouped_models()` for `/api/v1/models`. |
| `cli_plugin` (SINGULAR — not `cli_plugins`) | `install`, `uninstall`, `where`, `plugin_new`, `plugin_enable`, `plugin_disable`, `plugin_demand`, `plugin_inspect`, `catalog_keygen`, `catalog_sign`, `catalog_verify` | Routes call `plugin_enable`, `plugin_disable`, `install`, `plugin_inspect`. |
| `cli_profile` (SINGULAR — not `cli_profiles`) | (verify in Phase 1 by grepping; Typer-app-shaped) | Routes wrap. |
| `cli_skills` | (verify) | |
| `cli_skills_hub` | `do_search`, `do_inspect`, `do_install`, `do_uninstall`, `do_installed`, `do_audit` | Skills page uses these. |
| `cli_cron` | `cron_list`, `cron_create`, `cron_get`, `cron_pause`, `cron_resume`, `cron_run`, `cron_remove`, `cron_tick`, `cron_daemon`, `cron_status` | Cron routes wrap these. |

**Plan adjustment:** every plan code block referencing `cli_plugins.*` or `cli_profiles.*` must use `cli_plugin.*` / `cli_profile.*` (singular). Listed in plan §PR3 implementations as adapter targets.

### 10.4 Slash command registry — `opencomputer/agent/slash_commands.py` + `slash_dispatcher.py`

```
slash_commands.py:
  l.128  def register_builtin_slash_commands() -> None
  l.153  def get_registered_commands() -> list[Any]   # returns command objects
  l.163  def dispatch_slash(message: str) -> str       # `/save foo` → output text

slash_dispatcher.py:
  l.27   def parse_slash(message: str) -> tuple[str, str] | None
```

**Wire methods (Phase 26) implementation:**
- `slash.list` → call `get_registered_commands()`, return `{commands: [{name, description, aliases}]}`. Each command object exposes those attrs (verify in PR9 Phase 27).
- `slash.dispatch` → call `dispatch_slash(message="/" + name + " " + args)`. Return `{output: <returned text>, side_effects: {}}`.

### 10.5 Hermes TUI license

`/Users/saksham/.hermes/hermes-agent/LICENSE`: **MIT License** (Copyright (c) 2025 Nous Research).

Vendoring is permitted with copyright preservation. Each vendored file gets:

```
// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSES.md
```

A new `OpenComputer/THIRD_PARTY_LICENSES.md` records the full Hermes MIT notice.

### 10.6 Python version requirement

System Python is 3.9. Project requires 3.12+ (`pyproject.toml`: `requires-python = ">=3.12"`). The bus `type Handler = ...` uses PEP 695 syntax (3.12+). Use `uv sync && uv run pytest` or `/opt/anaconda3/bin/python3.13` for tests. The `.venv/` doesn't ship in the worktree; engineers create it locally with `uv sync`.

**Plan adjustment:** Phase 0 baseline-test step adds `uv sync` first; subsequent phases use `uv run pytest`.
