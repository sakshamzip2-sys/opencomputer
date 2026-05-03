# OpenClaw browser — AI / Chrome MCP / role-snapshot

> Captured from a read-only deep-dive subagent (2026-05-03). Treat as a skeleton; JIT deeper read of the named files when porting.

## One-line summary

OpenClaw produces a structured a11y/role tree the agent can read via three pipelines (Playwright AI snapshot, Playwright ARIA snapshot, Chrome MCP `take_snapshot`), unified by a ref-assignment system that lets the agent reference UI elements by `e1`/`e2` ids.

## What is "Chrome MCP"?

It's a **client to the upstream `chrome-devtools-mcp` server** (a separate MCP server, not OpenClaw's own code). OpenClaw spawns it as a subprocess:

```
npx chrome-devtools-mcp@latest --autoConnect --experimentalStructuredContent
```

It speaks the Model Context Protocol over stdio JSON-RPC and translates MCP tool calls (`take_snapshot`, `take_screenshot`, `click`, `fill`, …) into CDP commands against a user's running Chrome.

This is how OpenClaw drives the user's *real* Chrome (existing-session profile) without launching its own — Chrome MCP attaches to whatever Chrome instance is running.

## When Chrome MCP vs Playwright?

| Driver | Path | Has Playwright API? | Has `_snapshotForAI()`? |
|---|---|---|---|
| `openclaw` | Playwright direct CDP | Yes | Yes |
| `existing-session` (`user`) | Chrome MCP subprocess | No | No (uses MCP `take_snapshot`) |

`server-context.ts` reads `usesChromeMcp` capability and routes snapshot/action calls accordingly.

## Three snapshot paths

### Path 1 — Playwright AI snapshot (preferred when available)
```
page._snapshotForAI({ timeout, track: "response" })
  → string with embedded refs:  - button "OK" [ref=e1]
  → buildRoleSnapshotFromAiSnapshot extracts refs (no assignment needed; refs are self-contained)
```

### Path 2 — Playwright ARIA snapshot (fallback)
```
locator.ariaSnapshot()  // or frameLocator equivalent
  → ARIA tree as plain text, NO refs
  → buildRoleSnapshotFromAriaSnapshot assigns refs locally:
      - all INTERACTIVE_ROLES get a ref
      - CONTENT_ROLES with a name get a ref
      - STRUCTURAL roles never get refs
  → deduplication: role+name tracked; duplicates get [nth=0], [nth=1]
```

### Path 3 — Chrome MCP snapshot (existing-session)
```
mcp.callTool("take_snapshot")
  → ChromeMcpSnapshotNode tree { id, role, name, value, description, children }
  → buildAiSnapshotFromChromeMcpSnapshot walks the tree, lowercases roles,
     defaults missing roles to "generic", applies filters (interactive/compact/maxDepth)
  → assigns refs same way as Path 2
```

All three paths produce a unified output: `{ snapshotText, refs: { e1: { role, name, nth? }, ... } }`.

## Ref assignment + dedup logic

`RoleNameTracker` (in `pw-role-snapshot.ts`):
- Key: `"button:OK"` (role + name).
- Counts occurrences. Returns index 0 on first encounter, 1 on second, etc.
- `removeNthFromNonDuplicates()` strips `[nth=0]` from refs that turned out to be unique after the full tree was scanned.

End state: refs are stable, minimal-disambiguation strings the agent can pass back.

## Screenshot pipeline (`screenshot.ts`)

Constants:
- `DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE = 2000` px
- `DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES = 5_000_000`

Flow:
1. Take raw screenshot via Playwright `page.screenshot()` (or Chrome MCP `take_screenshot`).
2. Read metadata. If within limits → return as-is.
3. Otherwise iterate: resize + JPEG quality reduction. Pick smallest variant under the limit.
4. If no variant fits → error.

Element-targeted screenshots: Chrome MCP path takes a `uid` param; Playwright path uses `locator.screenshot()`.

## pw-ai module

Surprisingly thin:
- `pw-ai.ts` is 4 lines — re-exports Playwright tools and marks the module loaded.
- `pw-ai-module.ts` — dynamic loader (soft-fail if Playwright not installed).
- `pw-ai-state.ts` — singleton "loaded?" flag.

Actual "AI" work is in `pw-tools-core.snapshot.ts` (Playwright path) and `chrome-mcp.ts` (existing-session path). The "AI" naming refers to the Playwright `_snapshotForAI` API, not an LLM call inside this module.

## ARIA-role classification (`snapshot-roles.ts`)

64 lines of constants:
- `INTERACTIVE_ROLES`: button, link, textbox, checkbox, radio, combobox, listbox, slider, …
- `CONTENT_ROLES`: heading, article, main, navigation, region, …
- `STRUCTURAL_ROLES`: presentation, none, generic, …

Determines which elements get refs in the snapshot.

## Porting concerns for Python

- **`_snapshotForAI`** is a Playwright internal — playwright-python may not expose it. The port should fall back to `aria_snapshot()` (Path 2) cleanly. If `_snapshotForAI` ends up exposed via the underscore-prefixed accessor, prefer it.
- **Chrome MCP subprocess**: Python has the official `mcp` SDK (`pip install mcp`); use its `StdioClientTransport` equivalent. Or hand-roll JSON-RPC over `asyncio.subprocess` — same protocol.
- **Image ops**: Pillow handles resize + JPEG compression cleanly. (`Image.thumbnail`, `Image.save(..., "JPEG", quality=q)`.)
- **ARIA role constants**: just port the sets verbatim.
- **Ref dedup**: dict logic, no library needed.

## Open questions

- Do we ship with all three snapshot paths or just Playwright ARIA + Chrome MCP for v1 (skip the underscore-API gamble)?
- The Chrome MCP server is upstream code not under our control — should we vendor it or call npx at runtime? `npx` requires Node on the user's box.
- Element-targeted screenshots in the Chrome MCP path use `uid` — what's the uid mapping back to refs? (Need a deeper read of `chrome-mcp.snapshot.ts`.)

## Deep second-pass — function-by-function

> Source root: `extensions/browser/src/browser/` in the openclaw-main snapshot. Line numbers are file-local. This pass is intentionally exhaustive; treat the section above as the executive summary and this section as the porting reference.

### 1. Function tables

#### 1a. `pw-ai.ts` (68 lines, no functions of its own)

| Symbol | Role | Notes |
|---|---|---|
| `markPwAiLoaded()` (call at line 3) | Side-effect on import — flips the singleton in `pw-ai-state.ts` | The whole reason this file exists. See section 7. |
| 18 re-exports from `./pw-session.js` | Page lifecycle (open/close/focus/list, ref→Locator) | `WithSnapshotForAI` type-export is the gate that lets callers use the underscore API only when present. |
| 47 re-exports from `./pw-tools-core.js` | Action-side primitives plus the three snapshot entry points (`snapshotAiViaPlaywright`, `snapshotAriaViaPlaywright`, `snapshotRoleViaPlaywright`) | Surface area is large but flat — each re-export is one tool action verb. |

No callers reach into this file directly except via `pw-ai-module.ts`. That indirection is the whole point of the design (section 7).

#### 1b. `pw-ai-module.ts` (52 lines)

