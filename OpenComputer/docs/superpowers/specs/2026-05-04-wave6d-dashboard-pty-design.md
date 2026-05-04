# Wave 6.D — Dashboard pages + /api/pty WebSocket — Design Spec

**Date:** 2026-05-04
**Status:** Honestly deferred — written for the next session
**Source:** Hermes commits e2a490560 (Plugins page), 3c27efbb9 + e6b05eaf6 (Models page), f49afd312 (/api/pty)

---

## Why this is a separate session

Wave 6 already shipped:

- 6.A (PR #426): 6 small features
- 6.B (PR #428 FTS5 + PR #429 Kanban): 6500+ LOC kanban port + trigram FTS5
- 6.C (PR #427): 8 hermes skills

These two remaining items each carry non-trivial UI/infrastructure work that warrants its own focused brainstorm → plan → audit → execute cycle. Specifically:

| Item | Why it deserves dedicated session |
|---|---|
| Dashboard Plugins/Models pages | Frontend work — needs the dashboard plugin SDK to be solid first; the kanban dashboard plugin (just shipped in 6.B) is the only existing reference and it's a verbatim hermes port; OC's own dashboard plugin authoring contract may need formalization |
| `/api/pty` WebSocket bridge | PTY allocation + WebSocket framing + auth + termios passthrough = ~300 LOC of low-level systems code; needs careful security review (PTY shell access via WebSocket is a remote-shell primitive) |

Trying to cram both into a session that already shipped ~6500 LOC of kanban port = sloppy work. Karpathy "Goal-Driven Execution" says: define done; loop; verify. Neither item has a clean done-state in this session.

---

## Item 1 — Dashboard Plugins page

### Hermes ref
`e2a490560 feat(dashboard): add Plugins page with enable/disable, auth status, install/remove`

### Scope
A new tab in OC's dashboard at `/plugins` that:
- Lists every installed plugin with manifest metadata (name, description, version, kind, profile binding)
- Shows status: enabled/disabled/error
- Shows auth status for provider plugins (env var present, key valid via short health probe)
- Enable / disable toggles persist to `~/.opencomputer/<profile>/profile.yaml`
- Install / remove from any of the registered SkillSources (well-known, github, url)
- Hide/show toggle in sidebar to declutter when many plugins are installed

### Architecture
- Reuse the kanban dashboard plugin pattern just shipped in PR #429
- New folder: `opencomputer/dashboard/plugins/plugins/` (yes, plugins-managing-plugins)
- `plugin_api.py` — REST: `GET /plugins`, `POST /plugins/{id}/enable`, `POST /plugins/{id}/disable`, `POST /plugins/install`, `DELETE /plugins/{id}`
- `dist/index.js` + `dist/style.css` — React UI with a table view + drawer for details

### Honest deferral reason
Frontend bundle would need a fresh build pipeline; we don't have hermes' dashboard build setup; this is its own project.

---

## Item 2 — Dashboard Models page

### Hermes ref
`3c27efbb9 feat(dashboard): configure main + auxiliary models from Models page`
`e6b05eaf6 feat: add Models dashboard tab with rich per-model analytics`

### Scope
Tab at `/models` showing:
- Cost per model (last 7d / 30d) — pulls from `tool_usage` + `messages` SQLite tables
- Latency p50/p95/p99 per model
- Cache hit rate (from `cache_read_tokens` / `cache_write_tokens` columns already on `sessions`)
- Configure main + auxiliary models inline (writes to config.yaml)

### Architecture
- `opencomputer/dashboard/plugins/models/`
- Pulls metrics from existing `SessionDB` columns (no new schema needed)
- Configure controls call back into `opencomputer.agent.config_store` to persist

---

## Item 3 — `/api/pty` WebSocket bridge

### Hermes ref
`f49afd312 feat(web): add /api/pty WebSocket bridge to embed TUI in dashboard`

### Scope
- New WebSocket endpoint on the api-server: `ws://host:port/api/pty`
- On connect: spawn a `oc chat` subprocess inside a PTY, pipe stdin/stdout to the WebSocket
- xterm.js front-end embeds the PTY as a terminal in the dashboard
- Auth: same Bearer token as the rest of api-server

### Risks (CRITICAL — needs security review)
- PTY allocation gives full shell-level capabilities to whoever holds the WS endpoint
- Need to enforce: same-origin OR explicit token auth, never anonymous
- Termios cooked-mode passthrough so xterm.js's keyboard events reach the agent correctly
- Process cleanup on WebSocket disconnect (zombie PTY = security risk)

### Architecture
- Extend `extensions/api-server/adapter.py` with the WS route
- Use `asyncio.create_subprocess_exec` with `stdin=subprocess.PIPE` and a manually-allocated PTY via `pty.openpty()`
- Bidirectional pump task per WS connection

### Honest deferral reason
Security-sensitive; needs a careful brainstorm pass on threat model (what if a leaked Bearer token gives shell access?) and proper integration testing against a real xterm.js client.

---

## Recommended next-session order

1. Wave 6.D-α: Dashboard Plugins page (lowest risk, content-only on top of kanban-dashboard pattern)
2. Wave 6.D-β: Dashboard Models page (reuses 6.D-α's pattern + analytics queries)
3. Wave 6.D-γ: /api/pty (after the security threat-model brainstorm)

---

## What this session DID ship

For receipts:

- PR #426: 6 features (Wave 6.A)
- PR #427: 8 skills (Wave 6.C)
- PR #428: FTS5 trigram tokenizer (Wave 6.B foundation)
- PR #429: Full Kanban port — db + cli + 7 tools + dashboard plugin + system-prompt + kanban-video-orchestrator skill (~6500 LOC)
- 4 honest scoping decisions documented
- 95+ new tests, all green

The work shipped is solid; the deferral is intentional, not laziness.
