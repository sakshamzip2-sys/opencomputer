# Dashboard + TUI Full Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a 12-page React+Vite dashboard at port 9119 (Hermes look via `@nous-research/ui`) and the Hermes Ink+React TUI vendored to `OpenComputer/ui-tui/` (Hermes shell, OC-functionality wiring). 11 PRs across two tracks; ~27 days of work.

**Architecture:** Reuse existing FastAPI dashboard host. Add `OpenComputer/ui-web/` (Vite source) → `opencomputer/dashboard/static/spa/` (build artifact). Add `opencomputer/dashboard/routes/*.py` domain-split routers under `/api/v1/`. Add 2 new wire methods (`slash.list`, `slash.dispatch`) used by both ChatPage and TUI. Vendor `hermes-agent/ui-tui/` + `packages/hermes-ink/` to `OpenComputer/ui-tui/` + `OpenComputer/ui-tui/packages/oc-ink/`, rewrite `gatewayClient.ts` to speak OC's WS protocol.

**Tech Stack:** Python 3.12+ FastAPI/Pydantic v2/sqlite3/asyncio. Frontend: Vite 5+, React 19, TypeScript 5, `@nous-research/ui`@0.12, Tailwind v4, lucide-react, react-router v7, @xterm/xterm. TUI: Ink 6, vendored `oc-ink`, babel + react-compiler.

**Spec:** `docs/superpowers/specs/2026-05-07-dashboard-polish-design.md`

**Ship arc — 11 PRs:**
- **Track A (Dashboard) — 7 PRs:**
  - PR1 Foundation, PR2 Sessions+Logs+Models, PR3 Plugins+Profiles+Skills+Tools, PR4 OAuth+Cron, PR5 Config+Env, PR6 Chat+Analytics+Docs+i18n, PR7 Polish+CI+wheel.
- **Track B (TUI) — 4 PRs:**
  - PR8 Vendor scaffold, PR9 Wire client, PR10 Component wiring, PR11 CLI+tests+docs.

---

## Top-level file structure (new + modified)

| Action | Path | Responsibility |
|---|---|---|
| **Track A — backend** | | |
| Modify | `opencomputer/dashboard/server.py` | Register `routes/*` routers; mount SPA at `/`; SPA fallback for `/sessions`, `/logs`, etc. |
| Create | `opencomputer/dashboard/routes/__init__.py` | Export `ALL_ROUTERS` |
| Create | `opencomputer/dashboard/routes/_common.py` | `clamp_limit`, `get_session_db`, audit-log helper |
| Create | `opencomputer/dashboard/routes/_auth_dep.py` | `require_session_token`, `require_confirm_header` deps |
| Create | `opencomputer/dashboard/routes/status.py` | 1 route |
| Create | `opencomputer/dashboard/routes/sessions.py` | 5 routes |
| Create | `opencomputer/dashboard/routes/logs.py` | SSE |
| Create | `opencomputer/dashboard/routes/models.py` | 4 routes |
| Create | `opencomputer/dashboard/routes/providers_oauth.py` | 5 routes |
| Create | `opencomputer/dashboard/routes/profiles.py` | 7 routes |
| Create | `opencomputer/dashboard/routes/skills.py` | 3 routes |
| Create | `opencomputer/dashboard/routes/plugins.py` | 5 routes |
| Create | `opencomputer/dashboard/routes/cron.py` | 8 routes |
| Create | `opencomputer/dashboard/routes/config.py` | 6 routes |
| Create | `opencomputer/dashboard/routes/env.py` | 4 routes (consent-gated reveal) |
| Create | `opencomputer/dashboard/routes/analytics.py` | 3 routes |
| Create | `opencomputer/dashboard/routes/tools.py` | 1 route |
| Create | `opencomputer/dashboard/routes/dashboard_meta.py` | 4 routes |
| Create | `opencomputer/dashboard/routes/oc_update.py` | 2 routes |
| Create | `opencomputer/dashboard/routes/actions.py` | 1 route |
| Create | `opencomputer/dashboard/routes/events.py` | SSE multiplex |
| Modify | `opencomputer/gateway/protocol_v2.py` | Add `METHOD_SLASH_LIST`, `METHOD_SLASH_DISPATCH` constants + param schemas |
| Modify | `opencomputer/gateway/wire_server.py` | Dispatch new methods → OC slash dispatcher |
| **Track A — frontend** | | |
| Create | `OpenComputer/ui-web/package.json` | npm metadata |
| Create | `OpenComputer/ui-web/vite.config.ts` | Vite config |
| Create | `OpenComputer/ui-web/tsconfig.{json,app.json,node.json}` | TS config |
| Create | `OpenComputer/ui-web/eslint.config.js` | ESLint |
| Create | `OpenComputer/ui-web/index.html` | SPA shell with token+wire-url placeholders |
| Create | `OpenComputer/ui-web/public/{fonts,ds-assets}/` | Synced from @nous-research/ui at build |
| Create | `OpenComputer/ui-web/src/main.tsx` | React entry |
| Create | `OpenComputer/ui-web/src/App.tsx` | Router + layout shell |
| Create | `OpenComputer/ui-web/src/index.css` | Tailwind v4 + @theme |
| Create | `OpenComputer/ui-web/src/i18n/{context,en,zh,index,types}.ts*` | i18n |
| Create | `OpenComputer/ui-web/src/lib/{api,wire,events,theme}.ts` | Lib |
| Create | `OpenComputer/ui-web/src/hooks/{useApi,useWire,useEvent,useStatus,useTheme}.ts` | Hooks |
| Create | `OpenComputer/ui-web/src/contexts/{SystemActions,PageHeaderProvider,ToastProvider}.tsx` | Contexts |
| Create | `OpenComputer/ui-web/src/components/*.tsx` | 22 component files (Sidebar, StatusBar, etc.) |
| Create | `OpenComputer/ui-web/src/pages/*.tsx` | 12 page files |
| Create | `OpenComputer/ui-web/src/plugins/{registry,slots,types,usePlugins,index,PluginPage}.ts*` | Stub for v2 |
| **Track A — tests** | | |
| Create | `OpenComputer/tests/test_dashboard_routes_status.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_sessions.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_logs.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_models.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_providers_oauth.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_profiles.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_skills.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_plugins.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_cron.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_config.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_env.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_analytics.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_tools.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_meta.py` | |
| Create | `OpenComputer/tests/test_dashboard_routes_events.py` | |
| Create | `OpenComputer/tests/test_dashboard_spa_serving.py` | |
| Create | `OpenComputer/tests/test_dashboard_legacy_redirects.py` | |
| Create | `OpenComputer/tests/test_dashboard_wheel_includes_spa.py` | |
| Create | `OpenComputer/tests/test_wire_slash_dispatch.py` | wire methods |
| **Track B — TUI** | | |
| Create | `OpenComputer/ui-tui/package.json`, `tsconfig*.json`, `babel.compiler.config.cjs`, `vitest.config.ts`, `eslint.config.mjs` | Build/lint/test configs |
| Create | `OpenComputer/ui-tui/packages/oc-ink/` | Vendored hermes-ink workspace |
| Create | `OpenComputer/ui-tui/src/entry.tsx`, `app.tsx`, `theme.ts`, `gatewayClient.ts` | Entry + app + WS client |
| Create | `OpenComputer/ui-tui/src/components/*.tsx` | 21 vendored components, branding adapted |
| Create | `OpenComputer/ui-tui/src/hooks/*.ts*` | Vendored hooks |
| Create | `OpenComputer/tests/test_tui_gateway_client.py` (Python smoke around the binary) | |
| Create | `OpenComputer/ui-tui/src/__tests__/gatewayClient.test.ts` | vitest |
| Create | `opencomputer/cli_tui.py` | `oc tui` subcommand |
| Modify | `opencomputer/cli.py` | Mount `tui_app` |
| **Build / CI / docs** | | |
| Create | `OpenComputer/scripts/build-dashboard.sh` | Build SPA |
| Create | `OpenComputer/scripts/build-tui.sh` | Build TUI |
| Modify | `OpenComputer/pyproject.toml` | hatch package_data for SPA + TUI |
| Modify | `.github/workflows/test.yml` | Node setup + dashboard/TUI build steps |
| Modify | `OpenComputer/CHANGELOG.md` | Per-PR entries |
| Modify | `OpenComputer/README.md` | Dashboard + TUI sections |