| Symbol | Signature | Purpose | Callers | Gotchas |
|---|---|---|---|---|
| `PwAiModule` (type, line 3) | `typeof import("./pw-ai.js")` | TypeScript handle to the *shape* of `pw-ai.ts`, even if it never loads | All consumers in `pw-tools-core.snapshot.ts`, `server-context.ts` etc. | If you rename an export in `pw-ai.ts` the type breaks here at compile time — keeps the soft-loader honest. |
| `isModuleNotFoundError(err)` (line 10) | `(err: unknown) => boolean` | String-sniffs every flavor of "module not installed" | `loadPwAiModule` only | Has to handle `ERR_MODULE_NOT_FOUND` plus four English substrings (`"Cannot find module"`, `"Cannot find package"`, `"Failed to resolve import"`, `"Failed to resolve entry for package"`, `"Failed to load url"`) because Bun, Vite, Node, and tsx each phrase the same failure differently. |
| `loadPwAiModule(mode)` (line 25) | `(mode: "soft" \| "strict") => Promise<PwAiModule \| null>` | Single import attempt, returns `null` on miss in soft mode | `getPwAiModule` only | `strict` still returns `null` if the failure is recognized as "not installed" — only re-throws on *unexpected* import errors. Subtle: a syntax error inside `pw-ai.ts` would surface in strict mode but be silently swallowed in soft mode. |
| `getPwAiModule(opts?)` (line 39) | `(opts?: { mode?: PwAiLoadMode }) => Promise<PwAiModule \| null>` | Cached, idempotent loader | Every consumer that needs Playwright tools | Two separate caches — `pwAiModuleSoft` and `pwAiModuleStrict`. A failed soft load is permanently cached as `null`. Strict mode can still escalate later. |

Module-level state: `pwAiModuleSoft` and `pwAiModuleStrict` (line 7-8). Both are `Promise<PwAiModule | null> | null`.

#### 1c. `pw-ai-state.ts` (9 lines)

| Symbol | Purpose |
|---|---|
| `pwAiLoaded` (let, line 1) | Module-private boolean. |
| `markPwAiLoaded()` (line 3) | Side-effect. Called from `pw-ai.ts` line 3 — fires only if `pw-ai.ts` is *evaluated*. |
| `isPwAiLoaded()` (line 7) | Read-only check. Other modules use this to decide whether snapshot-ai code paths should even be attempted. |

This is a literal three-statement file. It exists so callers can answer "did Playwright actually load?" without re-importing the heavy `pw-ai.ts` graph (which would bring in `playwright`, `pw-session`, `pw-tools-core`, etc.).

#### 1d. `pw-role-snapshot.ts` (404 lines)

| Symbol | Signature (abbreviated) | Purpose | Notes |
|---|---|---|---|
| `RoleRef` (type, line 4) | `{ role: string; name?: string; nth?: number }` | The unit row in `RoleRefMap` — what an `e1` ref points back to. | `nth` only present when role+name collides. |
| `RoleRefMap` (type, line 11) | `Record<string, RoleRef>` | The full ref index in a snapshot result. | |
| `RoleSnapshotStats` (type, line 13) | `{ lines, chars, refs, interactive }` | Telemetry shape — surfaces in tool responses. | |
| `RoleSnapshotOptions` (type, line 20) | `{ interactive?, maxDepth?, compact? }` | The three knobs every snapshot path accepts. | |
| `getRoleSnapshotStats(snapshot, refs)` (line 29) | `(string, RoleRefMap) => RoleSnapshotStats` | Counts lines/chars and how many refs land on `INTERACTIVE_ROLES`. | Used by both Path 2 and Path 3. |
| `getIndentLevel(line)` (line 39, private) | `(line: string) => number` | `Math.floor(leading-spaces / 2)` | Implies snapshots are 2-space indented; nothing else respected. |
| `matchInteractiveSnapshotLine(line, options)` (line 44, private) | regex parse | Pulls `roleRaw`, `name`, `suffix` out of one line, or returns null. | Skips closing tags (lines starting `/`) and out-of-depth lines. |
| `RoleNameTracker` (type, line 69) | duplicate-counting state | Shared by Path 2 and (with a sibling impl) Path 3. | See section 4 worked example. |
| `createRoleNameTracker()` (line 78, private) | factory | `counts`, `refsByKey`, four methods | `getNextIndex` increments on every call — that's how dedup actually works. |
| `removeNthFromNonDuplicates(refs, tracker)` (line 111, private) | mutates `refs` | Strips `nth` from any ref whose key turned out unique after the full walk. | Single-pass after the tree walk completes. |
| `compactTree(tree)` (line 121, private) | `(string) => string` | Drops lines that have no `[ref=...]` descendant within their indent block. | Two-cursor walk; quadratic worst case but in practice trees are small. Lines containing `:` (with content after) are kept verbatim — that's how `text:` rows survive. |
| `processLine(line, refs, options, tracker, nextRef)` (line 156, private) | full per-line pipeline | The Path 2 inner loop. Decides interactive vs content vs structural, assigns ref, mutates `refs`. | Returns `null` to drop a line, `string` to keep it. |
| `buildInteractiveSnapshotLines(params)` (line 220, private) | shared interactive-only builder | Used by both Path 1 and Path 2 in `interactive: true` mode. | Distinct from `processLine` because interactive mode emits a flat list, not a tree. |
| `parseRoleRef(raw)` (line 258, exported) | `(string) => string \| null` | Lenient parser. Accepts `e1`, `@e1`, `ref=e1`. | Used when the agent passes a ref back in tool args. |
| `buildRoleSnapshotFromAriaSnapshot(ariaSnapshot, options?)` (line 271, exported) | Path 2 entry point | See section 3. | |
| `parseAiSnapshotRef(suffix)` (line 330, private) | regex `\[ref=(e\d+)\]` | Pulls a Playwright-assigned ref from the `_snapshotForAI` line suffix. | |
| `buildRoleSnapshotFromAiSnapshot(aiSnapshot, options?)` (line 339, exported) | Path 1 entry point | See section 3. | |

#### 1e. `snapshot-roles.ts` (64 lines)

Three exported `Set<string>` constants. Pure data; no functions. See section 5 for the full lists.

#### 1f. `chrome-mcp.ts` (647 lines)

The largest file in scope. It owns the subprocess client, session cache, and one wrapper per Chrome MCP tool.

