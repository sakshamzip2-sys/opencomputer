# OpenClaw browser — HTTP control layer (server + auth + routes + nav guard)

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; JIT deeper read of the named files when porting.

## One-line summary

OpenClaw exposes one HTTP API over two server flavors (long-lived control server + ephemeral per-port bridge), gated by Bearer/password auth + CSRF-on-loopback, with every navigation passing through an SSRF guard that pins hostnames and validates redirect chains.

## The two servers

| | Control server (`server.ts`) | Bridge server (`bridge-server.ts`) |
|---|---|---|
| Lifetime | Long-lived (one per running OpenClaw) | Ephemeral, per sandbox/session |
| Port | Configured (`controlPort`) | Dynamic (free port at startup) |
| Auth source | Persisted in config (`gateway.auth.token` etc.) | In-memory `bridge-auth-registry.ts`, keyed by port |
| Bound to | `127.0.0.1` only | `127.0.0.1` only |
| API surface | Identical | Identical |
| Started by | `startBrowserControlServerFromConfig()` | Sandbox lifecycle, on demand |

`bridge-auth-registry.ts` keys auth **by port** because multiple sandbox bridges can be live at once with different tokens — global auth would conflate them.

## Auth model

**Token format**: `crypto.randomBytes(24).toString("hex")` → 48-char hex string.

**Generation**: `ensureBrowserControlAuth()` auto-generates if `NODE_ENV !== "test"` and no creds exist. Persisted to config for control server; held in memory for bridge.

**Per-request validation** (in `installBrowserAuthMiddleware`):
1. Extract `Authorization: Bearer <token>` OR `X-OpenClaw-Password: <pw>`.
2. Timing-safe compare (`crypto.timingSafeEqual`) against expected.
3. Reject 401 on mismatch.

**CSRF defense** (`csrf.ts` middleware):
- Applied to mutating verbs (POST/PUT/PATCH/DELETE).
- Validates `Sec-Fetch-Site`, `Origin`, and `Referer` headers.
- Loopback-only requests pass; anything else rejected.
- Defends against malicious local websites making fetch() calls to the loopback API.

## Express middleware stack (in order)

```
abort signal middleware     # propagate request cancel into Playwright calls
JSON body parser            # standard
CSRF guard                  # loopback-only for mutations
auth middleware             # bearer or password
... routes ...
```

Order is load-bearing: abort first (so cancellations still land before parsing), CSRF before auth (so non-loopback hits 403 not 401).

## Request policy (`request-policy.ts`)