---

## Phase 0 — Audits

### Task 0.1: Verify worktree + parallel-session safety + baseline tests

- [ ] **Step 1: Confirm worktree on the right branch**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/dashboard-polish-2026-05-07/OpenComputer
git branch --show-current
git log --oneline -3
```

Expected: branch `feat/dashboard-polish-2026-05-07`. Tip should be `387a5b49` (the v1.0 main tip we branched from).

- [ ] **Step 2: Confirm parallel session is on a different worktree**

```bash
git worktree list
```

Confirm `pr-a-steer-wake-acp-2026-05-07` is a sibling worktree. We do NOT touch that branch.

- [ ] **Step 3: Run baseline tests**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/dashboard-polish-2026-05-07/OpenComputer
pytest tests/test_dashboard_server.py tests/test_dashboard_a1.py tests/test_dashboard_fastapi.py tests/test_dashboard_mutations.py tests/test_dashboard_sse.py tests/test_pty_bridge.py -q
```

Record PASS/FAIL counts in a scratchpad — must stay green throughout.

### Task 0.2: Audit OC's actual APIs (load-bearing — drives every later phase)

- [ ] **Step 1: SessionDB API**

```bash
grep -nE "    def (list_sessions|get_session|get_messages|search_messages|create_session|insert_message|delete_session|add_message)" \
  opencomputer/agent/state.py | head -30
```

Capture actual signatures (param names, return shapes). Append to spec § 10.

- [ ] **Step 2: TypedEventBus API**

```bash
grep -nE "    def (subscribe|unsubscribe|publish|apublish)|^def get_(default|process)_bus" \
  opencomputer/ingestion/bus.py
sed -n '50,180p' opencomputer/ingestion/bus.py
```

Capture: `subscribe(handler, topic_glob=...)` actual shape, `SignalEvent` field names, whether `get_default_bus()` exists. Append to spec § 10.

- [ ] **Step 3: cli_models / cli_plugins / cli_profiles / cli_cron / cli_skills exports**

```bash
for m in cli_models cli_plugins cli_profiles cli_cron cli_model_picker; do
  echo "=== $m ==="
  grep -nE "^def |^class " "opencomputer/${m}.py" 2>/dev/null | head -20
done
```

Append to spec § 10.

- [ ] **Step 4: Slash command registry**

```bash
grep -nE "class.*SlashRegistry|^def register_slash|^_REGISTRY" opencomputer/agent/slash_commands.py opencomputer/agent/slash_dispatcher.py
ls opencomputer/agent/slash_commands_impl/ | head -40
```

Capture: how to enumerate registered commands, how to dispatch one with args.

- [ ] **Step 5: Hermes TUI license**