| Symbol | Signature (abbreviated) | Purpose | Gotchas |
|---|---|---|---|
| `ChromeMcpStructuredPage` (type, 13) | `{ id: number; url?; selected? }` | Internal page row | `id` is `number`, but `BrowserTab.targetId` is `string` — converted in `toBrowserTabs`. |
| `ChromeMcpToolResult` (type, 19) | `{ structuredContent?, content?, isError? }` | Mirror of the MCP SDK's `CallToolResult` | `structuredContent` is an opt-in feature — server must be launched with `--experimentalStructuredContent`. |
| `ChromeMcpSession` (type, 25) | `{ client, transport, ready }` | The cached session bundle | `ready` is a `Promise<void>` that resolves once `connect()` and `listTools()` succeed. Held separately so concurrent callers can `await` it without re-running connect. |
| `ChromeMcpSessionFactory` (type, 31) | `(profile, userDataDir?) => Promise<ChromeMcpSession>` | DI seam for tests | Defaulted by `createRealSession`; replaced via `setChromeMcpSessionFactoryForTest`. |
| `DEFAULT_CHROME_MCP_COMMAND` (const, 36) | `"npx"` | | Hard-coded; consider env override for v1.1. |
| `DEFAULT_CHROME_MCP_ARGS` (const, 37) | `["-y", "chrome-devtools-mcp@latest", "--autoConnect", "--experimentalStructuredContent", "--experimental-page-id-routing"]` | | `--experimentalStructuredContent` is load-bearing — without it, Chrome MCP returns text-only responses and `extractStructuredPages` falls back to regex. |
| `sessions` (Map, 46) | `Map<string, ChromeMcpSession>` | Live cache, keyed by `JSON.stringify([profile, userDataDir])` | One entry per (profile, userDataDir) tuple. |
| `pendingSessions` (Map, 47) | `Map<string, Promise<ChromeMcpSession>>` | Coalesces concurrent attach attempts | Race-condition shield, exercised by the test at chrome-mcp.test.ts:177. |
| `sessionFactory` (let, 48) | swappable factory | Tests inject fakes | |
| `asPages(value)` (50, private) | unknown→pages | Defensive cast for `structuredContent.pages` | Drops entries missing `id: number`. |
| `parsePageId(targetId)` (69, private) | `string → number` | `Number.parseInt` with `BrowserTabNotFoundError` on non-finite | All MCP tool calls take `pageId: number`; OpenClaw stores tab ids as strings. |
| `toBrowserTabs(pages)` (77) | adapter | Stamps `title: ""` (Chrome MCP doesn't surface titles) and `type: "page"` | |
| `extractStructuredContent(result)` (86) | reads `result.structuredContent` | Returns `{}` on miss | |
| `extractTextContent(result)` (90) | reads `result.content[].text` | Filters empty strings | |
| `extractTextPages(result)` (100) | regex parse text fallback | `/^\s*(\d+):\s+(.+?)(?:\s+\[(selected)\])?\s*$/i` | Handles older Chrome MCP servers without structured content. Test exercises this at chrome-mcp.test.ts:85. |
| `extractStructuredPages(result)` (118) | structured-or-text dispatcher | Always prefers structured. | |
| `extractSnapshot(result)` (123) | reads `structuredContent.snapshot` | Throws if missing | This is the one place where the `--experimentalStructuredContent` flag is *not* optional. |
| `extractJsonBlock(text)` (132) | parses ` ```json fenced``` ` blocks | Falls through to plain `JSON.parse` | Used for `evaluate_script` returns. |
| `extractMessageText(result)` (138) | reads `structuredContent.message` or first non-empty text block | | |
| `extractToolErrorMessage(result, name)` (147) | builds error string from `isError: true` results | | |
| `extractJsonMessage(result)` (152) | tries every text candidate as JSON | Used by `evaluateChromeMcpScript` | Last error re-thrown if all candidates fail. |
| `normalizeChromeMcpUserDataDir(userDataDir?)` (170) | `trim` | Empty → undefined | |
| `buildChromeMcpSessionCacheKey(profile, userDataDir?)` (175) | `JSON.stringify([profile, normalizedUserDataDir ?? ""])` | Deterministic cache key | |
| `cacheKeyMatchesProfileName(key, profile)` (179) | parses key, compares index 0 | Used to enumerate "all sessions for profile X" | |
| `closeChromeMcpSessionsForProfile(profile, keepKey?)` (188) | tear down all sessions for a profile except `keepKey` | Called at the start of `getSession` to invalidate stale entries when `userDataDir` changes | Test at chrome-mcp.test.ts:266. |
| `buildChromeMcpArgs(userDataDir?)` (212, exported) | argv builder | Appends `--userDataDir <path>` if present | |
| `createRealSession(profile, userDataDir?)` (219, private) | spawns subprocess via `StdioClientTransport` + creates `Client` | Verifies `list_pages` tool exists in `listTools()` | Wraps connect failures in `BrowserProfileUnavailableError` with a humanized message. |
| `getSession(profile, userDataDir?)` (263, private) | full session-acquisition state machine | See section 2 deep dive below | This function is the load-bearing concurrency primitive. |
| `callTool(profile, userDataDir, name, args)` (306, private) | Single MCP RPC, with error-class triage | Transport errors → tear down session. Tool errors (`isError: true`) → throw without tearing down. | Critical distinction; tested at chrome-mcp.test.ts:206 and chrome-mcp.test.ts:241. |
| `withTempFile(fn)` (334, private) | mkdtemp + cleanup | Used by `takeChromeMcpScreenshot` because Chrome MCP writes screenshots to a server-side path | |
| `findPageById(profile, pageId, userDataDir?)` (344, private) | calls `list_pages`, finds match | `BrowserTabNotFoundError` on miss | |
| `ensureChromeMcpAvailable(profile, userDataDir?)` (357, exported) | preflight — just calls `getSession` | Used by health checks | |
| `getChromeMcpPid(profile)` (364, exported) | first session's transport pid | Used for diagnostics | Returns `null` if no live session. |
| `closeChromeMcpSession(profile)` (373, exported) | wrapper around `closeChromeMcpSessionsForProfile` | | |
| `stopAllChromeMcpSessions()` (377, exported) | shutdown hook | Iterates unique profile names | |
| `listChromeMcpPages(profile, userDataDir?)` (384, exported) | wraps `list_pages` MCP tool | | |
| `listChromeMcpTabs(profile, userDataDir?)` (392, exported) | converts to `BrowserTab[]` | | |
| `openChromeMcpTab(profile, url, userDataDir?)` (399, exported) | wraps `new_page` MCP tool, returns the selected (or last) page | | |
| `focusChromeMcpTab(profile, targetId, userDataDir?)` (418, exported) | wraps `select_page` with `bringToFront: true` | | |
| `closeChromeMcpTab(profile, targetId, userDataDir?)` (429, exported) | wraps `close_page` | | |
| `navigateChromeMcpPage(params)` (437, exported) | wraps `navigate_page` with `type: "url"` | Re-fetches page list to return the resolved URL | Server canonicalizes (`example.com` → `https://example.com/`). |
| `takeChromeMcpSnapshot(params)` (458, exported) | wraps `take_snapshot` | Calls `extractSnapshot` for the structured tree | This is Path 3's source of truth. |
| `takeChromeMcpScreenshot(params)` (469, exported) | wraps `take_screenshot` | Server writes to `filePath`; we read it back | Supports `uid` (element-targeted), `fullPage`, `format`. |
| `clickChromeMcpElement(params)` (489, exported) | wraps `click` | `dblClick: true` if `doubleClick` requested | |
| `fillChromeMcpElement(params)` (503, exported) | wraps `fill` | Single field | |
| `fillChromeMcpForm(params)` (517, exported) | wraps `fill_form` | Bulk version — `elements: [{uid, value}]` | |
| `hoverChromeMcpElement(params)` (529, exported) | wraps `hover` | | |
| `dragChromeMcpElement(params)` (541, exported) | wraps `drag` with `from_uid`, `to_uid` | | |
| `uploadChromeMcpFile(params)` (555, exported) | wraps `upload_file` with server-side `filePath` | | |
| `pressChromeMcpKey(params)` (569, exported) | wraps `press_key` | | |
| `resizeChromeMcpPage(params)` (581, exported) | wraps `resize_page` | | |
| `handleChromeMcpDialog(params)` (595, exported) | wraps `handle_dialog` | `accept`/`dismiss` + optional `promptText` | |
| `evaluateChromeMcpScript(params)` (609, exported) | wraps `evaluate_script` | Returns parsed JSON via `extractJsonMessage` | Body must be a JS function string. |
| `waitForChromeMcpText(params)` (624, exported) | wraps `wait_for` with text array | Optional `timeout` ms | |
| `setChromeMcpSessionFactoryForTest(factory)` (638, exported) | DI hook | Tests only | |
| `resetChromeMcpSessionsForTest()` (642, exported) | reset DI + close all | Tests only | |

#### 1g. `chrome-mcp.snapshot.ts` (185 lines)

| Symbol | Signature (abbreviated) | Purpose | Notes |
|---|---|---|---|
| `ChromeMcpSnapshotNode` (type, 11) | `{ id?, role?, name?, value?, description?, children? }` | The wire shape from Chrome MCP's `take_snapshot` | `id` is the *uid* — the same opaque string the agent must pass back to `click`/`fill`/etc. (See section 8.) |
| `normalizeRole(node)` (20, private) | `(node) => string` | Lowercases role; defaults missing/blank to `"generic"` | Mirrors what Playwright does for unrecognized roles. |
| `escapeQuoted(value)` (25, private) | escape `\` and `"` | Used when emitting `name="..."`, `value="..."`, `description="..."` segments | |
| `shouldIncludeNode({role, name, options?})` (29, private) | gate | Drops non-interactive when `options.interactive`; drops unnamed structural when `options.compact` | |
| `shouldCreateRef(role, name?)` (43, private) | `INTERACTIVE_ROLES.has(role) \|\| (CONTENT_ROLES.has(role) && Boolean(name))` | The classification rule that decides ref assignment | Identical logic to `processLine` in `pw-role-snapshot.ts` — must stay in sync. |
| `DuplicateTracker` (type, 47, private) | `{ counts, keysByRef, duplicates }` | Sibling of `RoleNameTracker` | Distinct because it tracks `keysByRef` to enable post-walk `nth` cleanup keyed by ref. |
| `createDuplicateTracker()` (53, private) | factory | | |
| `registerRef(tracker, ref, role, name?)` (61, private) | mutates tracker, returns `nth \| undefined` | First occurrence returns `undefined` (no `nth` yet); 2nd+ returns `count` | |
| `flattenChromeMcpSnapshotToAriaNodes(root, limit=500)` (78, exported) | tree → flat `SnapshotAriaNode[]` | DFS, preserving depth | `boundedLimit = clamp(1, 2000, floor(limit))`. Used outside the snapshot-builder for general aria-tree consumers. Test at chrome-mcp.snapshot.test.ts:27. |
| `buildAiSnapshotFromChromeMcpSnapshot(params)` (112, exported) | tree → `{snapshot, refs, stats, truncated?}` | Path 3 entry point | See section 3 pseudocode. Optional `maxChars` truncates with `[...TRUNCATED - page too large]` marker. |

#### 1h. `screenshot.ts` (58 lines)

| Symbol | Signature (abbreviated) | Purpose | Notes |
|---|---|---|---|
| `DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE` (8) | `2000` | Pixel cap | |
| `DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES` (9) | `5 * 1024 * 1024` (≈5 MB) | Byte cap | |
| `normalizeBrowserScreenshot(buffer, opts?)` (11, exported) | `(Buffer, {maxSide?, maxBytes?}?) => Promise<{buffer, contentType?: "image/jpeg"}>` | The whole pipeline | Returns the original buffer untouched (no contentType field) when within limits — caller can `Content-Type: image/png` it. Re-encoded outputs are JPEG. See section 6 for algorithm. |

It pulls in three helpers from `media/image-ops.ts` (which is itself re-exported from `openclaw/plugin-sdk/browser-setup-tools`):
- `IMAGE_REDUCE_QUALITY_STEPS = [85, 75, 65, 55, 45, 35]`
- `buildImageResizeSideGrid(maxSide, sideStart)` → unique sides from `[sideStart, 1800, 1600, 1400, 1200, 1000, 800]`, clamped to `<= maxSide`, sorted descending.
- `resizeToJpeg({buffer, maxSide, quality, withoutEnlargement})` → sharp (or sips on macOS Bun) implementation; auto-rotates EXIF; uses mozjpeg.
- `getImageMetadata(buffer)` → tries header sniff (PNG/GIF/WebP/JPEG) first; falls back to sharp/sips. Hard cap at `25_000_000` pixels.

### 2. Chrome MCP protocol deep dive

#### 2a. Subprocess transport

OpenClaw uses the **official Anthropic `@modelcontextprotocol/sdk`** (`chrome-mcp.ts:4-5`):

```ts
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
```

It does **not** hand-roll JSON-RPC framing. The SDK handles `Content-Length`-prefixed (or LSP-style newline) framing internally. Concretely:

```ts
const transport = new StdioClientTransport({
  command: "npx",
  args: [
    "-y",
    "chrome-devtools-mcp@latest",
    "--autoConnect",
    "--experimentalStructuredContent",
    "--experimental-page-id-routing",
    // optional: "--userDataDir", "/path/to/profile"
  ],
  stderr: "pipe",
});
const client = new Client({ name: "openclaw-browser", version: "0.0.0" }, {});
await client.connect(transport);                         // sends `initialize` JSON-RPC
const tools = await client.listTools();                  // sends `tools/list`
```

The MCP wire protocol is JSON-RPC 2.0 over stdio. Each message is a JSON object: `{"jsonrpc": "2.0", "id": <n>, "method": "...", "params": {...}}`.

#### 2b. MCP tools called

The full list of Chrome MCP tools wrapped by OpenClaw (one wrapper function per tool):

| MCP tool name | OpenClaw wrapper | Args (concrete) |
|---|---|---|
| `list_pages` | `listChromeMcpPages` | `{}` |
| `new_page` | `openChromeMcpTab` | `{url}` |
| `select_page` | `focusChromeMcpTab` | `{pageId, bringToFront: true}` |
| `close_page` | `closeChromeMcpTab` | `{pageId}` |
| `navigate_page` | `navigateChromeMcpPage` | `{pageId, type: "url", url, timeout?}` |
| `take_snapshot` | `takeChromeMcpSnapshot` | `{pageId}` |
| `take_screenshot` | `takeChromeMcpScreenshot` | `{pageId, filePath, format, uid?, fullPage?}` |
| `click` | `clickChromeMcpElement` | `{pageId, uid, dblClick?}` |
| `fill` | `fillChromeMcpElement` | `{pageId, uid, value}` |
| `fill_form` | `fillChromeMcpForm` | `{pageId, elements: [{uid, value}]}` |
| `hover` | `hoverChromeMcpElement` | `{pageId, uid}` |
| `drag` | `dragChromeMcpElement` | `{pageId, from_uid, to_uid}` |
| `upload_file` | `uploadChromeMcpFile` | `{pageId, uid, filePath}` |
| `press_key` | `pressChromeMcpKey` | `{pageId, key}` |
| `resize_page` | `resizeChromeMcpPage` | `{pageId, width, height}` |
| `handle_dialog` | `handleChromeMcpDialog` | `{pageId, action, promptText?}` |
| `evaluate_script` | `evaluateChromeMcpScript` | `{pageId, function, args?}` |
| `wait_for` | `waitForChromeMcpText` | `{pageId, text: string[], timeout?}` |

Eighteen tools total. Naming convention: most use `pageId` and `uid` as the two primary identifiers — `pageId` from `list_pages`, `uid` from `take_snapshot`. The `drag` tool is the snake-case outlier (`from_uid`/`to_uid`).

#### 2c. Concrete request/response: `take_snapshot`

Wire request (after MCP framing):
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "take_snapshot",
    "arguments": { "pageId": 2 }
  }
}
```

Wire response (with `--experimentalStructuredContent`):
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "structuredContent": {
      "snapshot": {
        "id": "root",
        "role": "document",
        "name": "Example",
        "children": [
          { "id": "btn-1", "role": "button", "name": "Continue" },
          { "id": "txt-1", "role": "textbox", "name": "Email", "value": "peter@example.com" }
        ]
      }
    },
    "content": [{ "type": "text", "text": "..." }]
  }
}
```

