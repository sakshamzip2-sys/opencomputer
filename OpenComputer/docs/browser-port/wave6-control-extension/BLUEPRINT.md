# Wave 6 — Browser-bridge Control Extension (v0.6)

> **Status**: planned. Surfaced from real LearnX-flow testing on 2026-05-03 + architectural diff vs OpenCLI.
> **Branch**: `feat/wave6-control-extension` (to be created)
> **Estimated scope**: ~3000 LOC across the extension + Python daemon integration + tests
> **Ownership**: any session can pick this up — this BLUEPRINT is self-contained.

---

## TL;DR

Port OpenCLI's Chrome extension (Apache 2.0) into OpenComputer as a sibling extension at `extensions/browser-bridge/control-extension/`. This eliminates the `chrome-devtools-mcp` dependency for the active-control path, removes the `chrome://inspect/#remote-debugging` toggle requirement, and gives us the same "drive your real Chrome" UX OpenCLI users get.

Two-track ship:
- **Track 1 — managed Chrome (`opencomputer` profile)**: bake `--load-extension=...` into the launch args. Zero user action; extension auto-loads on every managed-Chrome boot.
- **Track 2 — real Chrome (`user` profile)**: submit the extension to Chrome Web Store. One-click install for users who want their real Chrome driven instead of an isolated managed Chrome.

The control extension is **separate** from our existing passive `extensions/browser-bridge/extension/` (Layer 4 ambient awareness). Two extensions, two permission scopes — narrow ambient stays cheap; control gets the heavy `chrome.debugger` permission only when users opt in.

---

## Why this matters

### The problem we're closing

Real LearnX-flow testing on 2026-05-03 (session `f187c279…`) surfaced friction:

1. Every fresh Chrome session requires the user to toggle `chrome://inspect/#remote-debugging` before `chrome-devtools-mcp` can attach. OpenCLI never asks for this.
2. Even with the toggle, our chrome-devtools-mcp launches **without** `--browser-url`, so it spawns its own Chrome with its own user-data dir at `~/.opencomputer/browser/<profile>/user-data/` (~232 MB) — separate from the user's real Chrome. None of the user's logins are available; they have to re-login per site inside our isolated Chrome.

Disk evidence collected during diagnosis:
```
~/.opencomputer/browser/openclaw/user-data/   191 MB (separate Chrome)
~/.opencomputer/browser/user/user-data/       232 MB (separate Chrome — not the user's real one)
~/Library/Application Support/Google/Chrome/  user's real Chrome with all logins
```

### How OpenCLI solves it

OpenCLI ships a Chrome extension with the `chrome.debugger` permission. The extension lives **inside the user's real Chrome**, attaches to tabs via `chrome.debugger.attach({tabId})`, and drives them via CDP from the Chrome runtime itself. No external port, no toggle, no second Chrome process.

