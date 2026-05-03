# OpenClaw browser — pw-tools-core (the workhorse)

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; JIT deeper read of the named files when porting.

## One-line summary

`pw-tools-core` is the central switch-dispatch that turns each of the 11 `act` kinds (click/type/press/hover/drag/select/fill/scrollIntoView/wait/evaluate/close/resize) into a Playwright API call wrapped in stabilization + SSRF-aware nav-guard checks.

## Act-kind dispatch

`executeSingleAction()` is a switch over `kind`. Each kind:
1. Resolves `ref` → Playwright `Locator` (see Ref System below).
2. Does pre-action stabilization (scroll into view, wait for visible/enabled, etc.).
3. Calls the Playwright API (`locator.click()`, `locator.fill()`, `keyboard.press()`, etc.).
4. Wraps the call in `assertInteractionNavigationCompletedSafely()` — observes `framenavigated` events for a 250ms grace period to catch SSRF-blocked navs that fire mid-action.
5. Returns a normalized response shape.

Wrapping classification:
- **Mutating** (network may fire): click, type, press, evaluate → wrapped in nav-guard observation.
- **Non-mutating**: hover, drag (within page), select, fill, scrollIntoView, wait → not wrapped.

Batching: multiple actions can be sent in one request, nesting allowed up to depth 5, max 50 actions per batch.

## Ref system (two modes)

Refs (`e1`, `e2`, …) come from snapshot. They're stored in `PageState.roleRefs`, keyed by ref id, with mode tracked.

**Mode `"role"`** (default):
- Stored as `{ role, name, nth? }`.
- Resolved at action time via `page.getByRole(role, { name, exact: true })` chained with `.nth(idx)` if duplicate.