`extractSnapshot` (chrome-mcp.ts:123) pulls `result.structuredContent.snapshot` and returns it as a `ChromeMcpSnapshotNode`. The `id` field is the **uid** the agent will reference back via `click`/`fill`/etc. — see section 8.

#### 2d. Concrete request/response: `take_screenshot`

Wire request:
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "take_screenshot",
    "arguments": {
      "pageId": 2,
      "filePath": "/var/folders/.../openclaw-chrome-mcp-XXXX/<uuid>",
      "format": "png",
      "uid": "btn-1"
    }
  }
}
```

The Chrome MCP server writes the image bytes to `filePath` on its own filesystem. OpenClaw then reads that file back (`fs.readFile(filePath)` at chrome-mcp.ts:485). The temp dir is `withTempFile`-managed (chrome-mcp.ts:334) — `fs.mkdtemp(<openclaw tmp>/openclaw-chrome-mcp-)` and `fs.rm(dir, {recursive: true})` on exit. **Implication for porting**: this only works because Chrome MCP runs on the same machine as OpenClaw. If we ever proxied Chrome MCP across a network boundary the file-passing convention breaks.

The wire response is essentially status confirmation; the actual bytes come from disk.

#### 2e. Session caching

Cache keys are JSON arrays: `JSON.stringify([profileName, userDataDir ?? ""])`. Two parallel maps:

- `sessions: Map<string, ChromeMcpSession>` — live, ready sessions.
- `pendingSessions: Map<string, Promise<ChromeMcpSession>>` — concurrent attach coalescer.

`getSession` (chrome-mcp.ts:263) state machine:
1. Build `cacheKey`.
2. `closeChromeMcpSessionsForProfile(profileName, keepKey=cacheKey)` — preemptively kill any sessions for the same profile but a **different** `userDataDir`. (Test: switching `/tmp/brave-a` → `/tmp/brave-b` forces a fresh session.)
3. Look up `sessions.get(cacheKey)`. If present but `transport.pid === null` (subprocess died), drop it.
4. If still no session, look up `pendingSessions.get(cacheKey)`.
5. If no pending either, create a pending promise that runs `sessionFactory ?? createRealSession`, parks the result in `sessions` once resolved, and clears itself from `pendingSessions`.
6. `await pending`.
7. Then `await session.ready` — propagates connect/listTools failure.

#### 2f. Reconnection / destruction triggers

| Event | Behavior |
|---|---|
| Transport error during `callTool` (chrome-mcp.ts:320) | Session deleted from cache, `client.close()` called. Next call rebuilds. |
| Tool-level `isError: true` (chrome-mcp.ts:328) | Session **kept**. Just throws the error message. Tested at chrome-mcp.test.ts:206. |
| `userDataDir` mismatch on next `getSession` (chrome-mcp.ts:265) | Old session(s) for that profile are torn down, new one created. |
| `transport.pid === null` (chrome-mcp.ts:268) | Treated as dead, replaced. |
| Pending factory throws (chrome-mcp.ts:275-292) | `pendingSessions` entry cleared in `finally`, `sessions.set` skipped. Next caller retries. Tested at chrome-mcp.test.ts:293. |
| `closeChromeMcpSession(profile)` | Tears down all sessions matching `profileName` regardless of `userDataDir`. |
| `stopAllChromeMcpSessions()` | Shutdown hook — closes everything. |

### 3. Snapshot pipeline algorithms (pseudocode)

#### 3a. `buildRoleSnapshotFromAiSnapshot` — Path 1 (Playwright `_snapshotForAI`)

Input: a string like:
```
- document "Example" [ref=e0]:
  - button "Continue" [ref=e1]
  - textbox "Email" [ref=e2]
