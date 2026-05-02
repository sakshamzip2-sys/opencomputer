# OpenClaw browser — client + tabs + utilities

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; JIT deeper read of the named files when porting.

## One-line summary

The "client" is the agent-side library that hits the control server's HTTP routes — auth-attached, per-call timeouts, error-translated — plus a per-session tab registry that auto-closes the agent's tabs at session end.

## Client API surface (~40 functions across 5 layers)

### Layer 1 — Profile lifecycle (7)
`browserStatus`, `browserStart`, `browserStop`, `browserProfiles`, `browserCreateProfile`, `browserDeleteProfile`, `browserResetProfile`

### Layer 2 — Tab management (5)
`browserTabs`, `browserOpenTab`, `browserFocusTab`, `browserCloseTab`, `browserTabAction`

### Layer 3 — Snapshot (1)
`browserSnapshot` — `GET /snapshot` — returns ARIA-or-AI tree + refs

### Layer 4 — Actions (7)
- `browserAct` — polymorphic, dispatches click/type/hover/drag/fill/select/wait/evaluate/press/close/batch
- `browserNavigate`
- `browserScreenshotAction`
- `browserArmDialog`, `browserArmFileChooser`
- `browserWaitForDownload`, `browserDownload`

### Layer 5 — State + observation (20+)
Cookies, localStorage, sessionStorage, headers, geolocation, device emulation, timezone, locale, console messages, errors, requests, tracing.

## client-fetch.ts (HTTP plumbing)

Every call goes through one helper. Behavior:

- **Auth injection**: for loopback URLs (`127.0.0.1`/`localhost`), attach `Authorization: Bearer <token>` or `X-OpenClaw-Password: <pw>` from config. Non-loopback calls reject (defense in depth).
- **Timeout**: per-call, 1.5–20s depending on operation. Snapshot/screenshot at the high end; status/profiles low end.
- **Retry**: very limited — only on connection errors (refused, reset). No retries on 4xx/5xx (those are real errors the agent should see).
- **Error translation**: maps HTTP status + error body into typed errors (`errors.ts`):
  - 401 → AuthError
  - 403 → PolicyError (request-policy reject)
  - 404 → NotFoundError (e.g. tab not found)
  - 409 → ConflictError (e.g. browser already running)
  - 429 → RateLimitError (with hint message from `rate-limit-message.ts`)
  - 5xx → ServerError

## Session tab registry (`session-tab-registry.ts`)

Memory-only registry: `Map<sessionKey, Map<trackedId, { targetId, baseUrl, profile, trackedAt }>>`.

**Track**: every `browserOpenTab` call (in tool layer) calls `trackSessionBrowserTab({ sessionKey, targetId, baseUrl, profile })`.

**Untrack**: explicit close via `browserCloseTab` removes the entry.

**Cleanup on session end**: `closeTrackedBrowserTabsForSessions([sessionKey, ...])`:
1. Removes all entries for given session keys (atomic).
2. POSTs `/tabs/{targetId}` close for each.
3. Swallows "tab not found" — they may already be closed by the user.
4. Returns count of successfully closed.

This is what makes the cleanup contract work: agent opens 5 tabs during a session, those 5 tabs close at session end without the agent having to remember.

## Error model (`errors.ts`)

Errors are *thrown* (not returned). The control server returns JSON `{ error: { code, message } }`; client maps to typed error class.

Rate-limit messages are friendly: include a hint about how long to wait, derived from `Retry-After` header.

## Utility modules (one-liners)

- **form-fields.ts** — normalize `{ ref, type, value }` objects for bulk `fill` actions. Type-coercion (string → array for multi-select), validates required fields.
- **proxy-files.ts** — decodes base64 file payloads from the node-host proxy response, saves them under the local media store, rewrites paths in the result so the agent sees a real path.
- **target-id.ts** — resolves a user-provided prefix (e.g., `"ba"`) to a full targetId. Case-insensitive; ambiguous prefixes throw.
- **paths.ts** — `resolveExistingPathsWithinRoot()` validates paths stay inside a root dir; rejects symlinks that escape.
- **safe-filename.ts** — sanitize download filenames: cap at 200 chars, strip control chars, replace path separators.
- **output-atomic.ts** — atomic file write (write to sibling tmp, rename).
- **trash.ts** — `movePathToTrash()` — macOS `~/.Trash` + Linux `trash-cli` shell-out. Used when wiping a profile so it's recoverable.
- **rate-limit-message.ts** — shape the rate-limit hint shown to the agent.

## proxy-files (the node-host bridge case)

When `target=node`, results may include files (screenshots, downloads) the remote node generated. Wire format includes them as base64 in the response. `persistBrowserProxyFiles()` decodes + writes them locally; `applyBrowserProxyPaths()` walks the result tree and rewrites embedded paths so they point at the local file the agent can `Read`.

## Porting concerns for Python

- **httpx** is the natural mapping for client-fetch: native async, per-call timeouts, request hooks (for auth injection), event hooks (for status-based error translation).
- **Auth header injection on loopback only**: filter by URL host before attaching.
- **Error class hierarchy**: pure Python (Exception subclasses) — straightforward.
- **Session tab registry**: dict of dicts, `weakref.WeakValueDictionary` if we want auto-cleanup on session GC; otherwise explicit removal.
- **Symlink validation in `paths.py`**: `os.path.realpath()` + `commonpath()` check.
- **Atomic write**: `os.replace()` (atomic on POSIX and Windows ≥ Vista).
- **Trash on macOS**: `osascript -e 'tell application "Finder" to delete POSIX file ...'`, or `send2trash` library.
- **Form-field normalization**: dict munging.

## Open questions

- Does OpenComputer's existing session model (the SQLite session DB in `agent/state.py`) provide a `sessionKey` we can hook the tab registry into, or do we need a new id?
- Per-call timeout values — copy verbatim or rederive from latency budgets?
- Retry policy: keep "no retry on 4xx/5xx", or add a special case for 429 with backoff?
- Trash vs hard-delete on profile reset: keep the safety net or simplify?

---

## Deep second-pass — function-by-function

> Captured 2026-05-03 from a line-by-line read of the 20 named files under
> `/Users/architsakri/Downloads/Harnesses/openclaw-main/extensions/browser/src/browser/`.
> Corrects/refines the skeleton above where the first pass over-simplified.

### A. Top-level corrections to the skeleton