**Mode `"aria"`**:
- Stored as opaque aria-ref id.
- Resolved via `page.locator("aria-ref=e1")` (Playwright's internal selector, more stable across page rerenders).

`refLocator()` dispatches by mode.

Refs survive page recreation: `roleRefsByTarget: Map<targetId, RoleRefMap>` keeps refs alive across Page object swaps. LRU bounded.

## Snapshot pipeline

`snapshotRoleViaPlaywright()`:
1. Capture: `locator.ariaSnapshot()` (or `page._snapshotForAI()` for the AI-friendly format).
2. Parse the indented role tree.
3. Classify each node: interactive / content / structural (controlled by `snapshot-roles.ts`).
4. Assign refs:
   - All interactive roles get a ref (button, link, input, checkbox, …).
   - Content roles get a ref only if they have a name (heading, article, …).
   - Structural roles never get refs.
5. Deduplicate by `role:name` key — duplicates get `[nth=N]`; non-duplicates strip the nth marker.
6. Return both the text snapshot (for the agent to read) and a `RoleRefMap` (for later resolution).

Truncation supported via `maxChars` for huge pages.

## Downloads (`pw-tools-core.downloads.ts`)

Lifecycle: arm → trigger → capture → store.
1. **Arm**: register a one-shot listener on `page.on("download")`.
2. **Trigger**: agent calls an `act` (click on a download link) — Chrome fires the download event.
3. **Capture**: handler suspends Playwright's auto-save, queries the suggested filename, saves to a per-session downloads dir.
4. **Store**: result is reported back as a path the agent can `Read`.

Last-armed wins: if the agent arms twice without triggering, the first arming is discarded (avoid stale handlers).

## File chooser (`browserArmFileChooser`)

Same pattern as downloads: arm a one-shot listener (`page.on("filechooser")`), then the next click that opens a chooser is auto-filled with the supplied paths via `fileChooser.setFiles(paths)`.

## Dialog handling (`browserArmDialog`)

Arm one-shot for `page.on("dialog")` — accepts/dismisses next alert/confirm/prompt with optional `promptText`.

## Activity tracking (`pw-tools-core.activity.ts`)

Records last-action timestamps per page/profile. Used by:
- Idle detection (auto-close inactive sandbox sessions).
- Status reports (`browser status` shows when the last action ran).

## Storage tooling (`pw-tools-core.storage.ts`)

Read/write cookies, localStorage, sessionStorage. Routes through Playwright's `context.storageState()` for read; targeted sets via `context.addCookies()` and per-page `evaluate` for storage.

## Trace recording (`pw-tools-core.trace.ts`)

Wraps Playwright's tracing API — start/stop, save trace zip to a path. Useful for debugging or for replaying agent sessions later.

## Snapshot response shaping (`pw-tools-core.responses.ts`)

Normalizes every action's result into a consistent envelope: status + page URL/title + new role refs + any captured downloads/dialogs/console output that fired during the action.

## Porting concerns for Python

- All Playwright calls have direct `playwright-python` equivalents.
- `_snapshotForAI()` is *internal* to Playwright (underscore-prefixed) and may not be exposed in playwright-python — fallback to `aria_snapshot()` is required.
- Role classification constants port directly (just hard-coded sets).
- Ref dedup tracker is straightforward dict logic.
- Downloads: `page.expect_download()` context manager replaces the arm-and-wait pattern in playwright-python.
- File chooser: `page.expect_file_chooser()` context manager.
- Dialog: `page.on("dialog", handler)` with sync handler — same shape.
- Activity tracking: `time.monotonic()` instead of `Date.now()`.
- Trace: `context.tracing.start(...)` / `context.tracing.stop(path=...)`.

## Open questions

- Do we need all 11 act kinds for v1, or can we ship with click/type/press/fill/snapshot/wait and add the rest later?
- Is the role-ref system worth the implementation cost, or do we start with aria-ref only (simpler, more stable)?
- Trace recording is heavy — gate behind a config flag?

---

## Deep second-pass — function-by-function

> Captured 2026-05-03. Source root: `extensions/browser/src/browser/`. All filename:line refs below are relative to that root unless absolute. The first-pass section above is correct at the architectural level; this section adds line-level fidelity for porting.

### 1. Function table per file

#### `pw-tools-core.ts` (8 lines, pure barrel)

| Function | Notes |
|---|---|
| (none — `export * from` only) | Re-exports activity, downloads, interactions, responses, snapshot, state, storage, trace. The Python port should mirror this with a single `pw_tools_core/__init__.py` that lifts each public symbol. |

#### `pw-tools-core.shared.ts`

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `bumpUploadArmId` / `bumpDialogArmId` / `bumpDownloadArmId` | `() -> number` | Module-level monotonic counters used as "arming generation tokens". | `armFileUploadViaPlaywright`, `armDialogViaPlaywright`, `waitForDownloadViaPlaywright`, `downloadViaPlaywright` | Counters are **global to the process**, not per-page. The arm-id is stored on `PageState.armIdUpload/armIdDialog/armIdDownload`; the listener compares against the stored id, so a stale listener still sees its own id but the page state's id has moved on → it returns silently. |
| `requireRef` | `(value: unknown) -> string` | Strips a leading `@` or `ref=` prefix; falls through `parseRoleRef` first to recognize `e\d+` form. Throws `"ref is required"` if blank. | All interaction helpers that take a ref. | Accepts both `e7` and `@e7` and `ref=e7` shapes. The agent may emit any of those — porters MUST handle all three. |
| `requireRefOrSelector` | `(ref?, selector?) -> {ref?, selector?}` | Trims both, throws if both empty. | click/hover/drag/select/type/scrollIntoView. | If both are provided, **ref wins** (see callers — `resolved.ref ? refLocator(...) : page.locator(...)`). |
| `normalizeTimeoutMs` | `(timeoutMs?, fallback) -> number` | Clamps to `[500, 120_000]`. | dialogs, downloads, evaluate (outer), scrollIntoView, etc. | NOTE: this is the *generic* clamp. Interaction-specific clamps live in `act-policy.ts` and have a tighter ceiling of 60_000 (interaction) / 120_000 (wait). |
| `toAIFriendlyError` | `(error, selector) -> Error` | Pattern-matches Playwright error strings and rewrites them into agent-readable messages: strict-mode → "matched N elements, run a new snapshot"; timeout-visible → "not found or not visible"; pointer-intercept → "not interactable (hidden or covered)". | Every interaction helper's catch block. | The string matching is fragile and is a known port hazard — Playwright Python's exception messages are slightly different. See translation table below. |

#### `pw-tools-core.state.ts` (note: state is mostly defined in `pw-session.ts`; this file is the *activity* tracking)

The first-pass mistakenly listed a separate `pw-tools-core.state.ts`; actually the file present in the repo at this name is **`pw-tools-core.activity.ts`** (page-level activity records). The shared `PageState` type and helpers live in `pw-session.ts`:

- `pw-session.ts:307` `ensurePageState(page) -> PageState` — returns or initializes `{console, errors, requests, requestIds, nextRequestId, armIdUpload, armIdDialog, armIdDownload}` and attaches `console`/`pageerror`/`request`/`response`/`requestfailed`/`close` listeners.
- `pw-session.ts:412` `ensureContextState(context) -> {traceActive: bool}` — context-scope state, currently only the trace lock.
- `pw-session.ts:235` `rememberRoleRefsForTarget`, `:260` `storeRoleRefsForTarget`, `:285` `restoreRoleRefsForTarget` — three-tier ref persistence (page state cache + LRU `roleRefsByTarget` map keyed by `{cdpUrl, targetId}`).
- `pw-session.ts:851` `refLocator(page, ref)` — the resolution function; see decision tree below.

Console retention: `MAX_CONSOLE_MESSAGES`, errors: `MAX_PAGE_ERRORS`, requests: `MAX_NETWORK_REQUESTS` (constants defined elsewhere in `pw-session.ts`). Each is a sliding-window FIFO via `Array.shift()`.

#### `pw-tools-core.activity.ts`

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `getPageErrorsViaPlaywright` | `({cdpUrl, targetId?, clear?}) -> {errors}` | Read-and-optionally-flush `pageerror` records. | CLI `browser errors` subcommand. | `clear` mutates `state.errors` in-place. |
| `getNetworkRequestsViaPlaywright` | `({cdpUrl, targetId?, filter?, clear?}) -> {requests}` | Read network log; substring-filter by URL; optional flush. | CLI `browser requests`. | When `clear` is set, also resets `state.requestIds = new WeakMap()` to drop in-flight correlations. |
| `getConsoleMessagesViaPlaywright` | `({cdpUrl, targetId?, level?}) -> messages` | Returns console buffer, optionally filtered by minimum severity (`debug<info=log<warning<error`, mapped via `consolePriority`). | CLI `browser console`. | Returns a copy when `level` is unset (`[...state.console]`); returns a filter result (also a copy) when set. |

#### `pw-tools-core.responses.ts`

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `responseBodyViaPlaywright` | `({cdpUrl, targetId?, url, timeoutMs?, maxChars?}) -> {url, status?, headers?, body, truncated?}` | Subscribe to `page.on("response", ...)`, wait for the next response whose URL matches `pattern` (via `matchBrowserUrlPattern`), then read body via `text()` or `body()`. | CLI `browser body`, also called through `act` indirectly when the agent asks for response bodies post-click. | `maxChars` clamped to `[1, 5_000_000]`, default 200_000. Timeout default 20_000. The handler uses `page.off("response", handler as never)` cleanup; same-event-emitter binding gotcha as in interactions. |

> **Important:** `responses.ts` is *not* the "action result envelope" file — the first-pass note "Snapshot response shaping (`pw-tools-core.responses.ts`)" is wrong. There is no envelope normalizer in this file; what's normalized is the HTTP response body. Action results are returned ad-hoc by each interaction helper as either `void` (most) or `unknown` (evaluate). The Python port can choose to introduce an envelope — OpenClaw doesn't have one.

#### `pw-tools-core.state.ts` (the *real* file at this name)

The file at `pw-tools-core.state.ts` actually exposes the **emulation/context-knob** helpers:

| Function | Signature | Purpose | Underlying API |
|---|---|---|---|
| `setOfflineViaPlaywright` | `({cdpUrl, targetId?, offline})` | Toggle offline mode. | `context.setOffline(bool)`. |
| `setExtraHTTPHeadersViaPlaywright` | `({cdpUrl, targetId?, headers})` | Add request headers context-wide. | `context.setExtraHTTPHeaders`. |
| `setHttpCredentialsViaPlaywright` | `({cdpUrl, targetId?, username?, password?, clear?})` | Basic-auth creds. | `context.setHTTPCredentials({username, password})` or `null` for clear. |
| `setGeolocationViaPlaywright` | `({lat, lng, accuracy?, origin?, clear?})` | Geo override + auto-grant `"geolocation"` permission for the active origin (best-effort). | `context.setGeolocation` + `context.grantPermissions`. |
| `emulateMediaViaPlaywright` | `({colorScheme})` | Light/dark/no-preference. | `page.emulateMedia({colorScheme})`. |
| `setLocaleViaPlaywright` | `({locale})` | CDP `Emulation.setLocaleOverride`. | Falls through silently if "Another locale override is already in effect" — Chrome only allows one. |
| `setTimezoneViaPlaywright` | `({timezoneId})` | CDP `Emulation.setTimezoneOverride`. | Same single-override gotcha; "Invalid timezone" rewritten with `cause`. |
| `setDeviceViaPlaywright` | `({name})` | Look up `playwright-core.devices[name]`, then chain `setViewportSize` + CDP `Emulation.setUserAgentOverride` + `setDeviceMetricsOverride` + `setTouchEmulationEnabled`. | Throws `Unknown device "X"` if descriptor missing. Touch-emulation only fires when `descriptor.hasTouch`. |

Porters: this file's helpers go through `withPageScopedCdpClient`, which is a Playwright `CDPSession` wrapper — `playwright-python` exposes that as `context.new_cdp_session(page)` returning a session with `.send(method, params)`.

#### `pw-tools-core.snapshot.ts` (snapshot + navigate + close + pdf + resize)

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `snapshotAriaViaPlaywright` | `({cdpUrl, targetId?, limit?, ssrfPolicy?}) -> {nodes}` | Raw AX-tree dump via CDP `Accessibility.enable` + `Accessibility.getFullAXTree`, then `formatAriaSnapshot`. Limit clamped to `[1, 2000]`, default 500. | This is the low-level dump; agent typically reads the `snapshotRoleViaPlaywright` higher-level form. SSRF assertion runs *before* the snapshot when `ssrfPolicy` is provided. |
| `snapshotAiViaPlaywright` | `({cdpUrl, targetId?, timeoutMs?, maxChars?, ssrfPolicy?}) -> {snapshot, truncated?, refs}` | Calls `page._snapshotForAI({timeout, track:"response"})` (Playwright internal). Truncates to `maxChars` if set. Stores refs as `mode: "aria"` on PageState + LRU. | **`_snapshotForAI` is underscore-prefixed and not officially exported in playwright-python.** Hard fail with `"Playwright _snapshotForAI is not available. Upgrade playwright-core."` if missing. The Python port either (a) shims via the internal API surface or (b) drops to `aria_snapshot()` + role-snapshot post-processing. |
| `snapshotRoleViaPlaywright` | `({cdpUrl, targetId?, selector?, frameSelector?, refsMode?, options?, ssrfPolicy?}) -> {snapshot, refs, stats}` | The main snapshot entry. Two modes: `"aria"` → uses `_snapshotForAI`, refuses selector/frameSelector; `"role"` (default) → calls `locator.ariaSnapshot()` on the resolved scope (page/frame/locator), runs `buildRoleSnapshotFromAriaSnapshot`. | Stores refs with `mode` and the `frameSelector` for later `refLocator` resolution. |
| `navigateViaPlaywright` | `({cdpUrl, targetId?, url, timeoutMs?, ssrfPolicy?}) -> {url}` | Pre-asserts `assertBrowserNavigationAllowed` (URL block-list/SSRF), then `gotoPageWithNavigationGuard`. Retries once on detached-frame errors after a forced disconnect+reconnect. Post-asserts `assertPageNavigationCompletedSafely`. | Timeout clamped `[1000, 120_000]`, default 20_000. Final URL read from `page.url()` after navigation. |
| `resizeViewportViaPlaywright` | `({width, height})` | `page.setViewportSize({width: max(1, floor(w)), height: max(1, floor(h))})`. | No-op for negative or non-integer inputs. |
| `closePageViaPlaywright` | `({cdpUrl, targetId?})` | `page.close()`. | The `close` listener registered in `ensurePageState` fires and removes the page from `pageStates` and `observedPages`. |
| `pdfViaPlaywright` | `({cdpUrl, targetId?}) -> {buffer}` | `page.pdf({printBackground: true})`. | Chrome-only in Playwright; Firefox/WebKit will throw upstream. |

#### `pw-tools-core.downloads.ts`

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `buildTempDownloadPath` | `(fileName) -> string` | UUID + sanitized filename joined to `<oc-tmp>/downloads/`. | Sanitizes via `sanitizeUntrustedFileName(fileName, "download.bin")`. |
| `createPageDownloadWaiter` | `(page, timeoutMs) -> {promise, cancel}` | One-shot listener factory: registers `page.on("download", handler)`, resolves on first event or rejects on timeout. Always `page.off`-cleans. | `cancel()` is idempotent (`done` flag). |
| `saveDownloadPayload` | `(download, outPath) -> {url, suggestedFilename, path}` | Resolves output path (uses `buildTempDownloadPath` if blank), `mkdir -p` parent, then either `download.saveAs(resolvedOutPath)` directly (when path was blank) or via `writeViaSiblingTempPath` for atomic rename. | Atomic-rename only kicks in for caller-supplied paths. |
| `awaitDownloadPayload` | `({waiter, state, armId, outPath?}) -> {url, suggestedFilename, path}` | Awaits the waiter; checks `state.armIdDownload === armId` (rejects with `"Download was superseded by another waiter"` otherwise); saves payload. | The arm-id staleness check is the *post-resolution* lock-out — the waiter may resolve, but if a newer arm has happened since, this caller throws. |
| `armFileUploadViaPlaywright` | `({paths?, timeoutMs?}) -> void` | Bumps upload arm-id, fire-and-forget `page.waitForEvent("filechooser", {timeout})`. On fire: re-checks arm-id (stale → noop); if `paths` empty, presses Escape to dismiss; else `resolveStrictExistingPathsWithinRoot` against `DEFAULT_UPLOAD_DIR`, then `fileChooser.setFiles(paths)`, then dispatches synthetic `input`/`change` events on the underlying input. | Returns immediately; the filling happens later when the chooser opens. **Last-arm-wins**: a second arm bumps the id and the first listener's id-check fails → it silently stops. Timeout default 120_000. |
| `armDialogViaPlaywright` | `({accept, promptText?, timeoutMs?}) -> void` | Bumps dialog arm-id, fire-and-forget `page.waitForEvent("dialog", {timeout})`. On fire: id-check, then `dialog.accept(promptText)` or `dialog.dismiss()`. | Last-arm-wins same as upload. Default timeout 120_000 via `normalizeTimeoutMs`. |
| `waitForDownloadViaPlaywright` | `({path?, timeoutMs?}) -> {url, suggestedFilename, path}` | **Synchronous** caller waits for next download. Bumps id, creates waiter, awaits payload. | Path optional; falls back to temp path. The caller MUST trigger the download itself (e.g., a separate click) before awaiting; this is purely the receive-side. |
| `downloadViaPlaywright` | `({ref, path, timeoutMs?}) -> {url, suggestedFilename, path}` | Combined trigger+capture: arms a waiter, calls `locator.click({timeout})` on the ref, then awaits the payload. | `path` required (throws `"path is required"` if blank). On click error, `toAIFriendlyError(err, ref)` is thrown after canceling the waiter. |

#### `pw-tools-core.storage.ts`

| Function | Signature | Purpose | Security model |
|---|---|---|---|
| `cookiesGetViaPlaywright` | `({cdpUrl, targetId?}) -> {cookies}` | `context.cookies()`. | Returns the full context cookie jar (cross-origin). Caller-side scoping is the responsibility of the Python port if needed. |
| `cookiesSetViaPlaywright` | `({cookie: {name, value, url?|domain+path?, ...}})` | `context.addCookies([cookie])` after validating that `url` *or* `domain+path` is set, plus `name` and non-undefined `value`. | Throws `"cookie requires url, or domain+path"` if neither key set. |
| `cookiesClearViaPlaywright` | `({})` | `context.clearCookies()`. | Wipes everything in the context. |
| `storageGetViaPlaywright` | `({kind: "local"\|"session", key?})` | `page.evaluate` reads from `window.localStorage` or `window.sessionStorage`; if `key` set returns `{[key]: value}` (or `{}` if missing); else dumps all entries. | Per-page (origin-scoped). Cross-origin reads not possible. |
| `storageSetViaPlaywright` | `({kind, key, value})` | `page.evaluate` `store.setItem(k, v)`. Throws if `key` blank. | Same origin scope. |
| `storageClearViaPlaywright` | `({kind})` | `page.evaluate` `store.clear()`. | Same. |

> **Reachability rule:** Cookies are **context-wide** (every origin); local/session storage are **page (origin) -scoped**. The Python port should mirror this — don't accidentally hoist storage to context level.

#### `pw-tools-core.trace.ts`

| Function | Signature | Purpose |
|---|---|---|
| `traceStartViaPlaywright` | `({screenshots?, snapshots?, sources?})` | Guards against double-start via `ContextState.traceActive`; calls `context.tracing.start({screenshots: true, snapshots: true, sources: false})` (defaults shown). |
| `traceStopViaPlaywright` | `({path})` | Guards `traceActive`; writes via `writeViaSiblingTempPath` (atomic rename) into `DEFAULT_TRACE_DIR`. |

Trace lock is **context-scoped**, not page-scoped: starting a trace on tab A and trying to start on tab B (same context) throws `"Trace already running"`.

#### `pw-tools-core.interactions.ts` (43 KB — the workhorse)

| Function (line) | Signature | Purpose | Wrapped? | Gotchas |
|---|---|---|---|---|
| `resolveBoundedDelayMs` (51) | `(value, label, maxMs) -> number` | Helper for click `delayMs`/wait `timeMs` validation. Throws if negative or above max. | n/a | Used by click hover-delay and wait. |
| `getRestoredPageForTarget` (62) | `(opts) -> Page` | `getPageForTargetId` + `ensurePageState` + `restoreRoleRefsForTarget`. | n/a | Used by every helper that needs ref resolution. |
| `didCrossDocumentUrlChange` (75) | `(page, previousUrl) -> bool` | Detects real navigation (origin/path/search differ) vs hash-only. | n/a | Hash-only changes do NOT trigger SSRF. |
| `isHashOnlyNavigation` (102) | `(currentUrl, previousUrl) -> bool` | Same logic but treats same-URL as "not hash-only" (because the event firing IS the signal). | n/a | The asymmetry between this and `didCrossDocumentUrlChange` is the key navigation-guard subtlety. |
| `isMainFrameNavigation` (119) | `(page, frame) -> bool` | True when `frame === page.mainFrame()`. | n/a | If `mainFrame` is not a function (test mocks), defaults to true. |
| `assertSubframeNavigationAllowed` (126) | `(frameUrl, ssrfPolicy?) -> Promise<void>` | Skips non-http/https frames (about:blank, about:srcdoc); else `assertBrowserNavigationResultAllowed`. | n/a | Subframe SSRF is enforced; `about:srcdoc` is whitelisted explicitly. |
| `snapshotNetworkFrameUrl` (147) | `(frame) -> string\|null` | Reads `frame.url()` if it's http/https; null otherwise. | n/a | `try/catch` guards detached-frame access. |
| `assertObservedDelayedNavigations` (156) | `(opts) -> Promise<void>` | After observation completes: walks subframe URLs (collecting first error), then asserts main-frame nav if any. Re-throws subframe error after main-frame check. | n/a | Subframe error wins over main-frame "ok"; main-frame error wins over subframe error if both fire. |
| `observeDelayedInteractionNavigation` (185) | `(page, previousUrl) -> Promise<{mainFrameNavigated, subframes[]}>` | Listens to `framenavigated` for `INTERACTION_NAVIGATION_GRACE_MS = 250ms`; collects subframe URLs; resolves on first main-frame nav OR timeout. | n/a | Pre-checks `didCrossDocumentUrlChange` and short-circuits. |
| `scheduleDelayedInteractionNavigationGuard` (235) | `(opts) -> Promise<void>` | Same observer pattern but **asserts** the SSRF check on completion. Stores cleanup fn in `pendingInteractionNavigationGuardCleanup` (WeakMap) to dedup. | n/a | Used in success path only. Replaces any previously-pending guard for the same page. |
| `assertInteractionNavigationCompletedSafely<T>` (320) | `({action, cdpUrl, page, previousUrl, ssrfPolicy?, targetId?}) -> Promise<T>` | The wrap-and-guard helper. Phase 1: keeps a listener alive for the full duration of `action()`. Phase 2: checks `navigatedDuringAction || didCrossDocumentUrlChange`; if either, run SSRF check. If action errored *and* no nav observed, runs `observeDelayedInteractionNavigation` to catch late SSRF blocks (lets SSRF errors win over action errors). On success with no nav, schedules a delayed guard. | n/a (this IS the wrap) | If `ssrfPolicy` is undefined, the wrap is a transparent passthrough. Subframe error vs main-frame error precedence: subframe error throws first if both fire. |
| `awaitActionWithAbort<T>` (426) | `(actionPromise, abortPromise?) -> Promise<T>` | `Promise.race` between action and abort; suppresses unhandled rejection on the action. | n/a | If no abort, returns the action directly. |
| `createAbortPromise` / `createAbortPromiseWithListener` (442/449) | `(signal?, onAbort?) -> {abortPromise?, cleanup}` | Build a rejecting promise from an `AbortSignal`. `signal.aborted` short-circuits with `signal.reason`. | n/a | The `onAbort` hook is used by `evaluateViaPlaywright` to force-disconnect Playwright (since async `evaluate` may not respect timeouts). |
| `highlightViaPlaywright` (483) | `({ref})` | `refLocator(...).highlight()` — overlay outline (debug only). | No | Errors mapped via `toAIFriendlyError`. |
| `clickViaPlaywright` (497) | `({ref?, selector?, doubleClick?, button?, modifiers?, delayMs?, timeoutMs?, ssrfPolicy?})` | `refLocator` or `page.locator(selector)`; if `delayMs > 0`, hover-then-sleep first; then `click()` or `dblclick()`. Whole thing inside `assertInteractionNavigationCompletedSafely`. | **Yes** | `delayMs` clamped to `[0, ACT_MAX_CLICK_DELAY_MS=5000]`. Hover-with-delay simulates "hover-intent" UI patterns. |
| `hoverViaPlaywright` (554) | `({ref?, selector?, timeoutMs?})` | `locator.hover({timeout})`. | No | No SSRF guard since hover doesn't trigger network. |
| `dragViaPlaywright` (576) | `({startRef?\|startSelector?, endRef?\|endSelector?, timeoutMs?})` | Resolves both endpoints, `startLocator.dragTo(endLocator, {timeout})`. | No | Error label is `"<start> -> <end>"`. |
| `selectOptionViaPlaywright` (605) | `({ref?\|selector?, values, timeoutMs?})` | `locator.selectOption(values, {timeout})`. Throws if `values` empty. | No | `values` is `string[]` — for single-select pass `[value]`. |
| `pressKeyViaPlaywright` (631) | `({key, delayMs?, ssrfPolicy?})` | `page.keyboard.press(key, {delay})`. | **Yes** | No locator — operates on page-level keyboard. Wrapped because Enter on a focused form can submit. |
| `typeViaPlaywright` (659) | `({ref?\|selector?, text, submit?, slowly?, timeoutMs?, ssrfPolicy?})` | `slowly`: click + `locator.type(text, {delay:75})`; else `locator.fill(text)`. If `submit`: separate `locator.press("Enter")` wrapped in `assertInteractionNavigationCompletedSafely`. | Partially (only the submit step) | The fill/type body itself is NOT SSRF-wrapped (no nav expected); only the submit `press("Enter")` is. |
| `fillFormViaPlaywright` (703) | `({fields: BrowserFormField[], timeoutMs?})` | Iterates fields; for `checkbox`/`radio` calls `setChecked(bool)`; else `locator.fill(value)`. | No | Per-field error throws immediately (fail-fast on first bad field). Empty `field.ref` skips the field silently. |
| `evaluateViaPlaywright` (743) | `({fn, ref?, timeoutMs?, signal?, ssrfPolicy?}) -> unknown` | Compiles `fn` body into a `new Function` wrapper that runs in browser context with an internal `Promise.race` timeout (because Playwright's `{timeout}` doesn't bound execution). With `ref` → `locator.evaluate`; without → `page.evaluate`. Wrapped. Aborts force-disconnect Playwright. | **Yes** | Outer timeout normalized to `[500, 120_000]`, default 20_000; inner browser-context race uses `outer - 500`. **Only act-kind that can return data.** Disabled when `evaluateEnabled === false`. |
| `scrollIntoViewViaPlaywright` (867) | `({ref?\|selector?, timeoutMs?})` | `locator.scrollIntoViewIfNeeded({timeout})`. | No | Default 20_000, uses generic `normalizeTimeoutMs` not `resolveActInteractionTimeoutMs`. |
| `waitForViaPlaywright` (889) | `({timeMs?, text?, textGone?, selector?, url?, loadState?, fn?, timeoutMs?, signal?})` | Sequentially runs each present condition: `waitForTimeout(timeMs)` → `getByText(text).first().waitFor("visible")` → `getByText(textGone).first().waitFor("hidden")` → `locator(selector).first().waitFor("visible")` → `waitForURL` → `waitForLoadState` → `waitForFunction(fn)`. Each step is racable against `signal`. | No | All steps run in order, NOT short-circuit. `timeMs` clamped `[0, ACT_MAX_WAIT_TIME_MS=30000]`. Outer timeout clamped `[500, 120_000]`. `fn` requires `evaluateEnabled` (checked at dispatch level, not here). |
| `takeScreenshotViaPlaywright` (960) | `({ref?, element?, fullPage?, type?}) -> {buffer}` | If `ref`: `refLocator(...).screenshot({type})`. If `element`: `page.locator(element).first().screenshot`. Else `page.screenshot({type, fullPage})`. | No | `fullPage` rejected with locator-scoped screenshots (clear error message). |
| `screenshotWithLabelsViaPlaywright` (995) | `({refs, maxLabels?, type?}) -> {buffer, labels, skipped}` | For each ref: `boundingBox()`, viewport-clip filter, then a single `page.evaluate` injects DOM overlay (yellow boxes + ref labels), takes the screenshot, then removes the overlay (in `finally`). | No | Default `maxLabels=150`. Skips refs that throw or have no bounding box. Overlay uses `data-openclaw-labels` marker for cleanup. |
| `setInputFilesViaPlaywright` (1122) | `({inputRef?, element?, paths})` | Resolves input via `refLocator` or `page.locator(element).first()`; canonicalizes paths via `resolveStrictExistingPathsWithinRoot` against `DEFAULT_UPLOAD_DIR`; calls `locator.setInputFiles(paths)`; then dispatches synthetic `input`/`change` events. | No | `inputRef` and `element` are **mutually exclusive**. Empty `paths` throws. |
| `executeSingleAction` (1173) | `(action, cdpUrl, targetId?, evaluateEnabled?, ssrfPolicy?, depth, signal?)` | The dispatch switch over `action.kind`. Recurses for `kind: "batch"`. | n/a | Depth limit `ACT_MAX_BATCH_DEPTH=5`. |
| `executeActViaPlaywright` (1335) | `({action, ...}) -> {result?, results?}` | Single entry point: dispatches batch vs single; only `evaluate` returns `{result}`, only `batch` returns `{results}`. | n/a | This is the "act tool" surface. |
| `batchViaPlaywright` (1373) | `({actions, stopOnError?, ...}) -> {results}` | Loops `executeSingleAction`; checks abort signal between each; `stopOnError !== false` → break on first failure; else continue and accumulate. | n/a | Default `stopOnError` is true; max actions `ACT_MAX_BATCH_ACTIONS=100`. |

---

### 2. Per-act-kind playbook

For each kind: pre-action steps, Playwright API calls, wrap status, response shape, error mappings, Python equivalent.

#### `click`

- **Pre-action:** `getRestoredPageForTarget` (gets page + restores ref cache), `requireRefOrSelector`, build locator (`refLocator` or `page.locator`).
- **Stabilization:** if `delayMs > 0`, do `locator.hover({timeout})` then `setTimeout(delayMs)`. Otherwise no explicit stabilization — Playwright's auto-wait handles visibility/enabled/stable.
- **Playwright calls:** `locator.click({timeout, button, modifiers})` or `locator.dblclick(...)` if `doubleClick`.
- **Wrap:** YES (`assertInteractionNavigationCompletedSafely`). Click is the canonical mutating action.
- **Response shape:** `void` (returned as `{}` by `executeActViaPlaywright`).
- **Errors:** strict-mode → "matched N elements"; visibility timeout → "not found or not visible"; pointer-intercept → "not interactable". Otherwise raw Playwright error.
- **Python:**
  ```python
  async def click(page, ref, *, double_click=False, button=None, modifiers=None, delay_ms=0, timeout_ms=8000, ssrf=None):
      locator = ref_locator(page, ref)
      timeout = clamp_interaction_timeout(timeout_ms)
      previous_url = page.url
      async def action():
          if delay_ms:
              await locator.hover(timeout=timeout)
              await asyncio.sleep(delay_ms / 1000)
          if double_click:
              await locator.dblclick(timeout=timeout, button=button, modifiers=modifiers)
          else:
              await locator.click(timeout=timeout, button=button, modifiers=modifiers)
      await assert_nav_completed_safely(action, page=page, previous_url=previous_url, ssrf=ssrf)
  ```

#### `type`

- **Pre-action:** as click; locator resolved from ref/selector.
- **Stabilization:** `slowly=True` → `locator.click({timeout})` first to focus, then character-by-character type. `slowly=False` → `locator.fill(text)` (clears + replaces in one shot).
- **Playwright calls:** `locator.fill(text, {timeout})` OR (`locator.click(...)` + `locator.type(text, {timeout, delay:75})`). If `submit`: separate `locator.press("Enter", {timeout})` wrapped.
- **Wrap:** Body NO; submit-Enter step YES.
- **Response shape:** `void`.
- **Errors:** same set; the submit-Enter is where SSRF rejection appears.
- **Python:**
  ```python
  if slowly:
      await locator.click(timeout=timeout)
      await locator.type(text, timeout=timeout, delay=75)
  else:
      await locator.fill(text, timeout=timeout)
  if submit:
      await assert_nav_completed_safely(lambda: locator.press("Enter", timeout=timeout), ...)
  ```

#### `press`

- **Pre-action:** `getPageForTargetId` + `ensurePageState` only (no ref).
- **Playwright:** `page.keyboard.press(key, {delay})`.
- **Wrap:** YES — Enter on a focused form, Tab moving focus into a JS-hooked element, etc., can navigate.
- **Response shape:** `void`. **Errors:** raw Playwright (no `toAIFriendlyError` wrap; this is one of two kinds without it — the other is `pressKey`'s own line because it doesn't have a ref label to feed into the rewriter).
- **Python:** `await page.keyboard.press(key, delay=delay_ms)` inside the nav-guard.

#### `hover`

- **Playwright:** `locator.hover({timeout})`. Wrap NO. Errors via `toAIFriendlyError`. Python: `await locator.hover(timeout=timeout)`.

#### `drag`

- **Pre-action:** resolve start and end via `requireRefOrSelector` × 2.
- **Playwright:** `startLocator.dragTo(endLocator, {timeout})`.
- **Wrap:** NO (drag within page doesn't navigate). Errors via `toAIFriendlyError` with combined label.
- **Python:** `await start.drag_to(end, timeout=timeout)`.

#### `select`

- **Playwright:** `locator.selectOption(values, {timeout})`. Wrap NO. Throws if `values` empty.
- **Python:** `await locator.select_option(values, timeout=timeout)`.

#### `fill` (multi-field form fill)

- **Pre-action:** loop over `fields[]`; for each: skip empty `ref`, parse type (default per `DEFAULT_FILL_FIELD_TYPE`).
- **Playwright:**
  - checkbox/radio → `locator.setChecked(bool, {timeout})` (truthy on `true`/`1`/`"1"`/`"true"`).
  - default → `locator.fill(stringified, {timeout})`.
- **Wrap:** NO. Errors per-field via `toAIFriendlyError(err, ref)`.
- **Python:** straightforward loop with `set_checked` and `fill`.

#### `scrollIntoView`

- **Playwright:** `locator.scrollIntoViewIfNeeded({timeout})` (timeout via `normalizeTimeoutMs`, default 20_000 — different from interaction default 8_000!).
- **Wrap:** NO. Errors via `toAIFriendlyError`.
- **Python:** `await locator.scroll_into_view_if_needed(timeout=timeout)`.

#### `wait`

- **Playwright:** sequential; each step is `awaitActionWithAbort`-raced:
  1. `page.waitForTimeout(timeMs)` (clamped 0..30000)
  2. `page.getByText(text).first().waitFor({state:"visible", timeout})`
  3. `page.getByText(textGone).first().waitFor({state:"hidden", timeout})`
  4. `page.locator(selector).first().waitFor({state:"visible", timeout})`
  5. `page.waitForURL(url, {timeout})`
  6. `page.waitForLoadState(loadState, {timeout})`
  7. `page.waitForFunction(fn, {timeout})` — gated on `evaluateEnabled` at dispatch level.
- **Wrap:** NO.
- **Response:** `void`.
- **Python:** straight translations: `wait_for_timeout`, `get_by_text(...).first.wait_for(state=...)`, `wait_for_url`, `wait_for_load_state`, `wait_for_function`. Race against the abort signal via `asyncio.wait`.

#### `evaluate`

- **Pre-action:** `getRestoredPageForTarget`. Outer timeout normalized to `[500, 120_000]`, default 20_000. Inner browser-context timeout = `outer - 500` (300ms-buffered floor).
- **Playwright:** Compiles `fn` text into a new `Function(...)` stub that wraps `eval("(" + fnBody + ")")`, calls it (passing `el` if ref-mode), and `Promise.race`s any returned promise against an internal timeout. Then `locator.evaluate(stub, args)` (ref-mode) or `page.evaluate(stub, args)` (page-mode).
- **Wrap:** YES.
- **Response:** the JS value returned (returned as `{result}` by `executeActViaPlaywright`).
- **Errors:** "Invalid evaluate function: ..." rewrap inside the stub; outer timeout via the inner Promise.race ("evaluate timed out after Nms"); abort signal force-disconnects Playwright.
- **Python:** Trickier — Playwright Python's `page.evaluate(expression, arg)` accepts the JS source string directly; you DON'T need to manually `new Function()`. But the inner Promise.race timeout must be re-emitted because Python Playwright also doesn't bound async-evaluate execution.
  ```python
  inner_timeout = max(1000, min(120_000, outer_timeout - 500))
  wrapped_fn = f"""
  ((args) => {{
      const candidate = ({fn_text});
      const result = typeof candidate === "function" ? candidate(args.el) : candidate;
      if (result && typeof result.then === "function") {{
          return Promise.race([result, new Promise((_, r) => setTimeout(() => r(new Error("evaluate timed out after {inner_timeout}ms")), {inner_timeout}))]);
      }}
      return result;
  }})
  """
  if ref:
      return await locator.evaluate(wrapped_fn)
  return await page.evaluate(wrapped_fn)
  ```
  (Aborts: implement via cancellation of the awaited task; on cancel, call `force_disconnect_playwright_for_target` equivalent.)

#### `close`

- **Playwright:** `page.close()`. Wrap NO. Triggers `close` listener that drops `pageStates` entry.
- **Python:** `await page.close()`.

#### `resize`

- **Playwright:** `page.setViewportSize({width: max(1, floor(w)), height: max(1, floor(h))})`. Wrap NO.
- **Python:** `await page.set_viewport_size({"width": w, "height": h})`.

---

### 3. Ref resolution — `refLocator()` decision tree

`pw-session.ts:851`:

```
input ref (string)
  │
  ├─ strip leading "@"  → keep tail
  ├─ strip leading "ref="  → keep tail
  └─ otherwise unchanged
  ↓
normalized
  │
  ├─ matches /^e\d+$/  → look up ref in PageState
  │     │
  │     ├─ state.roleRefsMode === "aria"  →  scope.locator(`aria-ref=${normalized}`)
  │     │     └─ scope = state.roleRefsFrameSelector ? page.frameLocator(fs) : page
  │     │
  │     ├─ else (mode "role")  →  state.roleRefs[ref]
  │     │     ├─ missing  →  throw `Unknown ref "X". Run a new snapshot...`
  │     │     ├─ has name  →  scope.getByRole(role, {name, exact: true})
  │     │     ├─ no name   →  scope.getByRole(role)
  │     │     └─ if info.nth !== undefined  →  .nth(info.nth)
  │     └─ end
  │
  └─ doesn't match /^e\d+$/  →  page.locator(`aria-ref=${normalized}`)
        (e.g., the agent passed `e1foo` or other freeform — falls to the aria-ref selector.)
```

**Stale ref handling:** when a ref id no longer exists in `state.roleRefs`, the lookup throws an `Unknown ref` error. The agent must call snapshot again. There's no auto-resnapshot fallback — by design.

**Frame-scoped refs:** if the snapshot was taken within a frame (`snapshotRoleViaPlaywright` with `frameSelector`), `state.roleRefsFrameSelector` is set. `refLocator` then scopes via `page.frameLocator(fs)` first, so `getByRole` and `aria-ref` selectors run inside the frame.

**Cross-page persistence:** when the page object swaps (renderer recovery, navigation), `restoreRoleRefsForTarget` looks up `roleRefsByTarget` (LRU `Map<targetIdKey, {refs, frameSelector?, mode?}>`) and re-attaches to the new `PageState`. LRU bounded by `MAX_ROLE_REFS_CACHE`. The key is `${cdpUrl}:${targetId}`.

**`nth` disambiguation:** in role-mode snapshots, `buildRoleSnapshotFromAriaSnapshot` runs all candidate refs through `RoleNameTracker`. After the first pass, `removeNthFromNonDuplicates` strips `nth: 0` from refs whose `role:name` key occurs only once. Refs that are duplicates retain `nth` and feed into `.nth(idx)` at resolution time.

---

### 4. Snapshot pipeline algorithm

#### Path A — `mode: "role"` (default)

```
inputs: page, frameSelector?, selector?, options
─────────────────────────────────────────────
1. scope = page
   if frameSelector: scope = page.frameLocator(frameSelector)
   if selector:      scope = scope.locator(selector)
   else:             scope = scope.locator(":root")

2. ariaSnapshot = await scope.ariaSnapshot()
   (Playwright returns indented YAML-ish role tree, e.g. lines like:
      `- button "Submit"`
      `  - text "OK"`)

3. tracker = new RoleNameTracker()
   refs = {}
   counter = 0; nextRef = () => `e${++counter}`
   if options.interactive:
       for line in ariaSnapshot.split("\n"):
           parsed = matchInteractiveSnapshotLine(line, options)
           if !parsed or !INTERACTIVE_ROLES.has(parsed.role): skip
           ref = nextRef()
           nth = tracker.getNextIndex(role, name)
           tracker.trackRef(role, name, ref)
           refs[ref] = {role, name, nth}
           append `- {roleRaw}{name?}[ref={ref}]{nth>0?[nth=N]}{suffix?}`
   else:
       for line in lines:
           processLine(...)  // checks interactive/content/structural,
                              // assigns ref iff interactive OR (content AND name);
                              // structural never gets a ref.
   removeNthFromNonDuplicates(refs, tracker)

4. tree = output.join("\n")
   if options.compact: tree = compactTree(tree)  // drops unnamed structural
                                                  // branches that have no
                                                  // ref-bearing descendants.

5. storeRoleRefsForTarget({page, cdpUrl, targetId, refs, frameSelector, mode:"role"})
6. return {snapshot: tree, refs, stats: {lines, chars, refs.count, interactive.count}}
```

#### Path B — `mode: "aria"` (uses Playwright internal)

```
inputs: page  (no selector/frameSelector permitted — throws otherwise)
─────────────────────────────────────────────
1. result = await page._snapshotForAI({timeout: 5000, track: "response"})
   (Playwright returns an AI-friendly indented tree where every interactive
    element ALREADY has an [ref=eN] suffix that Playwright owns and
    re-issues stably across snapshots.)

2. for line in result.full.split("\n"):
       parse line; extract role + name + suffix containing [ref=eN]
       if interactive (and ref present): refs[ref] = {role, name?}
       (no nth — Playwright's refs are unique)

3. storeRoleRefsForTarget({..., mode:"aria"})
4. return {snapshot, refs, stats}
```

**Output format the agent reads** (`role` mode example):

```
- generic [ref=e1]
  - banner [ref=e2]
    - heading "Acme Corp" [ref=e3]
    - link "Sign in" [ref=e4]
  - main [ref=e5]
    - form [ref=e6]
      - textbox "Email" [ref=e7]
      - textbox "Password" [ref=e8]
      - button "Submit" [ref=e9]
      - button "Submit" [ref=e10] [nth=1]   ← duplicate role:name → nth retained
```

In aria mode, the `[ref=eN]` ids come from Playwright; in role mode they come from OpenClaw's `nextRef` counter.

---

### 5. Download lifecycle in detail

State machine per page (in `PageState.armIdDownload`):

```
        ┌────────────┐
        │  unarmed   │  armIdDownload = 0
        └─────┬──────┘
              │ waitForDownload / download() → bumpDownloadArmId()
              ▼
        ┌────────────┐
        │  armed N   │  armIdDownload = N, listener registered
        └─────┬──────┘
              │
              ├─ download fires → handler resolves with payload
              │     ▼
              │   awaitDownloadPayload checks armIdDownload === N
              │     │ true  → saveDownloadPayload → return result
              │     └ false → throw "Download was superseded by another waiter"
              │
              ├─ second arm before fire → armIdDownload bumped to N+1
              │     (the N-listener is still attached but its post-resolve
              │      check will fail; meanwhile a new listener is now attached
              │      for N+1.)
              │
              └─ timeout → handler rejects "Timeout waiting for download",
                            page.off cleanup runs.
```

**Lock-out behavior on double-arm:** the FIRST arming's listener stays attached but is functionally dead — when its event fires, the `state.armIdDownload !== params.armId` check throws and the caller sees `"Download was superseded by another waiter"`. Both callers compete for the next download event; whichever fires first will resolve **its own** waiter, and the OTHER waiter remains pending until either another download fires or its timeout expires.

> Subtle: this is NOT mutually-exclusive cancellation. The first listener is not actively cancelled. It just becomes a no-op. If you arm twice and only one download fires, exactly one caller gets it (the one whose waiter resolves first, modulo arm-id check). The other caller waits to timeout.

**Where files are saved:**
- Caller-supplied `path` → that path (after `path.resolve`), via atomic-rename through `writeViaSiblingTempPath`.
- Blank `path` → `<resolvePreferredOpenClawTmpDir()>/downloads/<uuid>-<sanitized-suggested-name>`.

**Filename collision policy:** UUID prefix in temp paths makes collisions impossible. For caller-supplied paths, the atomic rename overwrites if the target exists (no collision check — caller's responsibility).

---

### 6. Dialog + file-chooser arming

Same arm-id pattern as downloads. Differences:

- **File chooser** (`armFileUploadViaPlaywright`):
  - Fire-and-forget; returns immediately. Listener uses `page.waitForEvent("filechooser", {timeout})`.
  - On fire: arm-id check → bail if stale; if `paths` empty → press Escape (Playwright dropped `FileChooser.cancel()`); else canonicalize via `resolveStrictExistingPathsWithinRoot`; `fileChooser.setFiles(paths)`; dispatch synthetic `input`/`change` (best-effort for sites that don't observe `setFiles`).
  - Default timeout 120_000.

- **Dialog** (`armDialogViaPlaywright`):
  - Fire-and-forget. Listener uses `page.waitForEvent("dialog", {timeout})`.
  - On fire: arm-id check → `dialog.accept(promptText)` if `accept`, else `dialog.dismiss()`.
  - `promptText` only used for `prompt()` dialogs; alert/confirm ignore it.
  - Default timeout 120_000.

Both are last-arm-wins, and both have their stale-listener silently no-op (no error). This contrasts with `waitForDownload` which surfaces "superseded" to the caller — because dialogs and choosers are fire-and-forget there's no caller to surface to.

---

### 7. Activity tracking

Recorded fields on `PageState` (initialized in `ensurePageState`):

| Field | Source | Capacity | Reader |
|---|---|---|---|
| `console: BrowserConsoleMessage[]` | `page.on("console")` | `MAX_CONSOLE_MESSAGES` (FIFO) | `getConsoleMessagesViaPlaywright` |
| `errors: BrowserPageError[]` | `page.on("pageerror")` | `MAX_PAGE_ERRORS` (FIFO) | `getPageErrorsViaPlaywright` |
| `requests: BrowserNetworkRequest[]` | `page.on("request")` + status filled in via `response`/`requestfailed` | `MAX_NETWORK_REQUESTS` (FIFO) | `getNetworkRequestsViaPlaywright` |
| `requestIds: WeakMap<Request, id>` | bookkeeping for the above | unbounded (WeakMap) | n/a |
| `armIdUpload/Dialog/Download` | shared.ts bumpers | scalar | armed handlers |

The `close` listener wipes the page from `pageStates` and `observedPages`. When the agent runs `browser status`, the gateway joins these per-target.

Retention is **process-lifetime** — there's no disk persistence. Restarting the OpenClaw extension drops everything. The Python port should mirror this: per-page state in a `dict[Page, PageState]` (Python doesn't need `WeakMap` semantics since `Page` objects are explicit lifecycle).

---

### 8. Storage tooling — exact API surface and security model

| API | Scope | Reach |
|---|---|---|
| `cookiesGet/Set/Clear` | Browser **context** | All origins in the context |
| `storageGet/Set/Clear({kind:"local"\|"session"})` | **Page (origin)** | Same-origin only — runs as `page.evaluate` so SOP applies |

**Security boundary:** there's no CSRF/origin filter on `cookiesSet` — the agent can set a cookie for any origin (because Playwright uses CDP `Network.setCookie`). The only validation is "url OR domain+path required". The Python port should keep this — the SSRF guard happens elsewhere (at navigate time).

Storage reads/writes go through `page.evaluate` and therefore inherit the page's current origin — reading `localStorage` only sees the active origin's storage. Cross-origin storage is not reachable through this surface (would require navigating first).

---

### 9. Trace recording

- **Start:** `context.tracing.start({screenshots: true, snapshots: true, sources: false})`. All three default-true except `sources` (which is heavy). Guarded by `ContextState.traceActive` flag — double-start throws `"Trace already running. Stop the current trace before starting a new one."`.
- **Stop:** `context.tracing.stop({path: tempPath})` written via `writeViaSiblingTempPath` for atomic rename into `DEFAULT_TRACE_DIR`. Flag flips back to false.
- **File format:** Playwright's standard `.zip` trace bundle (openable in `npx playwright show-trace`).
- **Save location:** path is caller-supplied; `DEFAULT_TRACE_DIR` is the *root* the atomic-rename helper roots out of. So `path` should be absolute (or relative to the root).
- **Scope:** **context-scoped**, so traces capture every page in the context simultaneously. Cannot trace one tab while another is untraced (with the same context).

---

### 10. Edge cases / gotchas a Python re-implementer will hit

1. **`assertInteractionNavigationCompletedSafely`'s 250ms grace + listener-during-action two-phase design.** A naïve port that just runs the action and then checks navigation will miss SSRF blocks that fire *during* a slow click. You MUST keep a `framenavigated` listener attached for the entire duration of the action AND keep a 250ms post-action grace window. Phases:
   - Phase 1 (during action): collect all main-frame and subframe navigations.
   - Phase 2 (post action, success path): run a *delayed* observer (`scheduleDelayedInteractionNavigationGuard`) for 250ms in case the click resolved before the navigation event.
   - Phase 3 (post action, error path): ALSO run a delayed observer — if a delayed nav arrives that should be blocked, the SSRF error wins over the action error.

2. **`isHashOnlyNavigation` vs `didCrossDocumentUrlChange` asymmetry.** When the event firing IS the signal (subframenavigated), same URL must be treated as "navigation happened" (form re-submit, reload). When polling URL state, same URL means nothing changed. Don't unify these — they encode different semantics.

3. **Subframe-error precedence.** In `assertObservedDelayedNavigations`: subframe error is collected, then main-frame check runs, then subframe error is re-thrown LAST. So if both fire, the caller sees the subframe error message. But during an interaction, if the action itself errors AND a subframe SSRF blocks, the subframe error wins over the action error.

4. **`evaluate` Promise.race-in-browser timeout.** Playwright's `{timeout}` on `evaluate` only governs *installing* the function. To bound execution of an async eval, you MUST wrap the user function in `Promise.race(userPromise, setTimeout-rejector)` IN BROWSER CONTEXT. Skipping this strands the per-page CDP command queue.

5. **Abort-signal force-disconnect for evaluate.** If the agent aborts a long-running `evaluate`, the only reliable way to terminate it is to forcibly disconnect Playwright from the target (`forceDisconnectPlaywrightForTarget`). Just rejecting the JS promise wrapper doesn't stop the in-browser code.

6. **`page.off` binding.** All `page.on/off` calls in this codebase use `page.off!("framenavigated", listener)` directly — NOT a cached reference. Playwright's `EventEmitter` requires `this` binding from the page; caching `const off = page.off` and calling `off(...)` will silently fail. Python's `page.off` (or `page.remove_listener`) is bound to the instance so this is less of an issue, but still — pass the same listener function reference.

7. **Last-arm-wins vs superseded-throws.** Downloads throw `"superseded"` to the *previous* caller; uploads/dialogs silently noop. Don't unify — agent UX depends on the difference.

8. **Snapshot truncation semantics.** `snapshotAiViaPlaywright` truncates the rendered text BUT preserves all parsed refs from the un-truncated original (`buildRoleSnapshotFromAiSnapshot(snapshot)` runs on the truncated string, but Playwright already issued aria-refs in the un-truncated tree — they're stable and the truncation only loses the lines, not the ref dictionary). Re-running snapshot after truncation works because Playwright maintains its own ref state.

9. **`refLocator` does NOT call `restoreRoleRefsForTarget`.** Callers must do that before calling `refLocator` (most use `getRestoredPageForTarget` which does this). If you forget, refs from a previous page swap won't be available.

10. **Form-fill empty-ref skip.** `fillFormViaPlaywright` silently skips fields where `field.ref.trim() === ""`. Don't error-on-empty in the Python port — that's a feature (lets the agent batch a "best-effort" form fill without knowing every ref is valid). But beware: the agent might think a field was filled when it was actually skipped.

11. **`requireRefOrSelector` ordering.** When BOTH ref and selector are provided, **ref wins**. The Python port should match.

12. **Drag uses `dragTo`, not manual mouse events.** Playwright's `Locator.dragTo` simulates with `mousedown` + `mousemove` + `mouseup` and is the right surface — don't fall back to keyboard simulation or page.mouse.* unless `dragTo` fails on a specific target.

13. **`setInputFiles` synthetic events dispatch.** Some sites don't react to `setFiles` alone; OpenClaw dispatches `input`/`change` synthetic events as a best-effort follow-up. Do the same in Python.

14. **Trace start is context-scoped, not page-scoped.** A naive "I have a tracing object per page" port would duplicate-start.

15. **`snapshotRoleViaPlaywright` with `refsMode: "aria"` rejects `selector`/`frameSelector`.** Hard error: `"refs=aria does not support selector/frame snapshots yet."` Mirror this restriction.

16. **Subframe URLs that are `about:blank` / `about:srcdoc`.** Skipped from SSRF check (they don't cross the network boundary). Implement the same skip.

17. **`navigateViaPlaywright` retries once on detached-frame.** Force-disconnect, refetch page, retry. Python: same shape (catch the specific Playwright error class, force-reconnect, retry once).

18. **Evaluate clamped to `outer-500`.** The 500ms headroom is for routing+serialization. If your Python wire layer is slower, increase the headroom.

19. **`clearCookies()` vs `clearCookies({domain})`.** OpenClaw uses the unscoped form — wipes everything. If you want per-domain wipe, that's a separate API.

20. **Wait-step ordering matters.** The `waitForViaPlaywright` runs every present condition in fixed order: timeMs → text → textGone → selector → url → loadState → fn. Don't reorder.

---

### 11. `playwright-python` translation table

| TypeScript (Playwright Node) | Python (`playwright.async_api`) | Notes |
|---|---|---|
| `await getPageForTargetId(...)` | custom — fetch by `target_id` from CDP target list | OpenClaw-specific glue, port `pw-session.ts`'s logic. |
| `page.url()` | `page.url` | Property in Python, function in JS. |
| `page.locator(sel)` | `page.locator(sel)` | Same. |
| `page.frameLocator(sel)` | `page.frame_locator(sel)` | snake_case. |
| `page.getByRole(role, {name, exact})` | `page.get_by_role(role, name=name, exact=True)` | snake_case. |
| `page.getByText(text)` | `page.get_by_text(text)` | |
| `locator.first()` | `locator.first` | property. |
| `locator.nth(n)` | `locator.nth(n)` | same. |
| `locator.click({timeout, button, modifiers})` | `await locator.click(timeout=ms, button=..., modifiers=[...])` | |
| `locator.dblclick(...)` | `await locator.dblclick(...)` | |
| `locator.hover({timeout})` | `await locator.hover(timeout=ms)` | |
| `locator.fill(text, {timeout})` | `await locator.fill(text, timeout=ms)` | |
| `locator.type(text, {delay, timeout})` | `await locator.type(text, delay=75, timeout=ms)` | Note: `locator.type` is **deprecated** in newer Playwright Python; use `locator.press_sequentially(text, delay=75)`. |
| `locator.press(key, {timeout})` | `await locator.press(key, timeout=ms)` | |
| `locator.selectOption(values, {timeout})` | `await locator.select_option(values, timeout=ms)` | |
| `locator.setChecked(bool, {timeout})` | `await locator.set_checked(checked, timeout=ms)` | |
| `locator.dragTo(other, {timeout})` | `await locator.drag_to(other, timeout=ms)` | |
| `locator.scrollIntoViewIfNeeded({timeout})` | `await locator.scroll_into_view_if_needed(timeout=ms)` | |
| `locator.evaluate(fn, args)` | `await locator.evaluate(fn_string, arg)` | Python takes single `arg`, not destructured object. |
| `locator.boundingBox()` | `await locator.bounding_box()` | |
| `locator.elementHandle()` | `await locator.element_handle()` | |
| `locator.screenshot({type})` | `await locator.screenshot(type=...)` | |
| `locator.setInputFiles(paths)` | `await locator.set_input_files(paths)` | |
| `locator.ariaSnapshot()` | `await locator.aria_snapshot()` | snake_case. |
| `locator.highlight()` | `await locator.highlight()` | Same. |
| `page.locator(":root")` | `page.locator(":root")` | Same. |
| `page.locator("aria-ref=eN")` | `page.locator("aria-ref=eN")` | The `aria-ref=` selector exists in both. |
| `page._snapshotForAI({timeout, track:"response"})` | **NOT officially exposed.** Workarounds: (a) cast to internal `_impl` and access; (b) use `page.aria_snapshot()` + post-process to insert `[ref=eN]` ids ourselves. | **Hard porting cliff.** Document the limitation and ship "role" mode first. |
| `page.keyboard.press(key, {delay})` | `await page.keyboard.press(key, delay=ms)` | |
| `page.evaluate(fn, args)` | `await page.evaluate(fn_string, arg)` | |
| `page.screenshot({type, fullPage})` | `await page.screenshot(type=..., full_page=True)` | |
| `page.pdf({printBackground: true})` | `await page.pdf(print_background=True)` | Chromium only in both. |
| `page.setViewportSize({width, height})` | `await page.set_viewport_size({"width": w, "height": h})` | |
| `page.close()` | `await page.close()` | |
| `page.waitForTimeout(ms)` | `await page.wait_for_timeout(ms)` | |
| `page.waitForURL(url, {timeout})` | `await page.wait_for_url(url, timeout=ms)` | |
| `page.waitForLoadState(state, {timeout})` | `await page.wait_for_load_state(state, timeout=ms)` | |
| `page.waitForFunction(fn, {timeout})` | `await page.wait_for_function(fn_string, timeout=ms)` | |
| `page.waitForEvent("download", {timeout})` | `async with page.expect_download(timeout=ms) as info: ...; download = await info.value` | Python prefers context-manager pattern. **For arm-and-go pattern,** use `page.on("download", handler)` directly. |
| `page.waitForEvent("filechooser", {timeout})` | `async with page.expect_file_chooser(timeout=ms) as info: ...; chooser = await info.value` OR `page.on("filechooser", handler)`. | Same pattern. |
| `page.waitForEvent("dialog", {timeout})` | `page.on("dialog", handler)` | Synchronous handler taking the dialog (no await). |
| `page.on("framenavigated", listener)` / `page.off(...)` | `page.on("framenavigated", listener)` / `page.remove_listener("framenavigated", listener)` | Python uses `remove_listener` (or alias `off` in some versions). |
| `download.suggestedFilename()` | `download.suggested_filename` | property. |
| `download.url()` | `download.url` | property. |
| `download.saveAs(path)` | `await download.save_as(path)` | |
| `dialog.accept(promptText)` | `await dialog.accept(prompt_text)` | |
| `dialog.dismiss()` | `await dialog.dismiss()` | |
| `fileChooser.setFiles(paths)` | `await chooser.set_files(paths)` | |
| `context.cookies()` | `await context.cookies()` | |
| `context.addCookies([cookie])` | `await context.add_cookies([cookie])` | |
| `context.clearCookies()` | `await context.clear_cookies()` | |
| `context.setOffline(bool)` | `await context.set_offline(offline)` | |
| `context.setExtraHTTPHeaders(headers)` | `await context.set_extra_http_headers(headers)` | |
| `context.setHTTPCredentials({...})` | `await context.set_http_credentials(...)` | |
| `context.setGeolocation({...})` | `await context.set_geolocation({"latitude":..., "longitude":...})` | |
| `context.grantPermissions(["geolocation"], {origin})` | `await context.grant_permissions(["geolocation"], origin=origin)` | |
| `context.tracing.start({...})` | `await context.tracing.start(screenshots=True, snapshots=True, sources=False)` | |
| `context.tracing.stop({path})` | `await context.tracing.stop(path=...)` | |
| `page.emulateMedia({colorScheme})` | `await page.emulate_media(color_scheme="dark")` | |
| `playwrightDevices` | `from playwright.async_api import async_playwright; ...; pw.devices` | Devices dict on the playwright handle, same shape. |
| `page.context().new_cdp_session(page)` (TS: `withPageScopedCdpClient`) | `await page.context.new_cdp_session(page)` returns a `CDPSession` with `.send(method, params)` | Python exposes this directly. |
| `AbortSignal` | `asyncio.Task.cancel()` / `asyncio.CancelledError` | Different model — wrap actions in tasks and call `.cancel()` from the abort handler. |
| `WeakMap<Page, X>` | `dict[Page, X]` + manual lifecycle (clean on `page.on("close", ...)`) | Python `WeakValueDictionary` works only if Page is the value. Use `dict` and clean explicitly. |
| `crypto.randomUUID()` | `uuid.uuid4().hex` | |
| `Date.now()` | `time.monotonic()` for relative; `datetime.now(UTC)` for ISO timestamps | OpenClaw uses ISO strings (`new Date().toISOString()`) for activity records. |
| `new Function("el", "args", body)` | The Python port doesn't need this — Playwright Python's `evaluate` takes a JS source string directly. Just construct the wrapper-string and pass. | Saves a layer. |
| `page._snapshotForAI` | (see above — port hazard) | |
| `formatErrorMessage(err)` | custom helper — port from `infra/errors.ts` | Likely just `str(err)`. |
| `normalizeOptionalString(v)` | `str(v).strip() if v else ""` | One-liner. |

---

### Wrap status reference (concise)

| Kind | Wrapped in `assertInteractionNavigationCompletedSafely`? | Why |
|---|---|---|
| click | YES (full action) | navigation expected |
| type — fill body | NO | no nav |
| type — submit step | YES (Enter press only) | submit triggers form post |
| press | YES | Enter/Tab can navigate |
| hover | NO | no nav |
| drag | NO | within-page |
| select | NO | no nav |
| fill (multi-field) | NO | no nav per-field |
| scrollIntoView | NO | no nav |
| wait | NO | no nav (it's reading state) |
| evaluate | YES | arbitrary JS can navigate |
| close | NO | terminal |
| resize | NO | viewport-only |

---

### Constants reference (port directly)

```python
# act-policy.ts
ACT_MAX_BATCH_ACTIONS = 100
ACT_MAX_BATCH_DEPTH = 5
ACT_MAX_CLICK_DELAY_MS = 5_000
ACT_MAX_WAIT_TIME_MS = 30_000
ACT_MIN_TIMEOUT_MS = 500
ACT_MAX_INTERACTION_TIMEOUT_MS = 60_000
ACT_MAX_WAIT_TIMEOUT_MS = 120_000
ACT_DEFAULT_INTERACTION_TIMEOUT_MS = 8_000
ACT_DEFAULT_WAIT_TIMEOUT_MS = 20_000

# interactions.ts
INTERACTION_NAVIGATION_GRACE_MS = 250

# pw-session.ts (sliding-window caps — values not shown above; grep MAX_*)
# MAX_CONSOLE_MESSAGES, MAX_PAGE_ERRORS, MAX_NETWORK_REQUESTS, MAX_ROLE_REFS_CACHE
```

---

### Porting roadmap recommendation

For an MVP Python port that delivers most of the value:

1. **Phase 1 (must have):** click, type (without slowly), press, fill (single field), wait (timeMs+selector+url+loadState), `snapshotRoleViaPlaywright` in role-only mode (skip `_snapshotForAI`), `refLocator` role-mode resolution, basic nav-guard (just URL diff, skip the 250ms framenavigated dance for now), error rewriter `to_ai_friendly_error`, downloads (synchronous-receive only), `set_input_files`, screenshot (page+ref), close, resize.
2. **Phase 2:** dialog arming, file-chooser arming, the 250ms framenavigated grace window, evaluate (with browser-context Promise.race), `wait` extras (text/textGone/fn), batching, abort signals, drag, select, hover, scrollIntoView, slowly-type.
3. **Phase 3:** `_snapshotForAI` shim or fallback story, screenshot-with-labels, trace recording, storage tooling, emulation knobs (locale/timezone/device), retried-on-detach `navigate`.

Phase 1 covers ~80% of agent flows. Phase 2 closes the SSRF-correctness gap. Phase 3 is polish + tracing/debug surface.