```
Refs are already self-assigned by Playwright; OpenClaw just *parses* them.

```
INPUT: aiSnapshot:str, options:{interactive?, maxDepth?, compact?}
SPLIT into lines

IF options.interactive:
    out = []
    FOR each line:
        parsed = matchInteractiveSnapshotLine(line, options)        # depth + role + name + suffix
        IF parsed AND parsed.role IN INTERACTIVE_ROLES:
            ref = parseAiSnapshotRef(parsed.suffix)                 # regex /\[ref=(e\d+)\]/i
            IF ref:
                refs[ref] = { role, name? }
                emit "- {roleRaw} \"{name}\" [ref={ref}]{suffix}"
    RETURN { snapshot: out.join("\n") OR "(no interactive elements)", refs }

ELSE  (full-tree mode):
    out = []
    FOR each line:
        depth = getIndentLevel(line)
        IF maxDepth defined AND depth > maxDepth: skip
        match = regex /^(\s*-\s*)(\w+)(?:\s+"([^"]*)")?(.*)$/
        IF !match:
            out.push(line)
            continue
        IF roleRaw starts with "/": out.push(line); continue       # closing tag
        role = lowercase(roleRaw)
        IF options.compact AND role IN STRUCTURAL_ROLES AND !name: skip
        ref = parseAiSnapshotRef(suffix)
        IF ref: refs[ref] = { role, name? }
        out.push(line)                                              # keep verbatim
    tree = out.join("\n") OR "(empty)"
    RETURN { snapshot: options.compact ? compactTree(tree) : tree, refs }
```

Key insight: in full-tree mode, **lines are kept verbatim** — the function only *indexes* refs; it doesn't rewrite them. Compact mode runs `compactTree` post-hoc to drop empty branches.

#### 3b. `buildRoleSnapshotFromAriaSnapshot` — Path 2 (Playwright `ariaSnapshot()`)

Input: an ARIA tree text **without refs**:
```
- document "Example":
  - button "Continue"
  - textbox "Email"
```

```
INPUT: ariaSnapshot:str, options:{...}
SPLIT into lines
refs = {}; tracker = createRoleNameTracker(); counter = 0
nextRef = () => "e" + (++counter)