Gates which routes a *profile* can hit. Examples:
- `existing-session` profile cannot hit `/profiles/create` (its profile is the user's, not OpenClaw's to manage).
- `existing-session` cannot hit `/reset-profile` (would clobber the user's data).

`isPersistentBrowserProfileMutation(path)` → boolean for the gating logic.

`normalizeBrowserRequestPath(path)` strips trailing slashes, query strings — canonicalizes for policy lookup.

## Navigation guard (`navigation-guard.ts`)

The SSRF perimeter. Two phases:

**Pre-navigation**:
1. Parse target URL.
2. Resolve hostname → IP.
3. Reject blocked schemes (`file://`, `chrome://`, `chrome-extension://`, `about:`, `data:`, `javascript:`).
4. Reject private IPs (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, IPv6 equivalents).
5. Reject blocked hostnames (configurable list).

**Post-navigation** (after Playwright reports the final URL):
1. Validate the *resolved* URL again — handles redirects to a blocked target.
2. Strict-mode hostname pinning: if requested `https://example.com`, after redirects we should still be on `example.com` — no surprise redirect to attacker.com.
3. If violation detected: abort the route, mark the target blocked.

## URL pattern matching (`url-pattern.ts`)

Three modes for matchers:
- **Exact**: literal string compare (post-canonicalization).
- **Glob**: `*` and `?` wildcards.
- **Substring**: contains-match.

Used by both nav-guard (block lists) and request-policy.

## Route map (~32 endpoints)

**Basic** (`routes/basic.ts`):
- `GET /` — server status
- `POST /start`, `POST /stop` — Chrome lifecycle
- `GET /profiles`, `POST /profiles/create`, `POST /reset-profile`, `DELETE /profiles/:name`

**Tabs** (`routes/tabs.ts`):
- `GET /tabs` — list
- `POST /tabs/open` — open new tab
- `POST /tabs/focus` — focus existing
- `DELETE /tabs/:targetId` — close

**Agent** (`routes/agent.*.ts`):
- `POST /navigate`, `POST /screenshot`, `POST /pdf`, `POST /snapshot`
- `POST /act` — single or batch action
- `POST /hooks/dialog`, `POST /hooks/file-chooser` — arm helpers
- `GET /storage/cookies`, `POST /storage/cookies/set`
- `GET /storage/local`, `GET /storage/session`, …
- `GET /console`, `GET /errors`, `GET /requests` — observation
- `POST /trace/start`, `POST /trace/stop`
- `GET /debug` — diagnostic

**Routing dispatcher** (`routes/dispatcher.ts`): central router; resolves profile from query/body, dispatches to handler, normalizes errors.

## ServerContext orchestrator (`server-context.ts`)

The state holder that routes call into:

- `state.profiles: Map<profileName, ProfileContext>` — one ProfileContext per active profile.
- Each ProfileContext owns: Chrome process handle, Playwright Browser, currently-selected Page, role-ref cache.
- `lastTargetId` tracking: sticky tab selection — if no `targetId` is given, use the last one acted on. Falls back to first available tab.

Sub-modules:
- `server-context.lifecycle.ts` — startup/shutdown per profile
- `server-context.reset.ts` — reset profile (close browser, wipe state)
- `server-context.tab-ops.ts` — open/focus/close tab logic
- `server-context.selection.ts` — tab selection (lastTargetId fallback chain)
- `server-context.availability.ts` — "is browser running" probes
- `server-context.types.ts` — `BrowserServerState` shape

## Startup sequence

1. Load config → resolve browser config → bail if disabled
2. `ensureBrowserControlAuth()` — bootstrap or load token
3. Init `BrowserServerState` (no Chrome yet — Chrome is lazy)
4. Build Express app, install middleware in order (above)
5. Register routes
6. `app.listen(port, "127.0.0.1")`
7. Register port → auth in `bridge-auth-registry`
8. Log "ready"

## Shutdown sequence

1. Stop accepting new requests
2. For each active profile: close Playwright browser → kill Chrome process → wait briefly
3. Close HTTP server
4. Delete port from auth registry
5. Clear `state` to null

Force-close path bypasses graceful waits if shutdown deadline exceeded.

## Porting concerns for Python

- **FastAPI vs aiohttp**: FastAPI is the cleaner mapping (Express-style middleware → FastAPI dependencies + middleware). aiohttp is closer to raw if performance matters.
- **Middleware ordering**: FastAPI's middleware order is reverse of `add_middleware()` calls — easy gotcha.
- **Timing-safe compare**: `hmac.compare_digest()` from stdlib.
- **Token gen**: `secrets.token_hex(24)`.
- **CSRF**: write a small middleware checking `Sec-Fetch-Site` / `Origin` / `Referer`. No off-the-shelf FastAPI plugin matches this exact loopback-only model.
- **Route dispatcher**: Python doesn't need its own — FastAPI's routing handles it. The OpenClaw dispatcher exists because Express is too low-level.
- **IP-block-list**: `ipaddress` from stdlib (`is_private`, `is_loopback`, `is_link_local`).
- **Hostname pinning across redirects**: httpx + `event_hooks` to inspect intermediate responses.

## Open questions

- Do we keep the bridge/control server split for v1, or just ship the control server (no sandbox initially)?
- CSRF defense is loopback-only — Python's web frameworks don't ship this — write our own?
- Per-port auth registry is in-memory; how does that survive a server restart? (Probably it doesn't — bridge is ephemeral by design.)
- Trace endpoints: keep on day one or defer?

---

## Deep second-pass — function-by-function

> Captured 2026-05-03 from `/Users/architsakri/Downloads/Harnesses/openclaw-main/extensions/browser/src/browser/`. The first pass above is the architectural skeleton; this is the line-by-line excavation needed to produce a faithful Python port. Filename:line refs are relative to that root unless otherwise noted.

### 1. Function tables per file

#### 1.1 `control-auth.ts` (192 LOC) — token bootstrap and persistence

| Function | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `resolveBrowserControlAuth` (16-31) | `(cfg?, env=process.env) -> {token?, password?}` | Reads `gateway.auth` block + env via `resolveGatewayAuth`; tail-trims empty strings to `undefined`. | `ensureBrowserControlAuth`, every callsite that wants the current credentials. | Both fields can be unset; the empty string maps to `undefined`. The same record drives **both** the control server and any spawned bridge. |
| `shouldAutoGenerateBrowserAuth` (33-43) | `(env) -> boolean` | Refuses auto-gen in `NODE_ENV=test` or `VITEST` truthy. | `ensureBrowserControlAuth` | Vitest exports `VITEST=true` so the check covers the npm-script entrypoints — **but** `VITEST=0/false/off` is treated as "not in vitest". Mirrors this logic in Python: never auto-gen when `PYTEST_CURRENT_TEST` is set. |
| `hasExplicitNonStringGatewayCredentialForMode` (45-58) | `({cfg, mode}) -> boolean` | Detects when `gateway.auth.token` (mode=none) or `gateway.auth.password` (mode=trusted-proxy) is set but **not a plain string** — e.g., a `SecretRef`-shaped object. | Auto-gen branch in `ensureBrowserControlAuth` | The auto-gen path will refuse to overwrite a SecretRef-style placeholder; startup will then fail closed (no token resolved). |
| `generateBrowserControlToken` (60-62) | `() -> string` | `crypto.randomBytes(24).toString("hex")` → 48 hex chars (192 bits of entropy). | The two persist helpers. | Hex, not base64. Don't switch encoding mid-port — a hand-typed token from prior installs would stop matching. |
| `generateAndPersistBrowserControlToken` (64-94) | `({cfg, env}) -> {auth, generatedToken?}` | Mints a fresh token, writes the merged config to disk, then **re-reads** to defeat concurrent writers. Returns the persisted view; only flags `generatedToken` if our token survived the round-trip. | `ensureBrowserControlAuth` (mode=none branch) | The "re-read after write" pattern is load-bearing — without it, two startups racing on the same config would both think they own the token. |
| `generateAndPersistBrowserControlPassword` (96-126) | `({cfg, env}) -> {auth, generatedToken?}` | Same as above but persists into `gateway.auth.password` instead of `.token`. | Auto-gen branch when `mode === "trusted-proxy"`. | The carve-out exists because `mode=trusted-proxy` forbids `auth.token` (the proxy carries upstream auth itself). The browser still needs a loopback shared secret, so it gets one through `password`. |
| `ensureBrowserControlAuth` (128-192) | `({cfg, env?}) -> {auth, generatedToken?}` | Bootstrap orchestrator. Returns existing creds → returns nothing if test mode → respects explicit `mode=password` → re-reads config to survive races → branches on mode (`none` | `trusted-proxy` | unspecified) → delegates to gateway-startup helper for the unspecified case. | `runtime-lifecycle` (and tests). | The carve-outs collectively ensure: (a) test runs are deterministic (no token written), (b) explicit-password configs are not silently upgraded to a token, (c) SecretRef configs are not clobbered with plaintext, (d) trusted-proxy mode never leaks a `gateway.auth.token` to disk. |

#### 1.2 `csrf.ts` (88 LOC) — origin/Sec-Fetch-Site mutation guard

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `firstHeader` (5-7) | `(value: string|string[]|undefined) -> string` | Express returns array for repeated headers; this collapses to the first. | Empty → `""`, not `undefined`. |
| `isMutatingMethod` (9-12) | `(method) -> boolean` | True for POST/PUT/PATCH/DELETE. | GET/HEAD/OPTIONS bypass entirely. |
| `isLoopbackUrl` (14-25) | `(value) -> boolean` | Parses URL, checks `isLoopbackHost(parsed.hostname)`. Treats literal `"null"` as not-loopback (some browsers send `Origin: null`). | Returns `false` for unparseable URLs — fail closed. |
| `shouldRejectBrowserMutation` (27-56) | `({method, origin?, referer?, secFetchSite?}) -> boolean` | The decision core. See §3 below for the precedence rules. | Non-browser clients (curl/undici/Node) pass because they send neither Origin nor Referer — auth still gates them. |
| `browserMutationGuardMiddleware` (58-88) | `() -> ExpressMiddleware` | Lets OPTIONS through unconditionally (CORS preflight is non-mutating); 403 + `"Forbidden"` body on rejection. | Note OPTIONS bypass: a same-origin preflight to a CSRF-protected POST will succeed, then the actual POST is checked. |

#### 1.3 `http-auth.ts` (64 LOC) — Bearer / Basic / X-OpenClaw-Password matcher

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `firstHeaderValue` (5-7) | identical to csrf.ts version | — | Duplicated — fine for porting; centralize in Python. |
| `parseBearerToken` (9-15) | `(authorization) -> string|undefined` | `Authorization: Bearer <token>` (case-insensitive scheme). Trims and rejects empty. | Lowercase normalization is on the **scheme only**; the token bytes are preserved verbatim for the timing-safe compare. |
| `parseBasicPassword` (17-36) | `(authorization) -> string|undefined` | `Authorization: Basic base64(user:pass)` → returns just the password (post-`:`). User segment ignored. | Catches base64 errors → returns `undefined`. Empty password → `undefined`. |
| `isAuthorizedBrowserRequest` (38-64) | `(req, {token?, password?}) -> boolean` | Tries Bearer-vs-token first; then header `X-OpenClaw-Password` vs password; then Basic-vs-password. All three use `safeEqualSecret` (HMAC timing-safe). | Order matters only if both creds are configured — Bearer wins. Both can be configured simultaneously; either matching means authorized. |

#### 1.4 `server-middleware.ts` (52 LOC) — common stack + auth gate

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `hasVerifiedBrowserAuth` (12-14) | `(req) -> boolean` | Reads the `__openclawBrowserAuthVerified` Symbol-tag added by the auth middleware. | The bridge `/sandbox/novnc` route uses this so it can run **after** auth without re-checking. |
| `markVerifiedBrowserAuth` (16-18) | internal | Set the tag. | Mutates the request object directly; in Python use FastAPI `request.state` instead. |
| `installBrowserCommonMiddleware` (20-36) | `(app)` | Three middlewares, in order: (i) abort-signal wiring (`req.aborted` / `res.close` → AbortController), (ii) `express.json({limit:"1mb"})`, (iii) `browserMutationGuardMiddleware()`. | The 1MB body limit is the cap on `/act` payloads, snapshots, etc. The abort-signal exposure under `req.signal` is what lets Playwright calls cancel mid-request. |
| `installBrowserAuthMiddleware` (38-52) | `(app, {token?, password?})` | No-op if no creds → otherwise registers a 401-on-mismatch middleware that tags the request on success. | Critical: if no creds **and** no auto-gen, the server will accept anonymous loopback requests. Pair with `shouldAutoGenerateBrowserAuth` to fail closed during normal startup. |

#### 1.5 `bridge-server.ts` (153 LOC) — ephemeral per-port API

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `buildNoVncBootstrapHtml` (33-60) | `({noVncPort, password?}) -> string` | A self-redirecting HTML page that points the browser at the in-sandbox noVNC server with `autoconnect=1` and (optionally) the VNC password baked into the URL hash. | The hash fragment is preferred over a query param — fragments are not sent to the noVNC HTTP server, only consumed client-side. |
| `startBrowserBridgeServer` (62-139) | `(params) -> BrowserBridge` | Loopback-only Express. Refuses non-loopback host. Required auth (throws if both empty). Optional `/sandbox/novnc` route hidden behind a token resolver. Listens on port 0 if unspecified, then registers the resolved port + auth into `bridge-auth-registry`. | The state object is built **before** `app.listen` resolves — the routes get a `getState` closure that reads through the same reference, so the post-listen mutation of `state.server` is visible to handlers. |
| `stopBrowserBridgeServer` (141-153) | `(server) -> Promise<void>` | Reads address, deletes registry entry, awaits `server.close()`. | `server.close()` waits for in-flight requests by default — long-running screenshots will block shutdown unless externally cancelled. |

#### 1.6 `bridge-auth-registry.ts` (36 LOC) — port→auth map

`setBridgeAuthForPort` / `getBridgeAuthForPort` / `deleteBridgeAuthForPort` — three thin Map operations keyed by port number. Empty/whitespace strings are normalized to `undefined`. **Per-port not global** because a single OpenClaw can host multiple ephemeral sandbox bridges concurrently, each with its own minted token. In Python: `dict[int, BridgeAuth]` guarded by an `asyncio.Lock`.

#### 1.7 `request-policy.ts` (47 LOC) — profile-mutation gate + path normalizer

| Function | Signature | Purpose |
|---|---|---|
| `normalizeBrowserRequestPath` (9-19) | `(value) -> string` | Trim → ensure leading `/` → strip trailing slashes (but keep root `/`). |
| `isPersistentBrowserProfileMutation` (21-30) | `(method, path) -> boolean` | True for `POST /profiles/create`, `POST /reset-profile`, `DELETE /profiles/:name` (regex `^/profiles/[^/]+$`). |
| `resolveRequestedBrowserProfile` (32-47) | `({query?, body?, profile?}) -> string|undefined` | Precedence: query → body.profile → fallback string. Whitespace trimmed; empty → `undefined`. |

The gating policy is consumed by the **upstream** profile policy module (out of scope for this subsystem) — it asks "is this request a profile-level mutation?" to decide whether an `existing-session` profile can perform it. The answer for `existing-session` is always no.

#### 1.8 `url-pattern.ts` (15 LOC) — exact / glob / substring

| Branch | Behavior | Edge cases |
|---|---|---|
| `pattern === url` | Exact (post-trim). | Case-sensitive! `https://Example.com` ≠ `https://example.com`. Trailing slashes matter. |
| `pattern.includes("*")` | Regex: escape regex metachars, then `**` and `*` both → `.*`. Anchored `^...$`. | `**` and `*` are equivalent — there's no path-segment vs cross-segment distinction. `?` is **not** a wildcard despite the docstring claim in the first-pass note. |
| else | `url.includes(pattern)` (substring contains). | Case-sensitive. No URL canonicalization (query string, fragment, default ports all matter literally). |

#### 1.9 `navigation-guard.ts` (174 LOC) — SSRF perimeter (browser side)

| Function | Signature | Purpose | Gotchas |
|---|---|---|---|
| `isAllowedNonNetworkNavigationUrl` (18-21) | `(parsed) -> boolean` | True only for `about:blank`. | Hardcoded allowlist — adding `about:srcdoc` etc. is a deliberate decision. |
| `InvalidBrowserNavigationUrlError` (23-28) | class | Thrown on every block. | Distinct class so route layer can map to 4xx without leaking internals. |
| `withBrowserNavigationPolicy` (39-43) | `(ssrfPolicy?) -> {ssrfPolicy?}` | Spread helper to avoid passing `undefined`. | Call sites ergonomics only. |
| `requiresInspectableBrowserNavigationRedirects` (45-47) | `(ssrfPolicy?) -> boolean` | True when private network not allowed. | Forces Playwright path for tab open (only Playwright can inspect redirect chains in-browser). |
| `isIpLiteralHostname` (49-51) | `(hostname) -> boolean` | `node:net.isIP(...) !== 0`. | Used to skip the "must be IP literal" check when the URL already is. |
| `isExplicitlyAllowedBrowserHostname` (53-65) | `(hostname, ssrfPolicy?) -> boolean` | Two layers: exact-string allowlist, then glob-style hostname allowlist via `matchesHostnameAllowlist`. | The inner `normalizeHostname` lower-cases and IDNA-encodes — keep parity in Python via `idna` package. |
| `assertBrowserNavigationAllowed` (67-123) | `(opts) -> Promise<void>` | The main pre-nav gate. See §5 below. | Step ordering matters. |
| `assertBrowserNavigationResultAllowed` (131-153) | `(opts) -> Promise<void>` | Post-nav gate that **only** validates http/https/about:blank URLs — silently accepts `chrome-error://` style internal pages. | Empty URL → silently OK (early return). |
| `assertBrowserNavigationRedirectChainAllowed` (155-174) | `({request?}) -> Promise<void>` | Walks `redirectedFrom()` recursively, then validates **in chronological order** (`toReversed()`). | Each hop runs `assertBrowserNavigationAllowed` — DNS resolution fires per hop. |

#### 1.10 server-context cluster

`server-context.ts` (251 LOC):

| Function | Signature | Purpose |
|---|---|---|
| `listKnownProfileNames` (33-39) | `(state) -> string[]` | Union of declared (config) and live (Map) profile names. Used so a profile that's been removed from disk but is still running shows up in `/profiles`. |
| `createProfileContext` (44-116) | `(opts, profile) -> ProfileContext` | Wires the four sub-modules (tab-ops, availability, selection, reset) into a single profile facade. The closures share `getProfileState` so all four see the same lazily-created `ProfileRuntimeState`. |
| `createBrowserRouteContext` (118-251) | `(opts) -> BrowserRouteContext` | The route-facing orchestrator. `forProfile(name?)` resolves a name (defaulting to `state.resolved.defaultProfile`), runs hot-reload, throws `BrowserProfileNotFoundError` if missing. `listProfiles` enumerates everything (including missing-from-config profiles flagged with `missingFromConfig: true`). |

`server-context.lifecycle.ts` (28 LOC):

| Function | Purpose |
|---|---|
| `resolveIdleProfileStopOutcome` | When `profileState.running===null` but profile is `attachOnly` or remote, "stopping" is still meaningful (close the cached Playwright connection). Returns `{stopped:true, closePlaywright:true}` for those; `{stopped:false, closePlaywright:false}` otherwise. |
| `closePlaywrightBrowserConnectionForProfile` | Lazy-import `pw-ai` and call `closePlaywrightBrowserConnection({cdpUrl})`; swallow errors. |

`server-context.reset.ts` (67 LOC):

`createProfileResetOps` returns `{resetProfile}` which:
1. Throws `BrowserResetUnsupportedError` for non-local profiles (capabilities check).
2. Resolves user-data dir.
3. If port reachable but we don't own the running process → close the cached Playwright connection (best-effort kick-out).
4. If we **do** own a running browser → `stopRunningBrowser()`.
5. Close cached Playwright connection again (defensive).
6. If user-data dir does not exist → `{moved: false, from}`.
7. Else → move to trash via `movePathToTrash` → `{moved: true, from, to}`.

Trash-move (rather than `rm -rf`) is intentional: cookies and login state are recoverable for ~30 days.

`server-context.tab-ops.ts` (266 LOC) — the meat of tab management. `createProfileTabOps` returns `{listTabs, openTab}`:

- **listTabs**: branches on capability — chrome-mcp → `listChromeMcpTabs`; persistent-playwright → `listPagesViaPlaywright(cdpUrl, ssrfPolicy)`; otherwise CDP `/json/list` over HTTP. Filters out `targetId === ""` entries.
- **openTab**: branches the same way. The CDP fallback path (192-259) has two sub-flavors: `createTargetViaCdp` (preferred) → poll `listTabs` for up to `OPEN_TAB_DISCOVERY_WINDOW_MS` to find the new tab, with `assertBrowserNavigationResultAllowed` on the resolved URL. If `createTargetViaCdp` fails, fall back to `PUT /json/new?<encoded-url>` (with `GET` retry on HTTP 405). Every path stamps `profileState.lastTargetId = <new id>` and triggers `enforceManagedTabLimit` (closes excess tabs above `MANAGED_BROWSER_PAGE_TAB_LIMIT`).

`server-context.selection.ts` (169 LOC) — `createProfileSelectionOps` returns `{ensureTabAvailable, focusTab, closeTab}`:

- **ensureTabAvailable** is the lastTargetId-fallback heart. If no tabs → open `about:blank`. Filter to tabs with `wsUrl` for capability=supportsPerTabWs. Resolve given `targetId` via `resolveTargetIdFromTabs` (returns `ok|ambiguous|not-found`). If no targetId → `pickDefault()` returns: `lastTargetId` if it resolves (and not ambiguous), else first `type==="page"` tab, else `tabs[0]`. Updates `lastTargetId` to the chosen target.
- **focusTab**: chrome-mcp → `focusChromeMcpTab`; persistent-playwright → `focusPageByTargetIdViaPlaywright`; else CDP `/json/activate/:id`. All paths update `lastTargetId`.
- **closeTab**: same branching, but does **not** update `lastTargetId` — that's a deliberate safety, so the next call's fallback chain still has a valid hint.

`server-context.types.ts` (73 LOC) — the canonical `BrowserServerState`, `ProfileRuntimeState`, `ProfileContext`, `ProfileStatus` shapes. Reproduced in §8 below.

`server-context.availability.ts` (297 LOC) — `createProfileAvailability` returns `{isHttpReachable, isReachable, ensureBrowserAvailable, stopRunningBrowser}`:

- `resolveTimeouts`: when remote, uses configured `remoteCdpTimeoutMs` / `remoteCdpHandshakeTimeoutMs`; loopback uses `timeoutMs` directly.
- `attachRunning`: stores running Chrome handle, attaches an `exit` listener that nulls `profileState.running` if the server is still alive (guards against SIGUSR1 restart races).
- `reconcileProfileRuntime`: when `profileState.reconcile` is set (config hot-reload changed the profile shape), tear down old browser/MCP/Playwright before continuing.
- `waitForCdpReadyAfterLaunch`: poll `isReachable` until `CDP_READY_AFTER_LAUNCH_WINDOW_MS` deadline. Each attempt's timeout is bounded `[MIN, MAX]`.
- `waitForChromeMcpReadyAfterAttach`: poll `listChromeMcpTabs` until ready; throws `BrowserProfileUnavailableError` with last-error detail.
- `ensureBrowserAvailable`: orchestrator. Reconcile → chrome-mcp branch → check `isHttpReachable` → not reachable + attachOnly/remote → run `onEnsureAttachTarget` → loopback restart fallback → launch + wait. Reachable but not WS-reachable → ownership check → restart.
- `stopRunningBrowser`: reconcile → chrome-mcp close session OR (if no running handle) idle-stop outcome OR `stopOpenClawChrome(running)`.

`runtime-lifecycle.ts` (60 LOC) — `createBrowserRuntimeState` and `stopBrowserRuntime`. The latter awaits `stopKnownBrowserProfiles` (kills every active profile's Chrome), optionally closes the HTTP server, calls `clearState()`, and lazy-imports `pw-ai` to call `closePlaywrightBrowserConnection()` for any cached connection.

`server-lifecycle.ts` (47 LOC) — `ensureExtensionRelayForProfiles` is a stub kept for backward-compat; `stopKnownBrowserProfiles` iterates `listKnownProfileNames`, stopping the running Chrome and falling back to the per-profile-context `stopRunningBrowser()`. Errors swallowed per profile (best-effort cleanup).

#### 1.11 routes/

| File | Functions | Purpose |
|---|---|---|
| `index.ts` (11) | `registerBrowserRoutes(app, ctx)` | Calls basic + tabs + agent registrars in order. |
| `types.ts` (26) | `BrowserRequest`, `BrowserResponse`, `BrowserRouteHandler`, `BrowserRouteRegistrar` types. | Decouples route handlers from raw Express. |
| `utils.ts` (72) | `getProfileContext` (query→body→default), `jsonError`, `toStringOrEmpty`, `toNumber`, `toBoolean`, `toStringArray`. | Body/query coercion + canonical error response shape. |
| `dispatcher.ts` (133) | `compileRoute` (`:name` → `([^/]+)` capture), `createRegistry`, `normalizePath`, `createBrowserRouteDispatcher` | In-process invocation path: builds a small Express-shaped router as data, dispatches without HTTP. Used by the loopback dispatch path so the same handlers serve both HTTP and direct in-process calls. |
| `agent.ts` (13) | `registerBrowserAgentRoutes` | Composes snapshot + act + debug + storage. |
| `agent.shared.ts` (149) | `readBody`, `resolveTargetIdFromBody`, `resolveTargetIdFromQuery`, `handleRouteError`, `resolveProfileContext`, `getPwAiModule`, `requirePwAi`, `withRouteTabContext`, `withPlaywrightRouteContext` | The handler harness — every agent route uses one of the two `with*RouteContext` helpers, which resolve profile → ensure tab → optionally require Playwright → call user fn → catch+map errors. |
| `agent.snapshot.plan.ts` (97) | `resolveSnapshotPlan`, `shouldUsePlaywrightForAriaSnapshot`, `shouldUsePlaywrightForScreenshot` | Decides which snapshot/screenshot backend to use (Playwright vs raw CDP vs chrome-mcp) based on profile capabilities + query. |
| `agent.snapshot.ts` (594) | `clearChromeMcpOverlay`, `renderChromeMcpLabels` (overlay injection), `saveNormalizedScreenshotResponse`, `saveBrowserMediaResponse`, `resolveTargetIdAfterNavigate`, `registerBrowserAgentSnapshotRoutes` | `/navigate`, `/pdf`, `/screenshot`, `/snapshot`. |
| `agent.act.shared.ts` (53) | `ACT_KINDS` array, `isActKind`, `parseClickButton`, `parseClickModifiers` | Validates incoming `kind` and click modifiers. |
| `agent.act.errors.ts` (30) | `ACT_ERROR_CODES`, `jsonActError`, `browserEvaluateDisabledMessage` | Stable error-code namespace returned to clients. |
| `agent.act.normalize.ts` (322) | `normalizeActRequest`, `validateBatchTargetIds` | Coerces raw body into the per-kind `BrowserActRequest` discriminated union; raises on invalid shapes. |
| `agent.act.hooks.ts` (187) | `registerBrowserAgentActHookRoutes` | `/hooks/file-chooser`, `/hooks/dialog`. Chrome-MCP path uses an evaluated script that monkey-patches `window.{alert,confirm,prompt}` for one call then restores. |
| `agent.act.download.ts` (116) | `registerBrowserAgentActDownloadRoutes` | `/wait/download`, `/download`. Playwright-only. |
| `agent.act.ts` (683) | `registerBrowserAgentActRoutes` | `/act`, `/response/body`, `/highlight` plus delegating to hooks/download. Has the giant `switch (action.kind)` for both chrome-mcp and Playwright paths. |
| `agent.debug.ts` (148) | `registerBrowserAgentDebugRoutes` | `/console`, `/errors`, `/requests`, `/trace/start`, `/trace/stop`. Playwright-only. |
| `agent.storage.ts` (452) | `parseStorageKind`, `parseStorageMutationRequest`, `registerBrowserAgentStorageRoutes` | All cookie + local/session storage + emulation-set routes. Playwright-only. |
| `basic.ts` (225) | `registerBrowserBasicRoutes` | `/`, `/profiles`, `/start`, `/stop`, `/reset-profile`, `/profiles/create`, `DELETE /profiles/:name`. |
| `tabs.ts` (236) | `registerBrowserTabRoutes` | `/tabs`, `/tabs/open`, `/tabs/focus`, `DELETE /tabs/:targetId`, `/tabs/action` (legacy multiplexed). |
| `existing-session-limits.ts` (45) | `EXISTING_SESSION_LIMITS` const | Centralized error-message constants for chrome-mcp limitations (used by `/act`, `/screenshot`, `/snapshot`, `/pdf`, `/download`, etc.). |
| `output-paths.ts` (31) | `ensureOutputRootDir`, `resolveWritableOutputPathOrRespond` | Path-traversal guard for trace/download output paths (delegates to `path-output.ts`/`paths.ts`). |
| `path-output.ts` (1) | `export *` re-export of `../paths.js` | Glue. |

### 2. Auth lifecycle in full

**Token format**:
- 24 random bytes via Node `crypto.randomBytes` → 48 hex chars (192 bits entropy). `control-auth.ts:60-62`.
- Both "token" and "password" use the same generator; the difference is the **header** they bind to (Bearer vs `X-OpenClaw-Password`/Basic) and the **config key** (`gateway.auth.token` vs `.password`).

**Persistence path**: `gateway.auth.token` or `gateway.auth.password` inside the canonical OpenClaw config file written by `writeConfigFile()`. The config file is in `~/.openclaw/config.toml` (or wherever the loader resolves) — exact path is owned by the upstream config layer.

**Env-var override**: There is no single `OPENCLAW_GATEWAY_AUTH_TOKEN` shortcut visible in this subsystem. `resolveGatewayAuth` (in `gateway/auth.ts`, not on our reading list) consults env vars; the browser layer treats whatever it returns as authoritative. Port note: in Python, expose `OPENCOMPUTER_BROWSER_AUTH_TOKEN` and `OPENCOMPUTER_BROWSER_AUTH_PASSWORD` and have a single resolve helper read env first, config second, generate third.

**Bootstrap order during startup** (server-side):
1. Load config (`loadConfig()`).
2. `ensureBrowserControlAuth({cfg, env})`.
   1. `resolveBrowserControlAuth(cfg, env)`. Already set? Return.
   2. `shouldAutoGenerateBrowserAuth(env)` false (test mode)? Return empty (server will refuse to start auth-required routes).
   3. Explicit `mode === "password"` but unset? Return empty (admin must set it; we don't auto-gen passwords for password-mode).
   4. Re-read latest config (race-safe).
   5. If `mode==="none"` or `mode==="trusted-proxy"`: check `hasExplicitNonStringGatewayCredentialForMode` — if SecretRef-style placeholder is present, return empty (admin must resolve it).
   6. `mode==="trusted-proxy"`: persist into `gateway.auth.password`.
   7. `mode==="none"`: persist into `gateway.auth.token`.
   8. Other/unspecified: delegate to `ensureGatewayStartupAuth({cfg, env, persist:true})`.
3. Build state, register routes, install middleware.
4. Auth credentials passed into `installBrowserAuthMiddleware(app, {token, password})`.

**`shouldAutoGenerateBrowserAuth(env)` decision**: returns `false` if `NODE_ENV === "test"`, OR if `VITEST` is non-empty and not in `{"0","false","off"}`. Otherwise `true`. Python equivalent:
```python
def should_auto_generate_browser_auth(env: Mapping[str, str]) -> bool:
    if env.get("OPENCOMPUTER_ENV", "").lower() == "test":
        return False
    if env.get("PYTEST_CURRENT_TEST"):
        return False
    return True
```

**`allowLegacyPasswordModeWithoutSecret` carve-out**: not a single named function in this layer — instead encoded as the `mode === "password"` early-return on lines 145-147 and 155-157 of `control-auth.ts`. The semantics: if the operator has explicitly chosen password mode, the system trusts them to set a password, never auto-generates one, and `installBrowserAuthMiddleware` becomes a no-op (no auth → fail open) **only if** they also leave the password empty. This is the legacy behavior for users running on a fully trusted host; it is the single non-fail-closed path in the design. Carry this forward to Python only if there's a real customer need; the safer default is to delete the carve-out and require an explicit `--no-auth` flag.

**Password vs token paths**:
- Token: validated by `Authorization: Bearer <token>`. Single header, single byte-comparison.
- Password: validated by **two** headers — `X-OpenClaw-Password: <pw>` (preferred) **or** `Authorization: Basic base64(user:pw)` (the username is ignored). Tried in that order in `http-auth.ts:51-61`.
- Both can be configured simultaneously; either matching is sufficient.

**Timing-safe compare**: `safeEqualSecret` in `security/secret-equal.js` (out of scope) wraps Node's `crypto.timingSafeEqual` with a length-mismatch guard (different-length inputs would otherwise throw). Python: `hmac.compare_digest(a.encode(), b.encode())` — the stdlib version is already constant-time-ish for equal-length strings, but be sure to encode to bytes first.

### 3. CSRF mechanism — exact precedence

In `csrf.ts:27-56`:

```
shouldRejectBrowserMutation(method, origin?, referer?, secFetchSite?):
  1. if method not in {POST, PUT, PATCH, DELETE}: return false   (GET/HEAD/OPTIONS bypass)
  2. if Sec-Fetch-Site == "cross-site" (case-insensitive): return true
  3. if Origin header non-empty:
       return !isLoopbackUrl(Origin)            (pass only if loopback)
  4. if Referer header non-empty:
       return !isLoopbackUrl(Referer)           (pass only if loopback)
  5. return false                               (no headers — likely curl/Node, let auth gate it)
```

Subtleties:
- `Sec-Fetch-Site` only **rejects** on `cross-site`. Values `same-origin` / `same-site` / `none` do **not** themselves authorize — they just skip the strong-signal short-circuit, falling through to Origin/Referer checks. The reasoning (per the inline comment on line 41): `localhost` and `127.0.0.1` produce `same-site` even though the API only accepts loopback IPs, so we re-check explicitly via the Origin URL.
- `Origin: null` (some sandboxed iframes, file:// pages) is rejected because `isLoopbackUrl("null")` returns false.
- The **first** present of (Origin, Referer) wins — Referer is only checked if Origin is empty.
- The OPTIONS preflight bypass (`browserMutationGuardMiddleware:64-68`) is what allows browser-based callers to negotiate CORS before getting blocked.

Routes subject to CSRF: every mutating verb. Routes bypassed: every GET (which means `/`, `/profiles`, `/tabs`, `/cookies`, `/storage/:kind`, `/console`, `/errors`, `/requests`, `/snapshot`).

### 4. Request-policy gating

`request-policy.ts` provides only the **mutation-detection** half. The full policy table lives in `act-policy.ts` and `profile-capabilities.ts` (out of immediate scope but adjacent). The narrow gate this file is responsible for:

| Method | Path | `isPersistentBrowserProfileMutation` | Notes |
|---|---|---|---|
| `POST` | `/profiles/create` | true | Whole-fleet mutation |
| `POST` | `/reset-profile` | true | Wipes user-data dir |
| `DELETE` | `/profiles/:name` | true | Removes profile config |
| `POST` | `/start` | false | Per-profile, but not config-mutating |
| `POST` | `/stop` | false | Same |
| `POST` | `/navigate` | false | Per-tab |
| `DELETE` | `/tabs/:targetId` | false | Per-tab |
| `*` | other | false | — |

**Worked examples**:
- Request: `DELETE /profiles/foo?profile=bar`. `normalizeBrowserRequestPath("/profiles/foo")` → `"/profiles/foo"`. Regex `^/profiles/[^/]+$` matches. → mutation.
- Request: `DELETE /profiles/foo/bar`. Regex fails (slash inside). → not mutation. (This is by design — there's no nested `/profiles/x/y` route; if there were, the path normalizer wouldn't accidentally match it.)
- Request: `POST /reset-profile/`. Trailing slash stripped → `"/reset-profile"`. → mutation.
- Request: `POST /reset-profile?profile=alice`. Query string is on `req.query`, not in the path string. → mutation. (Query parsing is upstream.)

**Path normalization corner cases**:
- Empty input → empty output (caller must handle).
- `"foo"` → `"/foo"` (leading slash added).
- `"/"` → `"/"` (length≤1 short-circuit, no trailing-slash strip).
- `"/foo///"` → `"/foo"` (`/+$` strips all trailing).
- Query strings are **not** stripped — the function expects the caller to pass the path component only. If the upstream route layer hands it `"/foo?x=1"`, the regex match would fail. (The actual call sites only pass `req.path`, not `req.url`.)

For an `existing-session` profile, the upstream policy module that consumes `isPersistentBrowserProfileMutation` denies the request with HTTP 403 + a static "this profile cannot mutate global config" message.

### 5. SSRF nav-guard — full check sequence

**Pre-navigation** (`assertBrowserNavigationAllowed`, lines 67-123):
1. **URL-string normalization**: `normalizeOptionalString(opts.url)`; empty → `InvalidBrowserNavigationUrlError("url is required")`.
2. **Parse**: `new URL(rawUrl)`; throw on `URIError` → `InvalidBrowserNavigationUrlError("Invalid URL: ...")`.
3. **Scheme check**: `parsed.protocol` must be `http:` or `https:` **or** `parsed.href` must equal `"about:blank"`. Otherwise throw `"Navigation blocked: unsupported protocol "..."`. This blocks `file:`, `chrome:`, `chrome-extension:`, `data:`, `javascript:`, `about:` (other than blank), `about:srcdoc`, `view-source:`, etc.
4. **Proxy-env check** (strict mode only): if `hasProxyEnvConfigured()` is true (HTTPS_PROXY/HTTP_PROXY/etc.) AND private-network navigation is **not** allowed by policy → throw. The reasoning: the browser will use the proxy, which can route around our DNS pinning, so the strict mode invariants don't hold.
5. **Hostname-must-be-IP-literal check** (strict mode only): if policy is strict AND hostname is **not** an IP literal AND hostname is **not** in the explicit allowlist → throw. The reasoning: browser DNS may differ from Node DNS — without IP literals or an allowlist, we cannot guarantee the browser hits the same address Node-side resolution checked.
6. **DNS resolve + IP block**: `resolvePinnedHostnameWithPolicy(hostname, {lookupFn, policy})` resolves the hostname and validates each returned address against the IP block list (private/loopback/link-local/multicast etc.) per the policy. The "pinned" part means subsequent requests should reuse the resolved IP (the in-process `httpx` connection re-pinning lives in `infra/net/ssrf.ts`).

**Post-navigation** (`assertBrowserNavigationResultAllowed`, lines 131-153):
1. Empty/unparseable URL → silent return (don't false-positive on browser-internal pages).
2. If protocol is http/https or URL is `about:blank` → re-run `assertBrowserNavigationAllowed`. Other protocols (e.g. `chrome-error://chromewebdata/`) silently OK.
3. The re-run goes through the full pre-nav check, including DNS resolve + IP block — so a redirect to an internal IP is caught here even though the original URL passed.

**Redirect chain** (`assertBrowserNavigationRedirectChainAllowed`, lines 155-174):
1. Walk `request.redirectedFrom()` recursively → array of URL strings.
2. Reverse → chronological order (oldest first).
3. For each: `assertBrowserNavigationAllowed(url, lookupFn, ssrfPolicy)`.
4. First failure throws and aborts the route handler.

**"Strict mode" toggles** are not a single boolean — they're encoded as `isPrivateNetworkAllowedByPolicy(ssrfPolicy)`. Strict means `false`. In strict mode:
- Proxy env vars are forbidden (step 4 above).
- Hostname-based URLs are forbidden unless on the explicit allowlist (step 5).
- Redirect chain inspection is required → forces the Playwright path (see `requiresInspectableBrowserNavigationRedirects` in tab-ops:185-189).

**Hostname pinning across redirect chain**: not enforced as a literal "must be the same hostname" check — instead, **every** intermediate URL must independently pass the SSRF gate. So a redirect from `example.com` → `attacker.internal` is blocked because `attacker.internal` resolves to a private IP, not because the hostname changed.

### 6. URL pattern matcher — edge cases

| Input pattern | Input URL | Result | Why |
|---|---|---|---|
| `""` | anything | `false` | Empty pattern early-return at line 4. |
| `"https://example.com"` | `"https://example.com"` | `true` | Exact match. |
| `"https://example.com"` | `"https://Example.com"` | `false` | Case-sensitive. |
| `"https://example.com"` | `"https://example.com/"` | `false` | Trailing slash matters. |
| `"https://example.com"` | `"https://example.com/foo"` | `true` | Substring fallback (no `*` → contains-match). |
| `"*.example.com"` | `"https://api.example.com"` | `true` | Glob: regex `^.*\.example\.com$`. Note: dot is escaped, so `xexample.com` would NOT match. |
| `"*.example.com"` | `"https://example.com"` | `false` | Glob requires the prefix dot. |
| `"**"` | `"https://anything"` | `true` | `**` and `*` both → `.*`. |
| `"foo*bar"` | `"foozzbar"` | `true` | Single `*`. |
| `"a?b"` | `"a_b"` | `false` | `?` is **not** a wildcard despite the docstring. Treated as literal `?` → no `*` in pattern → substring contains check → only matches if URL contains literal `a?b`. |
| `"https://example.com/path"` | `"HTTPS://EXAMPLE.COM/PATH"` | `false` | All three modes are case-sensitive. |
| `"foo"` | `"barfoobaz?q=foo"` | `true` | Query-string content is part of the URL string for the contains check. |

**Trailing slashes / query strings**: not normalized. `pattern="https://x.com"` does **not** match `"https://x.com/"` or `"https://x.com?q=1"` exactly — but the substring fallback lets the latter pass. This is fine for block-list use (any substring presence wins) but a footgun for allow-list use.

### 7. Full route table

> `Auth?` column: ✓ when behind `installBrowserAuthMiddleware`. All routes are. `CSRF?` column: ✓ when method is mutating (POST/PUT/PATCH/DELETE) — these go through `browserMutationGuardMiddleware`. Body shapes are simplified.

#### 7.1 `routes/basic.ts`

| Method | Path | Body | Response | Errors |
|---|---|---|---|---|
| GET | `/` | — | `{enabled, profile, driver, transport, running, cdpReady, cdpHttp, pid, cdpPort, cdpUrl, chosenBrowser, detectedBrowser, detectedExecutablePath, detectError, userDataDir, color, headless, noSandbox, executablePath, attachOnly}` | 503 (server not started), 404 (profile not found), mapped browser errors |
| GET | `/profiles` | — | `{profiles: ProfileStatus[]}` | 500 |
| POST | `/start` | `{profile?}` | `{ok: true, profile}` | mapped browser errors |
| POST | `/stop` | `{profile?}` | `{ok, stopped, profile}` | mapped browser errors |
| POST | `/reset-profile` | `{profile?}` | `{ok, profile, moved, from, to?}` | 501 (remote profile), mapped errors |
| POST | `/profiles/create` | `{name, color?, cdpUrl?, userDataDir?, driver?}` | service-result | 400 (name required, bad driver), service errors |
| DELETE | `/profiles/:name` | — | service-result | 400 (name required), service errors |

#### 7.2 `routes/tabs.ts`

| Method | Path | Body / Query | Response | Errors |
|---|---|---|---|---|
| GET | `/tabs` | `?profile=` | `{running, tabs: BrowserTab[]}` | mapped |
| POST | `/tabs/open` | `{url, profile?}` | `BrowserTab` | 400 (url required), 400 (nav blocked), mapped |
| POST | `/tabs/focus` | `{targetId, profile?}` | `{ok: true}` | 400 (targetId required), 503 (not running), 404 (tab not found), 409 (ambiguous) |
| DELETE | `/tabs/:targetId` | — | `{ok: true}` | 400, 503, 404, 409 |
| POST | `/tabs/action` | `{action: "list"|"new"|"close"|"select", index?, profile?}` | depends on action | 400 (unknown action), 400 (index required for select), 404 (tab not found) |

#### 7.3 `routes/agent.snapshot.ts`

| Method | Path | Body / Query | Response | Errors |
|---|---|---|---|---|
| POST | `/navigate` | `{url, targetId?, profile?}` | `{ok, targetId, ...result}` (`result.url`, etc.) | 400 (url), nav-guard 400, mapped |
| POST | `/pdf` | `{targetId?, profile?}` | `{ok, path, targetId, url}` | 501 (existing-session), 501 (no Playwright) |
| POST | `/screenshot` | `{targetId?, fullPage?, ref?, element?, type? ("png"|"jpeg"), profile?}` | `{ok, path, targetId, url}` | 400 (fullPage+ref/element conflict), 400 (chrome-mcp + element) |
| GET | `/snapshot` | `?targetId&format&mode&labels&limit&maxChars&depth&...&profile=` | `{ok, snapshot, ...metadata}` | mapped |

#### 7.4 `routes/agent.act.ts` (and sub-routers)

| Method | Path | Body | Response | Errors |
|---|---|---|---|---|
| POST | `/act` | `{kind, ...action-fields, targetId?, profile?}` | per-kind: `{ok, targetId, [url], [result]}` | 400 (`ACT_KIND_REQUIRED`), 400 (`ACT_INVALID_REQUEST`), 400 (`ACT_SELECTOR_UNSUPPORTED`), 403 (`ACT_EVALUATE_DISABLED`), 403 (`ACT_TARGET_ID_MISMATCH`), 501 (`ACT_EXISTING_SESSION_UNSUPPORTED`) |
| POST | `/response/body` | `{url, targetId?, timeoutMs?, maxChars?, profile?}` | `{ok, targetId, response}` | 400 (url required), 501 (existing-session) |
| POST | `/highlight` | `{ref, targetId?, profile?}` | `{ok, targetId}` | 400 (ref required) |
| POST | `/hooks/file-chooser` | `{paths, ref?, inputRef?, element?, targetId?, timeoutMs?, profile?}` | `{ok}` | 400 (paths required), 400 (ref+inputRef/element conflict), 501 (chrome-mcp limits) |
| POST | `/hooks/dialog` | `{accept, promptText?, timeoutMs?, targetId?, profile?}` | `{ok}` | 400 (accept required), 501 (chrome-mcp + timeout) |
| POST | `/wait/download` | `{path?, targetId?, timeoutMs?, profile?}` | `{ok, targetId, download}` | 501 (existing-session/no Playwright) |
| POST | `/download` | `{ref, path, targetId?, timeoutMs?, profile?}` | `{ok, targetId, download}` | 400 (ref/path required), 501 |

#### 7.5 `routes/agent.debug.ts`

| Method | Path | Body / Query | Response | Errors |
|---|---|---|---|---|
| GET | `/console` | `?targetId&level&profile=` | `{ok, messages, targetId}` | 501 (no Playwright) |
| GET | `/errors` | `?targetId&clear&profile=` | `{ok, targetId, ...result}` | 501 |
| GET | `/requests` | `?targetId&filter&clear&profile=` | `{ok, targetId, ...result}` | 501 |
| POST | `/trace/start` | `{targetId?, screenshots?, snapshots?, sources?, profile?}` | `{ok, targetId}` | 501 |
| POST | `/trace/stop` | `{targetId?, path?, profile?}` | `{ok, targetId, path}` | 501, 400 (path traversal) |

#### 7.6 `routes/agent.storage.ts`

| Method | Path | Body / Query | Response | Errors |
|---|---|---|---|---|
| GET | `/cookies` | `?targetId&profile=` | `{ok, targetId, ...result}` | 501 |
| POST | `/cookies/set` | `{cookie: {name,value,url?,domain?,path?,expires?,httpOnly?,secure?,sameSite?}, targetId?, profile?}` | `{ok, targetId}` | 400 (cookie required), 501 |
| POST | `/cookies/clear` | `{targetId?, profile?}` | `{ok, targetId}` | 501 |
| GET | `/storage/:kind` | `?targetId&key&profile=` (`kind` ∈ local|session) | `{ok, targetId, ...result}` | 400 (bad kind), 501 |
| POST | `/storage/:kind/set` | `{key, value?, targetId?, profile?}` | `{ok, targetId}` | 400, 501 |
| POST | `/storage/:kind/clear` | `{targetId?, profile?}` | `{ok, targetId}` | 400, 501 |
| POST | `/set/offline` | `{offline, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/headers` | `{headers, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/credentials` | `{username, password, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/geolocation` | `{latitude, longitude, accuracy?, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/media` | `{media, profile?}` | `{ok}` | 501 |
| POST | `/set/timezone` | `{timezone, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/locale` | `{locale, targetId?, profile?}` | `{ok}` | 501 |
| POST | `/set/device` | `{device, targetId?, profile?}` | `{ok}` | 501 |

#### 7.7 Bridge-only

| Method | Path | Body / Query | Response | Errors |
|---|---|---|---|---|
| GET | `/sandbox/novnc` | `?token=` | HTML bootstrap document | 400 (token missing), 401 (auth not verified), 404 (token invalid) |

Total: **~46 endpoints**. The first-pass count of "~32" missed the storage emulation routes and the act sub-routes.

### 8. server-context state shape

#### TypeScript (canonical, from `server-context.types.ts`)

```ts
type ProfileRuntimeState = {
  profile: ResolvedBrowserProfile;                    // immutable resolved config
  running: RunningChrome | null;                       // null means we don't own a process
  lastTargetId?: string | null;                        // sticky tab selection
  reconcile?: {
    previousProfile: ResolvedBrowserProfile;
    reason: string;                                    // hot-reload trigger
  } | null;
};

type BrowserServerState = {
  server?: Server | null;                              // node:http Server (or null pre-listen)
  port: number;
  resolved: ResolvedBrowserConfig;
  profiles: Map<string, ProfileRuntimeState>;          // keyed by profile name
};
```

#### Python equivalents

```python
from dataclasses import dataclass, field
from typing import Optional
from asyncio import AbstractServer

@dataclass
class ReconcileMarker:
    previous_profile: "ResolvedBrowserProfile"
    reason: str

@dataclass
class ProfileRuntimeState:
    profile: "ResolvedBrowserProfile"
    running: Optional["RunningChrome"] = None
    last_target_id: Optional[str] = None
    reconcile: Optional[ReconcileMarker] = None

@dataclass
class BrowserServerState:
    port: int
    resolved: "ResolvedBrowserConfig"
    profiles: dict[str, ProfileRuntimeState] = field(default_factory=dict)
    server: Optional[AbstractServer] = None
```

**`lastTargetId`-fallback chain explicit** (from `selection.ts:62-71`):
1. Caller passes a `targetId` explicitly → resolve via `resolveTargetIdFromTabs`. If resolved → use it. If ambiguous → raise. If not found → raise `BrowserTabNotFoundError`.
2. Caller passes nothing →
   1. Try `profileState.lastTargetId`. If non-empty AND resolves cleanly (not ambiguous) → use it.
   2. Else: pick first tab with `type === "page"` (skip service workers / background pages).
   3. Else: pick `tabs[0]` (any tab).
   4. Else (no tabs): caller earlier opened `about:blank` defensively (line 44-46), so this branch shouldn't be reachable.
3. After picking, **always** stamp `profileState.lastTargetId = chosen.targetId` so the next call's hint is current.

`closeTab` deliberately does NOT update `lastTargetId` — leaves the previous hint in place, even though it now points to a closed tab. The next `ensureTabAvailable` call will see `lastResolved = null` (resolution fails → not ambiguous, just missing) and fall through to the `type==="page"` first-tab pick.

### 9. Lifecycle orderings

#### Startup (control server, end-to-end)

1. `loadConfig()` — parse the canonical config file. (out of scope)
2. `resolveBrowserConfig(cfg)` — produce `ResolvedBrowserConfig` with all profiles resolved. (out of scope)
3. **Bail if `resolved.enabled === false`**.
4. `ensureBrowserControlAuth({cfg, env})` — see §2 above.
   - **Failure mode**: returns empty auth. Server proceeds; `installBrowserAuthMiddleware` becomes a no-op. The operator must have explicitly chosen this (mode=password without password set, or SecretRef placeholder, or test mode).
5. `createBrowserRuntimeState({resolved, port, server: null, onWarn})`
   - Builds `BrowserServerState` with empty `profiles: Map`.
   - `ensureExtensionRelayForProfiles` — currently a no-op stub.
   - **Failure mode**: throws → startup aborts; nothing to clean up yet.
6. Build Express `app`.
7. `installBrowserCommonMiddleware(app)` — abort-signal, json parser, CSRF guard.
8. `installBrowserAuthMiddleware(app, auth)` — only if `auth.token || auth.password`.
9. `registerBrowserRoutes(app, ctx)` — basic + tabs + agent.
10. `app.listen(port, "127.0.0.1")` — wait for "listening" or "error" event.
    - **Failure mode**: port-in-use → reject the start promise, runtime-lifecycle's catch tears down the half-built state (clearState, no profiles to stop yet).
11. Stash `state.server = listenedServer`; update `state.port` from `address().port` (handles port=0).
12. `setBridgeAuthForPort(resolvedPort, auth)` — even for the control server, registry entry is registered (so any in-process consumer can look up auth by port).
13. Log "ready".

#### Shutdown (reverse order)

1. Stop accepting new requests — `server.close()` is *not yet called* — first we need to drain.
2. `stopKnownBrowserProfiles({getState, onWarn})` —
   - For each profile: if `profileState.running` is non-null → `stopOpenClawChrome(running)` (sends SIGTERM, falls back to SIGKILL after deadline).
   - Else → `ctx.forProfile(name).stopRunningBrowser()` for the chrome-mcp / cached-Playwright cleanup paths.
   - Errors per profile are swallowed (best-effort).
3. If `closeServer === true` and `state.server` exists → `await new Promise(r => server.close(r))`. **This blocks on in-flight requests**. The shutdown deadline is enforced **outside** this layer (the caller wraps the entire stop in a `Promise.race` with a timer).
4. `clearState()` — drops the `BrowserServerState` reference so handlers race-faulting after this point throw `"Browser server not started"`.
5. Lazy-import `pw-ai` and call `closePlaywrightBrowserConnection()` — only if `isPwAiLoaded()` (don't trigger the lazy import if it never happened).
6. `deleteBridgeAuthForPort(port)` — done inside `stopBrowserBridgeServer` for bridges; for the control server the registry entry persists for the process lifetime (acceptable since it dies on shutdown).

**What happens if step N fails**:
- Step 1-3 fail → caller decides; usually log+continue, since we want to keep tearing down.
- Step 4 (clearState) is synchronous and doesn't fail.
- Step 5 fails → swallowed (try/catch around `import`). Worst case: a Playwright connection survives until process exit.

**Force-close path** (shutdown deadline exceeded): the wrapping caller cancels via `AbortController`. In-flight requests' Playwright calls observe the abort and reject. The `server.close()` returns once the last connection drops. If the deadline is shorter than that, the caller proceeds without waiting — the OS will reap on process exit.

### 10. The dispatcher (`routes/dispatcher.ts`)

This is **not** the Express router — it's the in-process invocation path. Used when something inside the same Node process wants to invoke a route handler without going over HTTP (e.g., the loopback dispatch from a sandbox harness, or a unit test).

`createBrowserRouteDispatcher(ctx) -> {dispatch}` works as follows:

1. Build a tiny `BrowserRouteRegistrar` whose `get/post/delete` push entries into a flat list. Each entry stores `{method, path, regex, paramNames, handler}`.
2. `compileRoute(path)` parses path segments: `:name` → `([^/]+)` capture group, plain segment → escaped literal. Returns regex anchored with `^...$`.
3. `registerBrowserRoutes(registry.router, ctx)` — same call used by the HTTP server, so the same handlers are registered.
4. `dispatch({method, path, query?, body?, signal?}) -> {status, body}`:
   - Normalize path (ensure leading `/`).
   - Find first matching entry (linear scan; ~46 routes is fine).
   - Decode path params via `decodeURIComponent`; on `URIError` → `{status: 400, body: {error: "invalid path parameter encoding: <name>"}}`.
   - Build a `BrowserResponse` shim: `status(n)` mutates a local var, `json(body)` mutates another. No streaming.
   - Invoke handler with `{params, query, body, signal}`. On unhandled throw → `{status: 500, body: {error: String(err)}}`.
   - Return `{status, body: payload}`.

**Profile resolution from query/body**: not done by the dispatcher — that's `routes/utils.ts:getProfileContext`, called inside each handler via `resolveProfileContext`. Order is **query first, body fallback**. Empty/whitespace string → `undefined` → `forProfile()` uses default profile.

**Error normalization**: each handler is responsible for its own error mapping (via `ctx.mapTabError` and `toBrowserErrorResponse`). The dispatcher only catches uncaught throws as a 500 fallback. Validation errors return their own status codes (400, 403, 501) inside the handler.

For the Python port: FastAPI's router does what `compileRoute` + `dispatch` do, with proper async handling and OpenAPI metadata. Don't replicate the dispatcher; just use FastAPI routes. The one feature to preserve is **in-process invocation without HTTP** — for that, FastAPI+Starlette let you call routes via `TestClient` or directly via the underlying ASGI app.

### 11. Bridge vs control server — concrete differences

| Dimension | Control server | Bridge server |
|---|---|---|
| File | `server.ts` (out of scope, but mirrors the bridge) | `bridge-server.ts` |
| Lifetime | Long-lived, one per OpenClaw process | Ephemeral, per sandbox session |
| Port allocation | Configured (`gateway.controlPort` or env) | Dynamic (`port=0` → kernel assigns) |
| Auth source | Persisted in config (`gateway.auth.{token,password}`); loaded at startup, never re-read | Caller-supplied at `startBrowserBridgeServer({authToken, authPassword})`; thrown into in-memory `bridge-auth-registry` keyed by port |
| Auth bootstrap | `ensureBrowserControlAuth` — auto-gen + persist | Caller mints + passes; bridge-server **throws** if both empty |
| Bind | `127.0.0.1` only | `127.0.0.1` only (throws if non-loopback) |
| API surface | Identical (same `registerBrowserRoutes`) | Identical + optional `/sandbox/novnc` |
| Started by | `runtime-lifecycle.ts` from gateway startup | Sandbox lifecycle, on demand |
| Stopped by | `stopBrowserRuntime` | `stopBrowserBridgeServer` |
| Body limit | 1 MB (from common middleware) | 1 MB (same) |
| Sec-Fetch-Site | Loopback-only same-rules | Same |
| Has its own state? | Yes — owns one `BrowserServerState` | Yes — owns its own (separate Map of profiles) |

**Why per-port auth registry, not global**: multiple bridges can run concurrently (one per active sandbox), each with a different minted token. A global `currentBridgeAuth` would either (a) require all bridges to share a token (bad — token leak from one sandbox compromises all), or (b) require some mutex / "active bridge" concept. Per-port keying is the simplest correct model. The map lives in-memory only; bridges never survive a process restart so persistence is meaningless.

### 12. Edge cases

**Concurrent profile-create requests** (race on `POST /profiles/create`):
- The route delegates to `createBrowserProfilesService(ctx).createProfile({...})`.
- The service module reads + writes config; the writes go through `writeConfigFile` which writes-then-renames atomically.
- Two concurrent creates with different names: both succeed, both writes serialized at the rename boundary; final config has both.
- Two concurrent creates with the **same** name: race window between "exists?" check and write; one will overwrite the other. Mitigation in port: take a per-config-file lock around the read-modify-write sequence (`fcntl.flock` on Linux, `msvcrt.locking` on Windows, or a process-local `asyncio.Lock` if single-process).

**Reset-profile while a tab is open**:
- `resetProfile()` does (in order): kill cached Playwright connection if port reachable but not ours → stop running Chrome → close Playwright again → check user-data dir exists → `movePathToTrash(userDataDir)`.
- Open tabs in the running Chrome get their underlying process SIGTERM'd. Any in-flight `/screenshot` request observes the connection drop and rejects with a Playwright disconnect error. The `installBrowserCommonMiddleware` abort wiring fires → `mapTabError` translates → JSON error to client.
- The user-data dir is **moved to trash**, not deleted; recovery is possible for ~30 days via OS Trash.
- If the `movePathToTrash` itself fails (permission, disk full): the function throws; caller maps to 500.

**Auth bootstrap fails mid-startup**:
- The most common case: `writeConfigFile` fails (disk full, EROFS). `ensureBrowserControlAuth` propagates the error.
- Caller in `runtime-lifecycle` does NOT catch — startup fails.
- Whatever was built so far is GC'd (no listening server yet, no profiles started).
- Operator sees an error; restarts after fixing disk.

**Shutdown deadline exceeded**:
- The wrapping caller (in `gateway` lifecycle) typically gives the browser stop ~10s.
- `stopKnownBrowserProfiles` per-profile timeouts: SIGTERM, then SIGKILL after ~3-5s.
- `server.close()` waits for in-flight requests indefinitely.
- If the deadline fires first, the Promise that the caller is awaiting on is abandoned (no JS-level kill). The process exits via `process.exit()`. Connections drop ungracefully.

**Path traversal attempts in profile names**:
- `POST /profiles/create` with `name: "../../../etc/passwd"`:
  - `toStringOrEmpty` doesn't sanitize — passes the literal through.
  - The service module's `createProfile` is responsible for validating the name. If it accepts arbitrary strings, the user-data dir resolution could escape the profiles root.
  - Mitigation in port: validate `name` against `^[a-zA-Z0-9_-]{1,64}$` at the route layer.
- `DELETE /profiles/:name`:
  - `decodeURIComponent` runs in the dispatcher.
  - `name` is then passed to the service. Same validation requirement.
- `POST /reset-profile?profile=../foo`:
  - `getProfileContext` calls `forProfile(name)` which calls `resolveBrowserProfileWithHotReload`. If the name doesn't match a configured profile, throws `BrowserProfileNotFoundError` (404). So path traversal here just produces a 404 — the user-data-dir resolution is gated by config presence.
- Trace/download output paths: resolved via `resolveWritablePathWithinRoot` which **does** enforce `realpath(requested).startsWith(realpath(rootDir))`. Path traversal returns 400.

**Malformed URL in nav**:
- `POST /navigate` with `url: "not a url"`:
  - Route does `toStringOrEmpty(body.url)` → `"not a url"`.
  - 400 returned **only if** url is empty after trim. Otherwise passed through.
  - `assertBrowserNavigationAllowed` runs `new URL("not a url")` → throws `URIError` → catch → throws `InvalidBrowserNavigationUrlError("Invalid URL: not a url")`.
  - Route's catch maps to 400 via `handleRouteError`.
- `POST /navigate` with `url: "javascript:alert(1)"`:
  - URL parses (`javascript:` is a valid scheme).
  - Scheme check rejects (not http/https/about:blank) → 400.
- `POST /navigate` with `url: "http://127.0.0.1/"`:
  - Parses fine. Scheme OK. Strict-mode check: hostname is IP literal → skip. DNS resolve+IP block: 127.0.0.1 is loopback → blocked unless policy allows private network. → 400.

### 13. Python translation guide

#### FastAPI scaffolding

```python
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(AbortSignalMiddleware)         # propagate Request.is_disconnected to playwright
app.add_middleware(BodyLimitMiddleware, limit_bytes=1_000_000)
app.add_middleware(CSRFMiddleware)                # custom — see below
app.add_middleware(BrowserAuthMiddleware, auth=auth)
```

**Middleware order trap**: Starlette runs middlewares in **reverse** of `add_middleware()` calls. The first one added runs **outermost** (closest to network). So in the order above: BrowserAuth runs first on incoming requests, then CSRF, then body-limit, then abort-signal — which is the **opposite** of what we want.

To match OpenClaw's order (abort → body-parser → CSRF → auth on incoming), add in **reverse**: BrowserAuth first, then CSRF, then BodyLimit, then AbortSignal. Or use a single ASGI wrapper that imposes the explicit order. **Test this with a 401 vs 403 — a non-loopback unauthenticated request should hit 403, not 401.**

#### CSRF middleware (no off-the-shelf match)

```python
from urllib.parse import urlparse
from ipaddress import ip_address

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

def is_loopback_url(value: str) -> bool:
    if not value or value == "null":
        return False
    try:
        host = urlparse(value).hostname or ""
    except Exception:
        return False
    if host in LOOPBACK:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False

class CSRFMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            return await self.app(scope, receive, send)
        if scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        sec = headers.get("sec-fetch-site", "").lower()
        if sec == "cross-site":
            return await self.send_403(send)
        origin = headers.get("origin", "").strip()
        if origin:
            if not is_loopback_url(origin):
                return await self.send_403(send)
            return await self.app(scope, receive, send)
        referer = headers.get("referer", "").strip()
        if referer and not is_loopback_url(referer):
            return await self.send_403(send)
        return await self.app(scope, receive, send)
```

#### Auth middleware

```python
import hmac

def parse_bearer(auth: str) -> str | None:
    if not auth.lower().startswith("bearer "):
        return None
    return (auth[7:].strip()) or None

def parse_basic_password(auth: str) -> str | None:
    import base64
    if not auth.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:
        return None
    sep = decoded.find(":")
    if sep < 0:
        return None
    return decoded[sep + 1:].strip() or None

def is_authorized(req, auth: BrowserControlAuth) -> bool:
    h = req.headers.get("authorization", "").strip()
    if auth.token:
        b = parse_bearer(h)
        if b and hmac.compare_digest(b, auth.token):
            return True
    if auth.password:
        p = req.headers.get("x-openclaw-password", "").strip()
        if p and hmac.compare_digest(p, auth.password):
            return True
        bp = parse_basic_password(h)
        if bp and hmac.compare_digest(bp, auth.password):
            return True
    return False
```

#### Token gen + persistence

```python
import secrets
def generate_browser_control_token() -> str:
    return secrets.token_hex(24)   # 48 hex chars, 192 bits, identical to Node version
```

Persistence: write to `~/.opencomputer/<profile>/config.yaml` under `gateway.auth.token`. Use the existing config writer (atomic rename via `os.replace`). Re-read after write to defeat racing writers, exactly as the Node version does.

#### IP block list

```python
from ipaddress import ip_address

def is_blocked_ip(addr: str) -> bool:
    try:
        ip = ip_address(addr)
    except ValueError:
        return True   # unparseable → block
    return any([
        ip.is_private,      # 10/8, 172.16/12, 192.168/16, fd00::/8
        ip.is_loopback,     # 127/8, ::1
        ip.is_link_local,   # 169.254/16, fe80::/10
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,  # 0.0.0.0
    ])
```

#### Hostname pinning across redirects (httpx)

```python
import httpx

async def navigate_with_redirect_check(url: str, policy):
    chain = []
    async def hook(response):
        chain.append(str(response.url))
    async with httpx.AsyncClient(event_hooks={"response": [hook]}) as client:
        resp = await client.get(url, follow_redirects=True)
    for hop in chain:
        await assert_browser_navigation_allowed(hop, policy)
    return resp
```

For the actual browser navigation (not just HTTP fetch), Playwright's Python binding exposes `page.on("request", ...)` and `request.redirected_from` — same surface as the JS version, port the redirect-chain walker line-by-line.

#### Route dispatcher

Don't port `dispatcher.ts` — FastAPI's router does the same job with proper async, validation, and OpenAPI generation. The one feature to preserve is in-process invocation: use `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` to call routes without spinning a TCP server.

#### State shape (already shown in §8)

#### Lifecycle

Use `contextlib.AsyncExitStack` to compose the startup/shutdown:

```python
async with AsyncExitStack() as stack:
    cfg = await load_config()
    if not cfg.browser.enabled: return
    auth = await ensure_browser_control_auth(cfg)
    state = await create_browser_runtime_state(...)
    stack.push_async_callback(stop_browser_runtime, state)
    server = await asyncio.start_server(..., host="127.0.0.1")
    stack.push_async_callback(close_server, server)
    set_bridge_auth_for_port(state.port, auth)
    stack.callback(delete_bridge_auth_for_port, state.port)
    # serve until cancelled
    await server.serve_forever()
```

The `AsyncExitStack` ensures shutdown happens in reverse order even if startup throws partway through — direct equivalent to the Node `try/finally` ladder.

#### Single security test to write first

```python
def test_csrf_blocks_cross_site_post(client):
    r = client.post("/start", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403

def test_csrf_blocks_non_loopback_origin(client):
    r = client.post("/start", headers={"Origin": "https://evil.com"})
    assert r.status_code == 403

def test_csrf_allows_loopback_origin(client_with_auth):
    r = client_with_auth.post("/start", headers={"Origin": "http://127.0.0.1:18789"})
    assert r.status_code == 200

def test_auth_blocks_no_creds(client):
    r = client.get("/")
    assert r.status_code == 401

def test_nav_guard_blocks_private_ip(client_with_auth):
    r = client_with_auth.post("/navigate", json={"url": "http://10.0.0.1/"})
    assert r.status_code == 400

def test_nav_guard_blocks_non_http_scheme(client_with_auth):
    r = client_with_auth.post("/navigate", json={"url": "javascript:alert(1)"})
    assert r.status_code == 400
```

These six tests cover the three perimeter layers (CSRF, auth, SSRF). Pass them and the security perimeter is ported correctly.
