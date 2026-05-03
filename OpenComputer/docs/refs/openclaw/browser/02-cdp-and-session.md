# OpenClaw browser — CDP attach + Playwright session lifecycle

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; JIT deeper read of the named files when porting.

## One-line summary

OpenClaw connects to Chrome's CDP endpoint via Playwright's `connectOverCDP`, deduplicates concurrent connect attempts, recovers stale Page references via CDP ground-truth queries, and gates every navigation through a fail-closed SSRF guard before any network leaves.

## The three load-bearing concepts

1. **In-flight promise dedup** prevents thundering herd on concurrent connects.
2. **`pageTargetId()` polling** recovers stale Playwright Page refs by querying CDP ground truth.
3. **Fire-and-forget `browser.close()` + reconnect** pattern lets the system tolerate hung disconnects without wedging.

## CDP connection lifecycle

```
launch chrome (or attach existing)
  → discover ws endpoint (json/version, json/list, or process stdout)
  → playwright.connectOverCDP(wsEndpoint)
  → select default BrowserContext (the Chrome user-data-dir's context)
  → cache Browser instance in cachedByCdpUrl
  → ready
```

`connectBrowser()`:
- Deduplicates by CDP URL: `connectingByCdpUrl: Map<url, Promise<Browser>>`. If two callers race, the second awaits the first's promise.
- Retries with exponential backoff (3 attempts).
- Wraps the connect in a proxy-bypass scope (NO_PROXY env var temporarily set; reference-counted so external mutations aren't clobbered).
- On success, caches the Browser in `cachedByCdpUrl`.

## Stale Page reference recovery

Playwright can return *different Page objects* for the same Chrome target across reconnects. OpenClaw never trusts a Page identity — every operation that needs to know "which tab is this" calls `pageTargetId(page)`, which queries `Target.getTargetInfo` over CDP for ground truth.

If CDP resolution fails (page is dead/blocked), it falls back to polling Chrome's `/json/list` HTTP endpoint to enumerate targets and find the one matching the URL/title.

Role refs (snapshot refs like `e1`, `e2`) are cached *by targetId* in a Map called `roleRefsByTarget` — so even if the Page object changes, the refs survive the swap. LRU caps it at 50 entries.

## Navigation guard mechanism (SSRF defense)

This is the security perimeter. Before any navigation produces network traffic:

1. Playwright route handler intercepts every request via `page.route("**/*", ...)`.
2. Handler calls `assertBrowserNavigationAllowed()` against the URL — checks blocked-host list, blocked-IP ranges, blocked schemes (file://, chrome://, etc.).
3. **Fail-closed**: if the handler can't resolve the request's frame (e.g. ambiguous), it blocks the navigation rather than allowing.
4. Blocked nav: aborts the route, marks the target via `markTargetBlocked()` + `markPageRefBlocked()` — prevents re-selection and stops the agent from accidentally landing on the same blocked tab again.
5. Both top-level *and* subframe navigations are guarded.

## Timeout policy (`cdp-timeouts.ts`)

Three timeout tiers:
- HTTP CDP calls (e.g. `Target.getTargetInfo`)
- WebSocket CDP calls (the persistent CDP session)
- Per-profile clamps (`existing-session` gets shorter timeouts since it's racing with a user)

Timeouts are clamped at the boundary so a misbehaving page can't hang the agent loop — every CDP call runs under a deadline.

## Proxy bypass (`cdp-proxy-bypass.ts`)

Why it exists: corporate proxies (and even macOS system proxies) intercept localhost traffic in some configurations, breaking CDP-over-WebSocket.

How: temporarily prepends `localhost,127.0.0.1` to `NO_PROXY` env var around the connect call. **Reference-counted** so concurrent connects don't restore the var prematurely.

## Teardown / disconnect

- Graceful path: `browser.close()` is **fire-and-forget**. If it hangs, the next `connectBrowser()` makes a fresh connection.
- Force path: `forceDisconnectPlaywrightForTarget(targetId)` evicts the cache entry and (if needed) sends `Page.close` directly via raw CDP to kill stuck JS before reconnecting.
- Server shutdown: closes all cached Browsers in parallel with a deadline.

## Porting concerns for Python

- `playwright-python` has `playwright.chromium.connect_over_cdp(ws_url)` — direct equivalent.
- Async-only — Python port must be `asyncio` end to end. No sync API.
- `WeakMap` → `weakref.WeakValueDictionary` (or just a regular dict + manual cleanup).
- In-flight promise dedup → `asyncio.Future` map, same pattern.
- Route interception: `page.route(...)` exists in playwright-python with the same shape.
- `Target.getTargetInfo`: `cdp_session = await page.context.new_cdp_session(page); await cdp_session.send("Target.getTargetInfo")`.
- Fire-and-forget close: `asyncio.create_task(browser.close())` with the result discarded.

## Open questions

- Are we keeping support for `remote-cdp` driver, or only local-spawned Chromium for v1?
- How aggressively do we mirror the SSRF block-list — same defaults as OpenClaw (block private IPs, file://, chrome://) or stricter?
- Does OpenComputer's existing consent layer want to wrap the navigation guard, or replace it?

## Deep second-pass — function-by-function

> Captured 2026-05-03. All filename refs are relative to `extensions/browser/src/browser/` inside the OpenClaw repo at `/Users/architsakri/Downloads/Harnesses/openclaw-main/extensions/browser/src/browser/`. Tests skimmed for edge cases.

### File 1 — `cdp.ts` (CDP wire-protocol primitives, screenshot, evaluate, snapshots)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `normalizeCdpWsUrl` (cdp.ts:20) | `(wsUrl, cdpUrl) => string` | Rewrites a `webSocketDebuggerUrl` from `/json/version` so it points to the externally-routable host. Treats `0.0.0.0` and `[::]` as wildcards that need rewriting. Promotes `ws:` → `wss:` if the CDP URL is HTTPS. Carries username/password and search params over from the CDP URL. | `createTargetViaCdp` (cdp.ts:193), `tryTerminateExecutionViaCdp` (pw-session.ts:958), every place that derives a WS URL from an HTTP CDP base. | Containerised browsers (e.g. browserless) advertise `ws://0.0.0.0:<internal>` — the rewrite is load-bearing. Auth credentials in the original CDP URL must propagate or the WS handshake 401s. |
| `captureScreenshotPng` (cdp.ts:50) | `({wsUrl, fullPage?}) => Promise<Buffer>` | Convenience wrapper over `captureScreenshot` that forces `format: "png"`. | screenshot route handlers. | None beyond `captureScreenshot`. |
| `captureScreenshot` (cdp.ts:61) | `({wsUrl, fullPage?, format?, quality?}) => Promise<Buffer>` | Opens a CDP socket; for full-page captures, queries `Page.getLayoutMetrics` + `Runtime.evaluate` to read viewport, calls `Emulation.setDeviceMetricsOverride` to grow it, captures with `Page.captureScreenshot { captureBeyondViewport: true }`, then restores. | Snapshot tools, screenshot tool. | Chromium 130+ fixed bug 40760789; 146+ rejects `fromSurface: false` so we omit it. JPEG quality is clamped 0–100. The restore path re-applies emulation if clearing `setDeviceMetricsOverride` reverted to natural dims. |
| `createTargetViaCdp` (cdp.ts:169) | `({cdpUrl, url, ssrfPolicy?}) => Promise<{targetId}>` | Creates a new tab via raw CDP `Target.createTarget` (skips Playwright). Calls `assertBrowserNavigationAllowed` first, then either uses the URL directly if it's already a `ws(s):` URL or fetches `/json/version` to discover the WS endpoint. | Some routes that need a tab without paying the Playwright connect cost. | Throws on missing `targetId`. SSRF gate runs *before* DNS, then WS endpoint runs through `assertCdpEndpointAllowed`. |
| `evaluateJavaScript` (cdp.ts:229) | `({wsUrl, expression, awaitPromise?, returnByValue?}) => Promise<{result, exceptionDetails?}>` | Runs `Runtime.enable` (best-effort) then `Runtime.evaluate` with `userGesture: true, includeCommandLineAPI: true, returnByValue` defaulting to true. | `snapshotDom`, `getDomText`, `querySelector`. | Throws if no `result` field. `userGesture: true` matters for click-handlers that gate on user activation. |
| `formatAriaSnapshot` (cdp.ts:293) | `(nodes, limit) => AriaSnapshotNode[]` | Builds an indexed map by `nodeId`, picks a root by "not referenced as a child", DFS-walks (stack-based, push children in reverse so the visit order matches DOM order). Emits compact nodes with `ax{N}` refs. | `snapshotAria`. | Skips nodes that disappear from `byId`. Limit is hard cap; `out.length < limit` short-circuits the stack loop. |
| `snapshotAria` (cdp.ts:352) | `({wsUrl, limit?}) => Promise<{nodes}>` | `Accessibility.enable` (best-effort) → `Accessibility.getFullAXTree` → `formatAriaSnapshot`. Limit clamped 1..2000, default 500. | Aria snapshot route. | `Accessibility.enable` swallows errors — some hosts deny it. |
| `snapshotDom` (cdp.ts:367) | `({wsUrl, limit?, maxTextChars?}) => Promise<{nodes: DomSnapshotNode[]}>` | Inlines a JS expression that walks `document.documentElement` with a stack, collects `tag/id/class/role/aria-label/innerText/href/type/value`. Limit 1..5000 (default 800), text 0..5000 (default 220). | DOM snapshot route. | Pure JS in-page — does NOT use CDP DOM domain. Dependent on `innerText` being tolerant; wrapped in try/catch. |
| `getDomText` (cdp.ts:452) | `({wsUrl, format, maxChars?, selector?}) => Promise<{text}>` | Returns either `innerText` or `outerHTML`. Truncation clamped at 5,000,000 chars (default 200,000). Selector optional. | DOM-text route. | `format` is `"html"` | `"text"`. Adds `<!-- …truncated… -->` marker on overflow. |
| `querySelector` (cdp.ts:493) | `({wsUrl, selector, limit?, maxTextChars?, maxHtmlChars?}) => Promise<{matches: QueryMatch[]}>` | Inlined JS `document.querySelectorAll`, limited to 200, builds compact match objects with index, tag, id, class, text, value, href, outerHTML. | querySelector route. | All limits clamped server-side; selector is JSON-stringified into the expression — careful with quote injection (none — JSON is safe). |

#### Re-exports

`cdp.ts:12-18` re-exports `appendCdpPath`, `fetchJson`, `fetchOk`, `getHeadersWithAuth`, `isWebSocketUrl` from `cdp.helpers.ts`. Treat as the public surface.

### File 2 — `cdp.helpers.ts` (HTTP/WS plumbing, CDP send framing)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `parseBrowserHttpUrl` (cdp.helpers.ts:19) | `(raw, label) => {parsed, port, normalized}` | Strict-parses a CDP-ish URL, validates `http(s)/ws(s)` only, infers default port from protocol, strips trailing `/`. | Config validators. | Throws on unknown protocol / invalid port. |
| `isWebSocketUrl` (cdp.helpers.ts:51) | `(url) => boolean` | True iff `ws:` or `wss:`. Catches malformed URLs. | `createTargetViaCdp` (cdp.ts:181) — chooses direct-WS path vs `/json/version` discovery. | Returns false for any URL that fails `new URL()`. |
| `assertCdpEndpointAllowed` (cdp.helpers.ts:60) | `(cdpUrl, ssrfPolicy?) => Promise<void>` | Resolves hostname under SSRF policy via `resolvePinnedHostnameWithPolicy`. No-op if no policy. | All connect/fetch entry points. | Wraps SSRF errors as `BrowserCdpEndpointBlockedError`. |
| `redactCdpUrl` (cdp.helpers.ts:80) | `(cdpUrl) => string \| null \| undefined` | Strips username/password before logging. Falls back to `redactSensitiveText` on parse failure. | Logging. | Preserves null/undefined inputs as-is. |
| `getHeadersWithAuth` (cdp.helpers.ts:115) | `(url, headers={}) => headers` | If no `Authorization` header and the URL has user:pass, builds `Basic` auth header. | `fetchCdpChecked`, `openCdpWebSocket`. | Case-insensitive header check via `normalizeLowercaseStringOrEmpty`. |
| `appendCdpPath` (cdp.helpers.ts:135) | `(cdpUrl, path) => string` | Strips trailing `/` from base path, ensures suffix starts with `/`, joins. Preserves search params. | `/json/version`, `/json/list` calls. | Uses `URL` so query string is preserved automatically. |
| `normalizeCdpHttpBaseForJsonEndpoints` (cdp.helpers.ts:143) | `(cdpUrl) => string` | Coerces ws→http, wss→https, strips `/devtools/browser/<id>` and trailing `/cdp`. Used so `/json/list` works for direct-WS CDP URLs. | `findPageByTargetIdViaTargetList`, `tryTerminateExecutionViaCdp`. | Has a regex fallback if `URL` parsing fails. |
| `createCdpSender` (cdp.helpers.ts:170, internal) | `(ws) => {send, closeWithError}` | Wraps `ws.send` in promise-per-id. Maintains `pending: Map<id, {resolve,reject}>`. On `error`/`close` rejects all pending. | `withCdpSocket`. | Quirk: messages without numeric `id` are silently ignored — CDP events go nowhere. JSON parse errors swallowed. |
| `fetchJson<T>` (cdp.helpers.ts:231) | `(url, timeoutMs=1500, init?, ssrfPolicy?) => Promise<T>` | Wraps `fetchCdpChecked`, `.json()`, releases body. | `/json/version`, `/json/list` calls. | Default timeout = `CDP_HTTP_REQUEST_TIMEOUT_MS` = 1500. |
| `fetchCdpChecked` (cdp.helpers.ts:245) | `(url, timeoutMs?, init?, ssrfPolicy?) => Promise<{response, release}>` | `AbortController` deadline + `withNoProxyForCdpUrl` + `fetchWithSsrFGuard`. Maps 429 to a tool-friendly "do NOT retry" error; non-2xx → `HTTP <status>`. | `fetchJson`, `fetchOk`. | `release()` is idempotent (`released` flag). 429 message specifically says "Do NOT retry" — model-injection-aware error surface. |
| `fetchOk` (cdp.helpers.ts:293) | `(url, ...) => Promise<void>` | Like `fetchJson` but discards body. Used for control endpoints (`/json/close`, `/json/activate`). | Tab-ops routes. | Same SSRF/timeout semantics. |
| `openCdpWebSocket` (cdp.helpers.ts:303) | `(wsUrl, opts?) => WebSocket` | Constructs a `ws` `WebSocket` with handshake timeout (default 5000), Basic-auth headers, and a direct (non-proxy) HTTP agent if loopback. | `withCdpSocket`. | Uses `getDirectAgentForCdp` to skip system proxies for loopback. |
| `withCdpSocket<T>` (cdp.helpers.ts:320) | `(wsUrl, fn, opts?) => Promise<T>` | Lifecycle wrapper: open → run `fn(send)` → close. On any error, calls `closeWithError` to reject all pending. | All raw-CDP entry points (screenshot, evaluate, terminate). | Three event handlers race on `open`/`error`/`close` to resolve the open promise. The `finally` always tries to close, swallowing errors. |

### File 3 — `cdp-proxy-bypass.ts` (NO_PROXY lease manager)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `getDirectAgentForCdp` (cdp-proxy-bypass.ts:24) | `(url) => http.Agent \| https.Agent \| undefined` | Returns a singleton no-proxy agent for loopback URLs; `undefined` otherwise (caller falls back). | `openCdpWebSocket` (cdp.helpers.ts:312). | Returns `directHttpsAgent` for both `https:` and `wss:`; `directHttpAgent` for `http:`/`ws:`. |
| `hasProxyEnv` (cdp-proxy-bypass.ts:42) | `() => boolean` | Delegates to `hasProxyEnvConfigured()` — true if any of `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/http_proxy/...` are set. | `NoProxyLeaseManager.acquire`, `assertBrowserNavigationAllowed` (navigation-guard.ts:98). | The navigation guard *fails closed* in strict mode if proxy env is set — assumes the browser network stack is unsafe. |
| `noProxyAlreadyCoversLocalhost` (cdp-proxy-bypass.ts:48, internal) | `() => boolean` | True iff `NO_PROXY`/`no_proxy` already lists `localhost,127.0.0.1,[::1]` (substring check). | Lease acquire. | Substring match — order-independent. |
| `withNoProxyForLocalhost<T>` (cdp-proxy-bypass.ts:55) | `(fn) => Promise<T>` | Convenience: scope = `http://127.0.0.1`. | Tests, certain Chrome bootstrap paths. | Calls into `withNoProxyForCdpUrl`. |
| `isLoopbackCdpUrl` (cdp-proxy-bypass.ts:59, internal) | `(url) => boolean` | True for loopback hostnames (`localhost`, `127.x.x.x`, `[::1]`). | Lease acquire. | URL parse failure → false (fail-open at lease layer; no env mutation). |
| `NoProxyLeaseManager.acquire` (cdp-proxy-bypass.ts:77) | `(url) => (() => void) \| null` | Reference-counted env-var mutation. First lease snapshots `NO_PROXY`/`no_proxy`, prepends `localhost,127.0.0.1,[::1]`. Returns a release fn (or `null` to no-op). | `withNoProxyForCdpUrl`. | Snapshot only taken on `leaseCount === 0 && !alreadyCovered`. |
| `NoProxyLeaseManager.release` (cdp-proxy-bypass.ts:103, private) | `() => void` | Decrement; on zero, restore env iff env still equals `applied` (the value we wrote). If external code mutated `NO_PROXY` mid-flight, leave it alone. | The release closure returned by `acquire`. | This is the entire correctness story for concurrent connects — the "untouched" check prevents clobbering. |
| `withNoProxyForCdpUrl<T>` (cdp-proxy-bypass.ts:144) | `(url, fn) => Promise<T>` | `acquire(url)` → `await fn()` → release. Always releases via `finally`. | `connectBrowser` (pw-session.ts:453), `fetchCdpChecked` (cdp.helpers.ts:265). | Idempotent release (`released` flag in returned closure). |

#### Edge case (proxy-bypass test file pw-session.connections.test.ts notes)

- Concurrent connects to the same loopback URL share a single lease (count=2 at peak; one snapshot, one restore).
- A connect that throws still releases. The release closure is in a `finally`.

### File 4 — `cdp-timeouts.ts` (centralized constants + reachability helper)

| Symbol | Value / Signature | Where used |
|---|---|---|
| `CDP_HTTP_REQUEST_TIMEOUT_MS` (cdp-timeouts.ts:1) | 1500 | `fetchJson`/`fetchOk` defaults. |
| `CDP_WS_HANDSHAKE_TIMEOUT_MS` (cdp-timeouts.ts:2) | 5000 | `openCdpWebSocket` default. |
| `CDP_JSON_NEW_TIMEOUT_MS` (cdp-timeouts.ts:3) | 1500 | `/json/new` create-tab calls. |
| `CHROME_REACHABILITY_TIMEOUT_MS` (cdp-timeouts.ts:5) | 500 | Chrome boot probe. |
| `CHROME_WS_READY_TIMEOUT_MS` (cdp-timeouts.ts:6) | 800 | WS readiness wait. |
| `CHROME_BOOTSTRAP_PREFS_TIMEOUT_MS` (cdp-timeouts.ts:7) | 10000 | Prefs setup. |
| `CHROME_BOOTSTRAP_EXIT_TIMEOUT_MS` (cdp-timeouts.ts:8) | 5000 | Bootstrap clean exit. |
| `CHROME_LAUNCH_READY_WINDOW_MS` (cdp-timeouts.ts:9) | 15000 | Launch poll window. |
| `CHROME_LAUNCH_READY_POLL_MS` (cdp-timeouts.ts:10) | 200 | Poll cadence. |
| `CHROME_STOP_TIMEOUT_MS` (cdp-timeouts.ts:11) | 2500 | Graceful stop deadline. |
| `CHROME_STOP_PROBE_TIMEOUT_MS` (cdp-timeouts.ts:12) | 200 | Stop-probe cadence. |
| `CHROME_STDERR_HINT_MAX_CHARS` (cdp-timeouts.ts:13) | 2000 | Diagnostic clamp. |
| `PROFILE_HTTP_REACHABILITY_TIMEOUT_MS` (cdp-timeouts.ts:15) | 300 | Existing-session HTTP. |
| `PROFILE_WS_REACHABILITY_MIN_TIMEOUT_MS` (cdp-timeouts.ts:16) | 200 | Lower clamp. |
| `PROFILE_WS_REACHABILITY_MAX_TIMEOUT_MS` (cdp-timeouts.ts:17) | 2000 | Upper clamp. |
| `PROFILE_ATTACH_RETRY_TIMEOUT_MS` (cdp-timeouts.ts:18) | 1200 | Attach retry. |
| `PROFILE_POST_RESTART_WS_TIMEOUT_MS` (cdp-timeouts.ts:19) | 600 | Post-restart WS. |
| `CHROME_MCP_ATTACH_READY_WINDOW_MS` (cdp-timeouts.ts:20) | 8000 | Chrome-MCP attach window. |
| `CHROME_MCP_ATTACH_READY_POLL_MS` (cdp-timeouts.ts:21) | 200 | Poll cadence. |
| `normalizeTimeoutMs` (cdp-timeouts.ts:23, internal) | `(value) => number \| undefined` | `Math.max(1, Math.floor(value))` w/ NaN guard. | clamp helper. |
| `resolveCdpReachabilityTimeouts` (cdp-timeouts.ts:30) | `({profileIsLoopback, timeoutMs?, remoteHttpTimeoutMs, remoteHandshakeTimeoutMs}) => {httpTimeoutMs, wsTimeoutMs}` | Loopback path: HTTP = caller override or 300; WS = clamp(2*HTTP, 200..2000). Remote path: caller override is a *floor*, never lower than the remote constants; WS = `2 × HTTP` floored at remote handshake. | reachability probes. |

Loopback gets shorter timeouts because we know it can't be slow; remote gets longer because we may be racing with a corporate VPN.

### File 5 — `pw-session.ts` (the heart of session lifecycle)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `normalizeCdpUrl` (pw-session.ts:128, internal) | `(raw) => string` | Strip trailing `/`. | All cache lookups. | `targetKey` builds on this. |
| `findNetworkRequestById` (pw-session.ts:132, internal) | `(state, id) => entry?` | Reverse-linear search of last 500 requests. | `response`/`requestfailed` listeners. | O(n) but bounded by `MAX_NETWORK_REQUESTS`. |
| `targetKey` (pw-session.ts:142) | `(cdpUrl, targetId) => string` | `${normalized}::${targetId}`. | All blocked-target maps. | The `::` delimiter is load-bearing for prefix scans. |
| `roleRefsKey` (pw-session.ts:146) | `(cdpUrl, targetId) => string` | Same as `targetKey`. | `roleRefsByTarget` ops. | Aliased intentionally. |
| `isBlockedTarget` (pw-session.ts:150) | `(cdpUrl, targetId?) => boolean` | Look up `blockedTargetsByCdpUrl`. | Pre-resolution gate everywhere. | Empty/missing targetId → false. |
| `markTargetBlocked` (pw-session.ts:158) | `(cdpUrl, targetId?) => void` | Add to set. | `closeBlockedNavigationTarget`. | No-op for missing id (silently). |
| `clearBlockedTarget` (pw-session.ts:166) | `(cdpUrl, targetId?) => void` | Remove from set. | `createPageViaPlaywright`. | When a *new* page reuses an old targetId — unblock it. |
| `clearBlockedTargetsForCdpUrl` (pw-session.ts:174) | `(cdpUrl?) => void` | Prefix-scan delete (or full clear if no arg). | `closePlaywrightBrowserConnection`. | Iterates `blockedTargetsByCdpUrl` while mutating — safe because `Set` iterator is "live" in V8. |
| `blockedPageRefsForCdpUrl` (pw-session.ts:187) | `(cdpUrl) => WeakSet<Page>` | Lazy-create the per-URL `WeakSet`. | `markPageRefBlocked`, `isBlockedPageRef`. | `WeakSet` so dropped Pages get GC'd. |
| `isBlockedPageRef` (pw-session.ts:198) | `(cdpUrl, page) => boolean` | Membership check. | `partitionAccessiblePages`, `getPageForTargetId`. | `?.has(page) ?? false`. |
| `markPageRefBlocked` (pw-session.ts:202) | `(cdpUrl, page) => void` | Add to per-URL `WeakSet`. | `closeBlockedNavigationTarget`. | Page-ref quarantine survives even if `pageTargetId` fails. |
| `clearBlockedPageRefsForCdpUrl` (pw-session.ts:206) | `(cdpUrl?) => void` | Drop the entire `WeakSet` for the URL (or all). | `closePlaywrightBrowserConnection`. | `delete` on the outer `Map` collapses the inner WeakSet. |
| `clearBlockedPageRef` (pw-session.ts:214) | `(cdpUrl, page) => void` | Remove a single page from the WeakSet. | `createPageViaPlaywright` (after recreate). | No-op if WeakSet absent. |
| `hasBlockedTargetsForCdpUrl` (pw-session.ts:218) | `(cdpUrl) => boolean` | Prefix-scan to detect any blocked target for the URL. | `partitionAccessiblePages` — fail-closed when we can't resolve a `targetId` *and* this URL has any blocked target. | The fail-closed gate is the SSRF perimeter's last line. |
| `BlockedBrowserTargetError` (pw-session.ts:228) | class | "Browser target is unavailable after SSRF policy blocked its navigation." | Throws from `getPageForTargetId`, `resolvePageByTargetIdOrThrow`. | Caller maps to a specific error code in the route layer. |
| `rememberRoleRefsForTarget` (pw-session.ts:235) | `({cdpUrl, targetId, refs, frameSelector?, mode?}) => void` | Insert into `roleRefsByTarget` (Map). LRU evict when size > 50. | `storeRoleRefsForTarget`. | LRU is FIFO — relies on Map insertion-order. Re-insertion does NOT bump order; this is "first-in-first-out" not strict LRU. (Possible bug: if a target's refs are written then re-read but never re-written, they age out.) |
| `storeRoleRefsForTarget` (pw-session.ts:260) | `({page, cdpUrl, targetId?, refs, frameSelector?, mode}) => void` | Sets `pageStates[page].roleRefs` AND populates the by-target cache. | Snapshot tools after every snapshot. | Both copies kept in sync. |
| `restoreRoleRefsForTarget` (pw-session.ts:285) | `({cdpUrl, targetId?, page}) => void` | Pull from `roleRefsByTarget` and seed `pageStates[page].roleRefs` if empty. | Pre-action steps that need refs across a Page swap. | No-op if state already has refs — preserves any in-flight writes. |
| `ensurePageState` (pw-session.ts:307) | `(page) => PageState` | Create-if-absent. Wires `console`/`pageerror`/`request`/`response`/`requestfailed`/`close` listeners exactly once (`observedPages` WeakSet). | Everywhere a page is touched. | Ring buffers: console=500, errors=200, requests=500. `requestIds` is a WeakMap keyed by `Request`. |
| `observeContext` (pw-session.ts:399) | `(context) => void` | Idempotent context observer; ensures every existing + future page gets `ensurePageState`. | `observeBrowser`. | `WeakSet` to dedupe. |
| `ensureContextState` (pw-session.ts:412) | `(context) => ContextState` | Allocates `{traceActive: false}`. | `observeContext`, `createPageViaPlaywright`. | Single flag for now — extensible. |
| `observeBrowser` (pw-session.ts:422) | `(browser) => void` | For each context, observe. | `connectBrowser` after first connect. | Doesn't watch for new contexts (`browser.on('context')` is not used) — Chrome rarely creates >1 BrowserContext via CDP attach. |
| `connectBrowser` (pw-session.ts:428) | `(cdpUrl, ssrfPolicy?) => Promise<ConnectedBrowser>` | The dedupe + retry + bypass-scope entry point. **See "Algorithms" below.** | Every entry point that needs a Browser. | Cache check is first — `assertCdpEndpointAllowed` skipped on cache hit so transient DNS failures don't break a live session. |
| `getAllPages` (pw-session.ts:493, internal) | `(browser) => Promise<Page[]>` | `flatMap(c => c.pages())` across all contexts. | Multiple. | Synchronous but kept async for symmetry. |
| `partitionAccessiblePages` (pw-session.ts:499) | `({cdpUrl, pages}) => Promise<{accessible, blockedCount}>` | Filters pages by ref-block + targetId-block. **Fail-closed if `pageTargetId` fails AND there are any blocked targets for this URL.** | `getPageForTargetId`. | Otherwise (no blocked targets known), accept the page. |
| `pageTargetId` (pw-session.ts:530) | `(page) => Promise<string \| null>` | `page.context().newCDPSession(page)` → `Target.getTargetInfo` → detach. | Everywhere we need ground truth. | Returns null on empty/missing. `detach` is best-effort. |
| `matchPageByTargetList` (pw-session.ts:541) | `(pages, targets, targetId) => Page \| null` | Given Playwright pages + CDP `/json/list` targets, match by URL. If multiple pages share a URL, use ordinal position among same-URL targets. | `findPageByTargetIdViaTargetList`. | Heuristic: assumes Playwright order matches CDP enumeration order for same-URL tabs. |
| `findPageByTargetIdViaTargetList` (pw-session.ts:567) | `(pages, targetId, cdpUrl, ssrfPolicy?) => Promise<Page \| null>` | HTTP fallback: hit `/json/list`, match by URL. | `findPageByTargetId`. | Uses `normalizeCdpHttpBaseForJsonEndpoints` so direct-WS URLs work. Timeout 2000ms (overrides default 1500). |
| `findPageByTargetId` (pw-session.ts:585) | `(browser, targetId, cdpUrl?, ssrfPolicy?) => Promise<Page \| null>` | Probe each Page via CDP; if all CDP probes fail and there's only ONE page total → return it as a "best-effort". Otherwise fall through to HTTP `/json/list`. | `getPageForTargetId`, `resolvePageByTargetIdOrThrow`. | The single-page fallback is the "extension" case (Manifest V3 inspector etc. where attach is denied). |
| `resolvePageByTargetIdOrThrow` (pw-session.ts:618) | `({cdpUrl, targetId, ssrfPolicy?}) => Promise<Page>` | Block check → connect → find → throw `BrowserTabNotFoundError`. | `closePageByTargetIdViaPlaywright`, `focusPageByTargetIdViaPlaywright`. | First-line block check before paying connect cost. |
| `getPageForTargetId` (pw-session.ts:634) | `({cdpUrl, targetId?, ssrfPolicy?}) => Promise<Page>` | Public API: connect, partition, return first accessible (or matched targetId). Exhaustive block-check on the matched page. | Most route handlers. | If `targetId` provided but not found, single-page fallback applies (`pages.length === 1`). |
| `isTopLevelNavigationRequest` (pw-session.ts:680) | `(page, request) => boolean` | True iff request frame === main frame AND (`isNavigationRequest()` OR `resourceType()==='document'`). | `gotoPageWithNavigationGuard` route handler. | `frame()` resolution failure → `sameMainFrame = true` (fail closed: treat as top-level so guard fires). |
| `isSubframeDocumentNavigationRequest` (pw-session.ts:707) | `(page, request) => boolean` | Same shape but for non-main frames. | route handler. | If `frame()` throws here it returns true — fail closed. |
| `isPolicyDenyNavigationError` (pw-session.ts:737) | `(err) => boolean` | `instanceof SsrFBlockedError \|\| InvalidBrowserNavigationUrlError`. | nav guard error classifier. | Anything else re-throws. |
| `closeBlockedNavigationTarget` (pw-session.ts:741) | `({cdpUrl, page, targetId?}) => Promise<void>` | Marks page-ref blocked, marks targetId blocked (resolved via CDP if possible, else fallback), closes page best-effort. | `gotoPageWithNavigationGuard`, `assertPageNavigationCompletedSafely`. | Order matters — page-ref first so even if `pageTargetId` fails we still quarantine. |
| `assertPageNavigationCompletedSafely` (pw-session.ts:757) | `({cdpUrl, page, response, ssrfPolicy?, targetId?}) => Promise<void>` | Post-nav: redirect chain check + final URL check. On policy deny, close target. | After every `goto`. | Catches policy errors, closes, then rethrows. Non-policy errors still rethrow without closing. |
| `gotoPageWithNavigationGuard` (pw-session.ts:786) | `({cdpUrl, page, url, timeoutMs, ssrfPolicy?, targetId?}) => Promise<Response \| null>` | Installs `page.route("**", handler)`, calls `page.goto`, in `finally` unroutes and (if blocked) closes target. **See "Algorithms" below.** | All navigation routes. | `blockedError` closure variable carries the deny across the goto/abort race. |
| `refLocator` (pw-session.ts:851) | `(page, ref) => Locator` | Resolves an `e1`/`@e1`/`ref=e1` ref to a Playwright `Locator`. Mode "aria" uses `aria-ref=e1`; mode "role" uses `getByRole(role, {name, exact})` plus optional `nth()`. | Tool calls that take `ref`. | Throws on unknown ref with hint to re-snapshot. Frame scope optional. |
| `closePlaywrightBrowserConnection` (pw-session.ts:890) | `({cdpUrl?}) => Promise<void>` | Targeted disconnect (clears blocked sets, removes listener, awaits close best-effort) OR full disconnect (everything). | shutdown / explicit reset. | Removes the `disconnected` listener BEFORE close so a fresh connect doesn't get nulled by the old browser's death rattle. |
| `cdpSocketNeedsAttach` (pw-session.ts:922, internal) | `(wsUrl) => boolean` | True for `/cdp` browser-level sockets that need explicit `Target.attachToTarget`. | `tryTerminateExecutionViaCdp`. | Branch covers `/devtools/browser/<id>`. |
| `tryTerminateExecutionViaCdp` (pw-session.ts:933) | `({cdpUrl, targetId, ssrfPolicy?}) => Promise<void>` | Hit `/json/list` to find the target's `webSocketDebuggerUrl`, normalize, optionally `Target.attachToTarget {flatten:true}`, send `Runtime.terminateExecution` (1500ms timeout each), best-effort `Target.detachFromTarget`. | `forceDisconnectPlaywrightForTarget`. | Per-step deadline via `runWithTimeout`. All errors swallowed — pure best-effort. |
| `forceDisconnectPlaywrightForTarget` (pw-session.ts:1023) | `({cdpUrl, targetId?, reason?, ssrfPolicy?}) => Promise<void>` | The "stuck JS" recovery path. **See "Algorithms" below.** | Watchdog timers, route-level forcekills. | `browser.close()` is fire-and-forget. |
| `listPagesViaPlaywright` (pw-session.ts:1063) | `({cdpUrl, ssrfPolicy?}) => Promise<Array<{targetId,title,url,type}>>` | Lists tabs via the persistent Playwright connection. Skips blocked refs/targets. | Remote-profile tab-list route. | Fallback for ephemeral `/json/list`. |
| `createPageViaPlaywright` (pw-session.ts:1105) | `({cdpUrl, url, ssrfPolicy?}) => Promise<{targetId,title,url,type}>` | `context.newPage()` → `clearBlockedPageRef` (in case targetId reuse) → optional guarded `goto` + post-nav assert. | Remote-profile new-tab route. | If goto throws a non-policy error, swallow it (page already created); for policy errors rethrow. Then `assertPageNavigationCompletedSafely` runs anyway. |
| `closePageByTargetIdViaPlaywright` (pw-session.ts:1175) | `({cdpUrl, targetId, ssrfPolicy?}) => Promise<void>` | Resolve + `page.close()`. | Tab-close route. | `resolvePageByTargetIdOrThrow` performs the block-check. |
| `focusPageByTargetIdViaPlaywright` (pw-session.ts:1188) | `({cdpUrl, targetId, ssrfPolicy?}) => Promise<void>` | `page.bringToFront()` with raw-CDP `Page.bringToFront` fallback via page-scoped session. | Tab-focus route. | Re-throws original Playwright error if both paths fail. |

### File 6 — `pw-session.page-cdp.ts` (page-scoped CDP helper)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `withPlaywrightPageCdpSession<T>` (pw-session.page-cdp.ts:5, internal) | `(page, fn) => Promise<T>` | `page.context().newCDPSession(page)` → run → detach (best-effort). | `withPageScopedCdpClient`. | Detach swallow's errors. |
| `withPageScopedCdpClient<T>` (pw-session.page-cdp.ts:17) | `({cdpUrl, page, targetId?, fn}) => Promise<T>` | Public wrapper that exposes a `(method, params) => Promise<unknown>` send fn over a Playwright page-scoped session. | `focusPageByTargetIdViaPlaywright` raw-CDP fallback; other "I need a page-scoped CDP without lifecycle hassle" callers. | The `cdpUrl`/`targetId` args are kept on the signature for symmetry with raw-CDP wrappers, but only `page` is actually used. Coercion of `session.send` to a positional `(method, params)` shape — a Playwright-internal contract. |

### File 7 — `target-id.ts` (target-id input resolution)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `resolveTargetIdFromTabs` (target-id.ts:7) | `(input, tabs) => TargetIdResolution` | Tries exact match, then case-insensitive prefix. Single prefix match → ok; multiple → ambiguous; zero → not_found. | CLI/route layer that accepts user-typed (possibly-truncated) target ids. | Lowercase normalization via `normalizeLowercaseStringOrEmpty`. Whitespace trimmed. |

### File 8 — `navigation-guard.ts` (the SSRF perimeter for navigation)

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `isAllowedNonNetworkNavigationUrl` (navigation-guard.ts:18, internal) | `(parsed) => boolean` | True iff `about:blank`. | `assertBrowserNavigationAllowed`. | Hard whitelist — no other non-network URL is allowed. |
| `InvalidBrowserNavigationUrlError` (navigation-guard.ts:23) | class | Distinct from `SsrFBlockedError`; marks a guard-time policy violation. | guard throws. | Both constitute a "policy deny" via `isPolicyDenyNavigationError`. |
| `withBrowserNavigationPolicy` (navigation-guard.ts:39) | `(ssrfPolicy?) => {ssrfPolicy?}` | Spread-ready options. | Every guard caller. | Returns `{}` if no policy. |
| `requiresInspectableBrowserNavigationRedirects` (navigation-guard.ts:45) | `(ssrfPolicy?) => boolean` | True in strict mode. | Tools that want to know whether to wait for redirect chains. | Inverse of `isPrivateNetworkAllowedByPolicy`. |
| `isIpLiteralHostname` (navigation-guard.ts:49, internal) | `(hostname) => boolean` | Wraps Node `isIP`. | strict-mode hostname gate. | `0` is "not an IP". |
| `isExplicitlyAllowedBrowserHostname` (navigation-guard.ts:53, internal) | `(hostname, ssrfPolicy?) => boolean` | Checks `allowedHostnames` (exact) and `hostnameAllowlist` (pattern). | strict-mode hostname gate. | Empty allowlist → false. |
| `assertBrowserNavigationAllowed` (navigation-guard.ts:67) | `({url, lookupFn?, ssrfPolicy?}) => Promise<void>` | Multi-stage gate: parse → protocol whitelist → about:blank exception → strict-mode proxy-env fail-closed → strict-mode hostname-must-be-IP-literal-or-allowlist → DNS pin via `resolvePinnedHostnameWithPolicy`. | Pre-nav route handler, `createTargetViaCdp`, `createPageViaPlaywright`. | The "proxy env set + strict policy" combination throws unconditionally — Chromium DNS bypasses Node-level pinning. |
| `assertBrowserNavigationResultAllowed` (navigation-guard.ts:131) | `({url, ...}) => Promise<void>` | Re-runs `assertBrowserNavigationAllowed` on the post-redirect URL. Skips non-network URLs except about:blank. | `assertPageNavigationCompletedSafely`. | Designed not to fail on `chrome-error://` interstitials. |
| `assertBrowserNavigationRedirectChainAllowed` (navigation-guard.ts:155) | `({request?, ...}) => Promise<void>` | Walks `redirectedFrom()` chain, validates each URL. | post-nav. | Iterates reversed (start → end) so first failure is the earliest hop. |

## Algorithms in detail

### A1. `connectBrowser()` — full flow (pw-session.ts:428)

1. **Normalize URL** — strip trailing `/` (pw-session.ts:429).
2. **Cache hit fast path** — `cachedByCdpUrl.get(normalized)`. If present, return immediately. SSRF check is **skipped** here so transient DNS failures don't kill an already-live session (pw-session.ts:430-433, comment line 434-435).
3. **SSRF gate** — `await assertCdpEndpointAllowed(normalized, ssrfPolicy)` (pw-session.ts:436). Throws `BrowserCdpEndpointBlockedError` on policy deny.
4. **In-flight dedup** — `connectingByCdpUrl.get(normalized)`. If present, await it (pw-session.ts:437-440). This is the thundering-herd guard; tested in `pw-session.connections.test.ts:52` to confirm dedupe is per-URL not global.
5. **Define `connectWithRetry`** — closure with `let lastErr` (pw-session.ts:442).
6. **Retry loop** — `for (let attempt = 0; attempt < 3; attempt++)`:
   - Per-attempt timeout = `5000 + attempt * 2000` → 5000, 7000, 9000 ms (pw-session.ts:446).
   - Call `getChromeWebSocketUrl(normalized, timeout, ssrfPolicy).catch(() => null)` (pw-session.ts:447) — discovers the WS URL, gracefully returns `null` if discovery fails so the HTTP URL itself is used as the endpoint.
   - `endpoint = wsUrl ?? normalized` (pw-session.ts:450).
   - `headers = getHeadersWithAuth(endpoint)` (pw-session.ts:451).
   - **Proxy-bypass scope** — `await withNoProxyForCdpUrl(endpoint, () => chromium.connectOverCDP(endpoint, {timeout, headers}))` (pw-session.ts:453-455). The lease wraps only the connect call; once the WS is established, system proxies don't matter.
   - On success:
     - Build `onDisconnected` closure that drops *only* the cache entry whose `.browser` matches the current one (avoids racing with a fresh connection) (pw-session.ts:456-461).
     - Cache + register listener: `cachedByCdpUrl.set`, `browser.on("disconnected", onDisconnected)` (pw-session.ts:463-464).
     - `observeBrowser(browser)` — wires per-page event listeners (pw-session.ts:465).
     - Return.
   - On error:
     - Capture `lastErr`.
     - If `formatErrorMessage(err).includes("rate limit")` → break (pw-session.ts:471-472). Don't retry rate-limit errors; they get worse.
     - Backoff: `delay = 250 + attempt * 250` → 250, 500, 750 ms (pw-session.ts:474). Note this is *additive*, not exponential — total wall clock for 3 failed attempts ≈ 250+500 = 750ms of waiting (third delay never executes, the loop exits).
7. **Throw last error** — if `lastErr instanceof Error` rethrow, else wrap with `formatErrorMessage` (pw-session.ts:478-482).
8. **Register the in-flight promise** — `connectWithRetry().finally(() => connectingByCdpUrl.delete(normalized))` (pw-session.ts:485-487). The `.finally` ensures cleanup even on error so the next call retries from scratch.
9. `connectingByCdpUrl.set(normalized, pending)` then `await pending`.

**Key invariants**:
- The proxy-bypass scope entry/exit is *per attempt* (lease ref-count handles overlap).
- A retried connect re-enters the lease but the `NoProxyLeaseManager` snapshot is taken only on `count === 0`.
- The cache is populated *during* the retry loop on success; if a concurrent caller awaits the in-flight promise, it sees the same `connected` object.

### A2. `pageTargetId()` and the `/json/list` HTTP fallback (pw-session.ts:530)

**CDP path (preferred)**:
1. `session = await page.context().newCDPSession(page)` — opens a Playwright-managed CDP session scoped to this page.
2. `info = await session.send("Target.getTargetInfo")` — type `{targetInfo?: {targetId?: string}}`.
3. `targetId = normalizeOptionalString(info?.targetInfo?.targetId) ?? ""`.
4. `finally { await session.detach().catch(() => {}); }`.
5. Return `targetId || null`.

This can fail when:
- The page is an extension background context (Manifest V3) where `Target.attachToBrowserTarget` returns "Not allowed" — see `pw-session.get-page-for-targetid.extension-fallback.test.ts:101` mocking `newCDPSessionError: "Target.attachToBrowserTarget: Not allowed"`.
- The Page is dead / already closed.
- The browser is mid-disconnect.

**HTTP fallback** (`findPageByTargetIdViaTargetList`, pw-session.ts:567):
1. `cdpHttpBase = normalizeCdpHttpBaseForJsonEndpoints(cdpUrl)` — coerces `ws/wss` → `http/https`, strips `/devtools/browser/<id>` and `/cdp`. Confirmed by test (pw-session.get-page-for-targetid.extension-fallback.test.ts:88-91) which asserts the request goes to `http://127.0.0.1:18792/json/list?token=abc` even when the input was `ws://127.0.0.1:18792/devtools/browser/SESSION?token=abc`.
2. `await assertCdpEndpointAllowed(cdpUrl, ssrfPolicy)`.
3. `targets = await fetchJson<Array<{id, url, title?}>>(appendCdpPath(cdpHttpBase, "/json/list"), 2000)` — overrides default 1500ms timeout.
4. `matchPageByTargetList`: find target by id; among Pages whose `page.url() === target.url`, return either the unique match or — if multiple Playwright Pages share the URL AND there are an equal count of same-URL CDP targets — match by ordinal position.

**Single-page extension fallback** (`findPageByTargetId`, pw-session.ts:585): if no CDP probe ever succeeded *and* `pages.length === 1`, return that single page (pw-session.ts:612-614). Documented in `pw-session.get-page-for-targetid.extension-fallback.test.ts:55`.

### A3. The route-handler navigation guard (pw-session.ts:786 `gotoPageWithNavigationGuard`)

Installation:
- `await page.route("**", handler)` (pw-session.ts:827). Glob `**` matches every URL including subresources.
- The handler closure captures `blockedError: unknown = null` (pw-session.ts:795).

Order of checks per request:
1. **Short-circuit if already blocked** — if `blockedError` is non-null, abort immediately (pw-session.ts:798-801).
2. **Classify request**:
   - `isTopLevel = isTopLevelNavigationRequest(page, request)`.
   - `isSubframeDocument = !isTopLevel && isSubframeDocumentNavigationRequest(page, request)`.
   - If neither → `route.continue()` (subresources pass through unguarded) (pw-session.ts:805-808).
3. **Run guard** — `await assertBrowserNavigationAllowed({url, ...navigationPolicy})` (pw-session.ts:810-813).
4. **On policy deny**:
   - If top-level: latch `blockedError = err` (pw-session.ts:817).
   - Always: `await route.abort().catch(() => {})` then return (pw-session.ts:819-820).
5. **On non-policy throw** — rethrow (pw-session.ts:822). This means a guard *bug* propagates instead of being silently allowed.
6. **On allow** — `await route.continue()` (pw-session.ts:824).

Fail-closed conditions:
- `request.frame()` failure during top-level check → assume same-main-frame (pw-session.ts:683-687) so the guard *runs*.
- `request.frame()` failure during subframe check → return `true` (pw-session.ts:716) so the guard *runs*.
- `assertBrowserNavigationAllowed` requires DNS pinning except for `about:blank` and IP-literal hosts under strict policy.

After `goto`:
- If `blockedError` set, throw it (pw-session.ts:830-832, 835-837).
- `finally`: `unroute("**", handler).catch(() => {})` and if blocked, `closeBlockedNavigationTarget` (pw-session.ts:840-847). The page is killed and the targetId quarantined so the agent can't re-select it.

Target marking (`closeBlockedNavigationTarget`, pw-session.ts:741):
1. `markPageRefBlocked(cdpUrl, page)` — by Page object identity (WeakSet).
2. `pageTargetId(page)` (best-effort; falls back to `opts.targetId` if available).
3. `markTargetBlocked(cdpUrl, targetIdToBlock)` — by string key.
4. `await page.close().catch(() => {})`.

### A4. Stale-Page recovery via `roleRefsByTarget` (pw-session.ts:116, 235, 285)

**Problem**: Playwright may return a different `Page` object for the same Chrome target after a reconnect or context churn. The page ref `e1` would be lost.

**Solution**: keep two parallel stores —
- `pageStates: WeakMap<Page, PageState>` — per-Page object, dies with the Page.
- `roleRefsByTarget: Map<targetKey, RoleRefsCacheEntry>` — keyed by `${cdpUrl}::${targetId}`, survives Page swaps.

**Write path** (`storeRoleRefsForTarget`, pw-session.ts:260):
1. `state = ensurePageState(page); state.roleRefs = refs; state.roleRefsFrameSelector = frameSelector; state.roleRefsMode = mode;`.
2. If `targetId`: `rememberRoleRefsForTarget` writes to the shared map and FIFO-evicts when size > 50 (pw-session.ts:251-257).

**LRU detail**: it's actually FIFO-by-insertion — `roleRefsByTarget.keys().next()` returns the oldest entry. Re-writing the same key bumps it (Map preserves insertion order on `set`-after-`delete`-or-fresh). But re-*reading* doesn't bump. This means a long-stable target can age out if 50 other targets get touched. (Acceptable for single-user workflows; flag for porting if you want strict LRU.)

**Read path** (`restoreRoleRefsForTarget`, pw-session.ts:285):
1. Look up by `(cdpUrl, targetId)`.
2. If `state.roleRefs` already populated — no-op (don't clobber in-flight writes).
3. Else seed `state.roleRefs/Mode/FrameSelector` from the cache.

### A5. Force-disconnect path (pw-session.ts:1023 `forceDisconnectPlaywrightForTarget`)

This is the "JS is stuck" recovery. Used when an action like `page.evaluate` blocks indefinitely.

Sequence:
1. `cur = cachedByCdpUrl.get(normalized)`. If absent → return (pw-session.ts:1031-1033).
2. `cachedByCdpUrl.delete(normalized)` (pw-session.ts:1034).
3. `connectingByCdpUrl.delete(normalized)` (pw-session.ts:1037) — so the next call doesn't await a stale promise.
4. `cur.browser.off("disconnected", cur.onDisconnected)` if available (pw-session.ts:1040-1042) — prevent the old browser's death from racing with the next fresh connect.
5. **Best-effort terminate** — `tryTerminateExecutionViaCdp` if a `targetId` is known (pw-session.ts:1046-1053):
   - Hit `/json/list` to find `webSocketDebuggerUrl`.
   - Normalize via `normalizeCdpWsUrl`.
   - If the URL needs explicit attach (`/cdp` or `/devtools/browser/`): `Target.attachToTarget {targetId, flatten: true}` with 1500ms timeout (pw-session.ts:982-987).
   - `Runtime.terminateExecution` with the optional `sessionId` and 1500ms timeout (pw-session.ts:990).
   - `Target.detachFromTarget` fire-and-forget cleanup (pw-session.ts:993).
6. **Fire-and-forget close** — `cur.browser.close().catch(() => {})` *not* awaited (pw-session.ts:1056). Comment block (pw-session.ts:1003-1022) explains the rationale: Playwright shares a single Connection across Browser/BrowserType, so `Connection.close()` is forbidden — closing it corrupts the entire Playwright instance. Instead we drop our reference and let the next `connectBrowser()` make a fresh WS.

**No `Page.close` or `Target.closeTarget` is sent here.** The intent is to unblock execution, not destroy the tab. `closeBlockedNavigationTarget` (pw-session.ts:741) is the path that calls `page.close()` — that one is for SSRF policy denies, not stuck JS.

`Browser.close` (the CDP method) is also never used directly — `browser.close()` (Playwright) handles disconnect.

## Data structures

### `cachedByCdpUrl` (pw-session.ts:123)

```ts
const cachedByCdpUrl = new Map<string, ConnectedBrowser>();
type ConnectedBrowser = { browser: Browser; cdpUrl: string; onDisconnected?: () => void };
```

Python:
```python
@dataclass
class ConnectedBrowser:
    browser: "playwright.async_api.Browser"
    cdp_url: str
    on_disconnected: Optional[Callable[[], None]] = None

cached_by_cdp_url: dict[str, ConnectedBrowser] = {}
```

### `connectingByCdpUrl` (pw-session.ts:124)

```ts
const connectingByCdpUrl = new Map<string, Promise<ConnectedBrowser>>();
```

Python (`asyncio.Future` is the natural equivalent so that multiple awaiters share one underlying connect):
```python
connecting_by_cdp_url: dict[str, asyncio.Future[ConnectedBrowser]] = {}
```

Set on entry, cleared in a `finally` (analogous to TS `.finally` on the pending promise).

### `blockedTargetsByCdpUrl` (pw-session.ts:125)

```ts
const blockedTargetsByCdpUrl = new Set<string>();  // keys: `${normalizedCdpUrl}::${targetId}`
```

Python:
```python
blocked_targets_by_cdp_url: set[str] = set()  # same composite keys
```

Prefix scans (`hasBlockedTargetsForCdpUrl`, `clearBlockedTargetsForCdpUrl`) iterate while mutating; in Python, snapshot first: `for k in list(s): if k.startswith(prefix): s.discard(k)`.

### `blockedPageRefsByCdpUrl` (pw-session.ts:126)

```ts
const blockedPageRefsByCdpUrl = new Map<string, WeakSet<Page>>();
```

Python doesn't have `WeakSet<object>` for arbitrary objects only via `weakref.WeakSet`, which works on hashable weakly-referenceable objects. Playwright `Page` objects are weakly referenceable in CPython.
```python
import weakref
blocked_page_refs_by_cdp_url: dict[str, "weakref.WeakSet[Page]"] = {}
```

### `roleRefsByTarget` (pw-session.ts:116) + `MAX_ROLE_REFS_CACHE` (pw-session.ts:117)

```ts
const roleRefsByTarget = new Map<string, RoleRefsCacheEntry>();
type RoleRefsCacheEntry = { refs: RoleRefs; frameSelector?: string; mode?: "role" | "aria" };
type RoleRefs = Record<string, { role: string; name?: string; nth?: number }>;
const MAX_ROLE_REFS_CACHE = 50;
```

Python (use `OrderedDict` for explicit FIFO/LRU control):
```python
from collections import OrderedDict

@dataclass
class RoleRef:
    role: str
    name: Optional[str] = None
    nth: Optional[int] = None

@dataclass
class RoleRefsCacheEntry:
    refs: dict[str, RoleRef]
    frame_selector: Optional[str] = None
    mode: Optional[Literal["role", "aria"]] = None

role_refs_by_target: "OrderedDict[str, RoleRefsCacheEntry]" = OrderedDict()
MAX_ROLE_REFS_CACHE = 50
```

If you want true LRU instead of TS's accidental-FIFO, call `od.move_to_end(key)` on read.

### `PageState` (pw-session.ts:79)

```ts
type PageState = {
  console: BrowserConsoleMessage[];   // ring 500
  errors: BrowserPageError[];          // ring 200
  requests: BrowserNetworkRequest[];   // ring 500
  requestIds: WeakMap<Request, string>;
  nextRequestId: number;
  armIdUpload: number;
  armIdDialog: number;
  armIdDownload: number;
  roleRefs?: RoleRefs;
  roleRefsMode?: "role" | "aria";
  roleRefsFrameSelector?: string;
};
```

Python:
```python
@dataclass
class PageState:
    console: deque[BrowserConsoleMessage] = field(default_factory=lambda: deque(maxlen=500))
    errors: deque[BrowserPageError] = field(default_factory=lambda: deque(maxlen=200))
    requests: deque[BrowserNetworkRequest] = field(default_factory=lambda: deque(maxlen=500))
    request_ids: "weakref.WeakKeyDictionary[Request, str]" = field(default_factory=weakref.WeakKeyDictionary)
    next_request_id: int = 0
    arm_id_upload: int = 0
    arm_id_dialog: int = 0
    arm_id_download: int = 0
    role_refs: Optional[dict[str, RoleRef]] = None
    role_refs_mode: Optional[Literal["role", "aria"]] = None
    role_refs_frame_selector: Optional[str] = None
```

Use `deque(maxlen=N)` instead of manual `if (.length > MAX) .shift()`.

## Edge cases

### EC1. Concurrent connect to same CDP URL
Two callers race into `connectBrowser(url)`. The second hits `connectingByCdpUrl.get(normalized)` (pw-session.ts:437-440) and awaits the same promise. Both get the same `ConnectedBrowser`. Tested in `pw-session.connections.test.ts:52` for *different* URLs (parallel, no dedup); for same URL the test passes implicitly through normal cache hit.

Port note: `asyncio.Future` semantics line up; just be careful that the future is cleared in `finally` (analogous to `.finally(() => connectingByCdpUrl.delete(...))` at pw-session.ts:485-487) so a failed connect doesn't poison subsequent attempts.

### EC2. Connect during a teardown
`closePlaywrightBrowserConnection({cdpUrl})` (pw-session.ts:890) clears the cache, removes the disconnected listener, and awaits close. If a `connectBrowser` is in-flight at the time, the in-flight promise was `.set` *after* `cachedByCdpUrl.set` so a concurrent close can drop both. The `.finally` deletion (pw-session.ts:486) handles the in-flight key. Race is benign — the next call gets a fresh connect.

### EC3. WebSocket disconnect mid-call
- Playwright's `browser.on("disconnected", onDisconnected)` (pw-session.ts:464) fires; the `onDisconnected` closure (pw-session.ts:456-461) only deletes if `cachedByCdpUrl.get(normalized)?.browser === browser` — so a fresh connection isn't accidentally evicted by an old browser's death.
- For raw CDP sockets (`withCdpSocket`), `createCdpSender`'s `ws.on("close", ...)` rejects all pending with `Error("CDP socket closed")` (cdp.helpers.ts:224-226). Callers see a clean error.

### EC4. Page that crashes (`page.on("crash")`)
**Notable absence**: `ensurePageState` (pw-session.ts:307) wires `console`/`pageerror`/`request`/`response`/`requestfailed`/`close` but NOT `crash`. A crashed Page leaves stale entries in `pageStates`/`observedPages` until `close` fires (which it eventually does for crashed renderers). Port note: consider adding `crash` for proactive cleanup.

### EC5. Target that's blocked → re-selected
`partitionAccessiblePages` (pw-session.ts:499) filters Pages by ref-block AND target-block. The fail-closed branch at pw-session.ts:514 — when `pageTargetId` returns null AND `hasBlockedTargetsForCdpUrl(cdpUrl)` is true — counts the page as blocked, ensuring an agent can't sneak past quarantine on a transient CDP probe failure.

`createPageViaPlaywright` (pw-session.ts:1105) explicitly clears blocks on the new Page (pw-session.ts:1121) and on the new targetId (pw-session.ts:1123) — chrome may reuse target ids after recreate, so the unblock is necessary.

### EC6. Subframe navigation that's blocked
`isSubframeDocumentNavigationRequest` (pw-session.ts:707) ensures sub-frame document loads run through the same gate. Critical: when frame resolution throws (renderer churn), the function returns `true` (pw-session.ts:716) — fail-closed.

A blocked subframe nav calls `route.abort()` but does NOT latch `blockedError` (only top-level does, at pw-session.ts:817). The page is *not* closed for a subframe block — the top-level navigation may still be valid; only the sub-load is denied.

### EC7. Connect timeout vs Chrome readiness
`getChromeWebSocketUrl(normalized, timeout, ssrfPolicy).catch(() => null)` (pw-session.ts:447) — discovery failure is gracefully tolerated; we just connect to the HTTP base and let `connectOverCDP` figure it out. Some Chrome builds reject the HTTP URL when only the WS endpoint is supported — that's what triggers the retry.

### EC8. Direct-WS CDP URL with auth
`normalizeCdpWsUrl` (cdp.ts:20) carries username/password from CDP URL onto WS URL when WS lacks them (cdp.ts:38-41). `getHeadersWithAuth` (cdp.helpers.ts:115) builds `Basic` from URL credentials if no `Authorization` header was passed by the caller. If both URL credentials and an explicit `Authorization` header exist, the explicit header wins.

### EC9. NO_PROXY mid-flight clobber
If a user mutates `process.env.NO_PROXY` while a CDP connect is in-flight, `NoProxyLeaseManager.release` (cdp-proxy-bypass.ts:103) checks `currentNoProxy === applied` before restoring. If they diverge (user changed it), the manager leaves the user's value alone and never restores. Trade-off: the bypass might persist longer than scoped, but we don't clobber user state.

### EC10. 429 rate-limit response from CDP HTTP
`fetchCdpChecked` (cdp.helpers.ts:277-280) special-cases `res.status === 429` with a deterministic message including "Do NOT retry" so an LLM caller doesn't loop. Upstream response text is *not* echoed (log/agent injection risk).

In `connectBrowser`, if any `connectOverCDP` error contains "rate limit", the retry loop breaks immediately (pw-session.ts:471-472).

## Playwright API translation table

| TS (playwright-core) | Python (`playwright.async_api`) | Notes / risks |
|---|---|---|
| `import { chromium } from "playwright-core"` (pw-session.ts:11) | `from playwright.async_api import async_playwright` then `pw = await async_playwright().start(); pw.chromium` | Start/stop the playwright runtime explicitly. |
| `chromium.connectOverCDP(endpoint, {timeout, headers})` (pw-session.ts:454) | `await pw.chromium.connect_over_cdp(endpoint, timeout=ms, headers={...})` | `headers` accepted on the same shape. |
| `Browser`, `BrowserContext`, `Page`, `ConsoleMessage`, `Request`, `Response`, `Route` | Same names under `playwright.async_api`. | Async only; no sync API needed since the agent loop is async. |
| `browser.contexts()` | `browser.contexts` (property, sync) | Property access, no `await`. |
| `browser.on("disconnected", fn)` (pw-session.ts:464) | `browser.on("disconnected", fn)` | Synchronous callback or async — Python supports both. |
| `browser.off("disconnected", fn)` (pw-session.ts:903) | `browser.remove_listener("disconnected", fn)` | Different name. |
| `browser.close()` (pw-session.ts:905, 918, 1056) | `await browser.close()` (or `asyncio.create_task(browser.close())` for fire-and-forget) | Wrap fire-and-forget in `create_task` and shield/log on exception. |
| `browser.contexts()[0] ?? await browser.newContext()` (pw-session.ts:1116) | `browser.contexts[0] if browser.contexts else await browser.new_context()` | snake_case. |
| `context.pages()` | `context.pages` (property) | Property. |
| `context.on("page", fn)` (pw-session.ts:409) | `context.on("page", fn)` | Same. |
| `context.newPage()` (pw-session.ts:1119) | `await context.new_page()` | snake_case. |
| `context.newCDPSession(page)` (pw-session.ts:531, pw-session.page-cdp.ts:9) | `await context.new_cdp_session(page)` | snake_case. Returns `CDPSession`. |
| `session.send("Target.getTargetInfo")` (pw-session.ts:533) | `await session.send("Target.getTargetInfo")` | Same shape; positional `(method, params?)`. |
| `session.detach()` (pw-session.ts:537, pw-session.page-cdp.ts:13) | `await session.detach()` | Same. |
| `page.context()` (pw-session.ts:531) | `page.context` (property in Python) | **Risk**: in Python it's a property, not a method. Calling `page.context()` raises `TypeError`. |
| `page.url()` (pw-session.ts:551) | `page.url` (property) | Property. |
| `page.title()` (pw-session.ts:1091) | `await page.title()` | Method, async. |
| `page.mainFrame()` (pw-session.ts:683) | `page.main_frame` (property) | Property — no parens. |
| `request.frame()` (pw-session.ts:683) | `request.frame` (property) | Property. |
| `request.isNavigationRequest()` (pw-session.ts:693) | `request.is_navigation_request()` | snake_case. |
| `request.resourceType()` (pw-session.ts:702) | `request.resource_type` (property) | Property. |
| `request.method()` (pw-session.ts:357) | `request.method` (property) | Property. |
| `request.url()` (pw-session.ts:358) | `request.url` (property) | Property. |
| `request.failure()?.errorText` (pw-session.ts:387) | `request.failure` (property), then `.error_text` | Property. |
| `request.redirectedFrom()` (navigation-guard.ts:36, used via interface) | `request.redirected_from` (property) | Property. |
| `response.request()` (pw-session.ts:366) | `response.request` (property) | Property. |
| `response.status()`, `response.ok()` (pw-session.ts:375-376) | `response.status` (property), `response.ok` (property) | Properties. |
| `page.route("**", handler)` (pw-session.ts:827) | `await page.route("**", handler)` | Same glob. |
| `page.unroute("**", handler)` (pw-session.ts:840) | `await page.unroute("**", handler)` | Same. |
| `page.goto(url, {timeout})` (pw-session.ts:829) | `await page.goto(url, timeout=ms)` | kwargs. |
| `page.close()` (pw-session.ts:754, 1181) | `await page.close()` | Same. |
| `page.bringToFront()` (pw-session.ts:1195) | `await page.bring_to_front()` | snake_case. |
| `page.locator("aria-ref=...")` (pw-session.ts:887) | `page.locator("aria-ref=...")` | Same. |
| `page.frameLocator(sel)` (pw-session.ts:862) | `page.frame_locator(sel)` | snake_case. |
| `(scope as any).getByRole(role, {name, exact})` (pw-session.ts:880-882) | `scope.get_by_role(role, name=name, exact=True)` | snake_case + kwargs. The TS cast through `unknown as {...}` is a Playwright-internal workaround; in Python you can call directly. |
| `locator.nth(i)` (pw-session.ts:884) | `locator.nth(i)` | Same. |
| `route.abort()` (pw-session.ts:799, 819) | `await route.abort()` | Same. |
| `route.continue()` (pw-session.ts:806, 824) | `await route.continue_()` | **Risk**: Python uses `continue_` (trailing underscore) because `continue` is a keyword. |
| `aria-ref=...` selector engine | Same selector engine in playwright-python | Cross-language. |
| `page.on("console" \| "pageerror" \| "request" \| "response" \| "requestfailed" \| "close" \| "crash", fn)` | Same event names in Python | `crash` exists; consider wiring (see EC4). |
| `_snapshotForAI` (pw-session.ts:64, internal `WithSnapshotForAI` typing) | **Internal API** — not part of stable Playwright Python surface; uses `_snapshot_for_ai` if present, but treat as undocumented and gate on hasattr. | Major porting risk. |
| `WeakMap<Page, ...>` (pw-session.ts:109) | `weakref.WeakKeyDictionary[Page, ...]` | Same semantics. |
| `WeakSet<Page>` (pw-session.ts:111-112, 126) | `weakref.WeakSet[Page]` | Same. |
| `Map<Request, string>` (pw-session.ts:84) for `requestIds` | `weakref.WeakKeyDictionary[Request, str]` | Same — Request objects can be GC'd between events. |
| `setTimeout(...)` for delays | `await asyncio.sleep(seconds)` | Convert ms → seconds. |
| `AbortController` (cdp.helpers.ts:251) | `asyncio.timeout()` context manager (3.11+) or `asyncio.wait_for` | `asyncio.timeout` matches the deadline-style usage. |
| `Buffer.from(b64, "base64")` (cdp.ts:136) | `base64.b64decode(b64)` | stdlib. |
| `URL` parsing | `urllib.parse.urlsplit/urlparse` | More verbose; may want a small wrapper class. |

### Internal/underscore APIs to flag

- `_snapshotForAI` (pw-session.ts:64) — undocumented in Playwright; equivalent in Python may be `_snapshot_for_ai` but is not in the public stubs. Wrap with `getattr(page, "_snapshot_for_ai", None)` defensively.
- The `(scope as unknown as {...}).getByRole(role as never, ...)` cast (pw-session.ts:875-883) — pure TS type-system gymnastics; Python's `get_by_role` is just typed correctly.
- `session.send` is positional in TS (`(method, params?, sessionId?)`); in Python the third arg `session_id` may not exist on the public `CDPSession.send` — verify against the installed `playwright` version. `pw-session.page-cdp.ts:25-30` casts `session.send` to a 2-arg shape because the TS signature includes `sessionId` it doesn't want to expose.