IF options.interactive:
    out = buildInteractiveSnapshotLines({
        lines, options,
        resolveRef: ({role, name}) => {
            ref = nextRef()
            nth = tracker.getNextIndex(role, name)
            tracker.trackRef(role, name, ref)
            return { ref, nth }
        },
        recordRef: ({role, name}, ref, nth) => refs[ref] = {role, name, nth},
        includeSuffix: (suffix) => suffix.includes("[")            # keep [disabled], [checked], etc.
    })
    removeNthFromNonDuplicates(refs, tracker)
    RETURN { snapshot: out.join("\n") OR "(no interactive elements)", refs }

ELSE  (full-tree mode):
    out = []
    FOR each line:
        processed = processLine(line, refs, options, tracker, nextRef)
        IF processed !== null: out.push(processed)
    removeNthFromNonDuplicates(refs, tracker)
    tree = out.join("\n") OR "(empty)"
    RETURN { snapshot: options.compact ? compactTree(tree) : tree, refs }
```

`processLine` (pw-role-snapshot.ts:156) is the hot loop:
1. Drop if past `maxDepth`.
2. Regex parse → `roleRaw`, `name`, `suffix`.
3. Drop if interactive-only and not `INTERACTIVE_ROLES`.
4. Drop if compact + structural + unnamed.
5. Compute `shouldHaveRef = isInteractive || (isContent && name)`.
6. If no ref needed, return line verbatim.
7. Else allocate `nextRef()`, `tracker.getNextIndex`, mutate `refs[ref] = {role, name, nth}`, rebuild line as `"{prefix}{roleRaw} \"{name}\" [ref={ref}] [nth={nth}?] {suffix?}"`.

#### 3c. `buildAiSnapshotFromChromeMcpSnapshot` — Path 3 (Chrome MCP tree)

Input: a `ChromeMcpSnapshotNode` JSON tree with `id`/`role`/`name`/`value`/`description`/`children`.

```
INPUT: { root, options?, maxChars? }
refs = {}; tracker = createDuplicateTracker(); lines = []

VISIT(node, depth):
    role = node.role || "generic"   (lowercased)
    name = node.name (normalized)
    value = node.value
    description = node.description
    IF maxDepth defined AND depth > maxDepth: return

    IF shouldIncludeNode({role, name, options}):
        line = "  " * depth + "- " + role
        IF name: line += " \"" + escapeQuoted(name) + "\""
        ref = node.id (normalized)
        IF ref AND shouldCreateRef(role, name):
            nth = registerRef(tracker, ref, role, name)
            refs[ref] = nth === undefined ? {role, name} : {role, name, nth}
            line += " [ref=" + ref + "]"
        IF value: line += " value=\"" + escapeQuoted(value) + "\""
        IF description: line += " description=\"" + escapeQuoted(description) + "\""
        lines.push(line)

    FOR child IN node.children: VISIT(child, depth + 1)

VISIT(root, 0)

# Strip nth from refs whose key turned out unique
FOR (ref, data) IN refs:
    key = tracker.keysByRef.get(ref)
    IF key AND key NOT IN tracker.duplicates: delete data.nth

snapshot = lines.join("\n")
IF maxChars AND snapshot.length > maxChars:
    snapshot = snapshot.slice(0, maxChars) + "\n\n[...TRUNCATED - page too large]"
    truncated = true

stats = getRoleSnapshotStats(snapshot, refs)
RETURN { snapshot, refs, stats, truncated? }
```

Two notable differences from Path 2:
1. The ref *is* the Chrome MCP `uid` (`node.id`), not a freshly-allocated `e1` counter. This is what enables element-targeted clicks (section 8).
2. Tree is serialized fresh from JSON; Path 2 rewrites pre-formatted text in place.

#### 3d. Unification

All three paths land at `{ snapshotText: string, refs: RoleRefMap }` plus optional `stats`/`truncated`. The `refs` value is the same shape regardless of source. Path 1's refs preserve Playwright's `eN` ids; Path 2 mints its own; Path 3 uses the Chrome MCP uid.

### 4. Ref assignment + dedup walkthrough (worked example)

Suppose `ariaSnapshot()` returns:

```
- main "Content":
  - button "OK"
  - link "Home"
  - button "OK"
```

Path 2 walk:

| Line | depth | role | name | shouldHaveRef | counter | nth | ref | refs |
|---|---|---|---|---|---|---|---|---|
| `- main "Content":` | 0 | main | Content | yes (content+name) | 1 | 0 | e1 | `{e1: {role:"main", name:"Content", nth:0}}` |
| `  - button "OK"` | 1 | button | OK | yes (interactive) | 2 | 0 | e2 | + `{e2: {role:"button", name:"OK", nth:0}}` |
| `  - link "Home"` | 1 | link | Home | yes (interactive) | 3 | 0 | e3 | + `{e3: {role:"link", name:"Home", nth:0}}` |
| `  - button "OK"` | 1 | button | OK | yes (interactive) | 4 | 1 | e4 | + `{e4: {role:"button", name:"OK", nth:1}}` |

Tracker state after walk:
- counts: `{"main:Content": 1, "button:OK": 2, "link:Home": 1}`
- refsByKey: `{"main:Content": ["e1"], "button:OK": ["e2", "e4"], "link:Home": ["e3"]}`
- duplicate keys: `{"button:OK"}`

`removeNthFromNonDuplicates` strips `nth` from refs whose key is not in duplicates:
- e1 (`main:Content`) — not duplicate → delete `nth`.
- e2 (`button:OK`) — duplicate → keep `nth: 0`.
- e3 (`link:Home`) — not duplicate → delete `nth`.
- e4 (`button:OK`) — duplicate → keep `nth: 1`.

Final ref map:
```js
{
  e1: { role: "main",   name: "Content" },
  e2: { role: "button", name: "OK", nth: 0 },
  e3: { role: "link",   name: "Home" },
  e4: { role: "button", name: "OK", nth: 1 },
}
```

Final snapshot text:
```
- main "Content" [ref=e1]:
  - button "OK" [ref=e2] [nth=0]
  - link "Home" [ref=e3]
  - button "OK" [ref=e4] [nth=1]
```

Note that line 2 emits `[ref=e2] [nth=0]` *during* the walk (since `nth = 0` was returned), but the `[nth=0]` portion is purely cosmetic by the time the agent sees it — the cleanup pass already trimmed `nth` from non-duplicates' map entries, but **not** from the snapshot text. Reading the source carefully: in the `processLine` path the rebuilt line includes `[nth=N]` only when `nth > 0` (pw-role-snapshot.ts:209). So `e2`'s line above would emit *without* `[nth=0]` (only the duplicate at `nth=1` gets a visible suffix). The map cleanup is consistent with that: only `e4` retains `nth` in its `RoleRef`. (My table above showed the in-flight value before cleanup; the rendered suffix follows the `nth > 0` rule.)

The interactive-only path (`buildInteractiveSnapshotLines`) follows the same rule: `[nth=N]` is rendered only when `(resolved.nth ?? 0) > 0` (pw-role-snapshot.ts:247).

### 5. ARIA role classification (verbatim from `snapshot-roles.ts`)

```ts
export const INTERACTIVE_ROLES = new Set([
  "button",
  "checkbox",
  "combobox",
  "link",
  "listbox",
  "menuitem",
  "menuitemcheckbox",
  "menuitemradio",
  "option",
  "radio",
  "searchbox",
  "slider",
  "spinbutton",
  "switch",
  "tab",
  "textbox",
  "treeitem",
]);

