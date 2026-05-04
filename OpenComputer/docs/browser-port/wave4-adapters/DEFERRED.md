# Browser-port DEFERRED roadmap (v0.5+)

> Tracker for everything we deliberately did NOT include in v0.4 / Wave 4 (adapter promotion). Organized by intended wave so future sessions can pick up cleanly.
>
> Updated as v0.4 ships (or as priorities flip). Last updated: 2026-05-04 by s1.

---

## How to use this doc

When starting a new wave (v0.5, v0.6, ...):

1. Read the relevant section below
2. Pick the chunk you're going to ship in that wave
3. Read the corresponding "what to build" notes — they capture our thinking from when we deferred
4. Check the [parent BLUEPRINT.md](BLUEPRINT.md) for any context references that still apply
5. Promote the chunk into a new BLUEPRINT under `docs/browser-port/wave5-*/` (or whatever wave number)
6. Update this doc to mark it `→ shipped in vX.Y`

---

## v0.5-PRIORITY — Browser-bridge active control (close the chrome://inspect UX gap)

> **Why this is first.** Surfaced during real LearnX-flow testing on 2026-05-03: every fresh Chrome session requires the user to toggle `chrome://inspect/#remote-debugging` before `chrome-devtools-mcp` can attach. OpenCLI never asks for this — they ship a Chrome extension that owns the `chrome.debugger` permission and never depends on the external CDP port. Until we close this gap, every new install / new profile hits the same friction wall.

**The architectural difference**:

| | OpenCLI | OpenComputer (today) |
|---|---|---|
| Attach mechanism | Chrome extension with `chrome.debugger` permission | External `chrome-devtools-mcp` over CDP port 9222 |
| User setup | Install extension once → done | Toggle `chrome://inspect/#remote-debugging` per Chrome session |
| Multi-profile | Extension installed per profile | One CDP port shared across profiles |
| Tab control | Native via extension API | Routed through MCP wrapper |

