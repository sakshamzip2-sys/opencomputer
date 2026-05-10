# Vendored — opencli-bridge

This plugin bridges [`@jackwener/opencli`](https://github.com/jackwener/opencli) into OpenComputer. Two pieces are redistributed here under the upstream's Apache-2.0 license; everything else is original OC code.

## Vendored

| Path | Source | License | Why |
|---|---|---|---|
| `extension/v1.0.6/` | [`opencli-extension-v1.0.6.zip`](https://github.com/jackwener/OpenCLI/releases/download/v1.7.14/opencli-extension-v1.0.6.zip) (full unpacked) | Apache-2.0 | Side-loaded into the agent's own Chrome via `AGENT_BROWSER_EXTENSIONS`. Needed for `chrome.debugger` access; can't be lazy-loaded. |

The OpenCLI Node CLI itself is installed via `npm` (project-local, see [package.json](../../package.json)) — NOT vendored as source. Pinned at `^1.0.6` (npm resolves to whatever 1.x is current; tested against 1.7.14).

## Original OC code (this plugin)

- `plugin.json`, `plugin.py` — manifest + register entry
- `dispatcher.py` — subprocess wrapper around the `opencli` CLI with HOME-shim for per-OC-profile state isolation
- `tools.py` — 5 `BaseTool` wrappers (`OpenCliList`, `OpenCliRun`, `OpenCliBrowse`, `OpenCliAuthor`, `OpenCliInspect`)
- `actions.py` — `OpenCliBridgeActions` for `adapter-runner` (drop-in alternative to `BrowserHarnessActions`)
- `doctor.py` — three-step health check
- `skills/opencli-routing/SKILL.md` — OC-flavored routing guide (NOT a mirror of upstream's 5 skills; we wrote our own concise version since the GH API fetch was sandboxed)

## Architectural notes (read these before re-syncing the extension)

### HOME-shim mechanism

OpenCLI hardcodes `os.homedir() / ".opencli"` for state ([upstream main.js:29](../../node_modules/@jackwener/opencli/dist/src/main.js)). We don't want to clobber the user's real `~/.opencli/`, so `plugin.py:_setup_home_shim()` builds:

```
<oc_profile_home>/
├── opencli/                       ← REAL state, per-OC-profile
└── opencli-shim-home/
    └── .opencli  →  ../opencli    ← symlink the dispatcher's HOME points at
```

The dispatcher sets `HOME=<oc_profile_home>/opencli-shim-home/` per subprocess. opencli's `os.homedir()` returns the shim → `os.homedir() / ".opencli"` resolves to the real per-profile state. User's real `~/.opencli/` is untouched.

If you ever upgrade opencli and it stops resolving state via `os.homedir()`, this shim becomes useless and you'll need a different mechanism (probably `--config <path>` flag, if they ever add one).

### Extension side-loading

`plugin.py` appends `extension/v1.0.6/` to `AGENT_BROWSER_EXTENSIONS` (comma-separated). agent-browser's launcher passes that through as `--load-extension=<path>` to Chromium at boot. Verified loaded by reading `chrome://extensions` shadow DOM.

The append is idempotent — multiple plugin loads don't duplicate paths.

### Format flag

opencli uses `-f json` (NOT `--json`). Got bit by this in the first smoke test. dispatcher.py auto-injects `-f json` when `json_mode=True`.

### Daemon lifecycle

opencli's daemon (port 19825 by default) auto-starts on first browser command and lives as long as Chrome lives. We don't manage it. browser-harness's atexit hook closes the agent's Chrome on OC shutdown, which transitively kills the daemon (it exits when its WebSocket peers all disconnect).

## License attribution

The OpenCLI extension files in `extension/v1.0.6/` are © Jack Wener and contributors, redistributed under Apache-2.0. See the upstream [LICENSE](https://github.com/jackwener/OpenCLI/blob/main/LICENSE).

OC's plugin code (this directory excluding `extension/`) is MIT-licensed alongside the rest of OpenComputer.

## Re-sync checklist

To upgrade the bundled extension:

1. Download new `opencli-extension-vX.Y.Z.zip` from the [releases page](https://github.com/jackwener/opencli/releases).
2. Unpack to `extension/vX.Y.Z/`.
3. Update `_BUNDLED_EXTENSION` constant in `plugin.py` and `doctor.py`.
4. Bump version in `plugin.json`.
5. Smoke test: load OC, confirm extension shows in `chrome://extensions` of agent's Chrome.

To upgrade the npm CLI:

1. `cd OpenComputer && npm update @jackwener/opencli`.
2. Run smoke tests against several sites — adapters can drift.
3. Bump version pin in `package.json` if breaking change.
