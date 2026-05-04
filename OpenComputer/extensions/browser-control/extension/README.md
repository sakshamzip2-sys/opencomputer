# OpenComputer Browser Control Extension

Active control bridge for the OpenComputer agent. Drives Chrome tabs via `chrome.debugger` over a localhost WebSocket — no `chrome://inspect` toggle, no external `chrome-devtools-mcp` process.

This extension is the **third transport** for the `extensions/browser-control/` plugin, alongside the existing `managed` (Playwright) and `existing-session` (chrome-devtools-mcp) drivers.

## Two-track install

### Track 1 — Managed Chrome (zero user action)

When OpenComputer launches Chrome under the `opencomputer` profile, this extension is **automatically loaded** via Chrome's `--load-extension=...` flag. Nothing for the user to do.

```bash
opencomputer chat -p opencomputer
# Chrome opens with this extension pre-installed
```

The extension's popup (click the toolbar icon in that Chrome) shows daemon connection status.

### Track 2 — Real Chrome (one-click via Chrome Web Store)

For driving the user's real Chrome (with all their logins, tabs, etc.), the extension installs from the Chrome Web Store:

> **Status (v0.6):** Web Store submission is post-merge. Until it ships, you can install the unpacked extension manually:
>
> 1. Build: `bash build.sh` (or `npm install && npm run build`)
> 2. Open `chrome://extensions/` in your real Chrome
> 3. Toggle "Developer mode" on (top-right)
> 4. Click "Load unpacked" and select **this directory**
> 5. Configure your `user` profile to use `driver: control-extension`

When the Web Store listing is live, install becomes:
```
opencomputer setup
> "Install Browser Control extension from Chrome Web Store?" [Y/n]
> Opens https://chrome.google.com/webstore/detail/<our-id>
> Click "Add to Chrome"
> Done.
```

## Build

```bash
# One-time: install esbuild + typescript + chrome types
npm install

# Build dist/background.js
npm run build

# Or use the wrapper that does both:
bash build.sh
```

`dist/background.js` is the bundled MV3 service worker (loaded by `manifest.json`).

## Architecture (TL;DR)

```
opencomputer (CLI)         opencomputer daemon         Browser Control                     Chrome
agent / adapter call  ↔HTTP↔ extensions/browser-control ↔WS↔ extension (this dir)  ↔chrome.debugger↔ tab
                            (Python, port 18792)             (MV3 service worker)
```

Three operating modes (per workspace):

| Workspace prefix | Mode | Idle timeout | Behavior |
|---|---|---|---|
| `bound:*` | borrowed | none | Pinned to user's currently-focused tab. `chrome.debugger.attach({tabId})` to it. |
| `browser:*` / `operate:*` | owned | 10 min | New automation window in same Chrome process. Long-running. |
| (default) | owned | 30 s | New tab/window for adapter run. Auto-closes after 30 s idle. |

CDP domains used: `Page`, `Network`, `Runtime`, `Input`, `DOM`. Plus `chrome.tabs.*`, `chrome.windows.*`, `chrome.cookies.getAll` from the Chrome extension API surface.

## Permissions

```
debugger      ← attaches to tabs via chrome.debugger.attach (the heavy one — yellow warning bar)
tabs          ← list/create/close/move tabs
cookies       ← chrome.cookies.getAll for COOKIE-strategy adapter auth
activeTab     ← current-tab access for bind: workspaces
alarms        ← keepalive + idle-lease cleanup (survives MV3 SW death)
storage       ← chrome.storage.local for lease registry persistence

host_permissions: <all_urls>   ← needed for chrome.debugger to attach to any page
```

## Provenance

Adapted from [OpenCLI](https://github.com/jackwener/opencli) under Apache License 2.0. The full upstream license is at `LICENSES/openclai-apache-2.0.txt`. Each ported file carries an attribution header noting its OpenCLI origin and the modifications made.

Per-file mapping:

| File | OpenCLI source | Modifications |
|---|---|---|
| `manifest.json` | `extension/manifest.json` | Brand renames (name, description, homepage_url) |
| `src/protocol.ts` | `extension/src/protocol.ts` | DAEMON_PORT 19825 → 18792 only |
| `src/identity.ts` | `extension/src/identity.ts` | None (verbatim) |
| `src/cdp.ts` | `extension/src/cdp.ts` | Log-prefix renames `[opencli]` → `[opencomputer]` only |
| `src/background.ts` | `extension/src/background.ts` | Brand renames; storage keys `opencli_*` → `opencomputer_*`; `OPENCLI_*` constants → `OPENCOMPUTER_*` |
| `popup.html` / `popup.js` | `extension/popup.{html,js}` | Brand renames (logo, name, GitHub link) |
| `icons/*.png` | `extension/icons/*.png` | Placeholder; real OpenComputer brand icons TODO before Web Store submission |

## Status

- ✅ Track 1 wired (auto-loaded into managed Chrome)
- ⏳ Track 2 pending Web Store submission
- ⏳ Real OpenComputer brand icons TODO before Web Store submission
- 8 of 14 actions ship in v0.6 (`exec`, `navigate`, `tabs`, `cookies`, `screenshot`, `network-capture-start`, `network-capture-read`, `cdp`); rest in v0.6.x