**Foundation already in tree**: [`extensions/browser-bridge/extension/`](../../../extensions/browser-bridge/extension/) is currently a passive ambient-awareness extension (it observes Chrome state, doesn't drive it). v0.5-priority promotes it to **active control**.

**For our shape (~500-800 LOC)**:
- Extend [`extensions/browser-bridge/extension/manifest.json`](../../../extensions/browser-bridge/extension/manifest.json) to declare `chrome.debugger` + `tabs` + `webNavigation` permissions
- Add a control-channel: extension exposes a localhost websocket (or named pipe) that the Python side connects to
- Port the subset of `chrome-devtools-mcp` actions we actually use into extension-side handlers:
  - `new_page`, `close_page`, `select_page`, `list_pages` — `chrome.tabs.*` API
  - `navigate`, `evaluate`, `screenshot`, `wait_for_*` — `chrome.debugger.attach()` + `Runtime.evaluate` / `Page.navigate`
  - `resource_timing`, network capture — `chrome.debugger` `Network.*` events
- New driver in `extensions/browser-control/server/` — `BrowserBridgeDriver` peer of the existing `chrome_mcp` driver
- Wire the user-profile path (`profile_name="user"`) through `BrowserBridgeDriver` instead of `chrome_mcp`; CI / synthetic profiles keep using Playwright/MCP
- Setup story: `opencomputer browser-bridge install` opens Chrome to the extension's unpacked-load page with explicit instructions; one-time per machine, no per-session toggle

**Wins over chrome-devtools-mcp**:
- Zero `chrome://inspect` toggling
- Multi-profile clean (extension lives in the profile, not the global Chrome instance)
- No external Node process; the bridge is just Chrome itself
- Survives Chrome restarts without re-attach

**Risk / wrinkle**:
- Extension has to be loaded as unpacked (or shipped via Chrome Web Store — separate review track)
- Some CDP coverage gaps in the extension API surface (e.g. `Target.*` is partially available via `chrome.debugger`); investigate per-action before porting
- We still want `chrome-devtools-mcp` as a fallback for headless / CI / non-Chrome browsers

**Estimated scope**: ~500-800 LOC across the extension + a new Python driver + setup CLI

**Foundations already in v0.4/v0.5**: `BaseBrowserHandler` driver seam, owner-task pattern from PR #423 (Bug C fix), `DriverUnsupportedError` 501 mapping (Bug E fix). The driver swap point is already clean.

---

## v0.5 — High-leverage capabilities (next planned wave)

### A. Autofix flow (auto-repair broken adapters)

**The problem this solves**: adapters break when sites change. Bilibili pushes a redesign → the API endpoint moves → adapter starts returning empty results. Without autofix, every site change creates a manual maintenance burden.

**OpenCLI's solution**: agent reads the trace from a failed adapter run, walks back through the discovery flow (5 patterns), patches the adapter without human intervention. Shipped as `opencli-autofix` skill.

**For our shape (~400 LOC)**:
- A skill at `extensions/browser-control/skills/adapter-autofix/SKILL.md` that drives the repair flow
- New action `Browser(action="adapter_repair", site, name)` that:
  1. Reads the trace from the most recent failure
  2. Reads the existing adapter source
  3. Reads `~/.opencomputer/<profile>/sites/<site>/{endpoints, notes}.md`
  4. Re-runs site recon (`Browser(action="analyze")`)
  5. Diffs the new findings against the cached endpoints
  6. Hands the agent a structured diff + the adapter source for surgical edits
  7. Re-runs `verify` after the repair
- Verification fixtures (already in v0.4) are what flag adapters that need repair

**Foundations already in v0.4**: trace artifacts, verification fixtures, site memory. Wave 5 just adds the repair orchestration on top.

**Estimated scope**: ~400 LOC + a skill markdown file

---

### B. External CLI registration

**The problem this solves**: opencomputer should be able to wrap any local binary as an agent-callable tool. Right now adapters are website-specific. External CLI registration would let `gh`, `docker`, `kubectl`, custom scripts, etc. show up the same way.

**OpenCLI's solution**: `opencli external register <name>` — registers a binary; opencli routes commands through.

**For our shape (~200 LOC)**:
- New CLI: `opencomputer external register <name> --binary <path> --description <text>`
- Generates a registration file at `~/.opencomputer/<profile>/external/<name>.yaml`
- The `adapter-runner` (or a new sister plugin `external-runner`) discovers these and registers each as a tool
- Tool's `execute(call)` shells out to the binary with the call's arguments mapped to argv
- Output capture: stdout → `ToolResult.content`; non-zero exit → `is_error=True`

**Estimated scope**: ~200 LOC + tests

---

### C. Deeper Electron-app control

**The problem this solves**: v0.4's starter pack has 2 Electron-app adapters (cursor + chatgpt-app) but the integration is shallow — it's CDP-attach with the user's app already open. v0.5 should add app-spawning helpers, app-detection (which Electron apps are running?), and per-app skill files for common apps.

**For our shape (~250 LOC)**:
- `extensions/browser-control/chrome/electron_apps.py` — knows the CDP debug ports for popular Electron apps (Cursor, Codex, Notion, ChatGPT, Slack, VS Code, Discord, Linear desktop)
- Detect-running helper: `Browser(action="electron_apps_running")` returns which are alive
- Per-app skill files: `extensions/browser-control/skills/electron-cursor/SKILL.md`, `electron-notion/SKILL.md`, etc.
- More starter-pack adapters for Electron apps (notion/recent_pages, slack/unread, etc.)

**Estimated scope**: ~250 LOC + 5-6 skill markdown files + 4-5 starter adapters

---

### D. OpenCLI shell-out backend

**The problem this solves**: porting all 100+ OpenCLI adapters is a maintenance trap (we discussed this — adapters break, we'd inherit their burden). Instead: if the user has Node + opencli installed, we offer a generic `OpenCLIBackend` tool that proxies any of their commands.

**For our shape (~150 LOC)**:
- `extensions/opencli-backend/` plugin — separate from browser-control
- On register, checks `which opencli` + `opencli doctor` health
- If healthy: registers a meta-tool `OpenCLI(site, command, ...args)` that shells out
- Auto-discovery: parses `opencli list` output and registers tools per site
- Cross-runtime call: `subprocess.run(["opencli", site, command, ...])`, JSON output
- ToolResult mapping: stdout → content; exit code → is_error mapping

**Wrinkle**: opencli expects its own Chrome profile setup (their extension installed). We use Chrome MCP. The two might not play nice on the same Chrome instance — needs investigation. Possible mitigation: opencli runs in its own browser context entirely; ours stays separate.

**Estimated scope**: ~150 LOC + Node/opencli health check + tests

---

## v0.6 — Polish + ecosystem

### E. Adapter eject / reset

**Use case**: user wants to tweak the bundled `hackernews/top` adapter for their needs. Currently they'd have to fork the whole plugin.

**OpenCLI's solution**: `opencli adapter eject <site>` copies the bundled adapter into the user's local `~/.opencli/clis/`, where edits override the bundled version. `opencli adapter reset <site>` removes the local override.

**For our shape (~150 LOC)**:
- `opencomputer adapter eject <site>/<name>` copies `extensions/browser-control/adapters/<site>/<name>.py` → `~/.opencomputer/<profile>/adapters/<site>/<name>.py`
- adapter-runner's discovery already prefers user-local over bundled (just needs explicit ordering)
- `opencomputer adapter reset <site>/<name>` deletes the user-local copy

**Estimated scope**: ~150 LOC + tests

---

### F. Smart-search across adapters

**Use case**: as the adapter count grows past ~20, finding the right one is a problem. "I want a tool to track Amazon prices" — does that exist? Smart-search makes the registry searchable.

**For our shape (~100 LOC)**:
- New CLI: `opencomputer adapter search <query>`
- Searches across all registered adapters' descriptions, columns, sites, args
- Returns ranked matches with `<site>/<name>: <description>`

**Estimated scope**: ~100 LOC

---

### G. Adapter sharing via richer plugin registry

**Use case**: v0.4's `opencomputer plugin install github:user/repo` works but discovery is manual (user has to know the repo URL). A registry would let users browse / search community adapter packs.

**For our shape**: gated on real demand. If the community starts publishing adapter packs, build a registry. Otherwise, defer indefinitely.

**Possible shape**: a `~/.opencomputer/registry.json` that lists known adapter packs by category. `opencomputer plugin search <query>` searches it. Registry itself is a simple JSON file in a public GitHub repo, possibly community-PR'd like Homebrew tap.

**Estimated scope**: ~200 LOC if shipped, depends on registry-host design

---

## v0.7+ — Long-deferred (parked unless explicit demand)

### H. Hermes-style provider seam

**Original intent (from the OpenClaw port BLUEPRINT)**: `BaseBrowserProvider` ABC + retrofit so Browserbase / Firecrawl / Camoufox can plug in as alternate backends.

**Why deferred**: real users haven't asked for it. Adapter promotion (v0.4) addresses most of the use cases the provider seam was supposed to solve (e.g. Firecrawl-style scrape becomes "an adapter that uses the page's API instead").

**If revisited**: see [the original BLUEPRINT §11](../BLUEPRINT.md) for the design. Estimated scope ~600 LOC for the ABC + the first non-Playwright provider implementation.

---

## Cross-cutting notes

### Things v0.4 made *easier* for v0.5+

- **Trace artifacts** are the foundation for autofix. v0.5 just builds the orchestration layer.
- **Verification fixtures** are what flag adapters that need repair. They exist now.
- **Site memory** persistence means each authoring session's findings carry forward. Compound effect on adapter development speed grows.
- **Plugin packaging template** means distribution is solved. Sharing via plugin install just works.

### Things v0.4 made *harder* for v0.5+

- **None known yet.** If something surfaces during v0.5 implementation that v0.4 made awkward, document it here for v0.6+.

### Migration / compatibility commitments

- **Adapter recipe format is stable as of v0.4.** The `@adapter` decorator's keyword args + the `ctx` API surface should not break across minor versions. If we need to evolve them, ship a compatibility shim.
- **Site memory directory layout is stable.** External tools (e.g. opencli interop) can rely on the structure.
- **Verification fixture format is stable.** v0.5 autofix reads these; v0.6 ecosystem tools can too.

---

## How to flip something from "deferred" to "active"

When a deferred item gets prioritized:

1. Read this section + the parent BLUEPRINT
2. Decide which wave it goes in (v0.5, v0.6, etc.)
3. Create `docs/browser-port/wave<N>-<name>/BLUEPRINT.md` mirroring Wave 4's structure
4. Update this doc to remove the item from "deferred" and add it to "shipping in vX.Y"
5. Update top-level `docs/browser-port/IMPLEMENTATION_STATUS.md` to add a row for the new wave

The pattern is the same as Wave 4 — pick a chunk, write a BLUEPRINT, queue a subagent.