export const CONTENT_ROLES = new Set([
  "article",
  "cell",
  "columnheader",
  "gridcell",
  "heading",
  "listitem",
  "main",
  "navigation",
  "region",
  "rowheader",
]);

export const STRUCTURAL_ROLES = new Set([
  "application",
  "directory",
  "document",
  "generic",
  "grid",
  "group",
  "ignored",
  "list",
  "menu",
  "menubar",
  "none",
  "presentation",
  "row",
  "rowgroup",
  "table",
  "tablist",
  "toolbar",
  "tree",
  "treegrid",
]);
```

17 interactive, 10 content, 19 structural = 46 classified roles. Anything outside all three sets falls through `processLine`/`shouldIncludeNode` as "kept but no ref". Rules:
- `INTERACTIVE_ROLES` → always get a ref (regardless of name).
- `CONTENT_ROLES` → ref only if `name` is non-empty.
- `STRUCTURAL_ROLES` → never get a ref. Dropped entirely if `compact` is on **and** unnamed.
- Anything else (e.g. `paragraph`, `text`, `img`) → kept verbatim, no ref. Won't appear in `refs` map but *will* appear in the snapshot text.

### 6. Screenshot normalization (exact algorithm)

`normalizeBrowserScreenshot` (screenshot.ts:11):

```
INPUT: buffer, opts?: { maxSide?, maxBytes? }
maxSide  = max(1, round(opts?.maxSide  ?? 2000))
maxBytes = max(1, round(opts?.maxBytes ?? 5_242_880))

meta = await getImageMetadata(buffer)             # null if undecidable
width  = meta?.width  ?? 0
height = meta?.height ?? 0
maxDim = max(width, height)

# Fast path — already small enough
IF buffer.byteLength <= maxBytes
   AND (maxDim == 0 OR (width <= maxSide AND height <= maxSide)):
    RETURN { buffer }                              # original bytes, no contentType change

sideStart = maxDim > 0 ? min(maxSide, maxDim) : maxSide
sideGrid  = buildImageResizeSideGrid(maxSide, sideStart)
                                                   # = unique([sideStart, 1800, 1600, 1400, 1200, 1000, 800])
                                                   #   clamped to <= maxSide, sorted descending

smallest = null

FOR side IN sideGrid:                              # outer loop: pixel side
    FOR quality IN [85, 75, 65, 55, 45, 35]:       # inner loop: JPEG quality
        out = await resizeToJpeg({
            buffer, maxSide: side, quality, withoutEnlargement: true
        })
        IF !smallest OR out.byteLength < smallest.size:
            smallest = { buffer: out, size: out.byteLength }
        IF out.byteLength <= maxBytes:
            RETURN { buffer: out, contentType: "image/jpeg" }   # WIN — first under cap

# Exhausted grid; nothing fit. Throw using best we have.
best = smallest?.buffer ?? buffer
THROW Error("Browser screenshot could not be reduced below NMB (got XMB)")
```

Key behaviors:
- **No-op return** preserves the original format (could still be PNG). The caller decides Content-Type if no `contentType` field is set.
- **Grid is descending size, descending quality** — first pass at the tightest acceptable size at 85 quality. Each iteration is a fresh re-encode from the source buffer, never re-recompresses an already-compressed JPEG.
- **Abort condition** is *not* "tried everything" — it's "found one under maxBytes". The grid only fully exhausts when no combination fits.
- **EXIF rotation** is auto-applied inside `resizeToJpeg` (sharp `.rotate()` before `.resize()`).
- **Pixel-count guard** at 25M pixels enforced inside `resizeToJpeg` via `assertImagePixelLimit`. Will throw `Image dimensions exceed the 25,000,000 pixel input limit` for genuinely huge inputs.

### 7. The pw-ai module — why a 4-line file exists

`pw-ai.ts` is intentionally trivial because it has one job: **be the only place where Playwright's `pw-session.ts` and `pw-tools-core.ts` are evaluated**. The trick:

1. `pw-ai-state.ts` exports a private boolean `pwAiLoaded` and two functions to read/write it.
2. `pw-ai.ts` calls `markPwAiLoaded()` *at module top-level* (line 3). The function fires if and only if Node successfully evaluates the file.
3. `pw-ai-module.ts` wraps the import in a try/catch and offers a "soft" mode that returns `null` instead of throwing.

Net effect: any caller can ask "is Playwright actually usable in this process?" with `isPwAiLoaded()` (cheap boolean, no imports) without paying the cost of importing the entire Playwright tool surface.

The dynamic loader pattern in `pw-ai-module.ts` protects against:
1. **Playwright not installed** — `npm install` skipped or pruned. `ERR_MODULE_NOT_FOUND` is the canonical signal.
2. **Bun / Vite / tsx phrasing differences** — same failure surfaces with five distinct English error messages depending on the runtime; `isModuleNotFoundError` enumerates all of them.
3. **Concurrent load races** — both caches (`pwAiModuleSoft`, `pwAiModuleStrict`) are populated synchronously with a single in-flight promise, so 100 callers won't trigger 100 imports.
4. **Test mode strict-vs-soft escalation** — production code uses soft mode (`null` on miss). Tests/health-check code can demand strict mode (only swallows recognized "not installed" errors; bubbles real bugs).

Subtle gotcha: a soft-mode `null` is **permanent for the lifetime of the process**. If Playwright gets installed at runtime (e.g. user runs `npm install playwright` in another shell mid-session), OpenClaw won't see it without a restart. That's a deliberate trade-off — the cache prevents repeated import attempts on every snapshot call.

### 8. Element-targeted screenshots — uid trace

The flow when the agent says "screenshot button [ref=btn-1]":

1. **Agent receives a snapshot** from Path 3 (Chrome MCP). The snapshot text contains lines like `- button "Continue" [ref=btn-1]`. The `refs` map says `{"btn-1": {role: "button", name: "Continue"}}`.
2. **Agent passes `ref="btn-1"` back** to whatever screenshot tool is wired up.
3. The tool routes through the Chrome MCP path (because we're in `existing-session`). It calls `takeChromeMcpScreenshot({profileName, targetId, uid: "btn-1"})`.
4. Inside `takeChromeMcpScreenshot` (chrome-mcp.ts:469):
   ```ts
   await callTool(profileName, userDataDir, "take_screenshot", {
       pageId: parsePageId(targetId),
       filePath,
       format: "png",
       uid: "btn-1",      // <-- straight pass-through
       fullPage: false,
   });
   ```
5. Chrome MCP server resolves `uid: "btn-1"` against its internal accessibility-tree cache (from the most recent `take_snapshot`) and runs the CDP screenshot scoped to that node's bounding box.

So the mapping is **the identity function**: in Path 3, the openclaw `ref` *is* the Chrome MCP `uid`. This is the architectural reason `buildAiSnapshotFromChromeMcpSnapshot` reuses `node.id` directly instead of allocating fresh `e1`/`e2` identifiers — round-tripping the uid is free and lets the agent target individual elements without OpenClaw maintaining a translation table.

Compare Path 2 (ARIA snapshot): refs are minted as `e1`/`e2`/`e3`. The agent eventually calls a Playwright action with `ref=e3`. That dispatches via `refLocator(page, "e3")` (re-exported from `pw-session.ts`) which uses Playwright's `aria-ref://e3` Locator scheme. The translation table lives inside Playwright, not in OpenClaw.

