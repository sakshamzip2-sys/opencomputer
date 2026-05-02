# OpenClaw browser — Chrome process + profiles + config

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; do a JIT deeper read of the named files when actually porting this subsystem.

## One-line summary

OpenClaw resolves a profile config into a fully-derived `ResolvedBrowserProfile`, then either spawns its own Chrome with a managed user-data-dir, attaches to a user's already-running Chrome via Chrome MCP, or connects to a pre-existing remote CDP endpoint — three drivers, one config surface.

## The three profile drivers

OpenClaw does not have one "profile" notion — it has three:

| Driver | When it spawns Chrome | User-data-dir | Use case |
|---|---|---|---|
| `openclaw` (default) | Yes — managed, isolated | `$CONFIG_DIR/browser/{profileName}/user-data` | Agent-only browsing, persistent across restarts |
| `existing-session` | No — attaches via Chrome MCP subprocess | (user's real one) | "Use my logged-in Chrome" — host-only |
| `remote-cdp` | No — connects to external CDP URL | (remote) | Someone else launched a browser |

Profile name `"user"` typically maps to driver `existing-session`; profile name `"openclaw"` (default) maps to driver `openclaw`. The driver, not the name, decides the lifecycle.

## Config resolution pipeline (two-stage)

1. **`resolveBrowserConfig(cfg.browser, cfg)`** — parses the `browser:` section of `~/.openclaw/openclaw.json`, applies defaults, and derives the `controlPort` from the gateway config (browser control server is bound to that port on `127.0.0.1`).
2. **`resolveProfile(resolved, profileName)`** — resolves a single profile by name. Computes the CDP URL/port per profile. Special case: `existing-session` zeros all CDP fields (no managed CDP endpoint exists yet — Chrome MCP will attach to the running Chrome later).

Output type: `ResolvedBrowserProfile` carries name, driver, CDP URL/port (or empty), user-data-dir, executable hint, color/badge decoration, and capability flags.

## User-data-dir lifecycle (managed `openclaw` profiles)

- Created lazily at `$CONFIG_DIR/browser/{profileName}/user-data`.
- On first launch, OpenClaw spawns Chrome once with `--user-data-dir`, lets it bootstrap the profile, then closes it before the agent attaches.
- Decorated by mutating the profile's `Preferences` JSON: writes a name and an ARGB color so the user can visually distinguish OpenClaw-controlled windows from their personal Chrome.
- Persists across restarts. Cleanup is explicit (profile delete) — not automatic.

## Cross-platform Chrome detection

`chrome.executables.ts` tries multiple strategies in order:

- **macOS**: reads the launcher plist, falls back to `osascript` queries to ask LaunchServices, then a hardcoded candidate list (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`, Chromium, Brave, Edge…).
- **Linux**: calls `xdg-mime query default x-scheme-handler/http`, parses the resulting `.desktop` file's `Exec=` line, and falls back to a hardcoded candidate list.
- **Windows**: queries the registry (`HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe`), falls back to candidate paths under `Program Files`.

`readBrowserVersion()` exec's the binary with `--version` and parses the stdout. `parseBrowserMajorVersion()` extracts the major. The tool description requires Chromium ≥ 144 for `existing-session` (Chrome MCP needs a recent CDP feature).

## What the "user" profile differs in

| | `openclaw` | `existing-session` (`user`) |
|---|---|---|
| Spawns Chrome | Yes | No |
| Owns lifecycle | Yes | No (user does) |
| CDP URL in config | Filled | Empty (Chrome MCP discovers at runtime) |
| Cookies / login state | Isolated, agent-owned | User's real cookies |
| Target sandbox? | Yes | No (host-only) |
| Target node? | Yes | No (host-only) |
| `usesChromeMcp` capability | false | true |

`getBrowserProfileCapabilities(profile)` returns this capability bit; `server-context.ts` switches snapshot/action codepaths based on it.

## Porting concerns for Python

- **Process spawning**: replace Node `child_process` with `asyncio.create_subprocess_exec`. Pay attention to Windows child-process tracking (Python's job-object support is weaker than Node's).
- **plist on macOS**: use `plistlib` from stdlib.
- **Registry on Windows**: use `winreg` from stdlib.
- **`xdg-mime` shelling out**: just keep shelling out; same approach.
- **Profile JSON decoration** (writing the `Preferences` file): plain JSON read/mutate/write. Use `os.replace()` for atomic write.
- **Hot-reload of config**: file watcher + debounced reparse. `watchdog` is the standard Python lib.
- **`existing-session` driver**: porting this also requires the Chrome MCP subprocess (see `04-ai-and-snapshot.md`).

## Open questions for the port

- Do we need profile decoration on day one, or is that a UX-polish item that can defer?
- Do we want the same multi-profile system, or just "default" + "user" for v1?
- Should profile config live in OpenComputer's existing `~/.opencomputer/<profile>/config.yaml` or a separate browser-specific file?

---

## Deep second-pass — function-by-function

> Captured 2026-05-03 from a line-by-line read of the eleven source files plus the test files for edge cases. All file:line references point into `extensions/browser/src/browser/` in the OpenClaw repo. This is the JIT artifact the first pass deferred.

### `chrome.ts` — process lifecycle (490 lines)

| Symbol | Signature | Purpose | Callers / notes |
|---|---|---|---|
| `RunningChrome` (type) | `{ pid; exe: BrowserExecutable; userDataDir; cdpPort; startedAt; proc: ChildProcess }` | Handle returned by `launchOpenClawChrome`; consumed by `stopOpenClawChrome` and `server-context.lifecycle.ts`. | `proc` retains the Node `ChildProcess` — important for SIGTERM/SIGKILL. |
| `resolveOpenClawUserDataDir` | `(profileName?: string) => string` (line 81) | Returns `$CONFIG_DIR/browser/{profileName}/user-data`. Default profile name is `"openclaw"`. | Used by `launchOpenClawChrome`, `profiles-service.deleteProfile`, and tests. |
| `buildOpenClawChromeLaunchArgs` | `(params: {resolved; profile; userDataDir}) => string[]` (line 89) | Constructs the Chrome CLI argv. See "Args" below. | Pure function — re-export tested by `chrome.launch-args.test.ts`. |
| `isChromeReachable` | `(cdpUrl, timeoutMs?, ssrfPolicy?) => Promise<boolean>` (line 142) | HTTP `GET /json/version` probe (or WS handshake if `cdpUrl` is `ws://`). SSRF-checks first. | Called every poll in launch readiness loop and in `stopOpenClawChrome`. |
| `getChromeWebSocketUrl` | `(cdpUrl, timeoutMs?, ssrfPolicy?) => Promise<string \| null>` (line 197) | Resolves the browser-level WS URL via `/json/version`'s `webSocketDebuggerUrl` field, normalizing it back through `cdpUrl`'s host. | Calls `assertCdpEndpointAllowed` again on the WS URL — defense in depth. |
| `isChromeCdpReady` | `(cdpUrl, timeoutMs?, handshakeTimeoutMs?, ssrfPolicy?) => Promise<boolean>` (line 292) | Stronger than `isChromeReachable`: opens the WS, sends `{id:1, method:"Browser.getVersion"}`, waits for a JSON response with matching id. | This is the gate before declaring CDP "ready" for first attach in `server-context`. |
| `launchOpenClawChrome` | `(resolved, profile) => Promise<RunningChrome>` (line 305) | The main spawn-and-wait routine. See "Bootstrap launch" below. | Throws if profile is non-loopback; throws on launch timeout with a `stderr` hint. |
| `stopOpenClawChrome` | `(running, timeoutMs?) => Promise<void>` (line 459) | SIGTERM, poll until `isChromeReachable` returns false (max `CHROME_STOP_TIMEOUT_MS` = 2500ms), then SIGKILL. | Idempotent: bails on `proc.killed` early. |
| `canOpenWebSocket` (file-private, line 127) | `(url, timeoutMs) => Promise<boolean>` | One-shot WS-handshake probe. | Used only when the CDP URL is already `ws://`/`wss://`. |
| `fetchChromeVersion` (file-private, line 166) | Returns parsed `{webSocketDebuggerUrl?, Browser?, "User-Agent"?}` from `/json/version` or null. | Has its own `AbortController`. | The release-on-finally pattern is from `fetchCdpChecked`. |
| `canRunCdpHealthCommand` (file-private, line 217) | Sends `Browser.getVersion` over WS, races against a hardened timeout (`max(50, timeoutMs+25)`). | The `+25` is so the WS-level timeout fires *after* the handshake timer Node uses internally. |
| Re-exports | `BrowserExecutable`, `findChromeExecutable*`, `resolveBrowserExecutableForPlatform`, `decorateOpenClawProfile`, `ensureProfileCleanExit`, `isProfileDecorated` | Surface API. |

#### Bootstrap launch algorithm (chrome.ts:305-457)

1. **Validate**: throw if `!profile.cdpIsLoopback` (only `openclaw` driver gets here).
2. **Port check**: `ensurePortAvailable(profile.cdpPort)` — throws if anyone else holds the CDP port.
3. **Executable resolution**: see `chrome.executables.ts` below.
4. **Make user-data-dir**: `fs.mkdirSync(userDataDir, { recursive: true })`.
5. **Compute `needsBootstrap`**: `!exists("Local State")` OR `!exists("Default/Preferences")`. Two-file check — Chrome writes both during first run.
6. **Compute `needsDecorate`**: `!isProfileDecorated(userDataDir, profile.name, color.toUpperCase())`.
7. **If `needsBootstrap`**: spawn Chrome → poll every 100ms for both files (max `CHROME_BOOTSTRAP_PREFS_TIMEOUT_MS`=10s) → SIGTERM → wait up to `CHROME_BOOTSTRAP_EXIT_TIMEOUT_MS`=5s for exit.
8. **If `needsDecorate`**: call `decorateOpenClawProfile(userDataDir, {name, color})`. Logs but does not throw on failure (best-effort).
9. **Always**: call `ensureProfileCleanExit(userDataDir)` to suppress the "Chrome didn't shut down correctly" bubble.
10. **Real spawn**: same args. Stderr is `pipe`'d into a `Buffer[]` collector.
11. **Readiness loop**: poll `isChromeReachable(profile.cdpUrl)` every `CHROME_LAUNCH_READY_POLL_MS`=200ms, max `CHROME_LAUNCH_READY_WINDOW_MS`=15s.
12. **On failure**: SIGKILL, build error message including up to `CHROME_STDERR_HINT_MAX_CHARS`=2000 chars of stderr; on Linux without `noSandbox` add a sandbox hint.
13. **On success**: `proc.stderr.off("data", onStderr)` and `stderrChunks.length = 0` — important to release the buffer; otherwise long-running Chrome leaks memory.

#### Args produced (chrome.ts:95-124)

```
--remote-debugging-port=<cdpPort>
--user-data-dir=<userDataDir>
--no-first-run
--no-default-browser-check
--disable-sync
--disable-background-networking
--disable-component-update
--disable-features=Translate,MediaRouter
--disable-session-crashed-bubble
--hide-crash-restore-bubble
--password-store=basic
[--headless=new --disable-gpu]                         (if resolved.headless)
[--no-sandbox --disable-setuid-sandbox]                (if resolved.noSandbox)
[--disable-dev-shm-usage]                              (Linux only)
[...resolved.extraArgs]                                (last — can override)
```

Notable absences (covered by `chrome.launch-args.test.ts`): no `about:blank`, no `--remote-allow-origins`. The latter is implicit because `--remote-debugging-port` binds to localhost only, and Chrome's default origin policy already trusts loopback.

---

### `chrome.executables.ts` — cross-platform discovery (729 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `BrowserExecutable` (type) | `{ kind: "brave"\|"canary"\|"chromium"\|"chrome"\|"custom"\|"edge"; path: string }` | The discriminated handle for any Chromium-flavor binary. |
| `resolveBrowserExecutableForPlatform` | `(resolved, platform) => BrowserExecutable \| null` (line 703) | **Top-level orchestrator**. See algorithm below. |
| `findChromeExecutableMac/Linux/Windows` | `() => BrowserExecutable \| null` | Fallback hardcoded-path scans. |
| `findGoogleChromeExecutableMac/Linux/Windows` | `() => BrowserExecutable \| null` | Strict "Google Chrome only" variant — used for the privileged "real Chrome" case in `existing-session`. |
| `resolveGoogleChromeExecutableForPlatform` | `(platform) => BrowserExecutable \| null` (line 670) | Dispatcher for the strict variant. |
| `readBrowserVersion` | `(executablePath) => string \| null` (line 685) | Runs `<exe> --version`, normalizes whitespace. 2 s timeout. |
| `parseBrowserMajorVersion` | `(rawVersion) => number \| null` (line 693) | Extracts the **last** dotted version token's major (`Chromium 3.0/1.2.3` → `1`). Tested explicitly. |
| `inferKindFromIdentifier`, `inferKindFromExecutableName` | string → kind | Brand classification by substring. |
| Various `detectDefault*` and parsers | private | Per-platform default-browser detection. |

#### Resolution algorithm (`resolveBrowserExecutableForPlatform`)

```
1. If resolved.executablePath is set:
     - Throw "browser.executablePath not found" if it does not exist.
     - Else return { kind: "custom", path: resolved.executablePath }.
2. Try detectDefaultChromiumExecutable(platform)  ← user's actual default browser.
3. If that returns null, fall back to findChromeExecutable<Platform>() — hardcoded list.
4. Return null if no platform match.
```

##### macOS default-browser detection (lines 185-263)

1. Read `~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist`.
2. Run `/usr/bin/plutil -extract LSHandlers json -o - -- <plist>` (timeout 2s, maxBuffer 5 MB).
3. JSON parse → look for `LSHandlerURLScheme === "http"` then `https`. Take the role-string from `LSHandlerRoleAll` (or `LSHandlerRoleViewer`).
4. If that bundle id is in the `CHROMIUM_BUNDLE_IDS` allowlist, run `osascript -e 'POSIX path of (path to application id "<bundleId>")'`.
5. Read `Contents/Info.plist` via `/usr/bin/defaults read <appPath>/Contents/Info CFBundleExecutable` to get the real binary name (Chrome's binary in older versions used a different name than the .app).
6. Verify `Contents/MacOS/<exeName>` exists.

##### Linux default-browser detection (lines 265-297)

1. `xdg-settings get default-web-browser` (preferred) → fall back to `xdg-mime query default x-scheme-handler/http`.
2. If the desktop id is in `CHROMIUM_DESKTOP_IDS`, search:
   - `~/.local/share/applications/<id>`
   - `/usr/local/share/applications/<id>`
   - `/usr/share/applications/<id>`
   - `/var/lib/snapd/desktop/applications/<id>` (Snap support).
3. Parse the first `Exec=` line, split shell-style (handles single + double quotes, env var assignments, leading `env`), strip `%a`/`%u`/`%U` placeholders.
4. If the command is absolute, use it directly; else resolve via `which <cmd>` (800 ms timeout).
5. Final filename must be in `CHROMIUM_EXE_NAMES`.

##### Windows default-browser detection (lines 299-456)

1. Read user choice ProgId: `reg query HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice /v ProgId`.
2. Then read the open-command for that ProgId: `reg query HKCR\<ProgId>\shell\open\command /ve` (fallback `HKCR\http\shell\open\command`).
3. Expand `%VAR%` placeholders against `process.env`.
4. Extract the `.exe` path — quoted form first (`"...\\.exe"`), unquoted second.
5. The basename (lowercase, win32 style) must match `CHROMIUM_EXE_NAMES`.

##### Hardcoded fallback paths

- **mac**: `/Applications/{Google Chrome,Brave Browser,Microsoft Edge,Chromium,Google Chrome Canary}.app/Contents/MacOS/<exe>` plus the `~/Applications/...` mirrors.
- **Linux**: `/usr/bin/{google-chrome,google-chrome-stable,chrome,brave-browser,brave-browser-stable,brave,microsoft-edge,microsoft-edge-stable,chromium,chromium-browser}` plus `/snap/bin/{brave,chromium}`.
- **Windows**: under both `%LOCALAPPDATA%` and `%ProgramFiles%` / `%ProgramFiles(x86)%`, paths like `Google\Chrome\Application\chrome.exe`, `BraveSoftware\Brave-Browser\Application\brave.exe`, `Microsoft\Edge\Application\msedge.exe`, `Google\Chrome SxS\Application\chrome.exe` (Canary).

> Edge case: hardcoded scan tags `kind: "canary"` if path includes any of `beta`, `canary`, `sxs`, `unstable`. So `/usr/bin/google-chrome-beta` resolves as `{kind:"canary"}` (tested).

---

### `chrome.profile-decoration.ts` — JSON pref mutation (199 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `isProfileDecorated` | `(userDataDir, desiredName, desiredColorHex) => boolean` (line 56) | Idempotency check: returns true if all of name/seed/theme already match. |
| `decorateOpenClawProfile` | `(userDataDir, opts?: {name?; color?}) => void` (line 129) | Mutates `Local State` and `Default/Preferences` to set the OpenClaw name and color. |
| `ensureProfileCleanExit` | `(userDataDir) => void` (line 192) | Sets `exit_type:"Normal"` and `exited_cleanly:true` in `Default/Preferences`. Suppresses "Chrome didn't shut down correctly" bubble. |
| `decoratedMarkerPath` (private) | Writes `<userDataDir>/.openclaw-profile-decorated` with a unix-millis timestamp. | Just a marker — not consulted by `isProfileDecorated`; that one re-reads the prefs. |
| `parseHexRgbToSignedArgbInt` (private) | `"#FF4500"` → JS-signed 32-bit int with `0xFF` alpha. | Chrome stores `SkColor` as a signed int. Values > `0x7FFFFFFF` are wrapped via `- 0x1_0000_0000`. |
| `setDeep` (private) | `(obj, keys[], value) => void` | Optional-chaining mutation: builds intermediate objects when missing/non-object/array. |

#### JSON paths written (the load-bearing part)

`Local State` (one file per `userDataDir`, alongside `Default/`):

```
profile.info_cache.Default.name                        = desiredName
profile.info_cache.Default.shortcut_name               = desiredName
profile.info_cache.Default.user_name                   = desiredName
profile.info_cache.Default.profile_color               = "#FF4500"  (string, best-effort)
profile.info_cache.Default.user_color                  = "#FF4500"
profile.info_cache.Default.profile_color_seed          = <signed int>   ← actually used
profile.info_cache.Default.profile_highlight_color     = <signed int>
profile.info_cache.Default.default_avatar_fill_color   = <signed int>
profile.info_cache.Default.default_avatar_stroke_color = <signed int>
```

`Default/Preferences`:

```
profile.name                                           = desiredName
profile.profile_color                                  = "#FF4500"
profile.user_color                                     = "#FF4500"
autogenerated.theme.color                              = <signed int>   ← Chrome refresh
browser.theme.user_color2                              = <signed int>   ← user-selected theme
exit_type                                              = "Normal"       (via ensureProfileCleanExit)
exited_cleanly                                         = true
```

Both files are read with `safeReadJson` (returns `null` on parse failure or non-object), mutated, then written via `safeWriteJson` (ensures parent dir, indented 2-space JSON). Note: **not atomic**. `fs.writeFileSync` is used directly, no `os.replace()` equivalent. A crash mid-write can corrupt the prefs file. For Python: use `output-atomic` style (write-tmp + `os.replace`).

---

### `profiles.ts` — port allocation, validation, palette (114 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `CDP_PORT_RANGE_START` / `CDP_PORT_RANGE_END` | `18800` / `18899` | 100-port window. |
| `PROFILE_NAME_REGEX` | `/^[a-z0-9][a-z0-9-]*$/` | Used by `isValidProfileName`. |
| `isValidProfileName` | `(name) => boolean` (line 20) | Length ≤ 64; must match the regex; rejects empty. |
| `allocateCdpPort` | `(usedPorts: Set<number>, range?) => number \| null` (line 27) | First unused port in `[start..end]`; null if exhausted. Range is validated finite/positive/start≤end. |
| `getUsedPorts` | `(profiles) => Set<number>` (line 47) | Collects every `cdpPort` AND parses out the port from any `cdpUrl` (default 80/443 by protocol). |
| `PROFILE_COLORS` | 10-color palette (line 81) | Starts with `#FF4500` (lobster-orange / OpenClaw default). |
| `allocateColor` | `(usedColors: Set<string>) => string` (line 94) | First-free in palette, then cycles `index = used.size % 10`. |
| `getUsedColors` | `(profiles) => Set<string>` (line 106) | Uppercases all color values. |

> Reserved ports table (file header docstring): `18789` gateway WS, `18790` bridge, `18791` browser control, `18792-18799` reserved (canvas at `18793`). Don't pick these.

---

### `profiles-service.ts` — CRUD over the persisted profile registry (262 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `CreateProfileParams` (type) | `{ name; color?; cdpUrl?; userDataDir?; driver?: "openclaw"\|"existing-session" }` | The HTTP-route payload shape. |
| `CreateProfileResult` (type) | `{ ok: true; profile; transport: "cdp"\|"chrome-mcp"; cdpPort \| null; cdpUrl \| null; userDataDir \| null; color; isRemote }` | Returned to caller; `transport` is derived from `usesChromeMcp` capability. |
| `cdpPortRange` (private) | `(resolved) => {start; end}` (line 56) | Validates explicit range from config; falls back to `deriveDefaultBrowserCdpPortRange(controlPort)`. |
| `createBrowserProfilesService(ctx).listProfiles` | `() => Promise<ProfileStatus[]>` | Pass-through to `ctx.listProfiles()` (server-context). |
| `createBrowserProfilesService(ctx).createProfile` | `(params) => Promise<CreateProfileResult>` (line 85) | The 100-line workhorse. See flow below. |
| `createBrowserProfilesService(ctx).deleteProfile` | `(name) => Promise<DeleteProfileResult>` (line 200) | Refuses to delete `defaultProfile`; for `openclaw` driver, calls `ctx.forProfile(name).stopRunningBrowser()` then trashes the parent dir of the user-data-dir. |

`createProfile` flow (lines 85-198):

1. Trim name, normalize `cdpUrl` and `userDataDir`, force `driver` to either `"existing-session"` or `undefined` (defaults to `"openclaw"` later).
2. `isValidProfileName` gate.
3. Reject conflicts both in resolved state and in the on-disk config (two-stage idempotency).
4. Pick color: explicit `params.color` if it matches `^#[0-9A-Fa-f]{6}$`, else `allocateColor(usedColors)`.
5. Validate combinations:
   - `userDataDir` provided but `driver !== "existing-session"` → `BrowserValidationError`.
   - `userDataDir` doesn't exist → `BrowserValidationError`.
   - `cdpUrl` provided with `driver="existing-session"` → `BrowserValidationError`.
6. Build `profileConfig` payload:
   - `cdpUrl` path → parse + SSRF-check; persist `{cdpUrl, driver?, color}`.
   - `existing-session` no-URL path → `{driver, attachOnly: true, userDataDir?, color}`.
   - default openclaw path → allocate CDP port via `allocateCdpPort(usedPorts, cdpPortRange(state.resolved))`; persist `{cdpPort, driver?, color}`.
7. Spread into `nextConfig.browser.profiles[name]`, `await writeConfigFile(nextConfig)`.
8. Mutate `state.resolved.profiles[name]` directly (for immediate visibility), call `resolveProfile`, derive capabilities, return.

> Subtle: step 7 writes to disk *before* step 8 updates in-memory state. If the disk write fails the in-memory state stays clean — but the `BrowserProfileNotFoundError` thrown after `resolveProfile` returns null is unreachable in practice because we just inserted the entry. Treat it as defensive.

`deleteProfile` flow (lines 200-255):

- For `cdpIsLoopback && driver === "openclaw"`: try-catch `stopRunningBrowser()`, then `path.dirname(userDataDir)` (i.e. `$CONFIG_DIR/browser/{name}`) is moved to trash via `movePathToTrash`. Note `path.dirname` — they trash the entire profile dir, not just `user-data`.
- Always: rewrite config without the entry, delete from `state.resolved.profiles[name]` and `state.profiles.delete(name)`.

---

### `profile-capabilities.ts` — derive runtime feature bits (94 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `BrowserProfileMode` (type) | `"local-managed" \| "local-existing-session" \| "remote-cdp"` | Three cases. |
| `BrowserProfileCapabilities` (type) | See section 3 below. | The discriminator surface. |
| `getBrowserProfileCapabilities` | `(profile) => BrowserProfileCapabilities` (line 17) | Pure mapping from `(driver, cdpIsLoopback)` to caps. |
| `resolveDefaultSnapshotFormat` | `(params: {profile; hasPlaywright; explicitFormat?; mode?}) => "ai" \| "aria"` | `existing-session` → "ai"; `efficient` mode → "ai"; else `"ai"` if Playwright is wired, else `"aria"`. |
| `shouldUsePlaywrightForScreenshot` | `(params: {profile; wsUrl?; ref?; element?}) => boolean` | True if we lack a per-tab WS, OR if a CSS selector / element ref was provided. |
| `shouldUsePlaywrightForAriaSnapshot` | `(params: {profile; wsUrl?}) => boolean` | True iff there is no `wsUrl` (i.e. we have to drive via Playwright's CDP attach). |

Mapping is a 3-row truth table:

| Condition | mode | usesChromeMcp | usesPersistentPlaywright | supports {PerTabWs, JsonTabEndpoints, Reset, ManagedTabLimit} |
|---|---|---|---|---|
| `driver === "existing-session"` | `local-existing-session` | true | false | all false |
| `!cdpIsLoopback` | `remote-cdp` | false | true | all false |
| else | `local-managed` | false | false | all true |

---

### `config.ts` — root resolver and per-profile resolver (361 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `ResolvedBrowserConfig` (type) | See section 3. | The fully-derived browser-section type. |
| `ResolvedBrowserProfile` (type) | See section 3. | One profile after resolution. |
| `resolveBrowserConfig` | `(cfg?: BrowserConfig, rootConfig?: OpenClawConfig) => ResolvedBrowserConfig` (line 191) | Stage 1. See pipeline. |
| `resolveProfile` | `(resolved, profileName) => ResolvedBrowserProfile \| null` (line 297) | Stage 2. Computes per-profile cdpUrl/host/port. |
| `shouldStartLocalBrowserServer` | `(_resolved) => true` (line 359) | Always true currently — placeholder for future gating. |
| `normalizeHexColor` (private, line 89) | `string?` → `"#RRGGBB"` upper or `DEFAULT_OPENCLAW_BROWSER_COLOR`. | Adds `#` prefix if missing. |
| `normalizeTimeoutMs` (private, line 101) | Coerce/floor to ≥0; fall back if NaN/Infinity/negative. |
| `resolveCdpPortRangeStart` (private, line 106) | Validates `[1, 65535-rangeSpan]`; throws on out-of-range. |
| `resolveBrowserSsrFPolicy` (private, line 129) | Merges legacy `allowPrivateNetwork` and modern `dangerouslyAllowPrivateNetwork`; returns `{}` (not undefined) on default to keep guards engaged. |
| `ensureDefaultProfile` (private, line 158) | Inserts an `openclaw` entry if missing, using legacy top-level `cdpPort`/`cdpUrl` if present. |
| `ensureDefaultUserBrowserProfile` (private, line 176) | Inserts a `user` entry (driver `existing-session`, color `#00AA00`) if missing. |

#### `resolveBrowserConfig` pipeline (line 191-295)

```
gatewayPort      = resolveGatewayPort(rootConfig)            // may be undefined
controlPort      = deriveDefaultBrowserControlPort(gatewayPort ?? 18791)
defaultColor     = normalizeHexColor(cfg.color)              // → "#FF4500"
remoteHttpTo     = normalizeTimeoutMs(cfg.remoteCdpTimeoutMs, 1500)
remoteHsTo       = normalizeTimeoutMs(cfg.remoteCdpHandshakeTimeoutMs, max(2000, http*2))
derivedCdpRange  = deriveDefaultBrowserCdpPortRange(controlPort)
cdpRangeStart    = resolveCdpPortRangeStart(cfg.cdpPortRangeStart, derived.start, span)
cdpRangeEnd      = cdpRangeStart + span
if (cfg.cdpUrl):
    parse via parseBrowserHttpUrl  → {parsed, port, normalized}
else:
    derived port = controlPort + 1   (must be ≤ 65535)
    cdpInfo      = http://127.0.0.1:<derivedPort>
profiles = ensureDefaultUserBrowserProfile(
              ensureDefaultProfile(cfg.profiles, defaultColor,
                                   legacyCdpPort, cdpRangeStart, legacyCdpUrl))
defaultProfile = cfg.defaultProfile
              ?? (profiles.openclaw ? "openclaw" : profiles.user ? "user" : "openclaw")
extraArgs = (Array.isArray ? filter strings : [])
return { ...all-of-above..., ssrfPolicy: resolveBrowserSsrFPolicy(cfg) }
```

#### `resolveProfile` decision tree (line 297-357)

```
if profile.driver === "existing-session":
    return { name, cdpPort: 0, cdpUrl: "", cdpHost: "", cdpIsLoopback: true,
             userDataDir: resolveUserPath(profile.userDataDir) || undefined,
             color: profile.color, driver: "existing-session", attachOnly: true }

rawProfileUrl = profile.cdpUrl?.trim() ?? ""
hasStaleWsPath = (rawProfileUrl != "" && cdpPort > 0
                  && /^wss?:\/\//.test(rawProfileUrl)
                  && /\/devtools\/browser\//.test(rawProfileUrl))

if hasStaleWsPath:
    # User has a stale per-launch /devtools/browser/<id> path —
    # drop the path part, keep just <protocol>://<host>:<cdpPort>.
    cdpHost = parsed.hostname
    cdpUrl  = "<resolved.cdpProtocol>://<cdpHost>:<cdpPort>"
elif rawProfileUrl:
    parse via parseBrowserHttpUrl  → cdpHost, cdpPort, cdpUrl
elif cdpPort:
    cdpUrl = "<resolved.cdpProtocol>://<resolved.cdpHost>:<cdpPort>"
else:
    throw new Error(`Profile "<name>" must define cdpPort or cdpUrl.`)

return { name, cdpPort, cdpUrl, cdpHost,
         cdpIsLoopback: isLoopbackHost(cdpHost),
         color, driver: "openclaw",
         attachOnly: profile.attachOnly ?? resolved.attachOnly }
```

The "stale WS path" branch is the most non-obvious: Chrome's WS URLs are per-process (e.g. `ws://127.0.0.1:18800/devtools/browser/abc-123`) and stop working when the browser restarts. Storing the path-less `http://...:port` form lets `getChromeWebSocketUrl` re-discover the live UUID after every launch.

---

### `constants.ts` — defaults (9 lines)

```
DEFAULT_OPENCLAW_BROWSER_ENABLED        = true
DEFAULT_BROWSER_EVALUATE_ENABLED        = true
DEFAULT_OPENCLAW_BROWSER_COLOR          = "#FF4500"
DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME   = "openclaw"
DEFAULT_BROWSER_DEFAULT_PROFILE_NAME    = "openclaw"
DEFAULT_AI_SNAPSHOT_MAX_CHARS           = 80_000
DEFAULT_AI_SNAPSHOT_EFFICIENT_MAX_CHARS = 10_000
DEFAULT_AI_SNAPSHOT_EFFICIENT_DEPTH     = 6
```

---

### `paths.ts` — sandboxed within-root path resolution (277 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `DEFAULT_BROWSER_TMP_DIR` | `string` (line 25) | Resolved via `resolvePreferredOpenClawTmpDir()` if Node `fs` is available, else `"/tmp/openclaw"`. |
| `DEFAULT_TRACE_DIR`, `DEFAULT_DOWNLOAD_DIR`, `DEFAULT_UPLOAD_DIR` | strings | `<tmp>/`, `<tmp>/downloads`, `<tmp>/uploads`. |
| `resolvePathWithinRoot` | `(params: {rootDir; requestedPath; scopeLabel; defaultFileName?}) => {ok:true; path} \| {ok:false; error}` | Lexical jail-cell: rejects `..` traversal and absolute paths. |
| `resolveWritablePathWithinRoot` | (same args) → Promise | Adds **canonical** check via `realpath` of root and parent dir, rejects symlinks and hard-linked files (`nlink > 1`). |
| `resolvePathsWithinRoot` | `(params: {rootDir; requestedPaths[]; scopeLabel}) => {ok:true; paths[]} \| {ok:false; error}` | Bulk lexical version. |
| `resolveExistingPathsWithinRoot` / `resolveStrictExistingPathsWithinRoot` | (same args) → Promise | Use `openFileWithinRoot` to validate existence; the strict variant fails on missing files, the lax one falls back to lexical path. |
| `validateCanonicalPathWithinRoot` (private, line 61) | Returns `"ok" \| "not-found" \| "invalid"`. Rejects symlinks, wrong type, hard-linked files. |

The takeaway: this file is the security perimeter for browser file uploads/downloads. **Port to Python with care** — `os.path.realpath` + `os.lstat` + `Path.is_symlink()` + `os.stat().st_nlink` is the equivalent recipe.

---

### `config-refresh-source.ts` — runtime snapshot accessor (5 lines)

```ts
export function loadBrowserConfigForRuntimeRefresh(): OpenClawConfig {
  return getRuntimeConfigSnapshot() ?? createConfigIO().loadConfig();
}
```

Single tiny indirection. `getRuntimeConfigSnapshot()` returns whatever the in-memory snapshot is (set by the global hot-reload subsystem in `config/config.ts`); on cache miss, fall through to `createConfigIO().loadConfig()`. **Note: there is no `chokidar`/`fs.watch` here.** Hot reload is pull-based, not push-based.

### `resolved-config-refresh.ts` — request-time hot reload (108 lines)

| Symbol | Signature | Purpose |
|---|---|---|
| `changedProfileInvariants` (private) | `(current, next) => string[]` (line 5) | Diff fields: `cdpUrl, cdpPort, driver, attachOnly, cdpIsLoopback, userDataDir`. |
| `applyResolvedConfig` (private, line 31) | Replaces `current.resolved` with `freshResolved` but **preserves `evaluateEnabled`** from the running state (security comment: only full runtime reload may flip it). For each existing per-profile runtime, recompute `resolveProfile`; if missing → mark for reconcile + drop `lastTargetId`; if present → diff invariants and mark for reconcile if anything load-bearing changed. |
| `refreshResolvedBrowserConfigFromDisk` | `({current; refreshConfigFromDisk; mode}) => void` (line 67) | No-op if `refreshConfigFromDisk` is false. Otherwise pulls fresh snapshot via `loadBrowserConfigForRuntimeRefresh`, re-runs `resolveBrowserConfig`, applies. |
| `resolveBrowserProfileWithHotReload` | `({current; refreshConfigFromDisk; name}) => ResolvedBrowserProfile \| null` (line 84) | Try cached refresh → if `resolveProfile` returns null, retry with `mode:"fresh"`. Two-pass to avoid an unnecessary fresh disk read on the common path. |

So the hot-reload model is: at the start of every browser HTTP request that opts in (`refreshConfigFromDisk: true`), call `resolveBrowserProfileWithHotReload`. There is **no debouncing**, no file-watcher, no event bus. The `loadConfig`-side cache (TTL inside `config/config.ts`) provides the only debounce. For Python, this is the simpler-than-you'd-think pattern: re-resolve on demand, let the loader cache the parse.

---

## 3. Data structure field reference

### TS

```ts
// extensions/browser/src/browser/config.ts:53-73
export type ResolvedBrowserConfig = {
  enabled: boolean;
  evaluateEnabled: boolean;
  controlPort: number;                    // browser HTTP control server, gateway+1 by default
  cdpPortRangeStart: number;              // 18800 default
  cdpPortRangeEnd: number;                // 18899 default
  cdpProtocol: "http" | "https";
  cdpHost: string;                        // hostname, e.g. "127.0.0.1"
  cdpIsLoopback: boolean;
  remoteCdpTimeoutMs: number;
  remoteCdpHandshakeTimeoutMs: number;
  color: string;                          // "#FF4500"
  executablePath?: string;
  headless: boolean;
  noSandbox: boolean;
  attachOnly: boolean;
  defaultProfile: string;                 // profile name to use when none specified
  profiles: Record<string, BrowserProfileConfig>;   // raw config entries
  ssrfPolicy?: SsrFPolicy;
  extraArgs: string[];
};

// extensions/browser/src/browser/config.ts:75-85
export type ResolvedBrowserProfile = {
  name: string;
  cdpPort: number;                        // 0 for existing-session
  cdpUrl: string;                         // "" for existing-session
  cdpHost: string;                        // "" for existing-session
  cdpIsLoopback: boolean;                 // true for existing-session by definition
  userDataDir?: string;                   // only meaningful for existing-session
  color: string;
  driver: "openclaw" | "existing-session";
  attachOnly: boolean;
};

// extensions/browser/src/browser/profile-capabilities.ts:5-15
export type BrowserProfileCapabilities = {
  mode: "local-managed" | "local-existing-session" | "remote-cdp";
  isRemote: boolean;
  usesChromeMcp: boolean;                 // existing-session only
  usesPersistentPlaywright: boolean;      // remote-cdp only
  supportsPerTabWs: boolean;              // local-managed only
  supportsJsonTabEndpoints: boolean;      // local-managed only
  supportsReset: boolean;                 // local-managed only
  supportsManagedTabLimit: boolean;       // local-managed only
};
```

### Python

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

BrowserProfileMode = Literal["local-managed", "local-existing-session", "remote-cdp"]
BrowserDriver = Literal["openclaw", "existing-session"]


@dataclass(slots=True)
class SsrfPolicy:
    dangerously_allow_private_network: bool = False
    allowed_hostnames: Optional[list[str]] = None
    hostname_allowlist: Optional[list[str]] = None


@dataclass(slots=True)
class BrowserProfileConfig:
    """Raw entry from openclaw.json — pre-resolution."""
    cdp_port: Optional[int] = None
    cdp_url: Optional[str] = None
    color: str = "#FF4500"
    driver: Optional[BrowserDriver] = None
    attach_only: Optional[bool] = None
    user_data_dir: Optional[str] = None


@dataclass(slots=True)
class ResolvedBrowserConfig:
    enabled: bool = True
    evaluate_enabled: bool = True
    control_port: int = 18791
    cdp_port_range_start: int = 18800
    cdp_port_range_end: int = 18899
    cdp_protocol: Literal["http", "https"] = "http"
    cdp_host: str = "127.0.0.1"
    cdp_is_loopback: bool = True
    remote_cdp_timeout_ms: int = 1500
    remote_cdp_handshake_timeout_ms: int = 3000
    color: str = "#FF4500"
    executable_path: Optional[str] = None
    headless: bool = False
    no_sandbox: bool = False
    attach_only: bool = False
    default_profile: str = "openclaw"
    profiles: dict[str, BrowserProfileConfig] = field(default_factory=dict)
    ssrf_policy: Optional[SsrfPolicy] = None
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedBrowserProfile:
    name: str
    cdp_port: int                  # 0 for existing-session
    cdp_url: str                   # "" for existing-session
    cdp_host: str                  # "" for existing-session
    cdp_is_loopback: bool
    color: str
    driver: BrowserDriver
    attach_only: bool
    user_data_dir: Optional[str] = None


@dataclass(slots=True)
class BrowserProfileCapabilities:
    mode: BrowserProfileMode
    is_remote: bool
    uses_chrome_mcp: bool
    uses_persistent_playwright: bool
    supports_per_tab_ws: bool
    supports_json_tab_endpoints: bool
    supports_reset: bool
    supports_managed_tab_limit: bool
```

---

## 4. Edge cases / gotchas

1. **Decoration is best-effort and silently fails.** `decorateOpenClawProfile` writes to two files. Failure is `log.warn`'d, not raised. Don't gate the launch on it. **However**, `isProfileDecorated` is what `launchOpenClawChrome` queries — if the file is locked or a write was partial, the next launch will try again, which can create flaky tests. (See `chrome.test.ts` fixture pattern: each test gets a fresh `userDataDir`.)
2. **Profile JSON locking when Chrome is running.** Chrome takes a `LOCK` file inside `Default/`. Decorating while Chrome is alive is a *terrible* idea — Chrome will overwrite the prefs file on exit. OpenClaw's launch sequence ensures Chrome is killed *before* `decorateOpenClawProfile`. In Python: SIGTERM and wait for exit (`process.wait()`) before writing.
3. **Bootstrap exit can stall on Windows.** The 5s `CHROME_BOOTSTRAP_EXIT_TIMEOUT_MS` is a passive wait — there's no SIGKILL fallback after SIGTERM-and-poll. If Chrome takes longer than 5s to exit, the bootstrap process is leaked and Chrome may still be holding the user-data-dir LOCK when the "real" launch tries. `ensurePortAvailable` will *catch this* (port still bound), but the error message will be confusing.
4. **Race between bootstrap and attach.** `needsBootstrap` is computed once at the top of `launchOpenClawChrome`. If two callers race (two requests both calling `launchOpenClawChrome` for the same profile), both can decide they need to bootstrap. Higher-up code (`server-context.lifecycle.ts`) deduplicates this with a `Map<profileName, Promise<RunningChrome>>` pattern — when porting, **don't drop that dedup**.
5. **Port allocation never persists used-port set across processes.** It's recomputed every `createProfile` call from the *current* `state.resolved.profiles` + the on-disk config. If the user runs two OpenClaw daemons on overlapping port ranges, both will pick the same port. Intentional (matches the "single OpenClaw per machine" assumption).
6. **`hasStaleWsPath` regex is forgiving.** `cdpUrl: "wss://127.0.0.1:18800/devtools/browser/abc"` will be rewritten to `http://127.0.0.1:18800` — even though the original was `wss://`. The protocol is taken from `resolved.cdpProtocol` (which reflects the top-level browser config), not the per-profile URL. If you set the top-level `cdpUrl` to `https://...` and a per-profile to `ws://...`, results will surprise you.
7. **`extraArgs` are appended last.** They can override every preceding flag, including `--user-data-dir`. There is no validation. (For Python: be paranoid about accepting `extraArgs` from anything but trusted config.)
8. **`buildOpenClawChromeLaunchArgs` does not pass `HOME`** to Chrome — that's done in `launchOpenClawChrome` via the `env: { ...process.env, HOME: os.homedir() }` override. The intent is to defeat ambient `HOME` overrides set by tests/CI. Replicate in Python with `env=dict(os.environ, HOME=os.path.expanduser("~"))`.
9. **`stderr` buffer is freed on success.** A subtle memory hygiene point. Forgetting this on long-running Chrome (which emits warning lines steadily) leaks. Python equivalent: detach the reader task and discard buffered chunks once `is_chrome_reachable()` returns true.
10. **`ensureDefaultUserBrowserProfile` injects `user`.** Even if the user never declared it, the resolver synthesizes a `user` profile with `driver: "existing-session"`, `color: "#00AA00"`. Do not assume the on-disk config matches `resolved.profiles`.
11. **Color validation differs across files.** `profiles-service.ts` rejects bad hex with a validation error; `config.ts:normalizeHexColor` silently substitutes the default; `chrome.profile-decoration.ts:parseHexRgbToSignedArgbInt` returns `null` and degrades to a name-only "decorated" check. Three policies, one input — when porting, pick one.
12. **`PROFILE_NAME_REGEX` is more restrictive than the JSON.** OpenClaw's API rejects names that don't match `^[a-z0-9][a-z0-9-]*$`, but `resolveProfile` will still resolve any string key found in `profiles`. So a hand-edited config with `My Profile` works at resolve time but cannot be created via the API. Port the regex; document the asymmetry.
13. **`existing-session` profiles never call `launchOpenClawChrome`.** That function explicitly throws on non-loopback, but there's no early throw for `driver === "existing-session"` even though such profiles have `cdpIsLoopback: true`. The check passes — but `cdpPort: 0` will cause `ensurePortAvailable(0)` to misbehave. The actual code path is gated upstream in `server-context` (capabilities → `usesChromeMcp` → different lifecycle). Don't call `launchOpenClawChrome` for existing-session profiles in your port either.
14. **No file watcher.** Despite `config-refresh-source.ts`'s suggestive name, there is no `chokidar`. Hot reload is pull-based via `getRuntimeConfigSnapshot()` (cache TTL set elsewhere) and `resolveBrowserProfileWithHotReload`, which is called per request. Don't waste effort on `watchdog` unless you also need push.

---

## 5. Translation notes (TS → Python)

| TS pattern | Python equivalent |
|---|---|
| `value ?? default` | `value if value is not None else default` (or `value or default` when `0/""/false` are also "missing"). |
| `obj?.a?.b` | `obj.a.b if obj and obj.a else None` — or use `getattr(obj, "a", None)` + chain, or write `dict.get` cascades. For dotted JSON paths, write a helper `dig(d, *keys, default=None)`. |
| `setDeep(obj, ["a", "b", "c"], v)` | `def set_deep(d, keys, v): node=d; for k in keys[:-1]: nxt=node.get(k); node[k] = nxt if isinstance(nxt, dict) else {}; node=node[k]; node[keys[-1]] = v`. |
| `Map<K, Promise<V>>` dedup | `dict[str, asyncio.Task[V]]` — when a request comes in, check the dict; if hit, `await` the existing task; else create one and store it before awaiting. |
| `spawn(exe, args, {stdio:[...], env})` | `asyncio.create_subprocess_exec(exe, *args, stdout=DEVNULL, stderr=PIPE, env={...})`. |
| `proc.kill("SIGTERM")` then `kill("SIGKILL")` | `proc.terminate()` then `proc.kill()`. On Windows both map to TerminateProcess; that's accurate to the TS semantics too. |
| `fs.existsSync` | `os.path.exists` or `pathlib.Path(p).exists()`. |
| `JSON.parse(fs.readFileSync(...))` | `json.loads(Path(p).read_text())`. |
| `JSON.stringify(data, null, 2)` | `json.dumps(data, indent=2, ensure_ascii=False)`. |
| `fs.writeFileSync(path, data)` | **Don't** match exactly — use atomic write: write to `path + ".tmp"`, then `os.replace`. The TS code skips this and is racy. |
| `execFileSync(cmd, args, {timeout, encoding, maxBuffer})` | `subprocess.run([cmd, *args], capture_output=True, text=True, timeout=...)` with try/except `subprocess.TimeoutExpired`. No `maxBuffer` equivalent — Python's `Popen.communicate` will read all output; bound caller-side if needed. |
| `URL` constructor | `urllib.parse.urlparse` for parsing; build via f-string. |
| `AbortController` + `setTimeout(abort)` | `asyncio.wait_for(coro, timeout=...)` raising `TimeoutError`, or pass a `timeout` kwarg into `httpx`/`aiohttp`. |
| WebSocket handshake timeout pattern | `websockets.connect(url, open_timeout=N)` (`websockets` lib) or `aiohttp.ClientSession.ws_connect(url, timeout=N)`. |
| `process.platform === "darwin"/"linux"/"win32"` | `sys.platform == "darwin"/"linux"/"win32"`. |
| Plist on macOS (`plutil -extract LSHandlers json`) | `import plistlib; data = plistlib.loads(Path(p).read_bytes())`. Skip the plutil fork. |
| `xdg-mime`/`xdg-settings`/`which` | Just shell out — `subprocess.run(...)`. There is no portable Python equivalent for these. |
| Windows registry (`reg query`) | `import winreg; winreg.OpenKey(...)` + `QueryValueEx`. Cleaner than parsing `reg.exe` output. |
| `Buffer.concat(chunks).toString("utf8")` | `b"".join(chunks).decode("utf-8", errors="replace")`. |
| Signed ARGB int conversion | Python ints are arbitrary precision — but to match Chrome's `SkColor` semantics, do `argb = (0xFF << 24) \| rgb; return argb - 0x1_0000_0000 if argb > 0x7FFFFFFF else argb`. The negative form is what ends up in Chrome's preferences JSON. |
| `vi.spyOn(fs, "existsSync").mockImplementation(...)` | `unittest.mock.patch("pathlib.Path.exists", ...)` or use `pyfakefs` for filesystem fixtures. |

---

