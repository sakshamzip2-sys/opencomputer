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

The plugin is **enabled by default** — `oc design …` verbs are
available out of the box. You only need to install + build open-design
itself once.

```bash
# 1. Clone open-design somewhere the plugin can find it:
git clone https://github.com/nexu-io/open-design ~/.open-design
# (or set OPEN_DESIGN_HOME=/path/to/open-design)

# 2. Build the daemon once (needs Node 22+ and pnpm 10.33; engines
#    pin ~24 but Node 22 works in practice with the engine-check skip):
cd ~/.open-design
corepack enable
pnpm install
pnpm --filter '@open-design/contracts' --filter '@open-design/platform' \
     --filter '@open-design/sidecar-proto' --filter '@open-design/sidecar' \
     --filter '@open-design/daemon' build
npm rebuild better-sqlite3  # native binding for the Node version on PATH

# 3. Start the daemon:
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
- `single_instance: false` — the plugin's `register()` is cheap (wires
  verbs only), so the loader lock would be harmful with auto-enable
  across parallel sessions. Daemon-level singleton-ness is enforced by
  the profile-scoped PID file at
  `~/.opencomputer/<profile>/locks/open-design.pid` — `start()` refuses
  to spawn a second daemon when a live PID is recorded.
- `enabled_by_default: true` — verbs available out of the box. The
  daemon itself stays opt-in: `oc design start` brings it up only when
  the user asks.
- All boundary imports go through `plugin_sdk/*`; zero `opencomputer.*` imports.

## Source

Open Design is Apache-2.0. Plugin code Apache-2.0.
- Upstream: [github.com/nexu-io/open-design](https://github.com/nexu-io/open-design)
- Quickstart: [open-design.ai](https://open-design.ai)