Their architecture (verified via their open-source extension at [github.com/jackwener/opencli](https://github.com/jackwener/opencli)):

```
opencli (CLI)         daemon (Node.js)         Browser Bridge             YOUR
one-shot proc  ↔HTTP↔ localhost:19825   ↔WS↔  extension (Chrome MV3 SW) ↔chrome.debugger↔ Chrome tab
                      long-lived
```

### The architectural delta we're closing

We have the foundation for this (passive ambient extension at `extensions/browser-bridge/extension/`). v0.6 is the active-control upgrade.

---

## What ships in v0.6

### 1. New extension: `extensions/browser-bridge/control-extension/`

Verbatim port (with our 5 deltas — see §"Deviations" below) of OpenCLI's extension at [`/tmp/opencli/extension/src/`](https://github.com/jackwener/opencli/tree/main/extension/src). License: Apache 2.0; we attribute on every file.

Source files to port:

| Source file (OpenCLI) | LOC | Action | Target file (ours) |
|---|---|---|---|
| `protocol.ts` | 104 | Verbatim | `extensions/browser-bridge/control-extension/src/protocol.ts` |
| `identity.ts` | 71 | Verbatim | `.../src/identity.ts` |
| `cdp.ts` | 554 | Verbatim | `.../src/cdp.ts` |
| `background.ts` | 1585 | Adapt (daemon URL + workspace defaults) | `.../src/background.ts` |
| `manifest.json` | ~30 | Adapt (name, description, web-accessible-resources) | `.../manifest.json` |
| `popup.html` + `popup.js` | 223 | Rebrand (logo, name, link) | `.../popup.{html,js}` |

Total: ~2300 LOC of TS source, ~150 LOC of HTML/CSS, plus our integration code.

### 2. Python daemon WS server

Extend `extensions/browser-bridge/plugin.py` to host a WebSocket endpoint at `ws://127.0.0.1:18791/ext` (we already use 18791 for the ambient bridge HTTP listener — keep the port, add the WS path).

The daemon translates between our internal wire-protocol and OpenCLI's 14-action protocol that the extension speaks. Routing:

```
Browser tool action          → daemon translation     → extension action
─────────────────────────────────────────────────────────────────────────
Browser(action="navigate")   → action="navigate"      → Page.navigate
Browser(action="evaluate")   → action="exec"          → Runtime.evaluate
Browser(action="screenshot") → action="screenshot"    → Page.captureScreenshot
Browser(action="resource_timing") → action="exec"     → Runtime.evaluate (the JS we already use)
adapter ctx.trpc_query(...)  → action="exec"          → Runtime.evaluate (our trpc-fetch JS)
adapter ctx.cookies(domain)  → action="cookies"       → chrome.cookies.getAll
```

Three new modules:
- `extensions/browser-bridge/control_protocol.py` — wire types matching OpenCLI's `protocol.ts`, plus our adapter-to-extension translation
- `extensions/browser-bridge/control_daemon.py` — WS server, lease-tracking, command routing
- `extensions/browser-bridge/control_driver.py` — `BrowserBridgeControlDriver` with the same `ProfileDriver` interface as `chrome_mcp` (so the existing `server_context/lifecycle.py` plumbing works without changes)

### 3. Profile driver wiring

Add a third option to `BrowserDriver`:
```python
BrowserDriver = Literal["managed", "existing-session", "control-extension"]
```

(After Wave 5's rename, `"managed"` is what was `"openclaw"`; `"existing-session"` is the chrome-devtools-mcp path; we add `"control-extension"` for the new path.)

Mode mapping:
- `opencomputer` profile → `driver="managed"` + `--load-extension=.../control-extension/dist` baked into Chrome launch args. Track 1.
- `user` profile → `driver="control-extension"` + extension installed by user via Web Store. Track 2.
- `chrome-mcp-fallback` profile (new optional profile) → `driver="existing-session"`. Kept for headless / non-Chrome use cases.

### 4. Track 1: auto-load extension into managed Chrome

When `opencomputer` profile launches Chrome, append `--load-extension=<repo>/extensions/browser-bridge/control-extension/dist` to Chrome args. Zero user action.

Code lives in `extensions/browser-control/chrome/launch.py` — `_build_chrome_args` adds the flag when `driver=="managed"`.

### 5. Track 2: Chrome Web Store submission

Out of scope for v0.6 PR (Web Store review takes ~3 days and is async). Tracked separately: maintainer submits the same extension after v0.6 merges.

User onboarding flow (post-Web-Store):
```
opencomputer setup
> "Install OpenComputer Browser Bridge from the Chrome Web Store?"
> [Y/n] _
> Opens https://chrome.google.com/webstore/detail/<our-id> in default browser
> User clicks "Add to Chrome"
> Extension auto-pairs with running daemon via the contextId in chrome.storage.local
> Done.
```

---

## Deviations from OpenCLI (5 intentional)

| # | Deviation | Why |
|---|---|---|
| 1 | Daemon is Python (not Node.js) | OpenComputer is a Python project; we already have `extensions/browser-bridge/` as a Python plugin. The wire protocol on the WS line stays identical — only the daemon implementation language changes. |
| 2 | Two extensions, not one | We keep `extensions/browser-bridge/extension/` (passive ambient awareness, only `tabs` permission) for Layer 4. Add `control-extension/` as a sibling with `chrome.debugger`. Cleaner permission story; users who only want ambient awareness don't see the yellow warning bar. |
| 3 | Track 1 (managed-Chrome auto-load) | OpenCLI doesn't have managed-Chrome at all — they only operate in user's real Chrome. We keep the `opencomputer` (managed) profile path and gain extension support there too via `--load-extension`. Users get isolated-Chrome-with-control AND real-Chrome-with-control as two distinct profile options. |
| 4 | Default workspace mode | OpenCLI defaults adapter runs to `owned` (new tab/window in user's Chrome, 30s idle close). We adopt the same default but expose `--bind` flag for opting into `bound:*` mode (use user's currently-focused tab). |
| 5 | v0.6 ships 8 of 14 actions | MVP: `exec`, `navigate`, `tabs`, `cookies`, `screenshot`, `network-capture-start`, `network-capture-read`, `cdp` (raw passthrough). The other 6 (`set-file-input`, `insert-text`, `bind`, `frames`, `sessions`, `close-window`) come in v0.6.x as adapters demand them. Keeps the v0.6 PR reviewable. |

---

## Implementation order (for the next session)

This is the order to execute. Each step is independently testable, so you can pause/resume at any boundary.

### Step 1 — Source check + repo scout (~30 min)

1. Confirm OpenCLI source is present at `/tmp/opencli/extension/src/` (already cloned during this session). If not, `git clone --depth=1 https://github.com/jackwener/opencli.git /tmp/opencli`.
2. Read `/tmp/opencli/extension/src/protocol.ts`, `identity.ts`, `cdp.ts`, `background.ts` end-to-end. They total ~2300 LOC; budget 30 min.
3. Read OpenCLI's LICENSE — confirm Apache 2.0 (we already verified this).

### Step 2 — Scaffold the control extension (~1 day)

1. `mkdir -p extensions/browser-bridge/control-extension/{src,dist,icons}`
2. Create `manifest.json`:
   ```json
   {
     "manifest_version": 3,
     "name": "OpenComputer Browser Bridge",
     "version": "0.6.0",
     "description": "Active control bridge for the OpenComputer agent. Drives Chrome tabs via chrome.debugger over a localhost WebSocket.",
     "permissions": ["debugger", "tabs", "cookies", "activeTab", "alarms", "storage"],
     "host_permissions": ["<all_urls>"],
     "background": {"service_worker": "dist/background.js", "type": "module"},
     "icons": {"16": "icons/icon-16.png", "32": "icons/icon-32.png", "48": "icons/icon-48.png", "128": "icons/icon-128.png"},
     "action": {"default_title": "OpenComputer Browser Bridge", "default_popup": "popup.html"},
     "content_security_policy": {"extension_pages": "script-src 'self'; object-src 'self'"},
     "homepage_url": "https://github.com/sakshamzip2-sys/opencomputer"
   }
   ```
3. Port `src/protocol.ts` verbatim. Adapt:
   - `DAEMON_PORT = 18791` (was 19825 in OpenCLI; we reuse our existing browser-bridge port)
   - `DAEMON_WS_URL = ws://localhost:18791/ext`
   - `DAEMON_PING_URL = http://localhost:18791/ping`
4. Port `src/identity.ts` verbatim.
5. Port `src/cdp.ts` verbatim. No changes needed.
6. Port `src/background.ts`. Changes:
   - Replace `[opencli]` log prefixes with `[opencomputer]`
   - Replace `OPENCLI_*` constants/keys with `OPENCOMPUTER_*` (e.g. `OPENCOMPUTER_WINDOW_FOCUSED`, registry storage key `opencomputer_target_lease_registry_v1`)
   - `__OPENCLI_COMPAT_RANGE__` declare → `__OPENCOMPUTER_COMPAT_RANGE__`
7. Add a build script (`extensions/browser-bridge/control-extension/build.sh` or similar) that bundles src/*.ts → dist/background.js. OpenCLI uses esbuild; we should too. Alternatively, leverage our Node setup if there's existing tooling.
8. Build a minimal popup (port `popup.html`/`popup.js`). Apple-style status indicator.
9. Generate icons (4 sizes: 16, 32, 48, 128). Use OpenComputer brand color `#FF4500` (already our default).

**Attribution header on every ported file:**
```typescript
// Adapted from OpenCLI (https://github.com/jackwener/opencli) under Apache License 2.0.
// Original: extension/src/<filename>.ts
// Modifications: see git log of this file in github.com/sakshamzip2-sys/opencomputer
```

Plus `extensions/browser-bridge/control-extension/LICENSES/openclai-apache-2.0.txt` with the full Apache 2.0 license text.

### Step 3 — Python daemon WS server (~1 day)

1. Add `extensions/browser-bridge/control_protocol.py`:
   - Pydantic models matching `protocol.ts` Command/Result types
   - `Action = Literal["exec", "navigate", "tabs", "cookies", "screenshot", "network-capture-start", "network-capture-read", "cdp"]` (8 of 14 for MVP)

2. Add `extensions/browser-bridge/control_daemon.py`:
   - WebSocket server bound to `127.0.0.1:18791/ext` using `websockets` lib (already a dep — verify)
   - Per-connection state: `contextId`, `extensionVersion`, `compatRange`
   - Send Command via WS, await matching Result by id
   - 30s timeout per command (matches OpenCLI's idle-leases default)
   - Health endpoint `GET /ping` returns `{"daemon": "opencomputer", "version": "0.6.0"}`

3. Add `extensions/browser-bridge/control_driver.py`:
   - `BrowserBridgeControlDriver` implementing the `ProfileDriver` interface from `extensions/browser-control/server_context/lifecycle.py`
   - `spawn_chrome_mcp` → `spawn_browser_bridge_control` (rename for the new mode)
   - Returns a client object that the existing dispatcher code can call into

4. Wire the new driver into `extensions/browser-control/_dispatcher_bootstrap.py`:
   - When `driver=="control-extension"`, use the control daemon instead of chrome-devtools-mcp

### Step 4 — Track 1: managed-Chrome auto-load (~half day)

1. In `extensions/browser-control/chrome/launch.py`, find `_build_chrome_args`. After existing args, append:
   ```python
   ext_path = Path(__file__).parent.parent.parent / "browser-bridge" / "control-extension" / "dist"
   if ext_path.exists():
       args.append(f"--load-extension={ext_path}")
   ```
2. The managed-Chrome launch picks up the extension automatically. No user action.
3. The Chrome instance now has `chrome.debugger` capability via the extension. Adapter calls route through the daemon → extension → CDP.

### Step 5 — Tests (~1 day)

1. Unit tests for `control_protocol.py` (round-trip Command/Result encoding)
2. Mock-WS tests for `control_daemon.py` (commands with synthesized responses)
3. Integration test: spawn the extension via `--load-extension` in headless Chrome, send a `navigate` command, verify the page actually navigated.
4. Confirm existing `tests/test_browser_port_*.py` still pass.

### Step 6 — Docs + commit (~half day)

1. Update `extensions/browser-bridge/README.md` with the new control-extension story
2. Update `docs/browser-port/IMPLEMENTATION_STATUS.md` with v0.6 status
3. Update `docs/browser-port/wave4-adapters/DEFERRED.md` to mark the v0.5-PRIORITY browser-bridge section as `→ shipped in v0.6`
4. Commit per-step (granular history) and open PR

---

## File-by-file checklist (mechanical)

Use this when actually doing the port:

```
[ ] extensions/browser-bridge/control-extension/manifest.json
[ ] extensions/browser-bridge/control-extension/src/protocol.ts
[ ] extensions/browser-bridge/control-extension/src/identity.ts
[ ] extensions/browser-bridge/control-extension/src/cdp.ts
[ ] extensions/browser-bridge/control-extension/src/background.ts
[ ] extensions/browser-bridge/control-extension/popup.html
[ ] extensions/browser-bridge/control-extension/popup.js
[ ] extensions/browser-bridge/control-extension/icons/icon-16.png
[ ] extensions/browser-bridge/control-extension/icons/icon-32.png
[ ] extensions/browser-bridge/control-extension/icons/icon-48.png
[ ] extensions/browser-bridge/control-extension/icons/icon-128.png
[ ] extensions/browser-bridge/control-extension/LICENSES/openclai-apache-2.0.txt
[ ] extensions/browser-bridge/control-extension/build.sh (or package.json + tsconfig.json + esbuild config)
[ ] extensions/browser-bridge/control-extension/README.md
[ ] extensions/browser-bridge/control_protocol.py
[ ] extensions/browser-bridge/control_daemon.py
[ ] extensions/browser-bridge/control_driver.py
[ ] extensions/browser-bridge/plugin.py (extend to host the WS endpoint)
[ ] extensions/browser-control/chrome/launch.py (add --load-extension)
[ ] extensions/browser-control/profiles/config.py (add "control-extension" to BrowserDriver)
[ ] extensions/browser-control/profiles/resolver.py (default user profile → control-extension)
[ ] extensions/browser-control/_dispatcher_bootstrap.py (wire new driver)
[ ] tests/test_browser_bridge_control_protocol.py
[ ] tests/test_browser_bridge_control_daemon.py
[ ] tests/test_browser_bridge_control_e2e.py (headless Chrome integration)
[ ] docs/browser-port/IMPLEMENTATION_STATUS.md (add v0.6 row)
[ ] docs/browser-port/wave4-adapters/DEFERRED.md (mark v0.5-PRIORITY shipped)
[ ] CHANGELOG.md (v0.6 entry)
```

---

## Reference — OpenCLI's exact mechanism

This is what we're porting. Verbatim from session research.

### Two operating modes (per workspace)

| Workspace prefix | Mode | Idle timeout | What happens |
|---|---|---|---|
| `bound:*` | borrowed | none (-1) | `chrome.tabs.query({active: true, lastFocusedWindow: true})` finds user's current tab; `chrome.debugger.attach({tabId})` to it. Pinned to that tab until unbound. |
| `browser:*` / `operate:*` | owned | 600s (10min) | `chrome.windows.create()` opens a new automation window in same Chrome process. Long-running. |
| (default) | owned | 30s | Same as browser/operate but auto-closes after 30s idle. The adapter-run path. |

### CDP domains used

| Domain | Methods | Purpose |
|---|---|---|
| `Page` | `captureScreenshot`, `getFrameTree`, `getLayoutMetrics`, `navigate` | Page state + navigation |
| `Network` | `enable`, `getRequestPostData`, `getResponseBody`, `loadingFinished`, `requestWillBeSent`, `responseReceived` | Network capture |
| `Runtime` | `enable`, `evaluate`, `executionContextCreated`, `executionContextDestroyed` | Run JS in page |
| `Input` | `dispatchKeyEvent`, `dispatchMouseEvent`, `insertText` | Key/mouse/text input |
| `DOM` | `enable`, `getBoxModel`, `getContentQuads`, `getDocument`, `querySelector`, `querySelectorAll`, `scrollIntoViewIfNeeded`, `setFileInputFiles` | DOM ops |

### Chrome-API tab management (not CDP — uses standard extension APIs)

`chrome.tabs.create/get/update/remove/move/query`, `chrome.windows.create/get/onRemoved/remove`, `chrome.cookies.getAll` (for COOKIE-strategy auth — pulls cookies directly without round-tripping through CDP).

### MV3 service-worker survival

MV3 SWs die after 30s idle. To survive:
- Lease registry persisted to `chrome.storage.local` under `opencli_target_lease_registry_v1` (rename to `opencomputer_target_lease_registry_v1`)
- `chrome.alarms` for idle-timeout fallback (`setTimeout` doesn't survive SW kill)
- On SW restart, `reconcileTargetLeaseRegistry()` rebuilds in-memory state

This is **load-bearing**. Without it, every 30s the extension forgets all leases and orphans windows.

### Identity model

`targetId` (CDP UUID, cross-process stable) ↔ `tabId` (Chrome Tabs API int, extension-internal). Daemon refers to pages by `targetId`; extension translates via `chrome.debugger.getTargets()` cache. Cache is rebuilt lazily on miss; second miss → throw "stale page identity" (no guessing).

---

## Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Web Store review rejects our extension | Medium | Track 1 (`--load-extension` for managed Chrome) doesn't depend on Web Store. Ship Track 1 first; Track 2 unblocks when Web Store approves. |
| `chrome.debugger.attach` triggers permanent yellow warning bar | Certain | Document this as expected. Two-extension split keeps the yellow bar off the ambient extension. Users who care about not seeing the bar use chrome-devtools-mcp fallback. |
| OpenCLI's lease/idle code has bugs we inherit | Low | OpenCLI's tests are present at `/tmp/opencli/extension/src/*.test.ts` — port the test suite too where it makes sense. |
| Service-worker storage migration on extension version bump | Medium | Use `version` field in storage keys (`v1`, `v2`). Old data ignored if version mismatch. Idempotent migrations. |
| WebSocket port collision with another tool on user's machine | Low | Port 18791 is already our existing browser-bridge port; users have already implicitly accepted it. Provide a config option to override. |

---

## Acceptance criteria

The wave is "done" when:

1. ✅ Control extension loads in unpacked-mode Chrome (`chrome://extensions` → Load unpacked → `extensions/browser-bridge/control-extension/`)
2. ✅ Extension auto-connects to running OpenComputer daemon via WS
3. ✅ Popup shows "Connected to daemon" + daemon version + contextId
4. ✅ `Browser(action="navigate", url="https://example.com")` from agent → extension → tab actually navigates
5. ✅ `LearnxAssignments` adapter runs end-to-end against user's REAL Chrome (with real LearnX cookies) — no chrome-devtools-mcp involved
6. ✅ Track 1: launching `opencomputer` profile auto-loads the extension (zero user action)
7. ✅ All existing `test_browser_port_*.py` pass (no regression)
8. ✅ Yellow warning bar appears on debugged tabs (expected — document this)
9. ✅ DEFERRED.md updated to mark v0.5-PRIORITY as shipped
10. ✅ Single PR opened with proper Apache-2.0 attribution

Track 2 (Web Store) is acceptance criteria for a follow-up release, not v0.6 itself.

---

## Notes for the picking-up session

If you're a fresh session reading this:

1. The user wants this benchmark: `/Users/architsakri/Desktop/opencli-plugin-learnx-atria/` — their existing OpenCLI plugin. After v0.6 ships, re-run the LearnX adapter authoring flow autonomously. It should produce 5 adapters (`courses`, `assignments`, `today`, `active`, `grades`) using the user's real LearnX session.
2. The disk-rename done in PR #434 (rename openclaw → opencomputer) should already be merged. If not, rebase off main first.
3. Bug F (liveness probe on bring-up) and Bug C-reprise (Playwright owner-task) should also be merged. If not, they're orthogonal — v0.6 work doesn't depend on them, but both are merging concurrently.
4. The user explicitly authorized two-track ship (managed + real-Chrome) and verbatim port from OpenCLI Apache 2.0 source.
5. The `chrome-devtools-mcp` dependency stays in v0.6 as a fallback (for headless / non-Chrome / users who refuse to install the extension). Mark for deprecation in v0.7+.

Pick up from Step 1 above.