Path 1 (`_snapshotForAI`): same as Path 2, but Playwright already owns the ref-allocation; OpenClaw just parses what it got.

### 9. Python translation notes

#### 9a. MCP SDK

The official Anthropic Python MCP SDK is published as `mcp` on PyPI:

```bash
pip install mcp
```

Equivalent Python skeleton for `chrome-mcp.ts:219` (`createRealSession`):

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def create_real_session(profile_name: str, user_data_dir: str | None = None):
    args = ["-y", "chrome-devtools-mcp@latest", "--autoConnect",
            "--experimentalStructuredContent", "--experimental-page-id-routing"]
    if user_data_dir:
        args += ["--userDataDir", user_data_dir]

    server = StdioServerParameters(command="npx", args=args)
    read, write = await stdio_client(server).__aenter__()
    session = ClientSession(read, write)
    await session.initialize()
    tools = await session.list_tools()
    if not any(t.name == "list_pages" for t in tools.tools):
        raise RuntimeError("Chrome MCP server missing list_pages")
    return session
```

Then `await session.call_tool("take_snapshot", {"pageId": page_id})` returns a `CallToolResult` with `.structured_content` (snake_case in Python) and `.content` matching the JS shape.

Key Python-side considerations:
- `stdio_client` is an async context manager; need careful lifecycle in our session cache.
- `result.structured_content["snapshot"]` is a plain `dict` — feeds directly into a Python port of `ChromeMcpSnapshotNode` (use a `TypedDict` or `pydantic.BaseModel`).
- The session-cache concurrency primitives (`pendingSessions`) port to `asyncio.Lock` + an in-flight `asyncio.Future`.

#### 9b. Pillow image ops

Pillow is the obvious sharp replacement:

```python
from PIL import Image
import io

def resize_to_jpeg(buffer: bytes, max_side: int, quality: int, without_enlargement=True) -> bytes:
    img = Image.open(io.BytesIO(buffer))
    img = ImageOps.exif_transpose(img)  # auto-rotate from EXIF (Pillow >= 6.0)
    if without_enlargement:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    else:
        img = img.resize((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
```

Pillow gotchas vs sharp/mozjpeg:
- No mozjpeg by default (slightly larger files at the same quality). `Pillow-SIMD` plus `mozjpeg-lossless-optimization` can close the gap; for v1, vanilla Pillow is fine.
- `Image.thumbnail` mutates in place and only ever shrinks (matches `withoutEnlargement: true` semantics — no need for the explicit branch).
- `convert("RGB")` is required before JPEG-save if the source has an alpha channel, otherwise Pillow raises.

For metadata-only (`getImageMetadata`):
```python
def get_image_metadata(buffer: bytes):
    try:
        img = Image.open(io.BytesIO(buffer))
        img.verify()  # cheap header parse
        return {"width": img.width, "height": img.height}
    except Exception:
        return None
```

The Node side hand-rolls header sniffers for PNG/GIF/WebP/JPEG to avoid loading sharp until necessary. In Python, Pillow's lazy loader (`Image.open` is lazy until `.load()`) does the same job in a single line.

#### 9c. ARIA role constants

Trivial port — the three sets become Python frozensets:

```python
INTERACTIVE_ROLES = frozenset({
    "button", "checkbox", "combobox", "link", "listbox",
    "menuitem", "menuitemcheckbox", "menuitemradio", "option",
    "radio", "searchbox", "slider", "spinbutton", "switch",
    "tab", "textbox", "treeitem",
})
CONTENT_ROLES = frozenset({
    "article", "cell", "columnheader", "gridcell", "heading",
    "listitem", "main", "navigation", "region", "rowheader",
})
STRUCTURAL_ROLES = frozenset({
    "application", "directory", "document", "generic", "grid",
    "group", "ignored", "list", "menu", "menubar", "none",
    "presentation", "row", "rowgroup", "table", "tablist",
    "toolbar", "tree", "treegrid",
})
```

#### 9d. Ref dedup

`RoleNameTracker` ports to a `dataclass` with `collections.Counter` + `defaultdict(list)`. No external dependencies. The cleanup pass (`removeNthFromNonDuplicates`) is a single-line dict comprehension.

#### 9e. Pseudocode-to-Python alignment notes

- `getIndentLevel` is `len(line) - len(line.lstrip(" "))` then `// 2`.
- Regex `/^(\s*-\s*)(\w+)(?:\s+"([^"]*)")?(.*)$/` ports as-is to Python's `re` (use a raw string).
- `parseAiSnapshotRef` regex `\[ref=(e\d+)\]` is straight `re.IGNORECASE`.
- `compactTree` is a two-pointer walk; idiomatic Python uses `enumerate(lines)` + nested loop.
- The `interactive` mode path's "include suffix only when it contains `[`" preserves Playwright state attributes like `[disabled]`, `[checked]`, `[expanded]`. Python port should preserve verbatim — agents may parse them.

#### 9f. Subprocess lifecycle in Python

The `transport.pid === null` check (chrome-mcp.ts:268) — meaning "subprocess died" — has a Python equivalent: `process.returncode is not None` after `process.poll()`. The `mcp` Python SDK exposes the underlying `asyncio.subprocess.Process` via the stdio client; we can poll it the same way.

For session destruction on transport error vs preservation on tool error: Python's MCP SDK raises distinct exception types — `mcp.ClientError` for tool-level errors (preserve session) and various `OSError`/`ConnectionResetError` subclasses for transport (tear down). Match on the exception class, not the message.

#### 9g. File-passing for `take_screenshot`

The Node code uses `fs.mkdtemp` + `fs.readFile`. Python equivalent: `tempfile.TemporaryDirectory()` as an async context manager (or `aiofiles.tempfile.TemporaryDirectory` for the async path). The temp file path is passed to Chrome MCP as a string; we read bytes back with `aiofiles.open(path, "rb")`. The directory is cleaned up on exit regardless of success.

#### 9h. Open porting decisions

- **Path 1 underscore API**: Python Playwright (`playwright-python`) does not expose `_snapshotForAI` cleanly. We have two choices: monkeypatch via the underscore accessor (`page._impl_obj._snapshot_for_ai(...)` if it exists) or ship without Path 1 and rely on Path 2. Recommendation: ship Path 2 + Path 3 first; revisit Path 1 once the underscore-API stability is verified empirically.
- **Vendor vs `npx`**: keeping the upstream Chrome MCP server out-of-tree means users need Node + npx installed. Acceptable for v1 since installing OpenClaw already implies a dev-tools-friendly machine. Document the dep clearly.
- **Concurrency model**: the JS code uses Promise caching. Direct Python port uses `asyncio` `Future`s in the same shape — both maps (`sessions`, `pending_sessions`) are `dict[str, ...]`, no locking needed because the asyncio event loop serializes access.