```bash
head -20 /Users/saksham/.hermes/hermes-agent/LICENSE 2>&1
head -20 /Users/saksham/.hermes/hermes-agent/ui-tui/LICENSE 2>&1 || echo "no ui-tui-specific LICENSE"
```

If license is permissive (MIT/Apache/BSD), record header for vendoring; otherwise stop and surface to user.

- [ ] **Step 6: Append all findings to spec § 10**

Edit `docs/superpowers/specs/2026-05-07-dashboard-polish-design.md`, replace "(Filled during execution.)" with the captured signatures + license info.

- [ ] **Step 7: Commit Phase 0 + the design + plan docs**

```bash
git add docs/superpowers/
git commit -m "docs(dashboard): brainstorm spec + 11-PR plan + Phase 0 audit findings"
```

### Task 0.3: Verify Node.js available

- [ ] **Step 1**

```bash
node --version && npm --version
```

Expected: Node ≥ 20, npm ≥ 10. If absent: `brew install node@20`.

---

## PR 1 — Foundation (Phases 1-3)

## Phase 1 — Vite scaffold + `@nous-research/ui` install

### Task 1.1: Initialize npm project

- [ ] **Step 1: Create directory + package.json**

```bash
mkdir -p OpenComputer/ui-web && cd OpenComputer/ui-web
cat > package.json <<'EOF'
{
  "name": "oc-dashboard-web",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "sync-assets": "rm -rf public/fonts public/ds-assets && cp -r node_modules/@nous-research/ui/dist/fonts public/fonts && cp -r node_modules/@nous-research/ui/dist/assets public/ds-assets",
    "predev": "npm run sync-assets",
    "prebuild": "npm run sync-assets",
    "dev": "vite",
    "build": "tsc -b && vite build",
    "lint": "eslint .",
    "preview": "vite preview",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {},
  "devDependencies": {}
}
EOF
```

- [ ] **Step 2: Install runtime deps (Hermes look)**

```bash
npm install --save-exact \
  @nous-research/ui@^0.12.0 \
  @observablehq/plot@^0.6.17 \
  @xterm/xterm@^6.0.0 \
  @xterm/addon-fit@^0.11.0 \
  @xterm/addon-unicode11@^0.9.0 \
  @xterm/addon-web-links@^0.12.0 \
  @xterm/addon-webgl@^0.19.0 \
  class-variance-authority@^0.7.1 \
  clsx@^2.1.1 \
  lucide-react@^0.577.0 \
  react@^19.2.4 \
  react-dom@^19.2.4 \
  react-router-dom@^7.14.1 \
  tailwind-merge@^3.5.0
```

- [ ] **Step 3: Install build deps**

```bash
npm install --save-dev --save-exact \
  vite@^5.4.0 \
  @vitejs/plugin-react@^4.3.0 \
  @tailwindcss/vite@^4.2.1 \
  tailwindcss@^4.2.1 \
  typescript@^5.6.0 \
  @types/react@^19.2.14 \
  @types/react-dom@^19.2.0 \
  @types/node@^22.0.0 \
  eslint@^9 \
  @eslint/js@^9.39.4
```

### Task 1.2: Configs

- [ ] **Step 1: vite.config.ts**

```ts
// OpenComputer/ui-web/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const ROOT = path.resolve(__dirname);
const OUT_DIR = path.resolve(ROOT, "..", "opencomputer", "dashboard", "static", "spa");

export default defineConfig({
  root: ROOT,
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(ROOT, "src") } },
  build: { outDir: OUT_DIR, emptyOutDir: true, sourcemap: true, target: "es2022" },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:9119" },
  },
});
```

- [ ] **Step 2: tsconfig.json**

```json
{
  "compilerOptions": {
    "target": "ES2022", "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext", "skipLibCheck": true,
    "moduleResolution": "bundler", "allowImportingTsExtensions": false,
    "resolveJsonModule": true, "isolatedModules": true, "noEmit": true,
    "jsx": "react-jsx", "strict": true,
    "noUnusedLocals": true, "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "baseUrl": ".", "paths": { "@/*": ["src/*"] }
  },
  "include": ["src"]
}
```

- [ ] **Step 3: index.html**

```html
<!doctype html>
<html lang="en" class="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="oc-session-token" content="__SESSION_TOKEN__" />
    <meta name="oc-wire-url" content="__WIRE_URL__" />
    <title>OpenComputer Dashboard</title>
  </head>
  <body class="bg-zinc-950 text-zinc-100">
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 4: src/main.tsx + src/index.css + src/App.tsx**

```tsx
// src/main.tsx
import "./index.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode><BrowserRouter><App /></BrowserRouter></StrictMode>,
);
```

```css
/* src/index.css */
@import "tailwindcss";

@theme {
  --color-bg: #09090b;
  --color-fg: #f4f4f5;
  --color-muted: #a1a1aa;
  --color-accent: #06b6d4;
  --color-border: #27272a;
  --color-card: #18181b;
  --color-success: #22c55e;
  --color-warning: #f59e0b;
  --color-error: #ef4444;
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas;
}

html, body { height: 100%; }
body { font-family: var(--font-sans); }
```

```tsx
// src/App.tsx — initial shell with sidebar + 12 nav links (pages stubbed)
import { Routes, Route, NavLink } from "react-router-dom";
import { useApi } from "@/hooks/useApi";

const PLACEHOLDER = (name: string) => () => (
  <div className="p-6"><h1 className="text-2xl font-semibold">{name}</h1>
    <p className="text-zinc-400 mt-2">Coming in PR&hellip;</p></div>
);