The skeleton needs three corrections that ripple through the rest:

1. **The "client" has TWO transports, not one.** `fetchBrowserJson` (`client-fetch.ts:219-321`) checks `isAbsoluteHttp(url)` and either does a real `fetch()` or routes the call **in-process** through `dispatchBrowserControlRequest` from `local-dispatch.runtime.js`. The dispatcher path is taken when the URL is path-only (`/tabs`, `/snapshot?…`). This is how the same client code works in two contexts: agent talking to a remote gateway daemon (HTTP) and agent embedded in the same node process (in-memory dispatch). Skeleton implied HTTP-only.
2. **The error model in `errors.ts` is the SERVER-side error hierarchy** thrown inside the control service, with `toBrowserErrorResponse()` converting them to `{status, message}` for the wire. The CLIENT-side error surface is just `BrowserServiceError` + plain `Error` with appended hints — there is **no** `AuthError` / `PolicyError` / `ConflictError` etc. as the skeleton asserted. Statuses are mapped only for **rate-limit (429)** specially; everything else is a generic `BrowserServiceError(message)`.
3. **The Layer-5 surface enumerated in the skeleton is real but small.** The full state-side count is ~13 functions in `client-actions-state.ts` plus 7 in `client-actions-observe.ts`. There is no "errors", "requests", "console" tracing API at the level of granularity the skeleton claimed — they exist but are observe-mode reads only, not subscriptions.

### B. Function table per file

Format: `name(args) -> ret  /  HTTP  /  callers  /  gotchas`. All filenames refer to the directory above; line numbers are exact.

#### `client.ts` (335 lines — profile + tab + snapshot surface)

| Fn | Sig (abridged) | HTTP | Notes |
|---|---|---|---|
| `buildProfileQuery` `:80` | `(profile?) -> "?profile=..." \| ""` | n/a | Local helper, dup of `client-actions-url.ts:1`. Both exist because `client.ts` predates the actions split. |
| `withBaseUrl` `:84` | `(baseUrl?, path) -> string` | n/a | Strips trailing `/` from baseUrl, then concats. If baseUrl absent, returns path unchanged → triggers dispatcher branch. |
| `browserStatus` `:92` | `(baseUrl?, {profile}) -> BrowserStatus` | `GET /` | Timeout 1500ms. Lowest-budget call (used by the "is the daemon alive?" doctor check). |
| `browserProfiles` `:102` | `(baseUrl?) -> ProfileStatus[]` | `GET /profiles` | 3000ms. Unwraps `{profiles: [...]}` envelope. |
| `browserStart` `:112` | `(baseUrl?, {profile}) -> void` | `POST /start` | 15000ms — Chrome cold-start budget. |
| `browserStop` `:120` | `(baseUrl?, {profile}) -> void` | `POST /stop` | 15000ms. |
| `browserResetProfile` `:128` | `(baseUrl?, {profile}) -> {ok, moved, from, to?}` | `POST /reset-profile` | 20000ms — wipes profile dir (or trashes it). |
| `browserCreateProfile` `:153` | `(baseUrl, {name, color?, cdpUrl?, userDataDir?, driver?}) -> CreateResult` | `POST /profiles/create` | 10000ms. JSON body. |
| `browserDeleteProfile` `:186` | `(baseUrl, profile) -> DeleteResult` | `DELETE /profiles/{name}` | 20000ms. URL-encoded profile name. |
| `browserTabs` `:199` | `(baseUrl?, {profile}) -> BrowserTab[]` | `GET /tabs` | 3000ms. Unwraps `{running, tabs: [...]}`. |
| `browserOpenTab` `:211` | `(baseUrl, url, {profile}) -> BrowserTab` | `POST /tabs/open` | 15000ms — must wait for nav. |
| `browserFocusTab` `:225` | `(baseUrl, targetId, {profile}) -> void` | `POST /tabs/focus` | 5000ms. |
| `browserCloseTab` `:239` | `(baseUrl, targetId, {profile}) -> void` | `DELETE /tabs/{targetId}` | 5000ms. URL-encoded targetId. Called by registry cleanup. |
| `browserTabAction` `:251` | `(baseUrl, {action, index?, profile}) -> unknown` | `POST /tabs/action` | 10000ms. The "list/new/close/select by index" sugar. |
| `browserSnapshot` `:271` | `(baseUrl?, snapshotOpts) -> SnapshotResult` | `GET /snapshot?...` | 20000ms — most expensive call. Builds query manually with 11 optional params. |

Gotchas: `client.ts` exports `BrowserStatus`, `ProfileStatus`, `BrowserResetProfileResult`, `BrowserCreateProfileResult`, `BrowserDeleteProfileResult`, `SnapshotResult` types — duplicated re-exports of `client.types.ts` plus inlined extras. Port: keep type defs in one place (Python pydantic models).

#### `client.types.ts` (20 lines)

Three exported types only: `BrowserTransport = "cdp" | "chrome-mcp"`, `BrowserTab` (5 fields), `SnapshotAriaNode` (7 fields). Direct port to dataclasses/pydantic.

#### `client-actions.ts` (5 lines)

Pure barrel re-export of the four action submodules. Python: a single `client_actions.py` or `__init__.py` re-export.

#### `client-actions-core.ts` (195 lines)

| Fn | Sig | HTTP | Notes |
|---|---|---|---|
| `postDownloadRequest` `:28` (private) | `(baseUrl?, route, body, profile?) -> BrowserDownloadResult` | varies | 20000ms. Shared body shape for `/wait/download` + `/download`. |
| `browserNavigate` `:43` | `(baseUrl?, {url, targetId?, profile?}) -> {ok, targetId, url?}` | `POST /navigate` | 20000ms. Distinct from `browserOpenTab` — navigates an existing tab. |
| `browserArmDialog` `:60` | `(baseUrl?, {accept, promptText?, targetId?, timeoutMs?, profile?}) -> {ok}` | `POST /hooks/dialog` | 20000ms. Pre-arms an alert/confirm/prompt response. |
| `browserArmFileChooser` `:84` | `(baseUrl?, {paths, ref?, inputRef?, element?, targetId?, timeoutMs?, profile?}) -> {ok}` | `POST /hooks/file-chooser` | 20000ms. Stages files for the next file-input click. |
| `browserWaitForDownload` `:112` | `(baseUrl?, {path?, targetId?, timeoutMs?, profile?}) -> BrowserDownloadResult` | `POST /wait/download` | 20000ms wrapper timeout, but `timeoutMs` in body is the inner wait. |
| `browserDownload` `:133` | `(baseUrl?, {ref, path, ...}) -> BrowserDownloadResult` | `POST /download` | 20000ms. Direct download by ref. |
| `browserAct` `:156` | `(baseUrl?, BrowserActRequest, {profile?}) -> BrowserActResponse` | `POST /act` | 20000ms. Polymorphic on `req.kind`. |
| `browserScreenshotAction` `:170` | `(baseUrl?, {targetId?, fullPage?, ref?, element?, type?, profile?}) -> BrowserActionPathResult` | `POST /screenshot` | 20000ms. |

