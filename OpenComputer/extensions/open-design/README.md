# Open Design plugin

Sidecar bridge to [Open Design](https://github.com/nexu-io/open-design) — the
open-source alternative to Claude Design. Local-first design product that
turns 16 coding-agent CLIs into a design engine driven by 31 composable
Skills and 72 brand-grade Design Systems.

This plugin manages the open-design Node daemon lifecycle from inside
OpenComputer and exposes a Hermes Workspace "Design" tab.

## What it gives you

| Surface | What |
|---|---|
| `oc design start\|stop\|status\|url\|restart\|home` | Typer CLI verbs |
| `/design [status\|open\|start\|stop\|url\|restart]` | Chat slash command |
| `oc doctor` rows | Source-tree, Node, pnpm, daemon health |
| Hermes Workspace **Design** tab | iframes the daemon at `http://127.0.0.1:7456` |

## Setup

```bash
# 1. Clone open-design somewhere the plugin can find it:
git clone https://github.com/nexu-io/open-design ~/.open-design
# (or set OPEN_DESIGN_HOME=/path/to/open-design)

# 2. Build the daemon once (needs Node 24 + pnpm 10.33):
cd ~/.open-design
corepack enable
pnpm install
pnpm --filter @open-design/daemon build

# 3. Enable the plugin:
oc plugins enable open-design

# 4. Start the daemon:
oc design start
```

The daemon listens on `OD_PORT` (default `7456`). Override with
`OD_PORT=8080 oc design start`.

## Source-tree discovery order

`OPEN_DESIGN_HOME` env var → `~/Vscode/claude/open-design` →
`~/.open-design` → `/usr/local/share/open-design`. First match wins;
each candidate is required to contain `apps/daemon/package.json`.

## Hermes Design tab

When the daemon is running, the Hermes Workspace's **Design** sidebar
entry iframes the daemon URL. If the daemon is stopped, the tab shows a
"Start daemon" button that POSTs `/api/design/start` (which shells out
to `oc design start`).

## Cross-origin frame setup

The daemon is told (via `OD_ALLOWED_FRAME_ANCESTORS`) to accept
`http://localhost:9119` (Hermes default), `http://127.0.0.1:9119`, and
`http://localhost:3000`. Override per-deployment by exporting
`OD_ALLOWED_FRAME_ANCESTORS="space-separated origins"` before running
`oc design start`.

## Profile-scoped state

PID file: `~/.opencomputer/<profile>/locks/open-design.pid`
Log file: `~/.opencomputer/<profile>/logs/open-design.log`

Each OC profile manages its own daemon — switching profile (`oc -p
work`) stops/starts a separate daemon under that profile's directory.

## Implementation notes

- `kind: "mixed"` — CLI + slash + doctor; no agent tool / provider / channel.
- `single_instance: true` — loader-level PID lock prevents double-spawn from parallel sessions.
- `enabled_by_default: false` — opt-in (the daemon is a heavyweight Node process).
- All boundary imports go through `plugin_sdk/*`; zero `opencomputer.*` imports.

## Source

Open Design is Apache-2.0. Plugin code Apache-2.0.
- Upstream: [github.com/nexu-io/open-design](https://github.com/nexu-io/open-design)
- Quickstart: [open-design.ai](https://open-design.ai)