const ROUTES: [string, string, () => JSX.Element][] = [
  ["/chat", "Chat", PLACEHOLDER("Chat")],
  ["/sessions", "Sessions", PLACEHOLDER("Sessions")],
  ["/skills", "Skills", PLACEHOLDER("Skills")],
  ["/plugins", "Plugins", PLACEHOLDER("Plugins")],
  ["/cron", "Cron", PLACEHOLDER("Cron")],
  ["/logs", "Logs", PLACEHOLDER("Logs")],
  ["/models", "Models", PLACEHOLDER("Models")],
  ["/profiles", "Profiles", PLACEHOLDER("Profiles")],
  ["/env", "Env", PLACEHOLDER("Env")],
  ["/config", "Config", PLACEHOLDER("Config")],
  ["/analytics", "Analytics", PLACEHOLDER("Analytics")],
  ["/docs", "Docs", PLACEHOLDER("Docs")],
];

export default function App() {
  const status = useApi<{profile: string; wire_url: string; version: string}>("/api/v1/status");
  return (
    <div className="flex h-screen">
      <aside className="w-52 shrink-0 border-r border-zinc-800 bg-zinc-950 p-4">
        <h2 className="text-lg font-semibold mb-4">OpenComputer</h2>
        <nav className="flex flex-col gap-1 text-sm">
          {ROUTES.map(([p, label]) => (
            <NavLink key={p} to={p}
              className={({ isActive }) =>
                `rounded px-2 py-1 ${isActive ? "bg-zinc-800 text-cyan-300" : "text-zinc-400 hover:text-zinc-100"}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-6 text-xs text-zinc-500">
          {status.data && <>v{status.data.version} · profile <code>{status.data.profile}</code></>}
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<PLACEHOLDER name="Welcome" />()} />
          {ROUTES.map(([p, , Page]) => <Route key={p} path={p} element={<Page />} />)}
          <Route path="*" element={<div className="p-6 text-zinc-400">Not found</div>} />
        </Routes>
      </main>
    </div>
  );
}
```

- [ ] **Step 5: lib/api.ts + hooks/useApi.ts**

```ts
// src/lib/api.ts
const getToken = (): string => {
  const v = document.querySelector<HTMLMetaElement>('meta[name="oc-session-token"]')?.content ?? "";
  return v.includes("__SESSION_TOKEN__") ? "" : v;
};
const TOKEN = getToken();

export class ApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = "ApiError"; }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (TOKEN) headers.set("Authorization", `Bearer ${TOKEN}`);
  if (!headers.has("Content-Type") && init?.body) headers.set("Content-Type", "application/json");
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail ?? detail; } catch {}
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}
```

```ts
// src/hooks/useApi.ts
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";

export function useApi<T>(path: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<ApiError | undefined>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancel = false;
    setLoading(true); setError(undefined);
    api<T>(path)
      .then(d => { if (!cancel) { setData(d); setLoading(false); } })
      .catch((e: ApiError) => { if (!cancel) { setError(e); setLoading(false); } });
    return () => { cancel = true; };
  }, [path, tick, ...deps]);
  return { data, error, loading, refetch: () => setTick(n => n + 1) };
}
```

### Task 1.3: First build + smoke test

- [ ] **Step 1: Build**

```bash
cd OpenComputer/ui-web && npm run build
```

Expected: `OpenComputer/opencomputer/dashboard/static/spa/index.html` exists.

- [ ] **Step 2: Verify build output structure**

```bash
ls -la OpenComputer/opencomputer/dashboard/static/spa/
test -f OpenComputer/opencomputer/dashboard/static/spa/index.html
test -d OpenComputer/opencomputer/dashboard/static/spa/assets
```

- [ ] **Step 3: Verify token placeholder survives build**

```bash
grep "__SESSION_TOKEN__" OpenComputer/opencomputer/dashboard/static/spa/index.html
```

Expected: present (Vite does not template HTML root).

### Task 1.4: Commit Phase 1

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/dashboard-polish-2026-05-07
git add OpenComputer/ui-web/ OpenComputer/opencomputer/dashboard/static/spa/
git commit -m "feat(dashboard): scaffold Vite+React+@nous-research/ui SPA shell"
```

## Phase 2 — Backend foundation (`status` route + router skeleton + SPA serving)

### Task 2.1: Test for `/api/v1/status`

```python
# OpenComputer/tests/test_dashboard_routes_status.py
"""Tests for /api/v1/status — SPA's first call on mount."""

from fastapi.testclient import TestClient
from opencomputer.dashboard.server import build_app


def test_status_returns_profile_and_wire_url():
    app = build_app(wire_url="ws://127.0.0.1:18789", enable_pty=False)
    client = TestClient(app)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "profile" in body and "wire_url" in body and "version" in body
    assert body["wire_url"] == "ws://127.0.0.1:18789"


def test_status_is_loopback_public():
    app = build_app(enable_pty=False)
    resp = TestClient(app).get("/api/v1/status")
    assert resp.status_code == 200
```

```bash
pytest OpenComputer/tests/test_dashboard_routes_status.py -v
```

Expected FAIL.

### Task 2.2: Implement `routes/__init__.py` + `_common.py` + `_auth_dep.py` + `status.py` + stubs

Code per spec §2.4. (Concrete code in spec; copy verbatim.)

### Task 2.3: Wire routers into `dashboard/server.py`

In `_build_app`, after the plugin-router loop:

```python
    from opencomputer.dashboard.routes import ALL_ROUTERS
    for v1_router in ALL_ROUTERS:
        app.include_router(v1_router)
```

### Task 2.4: SPA serving + fallback + legacy redirects

Replace the existing `@app.get("/")` block in `dashboard/server.py` with:

```python
    _SPA_DIR = static_dir / "spa"

    if _SPA_DIR.exists():
        app.mount("/assets",
            StaticFiles(directory=str(_SPA_DIR / "assets"), html=False),
            name="spa-assets")

        @app.get("/", response_class=HTMLResponse)
        async def spa_root() -> Response:
            return _render_html(_SPA_DIR / "index.html")

        @app.get("/{spa_path:path}", response_class=HTMLResponse)
        async def spa_fallback(spa_path: str) -> Response:
            if spa_path.startswith(("api/", "static/", "assets/")):
                return Response(status_code=404)
            return _render_html(_SPA_DIR / "index.html")
```

### Task 2.5: SPA serving tests + commit

```python
# OpenComputer/tests/test_dashboard_spa_serving.py
# (see spec for test bodies)
```

```bash
pytest OpenComputer/tests/test_dashboard_routes_status.py OpenComputer/tests/test_dashboard_spa_serving.py -v
```

Commit when green:

```bash
git add OpenComputer/opencomputer/dashboard/ OpenComputer/tests/
git commit -m "feat(dashboard): /api/v1/status + router skeleton + SPA serving"
```

## Phase 3 — SessionsPage end-to-end

(Per spec §2.5 sessions section. Test → routes/sessions.py → SessionsPage.tsx → wire into App.tsx → smoke test → commit.)

Concrete code: see Task 3.1-3.5 in the prior smaller-scope plan version (preserved here under PR1 scope) — but page is `OpenComputer/ui-web/src/pages/SessionsPage.tsx` and tests are flat `OpenComputer/tests/test_dashboard_routes_sessions.py`.

After PR1 ships, open: `gh pr create --title "feat(dashboard): foundation — Vite+@nous-research/ui SPA + sessions"`.

---

## PR 2 — Sessions enhanced + Logs + Models + Events SSE

## Phase 4 — LogsPage + `/api/v1/logs` SSE

(SSE handler + ring buffer + de-dup'd `_ensure_handler_attached`. LogsPage with virtualized list. Code per the prior plan; paths updated to `OpenComputer/ui-web/src/pages/LogsPage.tsx`.)

## Phase 5 — ModelsPage + `/api/v1/models/*`

(4 routes, ModelsPage with provider list + set-default. Code per prior plan.)

## Phase 6 — Events SSE multiplex

(`routes/events.py` + multi-subscriber test. Code per prior plan.)

PR 2: `gh pr create --title "feat(dashboard): live + ops — sessions/logs/models/events"`.

---

## PR 3 — Plugins + Profiles + Skills + Tools

## Phase 7 — PluginsPage + `/api/v1/plugins/*`

```python
# routes/plugins.py
"""GET /api/v1/plugins (list), POST /enable, /disable, /install, /dashboard/install"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["plugins"])


class InstallBody(BaseModel):
    source: str  # URL or path


@router.get("/plugins")
async def list_plugins() -> dict:
    from opencomputer import cli_plugins
    raw = cli_plugins.list_installed()
    return {"items": [{"name": p.name, "version": getattr(p, "version", "—"),
                       "enabled": getattr(p, "enabled", True),
                       "kind": getattr(p, "kind", "unknown")} for p in raw]}


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str) -> dict:
    from opencomputer import cli_plugins
    if not cli_plugins.exists(name):
        raise HTTPException(404, "plugin not found")
    cli_plugins.enable(name)
    return {"ok": True, "name": name, "enabled": True}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str) -> dict:
    from opencomputer import cli_plugins
    if not cli_plugins.exists(name):
        raise HTTPException(404, "plugin not found")
    cli_plugins.disable(name)
    return {"ok": True, "name": name, "enabled": False}


@router.post("/plugins/install")
async def install_plugin(body: InstallBody) -> dict:
    from opencomputer import cli_plugins
    result = cli_plugins.install_from(body.source)
    return {"ok": True, "installed": result.get("installed", [])}


@router.post("/plugins/dashboard/install")
async def install_dashboard_plugin(body: InstallBody) -> dict:
    from opencomputer.dashboard import plugin_install
    result = plugin_install.install(body.source)
    return {"ok": True, **result}
```

(If the imported names don't match in Phase 0 grep, adjust.)

PluginsPage TSX per spec §2.4 components/pages catalogue.

## Phase 8 — ProfilesPage + `/api/v1/profiles/*`

7 routes — list/create/delete/setup-command/open-terminal/persona-get/persona-put. ProfilesPage TSX per spec.

## Phase 9 — SkillsPage + `/api/v1/skills/*`

3 routes — list/toggle/{name}. SkillsPage with toggle UI.

## Phase 10 — Tools route + ToolsToolset card on PluginsPage

GET `/api/v1/tools/toolsets` returns toolset list.

PR 3: `gh pr create --title "feat(dashboard): tooling — plugins/profiles/skills/tools"`.

---

## PR 4 — OAuth + Cron

## Phase 11 — Providers OAuth flow

5 routes — list/start/submit/poll/revoke. ModelsPage gains "Login" button per provider that opens an OAuth modal. The provider-specific OAuth driver is dispatched via the existing `extensions/<provider>-oauth-provider/` modules. Adapter layer at `routes/providers_oauth.py` maps `provider_id` → driver.

## Phase 12 — CronPage + `/api/v1/cron/jobs/*`

8 routes. CronPage with table + create/edit dialogs (cron-expression validator UI). Reuse `cli_cron` for backend.

PR 4: `gh pr create --title "feat(dashboard): OAuth providers + cron CRUD"`.

---

## PR 5 — Config + Env (consent-heavy)

## Phase 13 — ConfigPage + `/api/v1/config/*`

6 routes. Schema-driven form (use `/config/schema` to render). Raw mode toggles to a Monaco-style editor (or `<textarea>` for v1 — Monaco is post-v1).

## Phase 14 — EnvPage + `/api/v1/env/*`

4 routes — get/put/delete/reveal. Reveal endpoint requires `X-OC-Confirm: yes` header AND audit-logs the request (timestamp + key name only, never the value). UI: per-key "Reveal" button shows confirm dialog → fetch with header → display value with auto-clear-after-30s timer.

PR 5: `gh pr create --title "feat(dashboard): config + env editors"`.

---

## PR 6 — Chat + Analytics + Docs + i18n

## Phase 15 — ChatPage with live wire WS

ChatPage is the most architecturally distinct page. It opens its own WS to `meta[name="oc-wire-url"]` (= `ws://127.0.0.1:18789`), sends `chat` JSON-RPC, streams `assistant_message` / `tool_call` / `turn.end` events.

```ts
// src/lib/wire.ts
export class WireClient {
  ws: WebSocket | null = null;
  pending = new Map<string, {resolve: (v: unknown) => void; reject: (e: Error) => void}>();
  events = new EventTarget();
  url: string;
  reconnectMs = 1000;

  constructor(url: string) { this.url = url; this.connect(); }

  private connect() {
    this.ws = new WebSocket(this.url);
    this.ws.onopen = () => { this.reconnectMs = 1000; };
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id) {
        const cb = this.pending.get(m.id); this.pending.delete(m.id);
        if (cb) m.error ? cb.reject(new Error(m.error)) : cb.resolve(m.result);
      } else if (m.event) {
        this.events.dispatchEvent(new CustomEvent(m.event, { detail: m }));
      }
    };
    this.ws.onclose = () => {
      setTimeout(() => this.connect(), this.reconnectMs);
      this.reconnectMs = Math.min(30_000, this.reconnectMs * 2);
    };
  }

  call<T>(method: string, params: unknown): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const id = crypto.randomUUID();
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.ws!.send(JSON.stringify({ id, method, params }));
    });
  }
}
```

ChatPage uses this + `useWire` hook. Slash popover queries `slash.list` once and caches.

## Phase 16 — AnalyticsPage + `/api/v1/analytics/*`

3 routes. Use `@observablehq/plot` for time-series. Data sourced from CostGuard + tool_usage table.

## Phase 17 — DocsPage

Renders bundled markdown via `Markdown.tsx`. Documents to render: `OpenComputer/CLAUDE.md`, `OpenComputer/README.md`, `OpenComputer/extensions/*/README.md`. Backend route `GET /api/v1/docs/{slug}` returns markdown text.

## Phase 18 — i18n EN+ZH

Mirror Hermes's `i18n/{en,zh}.ts` shape. Wrap `App.tsx` with `<I18nProvider>`. LanguageSwitcher in StatusBar.

PR 6: `gh pr create --title "feat(dashboard): chat + analytics + docs + i18n"`.

---

## PR 7 — Polish + tests + CI smoke + wheel verification

## Phase 19 — Sidebar polish + StatusBar + ConnectionIndicator + Toast

(Components per spec.)

## Phase 20 — Build script + CI integration

```bash
#!/usr/bin/env bash
# OpenComputer/scripts/build-dashboard.sh
set -euo pipefail
cd "$(dirname "$0")/.."
cd ui-web
npm ci --no-audit --fund=false
npm run build
test -f ../opencomputer/dashboard/static/spa/index.html
echo "Dashboard SPA built."
```

`.github/workflows/test.yml` adds:

```yaml
      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: |
            OpenComputer/ui-web/package-lock.json
            OpenComputer/ui-tui/package-lock.json
      - name: Build dashboard SPA
        run: ./OpenComputer/scripts/build-dashboard.sh
      - name: Verify SPA artifact
        run: |
          test -f OpenComputer/opencomputer/dashboard/static/spa/index.html
          test -d OpenComputer/opencomputer/dashboard/static/spa/assets
```

## Phase 21 — Wheel package_data

Edit `OpenComputer/pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"opencomputer/dashboard/static/spa" = "opencomputer/dashboard/static/spa"
```

## Phase 22 — Wheel verification test

`OpenComputer/tests/test_dashboard_wheel_includes_spa.py` (per prior plan).

## Phase 23 — Docs

- `OpenComputer/README.md` — Dashboard section pointing at `oc dashboard`.
- `OpenComputer/CHANGELOG.md` — `[Unreleased]` entries per PR.

PR 7: `gh pr create --title "feat(dashboard): polish + CI + wheel + docs"`.

---

## PR 8 — TUI Vendor Scaffold

## Phase 24 — Vendor Hermes ui-tui + packages/hermes-ink

- [ ] **Step 1: Verify Hermes license is permissive (Phase 0 task already captured)**

If Phase 0 audit confirmed permissive license, proceed.

- [ ] **Step 2: Copy ui-tui sources**

```bash
mkdir -p OpenComputer/ui-tui
cp -r /Users/saksham/.hermes/hermes-agent/ui-tui/{package.json,tsconfig*.json,babel.compiler.config.cjs,vitest.config.ts,eslint.config.mjs,scripts,src} OpenComputer/ui-tui/
cp -r /Users/saksham/.hermes/hermes-agent/ui-tui/packages OpenComputer/ui-tui/
```

- [ ] **Step 3: Rename `hermes-ink` → `oc-ink` everywhere**

```bash
cd OpenComputer/ui-tui
mv packages/hermes-ink packages/oc-ink
sed -i.bak 's|@hermes/ink|@oc/ink|g' src/**/*.tsx src/**/*.ts package.json packages/oc-ink/package.json
sed -i.bak 's|"hermes-ink"|"oc-ink"|g' packages/oc-ink/package.json
sed -i.bak 's|"name": "hermes-tui"|"name": "oc-tui"|' package.json
find . -name "*.bak" -delete
```

- [ ] **Step 4: Update copyright headers**

Each modified file gets:
```
// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original copyright (preserved): <Hermes notice>
```

- [ ] **Step 5: Install + first build**

```bash
cd OpenComputer/ui-tui
npm install
npm run build
ls dist/entry.js
chmod +x dist/entry.js
```

- [ ] **Step 6: Commit Phase 24**

```bash
git add OpenComputer/ui-tui/
git commit -m "feat(tui): vendor hermes-agent/ui-tui + packages/oc-ink (rebrand)"
```

PR 8: `gh pr create --title "feat(tui): vendor scaffold (oc-tui + oc-ink)"`.

---

## PR 9 — TUI Wire Client + Entry

## Phase 25 — Replace gatewayClient.ts

```ts
// OpenComputer/ui-tui/src/gatewayClient.ts
import WebSocket from "ws";

export interface HelloResult { server: string; version: string; capabilities: string[]; }
export interface ChatStreamEvent { event: string; payload: unknown; }

type Pending<T> = { resolve: (v: T) => void; reject: (e: Error) => void; };

export class OCWireClient {
  ws: WebSocket | null = null;
  private pending = new Map<string, Pending<unknown>>();
  private listeners: ((e: ChatStreamEvent) => void)[] = [];
  private url: string;
  private reconnectMs = 1000;

  constructor(url: string = process.env.OC_WIRE_URL || "ws://127.0.0.1:18789") {
    this.url = url;
    this.connect();
  }

  private connect(): void {
    this.ws = new WebSocket(this.url);
    this.ws.on("open", () => { this.reconnectMs = 1000; });
    this.ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.id && this.pending.has(m.id)) {
        const cb = this.pending.get(m.id)!; this.pending.delete(m.id);
        m.error ? cb.reject(new Error(m.error)) : cb.resolve(m.result);
      } else if (m.event) {
        for (const l of this.listeners) l({event: m.event, payload: m});
      }
    });
    this.ws.on("close", () => {
      setTimeout(() => this.connect(), this.reconnectMs);
      this.reconnectMs = Math.min(30_000, this.reconnectMs * 2);
    });
  }

  call<T>(method: string, params: unknown = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const id = String(Math.random());
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.ws!.send(JSON.stringify({ id, method, params }));
    });
  }

  onEvent(handler: (e: ChatStreamEvent) => void): void { this.listeners.push(handler); }

  hello(): Promise<HelloResult> { return this.call("hello"); }
  chat(message: string, sessionId?: string): Promise<void> {
    return this.call("chat", { message, session_id: sessionId });
  }
  sessionsList(limit = 50): Promise<{items: Session[]}> { return this.call("sessions.list", {limit}); }
  search(q: string): Promise<{items: MessageRow[]}> { return this.call("search", {q}); }
  skillsList(): Promise<{items: Skill[]}> { return this.call("skills.list"); }
  slashList(): Promise<{commands: SlashCommand[]}> { return this.call("slash.list"); }
  slashDispatch(name: string, args: string, sessionId?: string): Promise<{output: string}> {
    return this.call("slash.dispatch", {name, args, session_id: sessionId});
  }
  steerSubmit(text: string): Promise<void> { return this.call("steer.submit", {text}); }
}

export interface Session { id: string; title: string | null; }
export interface MessageRow { id: number; role: string; content: string; timestamp: number; }
export interface Skill { name: string; enabled: boolean; }
export interface SlashCommand { name: string; description: string; aliases: string[]; }
```

## Phase 26 — Add `slash.list` + `slash.dispatch` to wire server

```python
# opencomputer/gateway/protocol_v2.py — add constants
METHOD_SLASH_LIST = "slash.list"
METHOD_SLASH_DISPATCH = "slash.dispatch"

class SlashListResult(_StrictModel):
    commands: list["SlashCommandInfo"]

class SlashCommandInfo(_StrictModel):
    name: str
    description: str
    aliases: list[str]

class SlashDispatchParams(_StrictModel):
    name: str
    args: str = ""
    session_id: str | None = None

class SlashDispatchResult(_StrictModel):
    output: str
    side_effects: dict[str, Any] = Field(default_factory=dict)
```

```python
# opencomputer/gateway/wire_server.py — extend dispatch
async def _dispatch_slash_list(self, params: dict[str, Any]) -> dict:
    from opencomputer.agent.slash_commands import iter_registered  # actual API per Phase 0 audit
    return {"commands": [
        {"name": c.name, "description": c.description, "aliases": list(c.aliases)}
        for c in iter_registered()
    ]}

async def _dispatch_slash_dispatch(self, params: dict[str, Any]) -> dict:
    from opencomputer.agent.slash_dispatcher import dispatch
    out = await dispatch(name=params["name"], args=params.get("args", ""),
                          session_id=params.get("session_id"))
    return {"output": out.output, "side_effects": out.side_effects}
```

(Adapt names per Phase 0 audit findings.)

## Phase 27 — Tests for wire methods + first round-trip

```python
# OpenComputer/tests/test_wire_slash_dispatch.py
"""Tests slash.list + slash.dispatch over wire."""
import pytest
from opencomputer.gateway.wire_server import WireServer
# ... boot wire server in test, send slash.list, assert non-empty list
```

PR 9: `gh pr create --title "feat(tui): wire client + slash methods"`.

---

## PR 10 — TUI Component Wiring

## Phase 28-30 — 21 components reconciled with OC backend

Each component gets a 1-line code review pass: imports updated to `@oc/ink`, Hermes-specific routes (e.g. `gateway.hermesUpdate()`) removed or remapped to OC equivalents.

- `branding.tsx` — replace Hermes ASCII with OC's banner art (port `cli_banner_art.py` to TS or hardcode the ASCII).
- `modelPicker.tsx` — fetches via `gatewayClient.modelsList()` (REST passthrough) instead of Hermes's wire method.
- `sessionPicker.tsx` — `sessionsList()` (existing wire method).
- `skillsHub.tsx` — `skillsList()` (existing).
- `prompts.tsx` + `overlayControls.tsx` — slash palette uses `slashList()`.
- `streamingAssistant.tsx` + `streamingMarkdown.tsx` — react to `assistant_message` events from OC's `chat`.
- All others vendored as-is.

## Phase 31 — TUI tests

`OpenComputer/ui-tui/src/__tests__/gatewayClient.test.ts` (vitest) with mocked WebSocket.

PR 10: `gh pr create --title "feat(tui): component wiring to OC backend"`.

---

## PR 11 — `oc tui` CLI + tests + docs + CI

## Phase 32 — `cli_tui.py`

```python
# opencomputer/cli_tui.py
"""``opencomputer tui`` CLI command."""
from __future__ import annotations
import os, sys
from typing import Annotated
import typer

tui_app = typer.Typer(name="tui", help="Run the Ink+React TUI.",
                      no_args_is_help=False, invoke_without_command=True)


@tui_app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    wire_url: Annotated[str, typer.Option(help="Wire server URL")] = "ws://127.0.0.1:18789",
    dashboard_url: Annotated[str, typer.Option(help="Dashboard URL")] = "http://127.0.0.1:9119",
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    import importlib.resources
    try:
        entry = importlib.resources.files("opencomputer").joinpath("ui-tui/dist/entry.js")
    except Exception:
        typer.echo("TUI build artifact not found. Run scripts/build-tui.sh.", err=True)
        raise typer.Exit(2)

    if not entry.exists():
        typer.echo(f"TUI not built at {entry}. Run scripts/build-tui.sh.", err=True)
        raise typer.Exit(2)

    env = os.environ.copy()
    env["OC_WIRE_URL"] = wire_url
    env["OC_DASHBOARD_URL"] = dashboard_url

    if sys.platform == "win32":
        node_exe = "node.exe"
    else:
        node_exe = "node"

    os.execvpe(node_exe, [node_exe, str(entry)], env)
```

Mount in `cli.py`:

```python
from opencomputer.cli_tui import tui_app
app.add_typer(tui_app)
```

## Phase 33 — `scripts/build-tui.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cd ui-tui
npm ci --no-audit --fund=false
npm run build
test -f dist/entry.js
chmod +x dist/entry.js
echo "TUI built: $(realpath dist/entry.js)"
```

## Phase 34 — Wheel ships TUI dist

`pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"opencomputer/dashboard/static/spa" = "opencomputer/dashboard/static/spa"
"ui-tui/dist" = "opencomputer/ui-tui/dist"
"ui-tui/packages/oc-ink/dist" = "opencomputer/ui-tui/packages/oc-ink/dist"
```

## Phase 35 — CI integration

`.github/workflows/test.yml` — add `Build TUI` step calling `scripts/build-tui.sh`.

## Phase 36 — Smoke test + docs + final PR

- Manual smoke: `oc dashboard &` + `oc gateway &` + `oc tui` — verify Ink TUI boots, hello round-trips, message → assistant stream → todo panel → slash palette.
- README "TUI" section.
- CHANGELOG.

PR 11: `gh pr create --title "feat(tui): oc tui CLI + tests + docs + CI"`.

---

## Appendix A — Tracked but out of scope

Same as spec §9.

## Appendix B — Higher-priority backlog from deep-comparison doc

These items rank above this dashboard work in the doc's recommendation list and are unaffected:
- S1 — tool-result middleware (3d)
- S2 — credential pool rotation finish (3d)
- S3 — hooks management CLI (2d)
- S4 — `oc backup` + profile clone/export/import (4-5d)
- A2 — Edge TTS + Groq STT (1-2d)

If priorities shift mid-flight, this plan can pause cleanly between PRs.

---

## Self-Review (run by author after writing)

**Spec coverage:**
- 12 pages → tasks in Phases 3, 4, 5, 7, 8, 9, 11, 12, 13, 14, 15, 17 ✓
- ~58 routes → distributed across all PRs ✓
- 2 new wire methods → Phase 26 ✓
- TUI vendor + branding + wiring → PRs 8-11 ✓
- Phase 0 audits drive later phases → Task 0.2 ✓
- 9-lens audit refinements baked into design → spec §5 ✓

**Placeholder scan:** No "TBD/TODO". Some sections compress with "(per spec §x)" where the spec contains the verbatim code — engineer reads spec for those bits.

**Type consistency:**
- `OCWireClient` API consistent across TUI + dashboard wire client ✓
- `useApi<T>` returns `{data, error, loading, refetch}` everywhere ✓
- Routes always under `/api/v1/<domain>/*` ✓
- Wire methods always `<domain>.<verb>` ✓