Gotcha: `BrowserActResponse.results: Array<{ok, error?}>` is populated only for `kind:"batch"`. Single-action calls leave `results` undefined and use `result: unknown`.

#### `client-actions-observe.ts` (185 lines)

| Fn | HTTP | Notes |
|---|---|---|
| `buildQuerySuffix` `:10` (private) | n/a | Builds `?k=v&...` from `[key, value]` pairs, dropping empty strings/undefined; booleans coerce to `"true"`/`"false"`. |
| `browserConsoleMessages` `:25` | `GET /console` | Optional `level`, `targetId`, `profile`. Returns `{ok, messages, targetId}`. |
| `browserPdfSave` `:41` | `POST /pdf` | Saves current page to PDF; returns `{ok, path, targetId, url?}`. |
| `browserPageErrors` `:54` | `GET /errors` | Optional `clear=true` drains the buffer. |
| `browserRequests` `:70` | `GET /requests` | Optional `filter` (server-side substring), `clear`. |
| `browserTraceStart` `:92` | `POST /trace/start` | Toggles screenshots/snapshots/sources. |
| `browserTraceStop` `:116` | `POST /trace/stop` | Returns saved trace path. |
| `browserHighlight` `:129` | `POST /highlight` | Visual highlight via ref — debug-only. |
| `browserResponseBody` `:142` | `POST /response/body` | Match-by-URL. Has both inner `timeoutMs` (wait for response) and outer 20000ms wrapper. `maxChars` truncates. |

Gotcha: `console`/`errors`/`requests` are GET with query strings; everything that mutates browser state is POST. Symmetric with REST norms.

#### `client-actions-state.ts` (279 lines)

Two private helpers:
- `buildStateQuery` `:24` — like `buildQuerySuffix` but only for `targetId`/`key`/`profile`.
- `postProfileJson<T>` `:39` and `postTargetedProfileJson` `:52` — eliminate boilerplate for the 13 POSTs that all share `Content-Type: json`, 20000ms timeout, profile query, body merge.

| Fn | HTTP | Notes |
|---|---|---|
| `browserCookies` `:70` | `GET /cookies` | |
| `browserCookiesSet` `:82` | `POST /cookies/set` | Body: `{cookie: {...}}`. Cookie record passed through opaque. |
| `browserCookiesClear` `:97` | `POST /cookies/clear` | |
| `browserStorageGet` `:108` | `GET /storage/{local\|session}` | Optional key narrows to single entry. |
| `browserStorageSet` `:125` | `POST /storage/{local\|session}/set` | |
| `browserStorageClear` `:146` | `POST /storage/{local\|session}/clear` | |
| `browserSetOffline` `:157` | `POST /set/offline` | |
| `browserSetHeaders` `:168` | `POST /set/headers` | Replaces extra-headers map; pass `{}` to clear. |
| `browserSetHttpCredentials` `:183` | `POST /set/credentials` | `clear:true` removes. |
| `browserSetGeolocation` `:198` | `POST /set/geolocation` | `clear:true` removes. |
| `browserSetMedia` `:215` | `POST /set/media` | `colorScheme: "dark" \| "light" \| "no-preference" \| "none"`. `"none"` = unset emulation. |
| `browserSetTimezone` `:233` | `POST /set/timezone` | |
| `browserSetLocale` `:247` | `POST /set/locale` | |
| `browserSetDevice` `:258` | `POST /set/device` | Named devices ("iPhone 13" etc.). |
| `browserClearPermissions` `:269` | `POST /set/geolocation` (sic) | **Bug-or-feature**: re-uses `/set/geolocation` with `clear:true` to clear all permissions. Server-side handler must special-case. |

#### `client-actions-types.ts` (16 lines)

Four envelope types: `BrowserActionOk`, `BrowserActionTabResult`, `BrowserActionPathResult`, `BrowserActionTargetOk`. The whole API surface returns one of these or a custom-shaped result.

#### `client-actions-url.ts` (12 lines)

Two helpers (`buildProfileQuery`, `withBaseUrl`) — shared with `client.ts` (which has its own duplicates `:80`, `:84`).

#### `client-actions.types.ts` (88 lines)

Sole export: `BrowserActRequest` discriminated union with **13 variants** (`click`, `type`, `press`, `hover`, `scrollIntoView`, `drag`, `select`, `fill`, `resize`, `wait`, `evaluate`, `close`, `batch`). Plus `BrowserFormField = {ref, type, value?}`. Note `wait` has six mutually-soft-exclusive parameters (`timeMs`/`text`/`textGone`/`selector`/`url`/`fn`/`loadState`) — the server picks the first applicable one.

#### `client-fetch.ts` (326 lines) — see Section C below for full trace

#### `session-tab-registry.ts` (190 lines) — see Section D below

#### `errors.ts` (109 lines) — see Section E below

#### `paths.ts` (277 lines) — see Section F.1 below

#### `target-id.ts` (35 lines)

| Fn | Notes |
|---|---|
| `resolveTargetIdFromTabs(input, tabs[])` `:7` | 1) trim. 2) **exact match wins** (case-sensitive). 3) lowercase prefix match across all tabs. 4) If exactly one prefix match → `{ok, targetId: theFullId}`. 5) Zero → `{ok:false, reason:"not_found"}`. 6) Multi → `{ok:false, reason:"ambiguous", matches: [...]}`. |

Gotchas:
- The exact-match check happens BEFORE lowercase normalization, so a real-targetId-that-is-also-a-prefix-of-another collides correctly only when an exact match wins — i.e. user typing the full id is unambiguous even if it's a prefix of another id. (Test this in the port.)
- `normalizeLowercaseStringOrEmpty` is from `openclaw/plugin-sdk/text-runtime`; in Python use `.casefold()` for Unicode-correct lowering.

#### `output-atomic.ts` (52 lines)

| Fn | Notes |
|---|---|
| `buildSiblingTempPath(targetPath)` `:7` | `crypto.randomUUID()` + `sanitizeUntrustedFileName(basename)` → `.openclaw-output-<uuid>-<safeName>.part` in same directory. |
| `writeViaSiblingTempPath({rootDir, targetPath, writeTemp})` `:13` | (1) realpath the root (with fallback if doesn't exist). (2) realpath the target's **parent dir** (not the target itself, which may not exist). (3) verify relative path stays inside rootDir. (4) call `writeTemp(tempPath)`. (5) atomic rename via `writeFileFromPathWithinRoot` (which is `infra/fs-safe.ts`'s safe rename). (6) On failure, `fs.rm(tempPath)` cleanup. |

Gotchas:
- **No fsync.** Linux/macOS rename is atomic w.r.t. visibility but durability is on the filesystem. For OpenComputer, `os.replace()` is the equivalent — same atomicity guarantee.
- The temp path lives in the same directory as the target so the rename is intra-filesystem (cross-fs rename = copy+unlink, not atomic).
- Sanitization of the basename means the visible `.part` file is safe to glance at in a shell.

#### `safe-filename.ts` (28 lines)

`sanitizeUntrustedFileName(fileName, fallbackName)` `:4`:
1. `normalizeOptionalString` (trim → null if empty).
2. If empty → return fallback.
3. `path.posix.basename` → strips `/...`.
4. `path.win32.basename` → strips `\...`. Both passes catch path-traversal in mixed inputs.
5. Loop char-by-char: drop `< 0x20` (control chars including newline) and `0x7f` (DEL).
6. Trim again.
7. If `""` or `"."` or `".."` → fallback.
8. Cap at 200 chars (slice; no extension preservation — long filenames lose extensions).

Gotchas: no preservation of extension after the 200-char cap. Python port: use `pathlib.PurePosixPath(name).name` then `pathlib.PureWindowsPath(name).name`, control-char strip, length cap with extension preservation if we want better UX.

#### `trash.ts` (22 lines)

`movePathToTrash(targetPath)` `:7`:
1. Try `runExec("trash", [targetPath], {timeoutMs: 10000})` — Linux `trash-cli` or any `trash`-named tool.
2. On failure (no `trash` binary), fallback: `~/.Trash/<basename>-<Date.now()>` (macOS-native dir).
3. If destination exists, append `-<generateSecureToken(6)>` (hex token) for uniqueness.
4. `fs.renameSync` — the actual move.
5. Returns destination path.

Gotchas:
- macOS `~/.Trash` rename works only if same volume. External-volume files will throw on rename. Code does NOT fall back further.
- Linux fallback to `~/.Trash` is **wrong** — XDG says `~/.local/share/Trash/files/`. Acceptable on macOS but degraded UX on Linux without `trash-cli`. Port uses `send2trash` lib (handles all OSes correctly).

#### `form-fields.ts` (34 lines)

| Fn | Notes |
|---|---|
| `normalizeBrowserFormFieldRef(value)` `:8` | trim → "" if not a usable string. |
| `normalizeBrowserFormFieldType(value)` `:12` | trim → DEFAULT_FILL_FIELD_TYPE (`"text"`) if empty. |
| `normalizeBrowserFormFieldValue(value)` `:17` | Pass-through if `string\|number\|boolean`, else `undefined`. |
| `normalizeBrowserFormField(record)` `:23` | Required: `ref` (else null). Default `type="text"`. Drop `value` field entirely if undefined (vs setting to undefined). |

Gotcha: the docstring in the skeleton claimed "type-coercion (string → array for multi-select)". **That's wrong** — this module does no array coercion. Multi-select is handled at the `select` action variant (`values: string[]`), not the `fill` variant.

#### `proxy-files.ts` (40 lines)

| Fn | Notes |
|---|---|
| `persistBrowserProxyFiles(files?)` `:9` | For each `{path, base64, mimeType?}` file, base64-decode into a Buffer, call `saveMediaBuffer(buffer, mimeType, "browser")` to write into local media store. Returns `Map<originalRemotePath, localPath>`. |
| `applyBrowserProxyPaths(result, mapping)` `:22` | **Shallow walk only.** Only checks three known fields: `result.path`, `result.imagePath`, `result.download.path`. Mutates in place. Does NOT recurse into arrays or unknown sub-objects. |

Gotchas:
- This is a known limitation: any new server response that adds a path-bearing field would be missed. Port should generalize to a recursive walk OR enumerate all known path fields with type checks (latter is what we ship today).
- Mutates `result` in place. If we want immutability in Python, deep-copy first or build a new dict.

#### `rate-limit-message.ts` (32 lines)

`resolveBrowserRateLimitMessage(url)` `:27`:
1. If absolute http(s) URL with hostname `browserbase.com` or `*.browserbase.com` → "Browserbase rate limit reached (max concurrent sessions). Wait for the current session to complete, or upgrade your plan."
2. Otherwise → "Browser service rate limit reached. Wait for the current session to complete, or retry later."

Gotcha: **does NOT use the `Retry-After` header.** Skeleton's claim ("derived from Retry-After header") was wrong. Messages are static. Port: keep static or enrich, but don't promise something that isn't built.

### C. `client-fetch.ts` internals — exact trace

The client-fetch wrapper is the most complex single file in the subsystem. Below is a fully-resolved trace for two paths.

#### Setup constants

- `BROWSER_TOOL_MODEL_HINT` `:100` — appended to every error so the LLM doesn't loop on "browser unavailable" by retrying. Plain English. Port verbatim.
- `BrowserServiceError` `:13` — internal class for "service reachable, returned error" — must NOT be wrapped with "Can't reach…" suffixing. The class is private but its discriminator-like `instanceof` check is the only thing keeping the two error categories straight.

#### Auth injection (`withLoopbackBrowserAuthImpl` `:38`)

```
1. Headers init from init?.headers (preserving caller-supplied).
2. If 'authorization' OR 'x-openclaw-password' already set → return as-is.
   (Caller-supplied auth wins; never override.)
3. If !isLoopbackHttpUrl(url) → return as-is.
   (Loopback check uses isLoopbackHost from gateway/net.ts — accepts:
    127.0.0.0/8, ::1, ::ffff:127.0.0.1, "localhost".)
4. Try config-derived auth:
   a. cfg = loadConfig()
   b. auth = resolveBrowserControlAuth(cfg)
   c. If auth.token → headers.set('Authorization', `Bearer ${token}`)
   d. Else if auth.password → headers.set('x-openclaw-password', password)
   e. Catch + ignore.
5. Fallback: ephemeral bridge auth (sandbox child processes use this).
   a. parsed = new URL(url); port = parsed.port || (https ? 443 : 80)
   b. bridgeAuth = getBridgeAuthForPort(port)
   c. Same Bearer-or-X-OpenClaw-Password choice.
   d. Catch + ignore.
6. Return {...init, headers}.
```

The `isAbsoluteHttp` guard at `client-fetch.ts:26` (`/^https?:\/\//i`) drives the dispatcher-vs-HTTP fork at the top of `fetchBrowserJson`.

#### HTTP path (`fetchHttpJson` `:169`)

```
1. timeoutMs = init.timeoutMs ?? 5000.
2. ctrl = new AbortController().
3. If init.signal exists:
   a. If already aborted → ctrl.abort(reason).
   b. Else attach a listener that propagates abort. Cleanup in finally.
4. setTimeout(() => ctrl.abort(new Error('timed out')), timeoutMs).
5. fetchWithSsrFGuard({url, init, signal, policy: {allowPrivateNetwork: true}, auditContext: 'browser-control-client'})
   — this is the SSRF guard (allows loopback even though loopback is private).
6. release = guarded.release; res = guarded.response.
7. If !res.ok:
   a. If 429: discard body (no agent injection of upstream text!), throw
      BrowserServiceError(`${resolveBrowserRateLimitMessage(url)} ${HINT}`).
   b. Else: text = await res.text().catch(()=>''); throw
      BrowserServiceError(text || `HTTP ${res.status}`).
8. return await res.json() as T.
9. finally: clearTimeout, release(), unhook upstream signal listener.
```

Critical: 4xx and 5xx (other than 429) propagate the **server-supplied body text** as the error message. So `BrowserTabNotFoundError("tab not found")` thrown server-side becomes `BrowserServiceError("tab not found")` client-side. The client doesn't reconstruct the typed error class — callers have to `error.message.includes("tab not found")` to recover discriminator info. (See `session-tab-registry.ts:41-49` `isIgnorableCloseError` — same string-matching pattern.)

#### Dispatcher path (in-process)

```
1. Dynamic import ./local-dispatch.runtime.js (lazy, code-split).
2. Parse URL relative to http://localhost. Extract pathname + searchParams.
3. Convert string body via JSON.parse (best effort; fall back to raw).
4. Build a fresh AbortController. Mirror upstream signal as in HTTP path.
5. Race dispatchPromise vs abortPromise. Optional timeout.
6. result = {status, body}.
7. If status >= 400:
   a. 429 → BrowserServiceError(resolveBrowserRateLimitMessage(url) + HINT).
      (Note: again, no upstream body text reflection.)
   b. Else: prefer body.error if shape matches, else `HTTP ${status}`.
      → BrowserServiceError(message).
8. else: return result.body as T.
```

#### Error-wrapping policy

- `BrowserServiceError` thrown above → re-thrown as-is. **No "Can't reach…" wrapping.**
- Dispatcher path other errors → `enhanceDispatcherPathError`: keep original message, append operator hint + model hint. Preserves `cause`.
- HTTP path other errors (network, abort) → `enhanceBrowserFetchError`: prefix with "Can't reach the OpenClaw browser control service" (timeout-specific variant if message looks like a timeout). Append model hint.

Worked example — `browserStatus(undefined, {profile: 'work'})`:

1. `client.ts:96` builds path `/?profile=work`, calls `fetchBrowserJson(`/?profile=work`, {timeoutMs: 1500})`.
2. `client-fetch.ts:226` — `isAbsoluteHttp('/?profile=work')` → false. Set `isDispatcherPath = true`.
3. Dynamic-import `local-dispatch.runtime.js`. Parse pathname=`/`, query=`{profile: 'work'}`. body undefined.
4. AbortController + 1500ms setTimeout.
5. `dispatchBrowserControlRequest({method: 'GET', path: '/', query: {profile: 'work'}, body: undefined, signal})`.
6. If returned `{status: 200, body: {...}}` → return body cast as `BrowserStatus`.
7. If returned `{status: 404, body: {error: 'profile not found'}}` → `BrowserServiceError('profile not found')` thrown.
8. If dispatch throws (e.g. internal 500): `enhanceDispatcherPathError` wraps into Error with the original message + "Restart the OpenClaw gateway…" + model hint. Cause preserved.

Worked example — `browserStatus('http://127.0.0.1:18888', {profile: 'work'})`:

1. Path `http://127.0.0.1:18888/?profile=work`, timeout 1500ms.
2. `isAbsoluteHttp` → true. `withLoopbackBrowserAuth` injects Bearer token.
3. `fetchHttpJson` runs SSRF guard (allows loopback), real fetch with 1500ms abort.
4. 200 → return `await res.json()`.
5. 429 → discard body + rate-limit-message + hint → `BrowserServiceError`.
6. 500 with body "internal" → `BrowserServiceError('internal')`. Note: NOT wrapped with "Can't reach…".
7. ETIMEDOUT (network) → `enhanceBrowserFetchError`: "Can't reach the OpenClaw browser control service (timed out after 1500ms). Restart the OpenClaw gateway… Do NOT retry…".

**No retry logic exists** in this file. Skeleton claimed "retry-on-connection-error" — that was wrong. Every failure surfaces immediately. The model-hint exists precisely because there's no retry: tell the LLM not to loop.

### D. Session tab registry algorithms

Core data: `trackedTabsBySession: Map<string, Map<string, TrackedSessionBrowserTab>>` `:15`.

**Inner map key (`trackedId`)** is `${targetId} ${baseUrl ?? ""} ${profile ?? ""}` `:37`. NUL-byte separator avoids collision in any practical input. The composite key means the SAME `targetId` opened against different `baseUrl`s or `profile`s tracks independently — important when one agent run touches multiple gateways or profiles.

#### `trackSessionBrowserTab` `:51`

1. Trim raw `sessionKey` and `targetId`. Empty either → no-op (silent).
2. Normalize: sessionKey lowercased, targetId trim only (preserve case — Chrome target IDs are case-sensitive hex), baseUrl trim, profile lowercased.
3. Build `tracked = {sessionKey, targetId, baseUrl, profile, trackedAt: Date.now()}`.
4. Get-or-create the inner map; `trackedForSession.set(trackedId, tracked)` — last-write-wins on dup track.

Not atomic in the multi-threaded sense (Node single-threaded; `Map.set` is sync). For Python with asyncio: same — single-thread async = no lock needed unless we ever introduce threads.

#### `untrackSessionBrowserTab` `:82`

1. Trim raw values. No-op on empty.
2. Look up inner map. No-op if missing (idempotent).
3. Build `trackedId` and call `delete`. **Then** if inner map is now empty, delete the outer entry. This is atomic relative to `track` (single-threaded) but **races with a parallel `track`** in async land if two are awaited concurrently — there's no lock. Practical impact: tiny, since track is sync.

#### `closeTrackedBrowserTabsForSessions` `:142`

```
1. takeTrackedTabsForSessionKeys: dedupe keys, normalize-lowercase, then for
   each key:
   a. Read inner map.
   b. **DELETE outer entry FIRST** (`trackedTabsBySession.delete(sessionKey)`).
   c. THEN iterate values and push to `tabs[]` deduplicated by trackedId.
2. For each tab:
   a. await closeTab({targetId, baseUrl, profile})
      — defaults to browserCloseTab(baseUrl, targetId, {profile}).
   b. If success → counter++.
   c. If error AND `isIgnorableCloseError(err)` → swallow silently.
      Otherwise call onWarn(message).
3. Return total successful close count.
```

**Critical ordering:** registry mutation happens BEFORE the network call. This means:
- A concurrent `trackSessionBrowserTab` for the same session that races with the close will succeed and the new tab will leak (not closed). This is acceptable because session-end cleanup runs after the session is sealed.
- A concurrent `closeTrackedBrowserTabsForSessions` for the same session will get an empty list (idempotent — `tabs.length === 0 → return 0`).

`isIgnorableCloseError` `:41` — case-insensitive substring match against:
- `"tab not found"`
- `"target closed"`
- `"target not found"`
- `"no such target"`

These cover both the server-side `BrowserTabNotFoundError("tab not found")` from `errors.ts:48` and Chrome CDP error strings.

Test helpers `__resetTrackedSessionBrowserTabsForTests` `:176` and `__countTrackedSessionBrowserTabsForTests` `:180` are public-but-prefixed conventions. Port: `_reset_tracked_session_browser_tabs_for_tests` (PEP8) or move to a test fixture module.

### E. Error model in detail (`errors.ts`)

#### Hierarchy

```
Error
└── BrowserError                       (status: number; default 500)
    ├── BrowserCdpEndpointBlockedError       (400, msg: "browser endpoint blocked by policy")
    ├── BrowserValidationError               (400)
    ├── BrowserConfigurationError            (400)
    ├── BrowserResetUnsupportedError         (400)
    ├── BrowserTabNotFoundError              (404, default msg: "tab not found")
    ├── BrowserProfileNotFoundError          (404)
    ├── BrowserTargetAmbiguousError          (409, default msg: "ambiguous target id prefix")
    ├── BrowserConflictError                 (409)
    ├── BrowserProfileUnavailableError       (409)
    └── BrowserResourceExhaustedError        (507)
```

Plus two ambient errors handled in `toBrowserErrorResponse` `:83`:
- `SsrFBlockedError` from `infra/net/ssrf.js` → `{status: 400, message: "browser navigation blocked by policy"}`. (CDP-endpoint blocks are pre-converted to `BrowserCdpEndpointBlockedError`, so what reaches this branch is always navigation-target.)
- `InvalidBrowserNavigationUrlError` (or anything `name === "InvalidBrowserNavigationUrlError"`) → 400 with original message.
- `BlockedBrowserTargetError` (by name match) → 409.
- Anything else returns `null` (caller decides — usually treats as 500).

#### Status mapping (server emits → client sees)

| Status | Server class | Client-visible message |
|---|---|---|
| 400 | Validation/Config/CdpBlocked/ResetUnsupported/SSRF/InvalidNav | original |
| 404 | TabNotFound, ProfileNotFound | original |
| 409 | TargetAmbiguous, Conflict, ProfileUnavailable, BlockedBrowserTarget | original |
| 429 | (rate-limit middleware, not in errors.ts) | static rate-limit message + model hint |
| 500 | (default fallback) | original |
| 507 | ResourceExhausted | original |

Client-side discrimination is **string-matching the message** (only `isIgnorableCloseError` does this today, in `session-tab-registry.ts:41`). Skeleton's claim of typed error classes on the client side was wrong.

#### Rate-limit message (`rate-limit-message.ts`)

Inputs: just the URL. Logic:
- If absolute http(s) AND hostname is `browserbase.com` / `*.browserbase.com` → Browserbase-flavored message (mentions plan upgrade).
- Otherwise → generic message ("Wait for the current session to complete, or retry later.").

No use of:
- Response body (intentionally — log/agent-injection risk; see `client-fetch.ts:201` "Do not reflect upstream response text into the error surface").
- `Retry-After` header (just not implemented).
- Status semantics other than the URL host.

### F. Utility modules in depth

#### F.1 `paths.ts`

Three public layers:

**Lexical** (`resolvePathWithinRoot` `:87`) — sync, no FS access:
1. `path.resolve(rootDir)` → absolute.
2. `requestedPath.trim()`.
3. If empty: return `defaultFileName` join, or "path is required".
4. `path.resolve(root, raw)` → absolute resolved.
5. `path.relative(root, resolved)`. Reject if empty (= same as root), starts with `..`, or is absolute (Windows different-drive case).
6. Return `{ok, path: resolved}`.

**Existence-checked, can return non-existent** (`resolveExistingPathsWithinRoot` `:169`):

Wraps `resolveCheckedPathsWithinRoot({allowMissingFallback: true})`. Per requested path:
1. `realpath(rootDir)`. If root doesn't exist, leave undefined and continue (legacy compat).
2. `resolvePathWithinRoot` first — if lexical OK, take that.
3. ELSE if lexical failed AND root real-resolved AND raw is absolute: try `realpath(raw)` and check it's inside `rootRealPath`. (This handles the case where `requestedPath` is an absolute symlink target that lands inside the root.)
4. With the `relativePath`, call `openFileWithinRoot({rootDir, relativePath})` — this is `infra/fs-safe.ts`'s tied-to-root-by-fd open. It opens the file with O_NOFOLLOW-equivalent semantics, ensures non-symlink, ensures regular file, ensures inside root.
5. Push `opened.realPath`. Close handle in finally.
6. **Failure modes**:
   - `SafeOpenError("not-found")` AND allowMissingFallback → push `pathResult.fallbackPath` (lexical resolved). This is what makes "save to a path that doesn't exist yet" work.
   - `SafeOpenError("outside-workspace")` → `{ok:false, error: "File is outside <scopeLabel>"}`.
   - Other → `{ok:false, error: "Invalid path: must stay within <scope> and be a regular non-symlink file"}`.

**Strict** (`resolveStrictExistingPathsWithinRoot` `:180`) — same as above but `allowMissingFallback: false`. Used for "must already exist" callers.

**Writable** (`resolveWritablePathWithinRoot` `:109`) — combines:
1. Lexical resolve.
2. `resolveTrustedRootRealPath` `:49`: lstat root, ensure directory and **not a symlink** itself, then realpath.
3. `validateCanonicalPathWithinRoot` for parent dir (must be directory, not symlink, not nlink>1).
4. Same check for target (must be file or not-exist).
5. `nlink > 1` rejection on file targets prevents hard-link escapes.

**Symlink stance**:
- `validateCanonicalPathWithinRoot` `:61`: lstat says symlink → `"invalid"` immediately. So symlinks anywhere in the path are rejected unless they're the legacy `realpath(absolute)` branch in `resolveCheckedPathsWithinRoot`.
- This is **stricter** than the skeleton claimed ("rejects symlinks that escape"). The library actually rejects most symlinks even if they would resolve safely. Trade-off: zero TOCTOU risk vs developer surprise on systems that symlink `/tmp`.

**Case sensitivity**: relies on the FS. macOS HFS+/APFS is case-insensitive by default → two paths that differ only in case will share the same real path; the lexical check would see them as different, but the realpath check unifies them. Linux is case-sensitive throughout.

**Trailing slash**: `path.relative` strips it; `path.resolve` strips it. Trailing-slash inputs treated as the directory.

#### F.2 `target-id.ts`

Already covered above. Gotcha clarification: the **lowercase prefix match** is checked against `lower = needle.toLowerCase()` and each `id.toLowerCase().startsWith(lower)`. So `"BAaaa"` would match `"baAAA1234"` (case-insensitive prefix), but `"baaaa"` would also match. Substring match is **NOT** done — only prefix.

#### F.3 `output-atomic.ts`

Detailed earlier. Sequence:
1. realpath root (best-effort).
2. realpath parent of target (so dir-symlink is resolved before path build), keep basename.
3. relpath check inside root.
4. Build sibling temp path with UUID + sanitized basename.
5. Caller writes to the temp path (caller chooses fs API: write, pipe, etc.).
6. Call into `writeFileFromPathWithinRoot({mkdir: false})` — this does the rename atomically inside fs-safe.
7. On any error before step 6 succeeds, `fs.rm(tempPath, {force: true})` cleans up — best effort, errors swallowed.

No `fsync` of the temp file or directory. The atomic-rename guarantee is only for visibility, not durability across crash. For OpenComputer's threat model (interrupted process, not power loss), fine.

#### F.4 `safe-filename.ts`

Detailed earlier. Important: the "control char" predicate is `code < 0x20 || code === 0x7f`. This drops:
- NUL, SOH, STX, …, US (0x00–0x1F).
- DEL (0x7F).
- Does NOT drop high-bit characters or multi-byte UTF-8 (preserves Unicode names).
- Does drop newlines (LF=0x0A, CR=0x0D inside the range) — important for log injection avoidance.

#### F.5 `trash.ts`

Detailed earlier. Worth noting: `runExec` is from `process/exec.js` — wraps `child_process.spawn` with timeout. So macOS systems WITHOUT `trash` binary fall back to `~/.Trash/<base>-<timestamp>` direct rename.

Python port flow:
```python
try:
    send2trash(path)        # platform-aware: macOS Finder, Linux gio/trash-cli, Windows recycle bin
    return path
except (FileNotFoundError, send2trash.TrashPermissionError):
    # fallback rename to ~/.Trash on macOS or ~/.local/share/Trash/files on Linux
```

The `send2trash` library handles the platform branches that OpenClaw's manual rename misses.

#### F.6 `form-fields.ts`

Already detailed. Put in plain Python:
```python
def normalize_form_field(record: dict) -> Optional[dict]:
    ref = (record.get("ref") or "").strip()
    if not ref:
        return None
    type_ = (record.get("type") or "").strip() or "text"
    value = record.get("value")
    if not isinstance(value, (str, int, float, bool)):
        value = None
    return {"ref": ref, "type": type_, **({"value": value} if value is not None else {})}
```

(Note: Python's `bool` is a subclass of `int`, so the `isinstance` order matters if we ever care.)

#### F.7 `proxy-files.ts`

Detailed earlier. Two key ports:
1. `persistBrowserProxyFiles`: take base64 string → `base64.b64decode()` → write to `media/store.py`'s `save_media_buffer(buf, mime, "browser")` → return `{remote_path: local_path}` dict.
2. `applyBrowserProxyPaths`: shallow walker that mutates three known fields (`path`, `imagePath`, `download.path`). Recommended Python port: enumerate known fields up front, but ALSO add a depth-1 recursion into list values (the current TS does not — adopting it would prevent silent drops on future server changes).

Risk: this code mutates `result` in place. In Python, prefer building a fresh dict (immutability easier to reason about); cost is just a deep-copy at this layer.

### G. `client-actions-*` modules — purpose split

The actions files exist as a **single logical module** that was split for file-size/cohesion. The boundaries:

- **`client-actions-core.ts`** — actions that "do something" to the browser. Navigate, screenshot, dialog/file-chooser arming, downloads, the polymorphic `act` (click/type/etc.). Roughly: state-MUTATING actions that the agent invokes intentionally.

- **`client-actions-observe.ts`** — read-only-ish reads of accumulated state. Console buffer, page errors, network requests, response bodies. Plus tracing (start/stop) which is technically mutating but lives here because traces are observation infrastructure. Plus `pdf` and `highlight` (debug aids).

- **`client-actions-state.ts`** — emulation/configuration state. Cookies, storage, headers, geolocation, timezone, locale, device emulation, offline, http-credentials, color-scheme, permissions. All idempotent setters/clearers; no act-like side effects on the page itself.

- **`client-actions-types.ts`** — the four envelope return types (`BrowserActionOk`, `BrowserActionTabResult`, `BrowserActionPathResult`, `BrowserActionTargetOk`).

- **`client-actions-url.ts`** — the two helpers (`buildProfileQuery`, `withBaseUrl`).

- **`client-actions.types.ts`** — `BrowserActRequest` discriminated union + `BrowserFormField`. Note the `.types.ts` (vs `-types.ts`) distinction — historical inconsistency.

The boundary between core and state isn't airtight (e.g. dialog/file-chooser arming could be argued as state). Python port: collapse into one `client/actions.py` with logical sections; or keep three modules (`actions_core.py`, `actions_observe.py`, `actions_state.py`) if file size warrants.

### H. Python translation guide

**HTTP layer (`client_fetch.py`)** — use `httpx.AsyncClient`:

- **Auth header injection on loopback**: `httpx` event hook on `request`. Inspect `request.url.host` against `ipaddress.ip_address(host).is_loopback` (after trying to resolve `localhost` → `127.0.0.1`). Inject `Authorization: Bearer <token>` only on loopback. Plus skip if header already present (caller wins).
  ```python
  import ipaddress
  def _is_loopback_host(host: str) -> bool:
      if host in ("localhost",):
          return True
      try:
          return ipaddress.ip_address(host).is_loopback
      except ValueError:
          return False
  ```
- **Per-call timeouts**: `httpx.Timeout(connect=…, read=…, write=…, pool=…)` per request, NOT on the client (per-call differs from 1500ms to 20000ms). Pass `timeout=` to each `client.request()`.
- **Abort signal propagation**: callers pass an `asyncio.CancelledError` task; httpx supports cancellation natively if the awaiter is cancelled. Also support an explicit `cancel_token` if we adopt the trio-style.
- **Status mapping**: don't bother with a generic event hook — do it in a thin wrapper. 429 → custom `BrowserRateLimitError(rate_limit_message(url))`. 4xx/5xx → `BrowserServiceError(text)`. Connection error → `BrowserUnreachableError(...)`.
- **No retries.** Match the TS exactly.
- **Errors module**: server-side classes mirror `errors.ts`. Status-to-class map at the route boundary; client-side just sees `BrowserServiceError`.

**Path validation (`paths.py`)**: 
- `os.path.realpath()` for symlink resolution.
- `os.path.commonpath([root_real, target_real])` to verify containment — equivalent to `path.relative` + `..` check but more readable.
- Equivalent of `openFileWithinRoot`: open with `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)` then `fstat` to verify regular file + nlink and `os.path.realpath('/proc/self/fd/<fd>')` (Linux) or kernel-equivalent on macOS to confirm post-open path. Or use `pathlib` + symlink rejection upfront.

**Output atomicity (`output_atomic.py`)**: `tempfile.NamedTemporaryFile(dir=parent, delete=False)` + `os.replace(tmp.name, target)`. Atomic on POSIX and Windows ≥ Vista within a single filesystem.

**Trash (`trash_module.py`)**: `from send2trash import send2trash`. Handles macOS Finder API, Linux gvfs/trash-cli/XDG, Windows recycle bin. Falls back to a manual rename to `~/.Trash` only if `send2trash` raises.

**Form fields (`form_fields.py`)**: dict normalization with `(record.get("ref") or "").strip()` style. Type-check `value` with `isinstance(value, (str, int, float, bool))` — careful that `bool` is a subclass of `int`.

**Filename sanitization (`safe_filename.py`)**: `pathlib.PurePosixPath(name).name` then `pathlib.PureWindowsPath(name).name`, control-char filter via `''.join(c for c in s if ord(c) >= 0x20 and ord(c) != 0x7f)`, length cap with extension preservation:
```python
if len(name) > 200:
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 10:
        name = stem[:200 - len(dot) - len(ext)] + dot + ext
    else:
        name = name[:200]
```

**Session tab registry (`session_tab_registry.py`)**: `dict[str, dict[str, TrackedTab]]`, single `asyncio.Lock` if we ever go multi-event-loop, otherwise no lock needed. Use `dataclasses.dataclass(frozen=True)` for `TrackedTab`. The composite-key approach (NUL-separated) becomes a tuple `(target_id, base_url or "", profile or "")` — Python tuples are hashable and clearer than string concatenation.

**Proxy files (`proxy_files.py`)**: `base64.b64decode` + `media.store.save_media_buffer(buf, mime, "browser")`. For path rewriting, prefer a recursive walk over the result tree:
```python
def apply_paths(node, mapping: dict[str, str]) -> None:
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k in {"path", "imagePath"} and isinstance(v, str) and v in mapping:
                node[k] = mapping[v]
            else:
                apply_paths(v, mapping)
    elif isinstance(node, list):
        for item in node:
            apply_paths(item, mapping)
```
This is one place where Python should improve on the TS source (which had a known shallow-walk limitation).

### I. Cross-cutting porting checklist

1. Carry the model-hint string verbatim — it's load-bearing prompt engineering, not a stylistic choice.
2. Don't reflect upstream response text into agent-visible errors for 429 (and consider extending the same rule to other 4xx classes that carry server stack traces).
3. The ordering "delete from registry FIRST, then close" in `closeTrackedBrowserTabsForSessions` is intentional — keep it.
4. The composite registry key (`targetId, baseUrl, profile`) means tracking is per-(gateway × profile × tab). A session that hops gateways will track each separately.
5. `string-prefix` resolution in `target-id.ts` is case-insensitive — so are session keys, but NOT target IDs (the exact-match arm preserves case).
6. Do not silently swallow non-ignorable close errors — `onWarn` must be wired to a real logger.
7. The `path.win32.basename` then `path.posix.basename` defense is OS-independent — port both to Python (PurePosixPath then PureWindowsPath, or vice versa).
8. The dispatcher path is what enables in-process agent execution; preserve the symmetry. In Python this maps to: same `client_fetch` interface, but with a "transport" that's either `httpx` or a direct dict-passing function that the embedded service registers.
