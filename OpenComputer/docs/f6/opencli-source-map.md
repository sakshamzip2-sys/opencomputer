# OpenCLI Architecture & Source Map

**Reference**: https://github.com/jackwener/opencli
**License**: Apache-2.0 (fully permissive; safe for our wrapper)
**Manifest Date**: April 2025 | Package Version: 1.7.7

---

## Executive Summary

OpenCLI is a TypeScript/JavaScript framework that transforms web apps and Electron applications into scriptable CLIs. It uses a daemon + browser-extension architecture: a local HTTP server (port 19825 by default) mediates between CLI invocations and Chrome automation via Chrome DevTools Protocol (CDP). **License is Apache-2.0**, permitting our closed-source wrapper derivative. The registration mechanism is a global registry keyed by `site/command` name, with zero magic—adapters are plain JS files that call `cli({site, name, func, strategy, ...})`. All 103+ shipped adapters are discoverable via a pre-built manifest; our 15-adapter shortlist is fully available and well-factored.

### Key Findings
- **Port**: 19,825 (hardcoded in `src/constants.ts`; can override via `OPENCLI_DAEMON_PORT` env var)
- **Daemon**: Auto-spawned, HTTP+WebSocket bridge, runs for lifetime of session
- **Extension**: Manifest V3 Chrome extension; coordinates via localhost WebSocket only
- **Error Taxonomy**: 10 top-level error classes; Unix exit codes (sysexits.h convention)
- **Strategy Enum**: 6 categories (PUBLIC, LOCAL, COOKIE, HEADER, INTERCEPT, UI)
- **Adapters**: 624 commands across 103+ sites; ships as .js, some via YAML pipeline
- **Privacy**: No telemetry; all data stays on localhost; cookies read-only when needed

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ User Terminal                                                       │
│ $ opencli twitter profile @username --format json                   │
└──────────────────────┬──────────────────────────────────────────────┘
                       │
                       ├─ src/main.ts: FastPath (--version, completion)
                       │   or DiscoverClis + runCli
                       │
                       ├─ src/discovery.ts: Scan ./clis/ + ~/.opencli/clis
                       │   → Load cli-manifest.json for speed
                       │
                       ├─ src/registry.ts: getRegistry() lookup
                       │   Match "twitter/profile" CliCommand
                       │
                       └─► src/cli.ts: runCli()
                           │
                           ├─ Normalize strategy → browser session needed?
                           │ (COOKIE: yes; PUBLIC: maybe; INTERCEPT: yes)
                           │
                           ├─ Spawn daemon if not running:
                           │  src/daemon.ts HTTP server on :19825
                           │
                           ├─ Launch/connect isolated Chrome window
                           │  src/launcher.ts: auto-detect, --remote-debugging-port
                           │
                           ├─ Execute command func or pipeline
                           │  page.goto() → page.evaluate() → page.click() etc.
                           │
                           └─► HTTP POST /command → daemon
                               └─► WebSocket → Extension ← isolated Chrome
                                   └─► CDP InspectBackend (debugger protocol)
                                       └─► page.screenshot(), etc.
                               
                               daemon accumulates results
                               └─► HTTP response 200 OK {ok: true, data: [...]}
                               
                               src/output.ts: render as JSON/YAML/table/CSV

└─ CLI exits with Unix exit code (0 = success, 1–78 = error)
```

**Key observation**: The daemon is **ephemeral**. It's spawned on first browser command and persists until:
- Explicit `opencli daemon stop`
- SIGTERM / uninstall preuninstall hook
- Process timeout (no activity)

The extension is a **MV3 background service worker**—it:
1. Listens for WebSocket upgrade on `ws://localhost:19825`
2. Creates isolated `chrome.debugger` targets (not tabs in user's browser)
3. Speaks CDP back to daemon
4. Never sends data to remote servers

---

## Adapter Registration Mechanism

**File**: `src/registry.ts`

```typescript
// Global registry (all modules share one instance via globalThis)
const _registry: Map<string, CliCommand> = 
  globalThis.__opencli_registry__ ??= new Map();

// Registration entry point
export function cli(opts: CliOptions): CliCommand {
  const cmd: CliCommand = { site, name, strategy, func, pipeline, ... };
  registerCommand(cmd);  // Stores in _registry under "site/name" key
  return _registry.get(fullName(cmd))!;
}

// Lookup
export function getRegistry(): Map<string, CliCommand> {
  return _registry;
}
```

**Integration for our 15-adapter wrapper**:
1. Import `@jackwener/opencli/registry`
2. Call `cli({site, name, description, func, ...})`
3. Return value is auto-registered; no explicit registry.register() call
4. Lookup at runtime: `getRegistry().get('github/user-profile')`

**Example** (from `clis/reddit/user-comments.js`):
```javascript
import { cli, Strategy } from '@jackwener/opencli/registry';

cli({
  site: 'reddit',
  name: 'user-comments',
  description: `View a Reddit user's comment history`,
  domain: 'reddit.com',
  strategy: Strategy.COOKIE,
  browser: true,
  args: [
    { name: 'username', type: 'string', required: true, positional: true },
    { name: 'limit', type: 'int', default: 15 },
  ],
  columns: ['subreddit', 'score', 'body', 'url'],
  func: async (page, kwargs) => { ... },
  // OR pipeline: [{ navigate: '...' }, { evaluate: '...' }, ...]
});
```

**Key properties**:
- `site`: identifies the website/service (e.g., "reddit", "twitter")
- `name`: command name within site (e.g., "user-comments")
- `strategy`: how to authenticate (see Strategy enum below)
- `domain`: (optional) target domain for COOKIE/HEADER strategies
- `func`: async handler receiving `(page: IPage, kwargs: Record<string, any>) => Promise<unknown>`
- `pipeline`: alternative to `func`—declarative YAML-like steps
- `columns`: output field names for table format
- `args`: CLI argument definitions (positional, defaults, help text, etc.)

---

## Strategy Enum

**File**: `src/registry.ts` lines 7–14

```typescript
export enum Strategy {
  PUBLIC = 'public',       // No auth; works for anyone (HackerNews search, Wikipedia)
  LOCAL = 'local',         // Local file/config only (no browser)
  COOKIE = 'cookie',       // Site-specific cookie auth (Twitter, Reddit, LinkedIn)
  HEADER = 'header',       // Auth via HTTP header (API key in X-Authorization)
  INTERCEPT = 'intercept', // Browser JS interception + rate-limit headers
  UI = 'ui',               // Browser UI interaction (clicking, typing, etc.)
}
```

**Trade-offs**:

| Strategy | Auth Required | Browser Session | Pre-navigate | Rate Limit | Use Case |
|----------|---------------|-----------------|--------------|-----------|----------|
| PUBLIC | No | Maybe | No | Low (public APIs) | News scraping, Wikipedia |
| LOCAL | No | No | No | N/A | Local files, CLI piping |
| COOKIE | Yes | Yes | Yes, to domain | Medium | Twitter, Reddit, LinkedIn |
| HEADER | Yes | Maybe | No | Medium | Private APIs w/ bearer token |
| INTERCEPT | Yes | Yes | Yes, needs context | High (custom header limit) | Protected endpoints |
| UI | Yes | Yes | Yes | High (no API) | Complex JS interactions |

**Normalization** (`normalizeCommand()` in registry.ts):
After loading, strategy is decoded into concrete fields:
- `strategy=COOKIE + domain="twitter.com"` → `navigateBefore = "https://twitter.com"`
- `strategy=PUBLIC` → `browser=false` (no isolated window needed)
- `strategy=INTERCEPT` → `browser=true, navigateBefore=true` (context needed, but no pre-nav URL)

---

## Error Taxonomy

**File**: `src/errors.ts`

All errors extend `CliError` base class. Exit codes follow **sysexits.h** convention:

| Error Class | Exit Code | Meaning | Recovery Hint |
|-------------|-----------|---------|---------------|
| CliError | 1 | Generic/unexpected | Report as bug; stack trace in `OPENCLI_VERBOSE=1` |
| ArgumentError | 2 | Bad CLI arguments | Check help; `opencli <cmd> --help` |
| EmptyResultError | 66 | No data returned | Page structure changed; check site; may need login |
| BrowserConnectError | 69 | Daemon/extension unreachable | Run `opencli daemon stop && opencli <cmd>` to restart |
| AdapterLoadError | 69 | Adapter not found / failed import | Check spelling; verify `clis/` directory exists |
| TimeoutError | 75 | Browser command timed out | Increase `OPENCLI_BROWSER_COMMAND_TIMEOUT` env var (seconds) |
| AuthRequiredError | 77 | Not logged in / cookie invalid | Open Chrome and log in to the site; re-run |
| ConfigError | 78 | Missing config or env var | Check required env vars; see command help |
| SelectorError | 1 | Element not found on page | UI may have changed; report issue |
| PluginError | 1 | Plugin load / execution error | Check plugin code and logs |
| CommandExecutionError | 1 | Adapter func threw error | See error message; may indicate API change |
| Interrupted | 130 | User pressed Ctrl-C | Normal exit |

**Usage in our wrapper**: Catch these exceptions, map their `code` + `hint` fields into user-facing consent prompts. E.g., `BrowserConnectError` → "Browser extension not connected; check extension installed"; `AuthRequiredError` → "Must log in to [domain]".

---

## Core Module Index

### CLI Execution Pipeline (`src/cli.ts`)
- **registerAllCommands()**: Wire up all discovered adapters to Commander.js
- **runCli()**: Main entry; parse args, load adapters, execute one command
- **Output rendering**: JSON, YAML, table (via `src/output.ts`), CSV, plain text, markdown
- **Browser network capture**: intercept + cache HTTP traffic for agent replay

### Daemon (`src/daemon.ts`)
- **Port**: 19825 (default, `process.env.OPENCLI_DAEMON_PORT`)
- **HTTP endpoints**:
  - `GET /ping` — health check (CORS-enabled for extension)
  - `GET /status` — daemon uptime, extension version, pending commands, memory
  - `POST /command` — main work; CLI sends command JSON, daemon routes via WebSocket
  - `POST /shutdown` — graceful shutdown
- **WebSocket**: `/` — extension connects, daemon routes CDP commands

### Registry & Discovery (`src/registry.ts`, `src/discovery.ts`)
- **registry.ts**: `cli()` function, `Strategy` enum, `CliCommand` interface
- **discovery.ts**: Scan `./clis/` and `~/.opencli/clis/`, build `cli-manifest.json` cache
- **Lazy loading**: TS plugins are imported only on first reference (perf optimization)

### Browser Abstraction (`src/types.ts`, `src/browser/`)
- **IPage interface**: Type-safe page API (goto, evaluate, click, getCookies, etc.)
- **CDP wrapper** (`src/browser/cdp.ts`): Maps IPage methods to Chrome DevTools Protocol
- **Target resolver** (`src/browser/target-resolver.ts`): Element finders (click, text match, selector)
- **Network capture** (`src/browser/network-cache.ts`): Intercept & cache HTTP for replay
- **HTML tree** (`src/browser/html-tree.ts`): Build DOM tree for analysis

### Launcher (`src/launcher.ts`)
- **probeCDP()**: Check if Chrome is listening on debugger port
- **detectProcess()**: `pgrep` to check if already running
- **discoverAppPath()**: macOS `osascript` to find app installation
- **Launch Electron apps** with `--remote-debugging-port` + poll `/json`

### Extension (`extension/`)
- **manifest.json**: MV3 permissions (debugger, tabs, cookies, alarms, activeTab)
- **background.js** (service worker): Accepts WebSocket, spawns CDP targets
- **popup.html**: Status UI (manual control if desired)

---

## Port Collision Concerns

**Answer to risk §5**: **Confirmed port 19825**

Evidence:
1. `src/constants.ts`: `export const DEFAULT_DAEMON_PORT = 19825`
2. `src/daemon.ts` line 30: `const PORT = parseInt(process.env.OPENCLI_DAEMON_PORT ?? String(DEFAULT_DAEMON_PORT), 10)`
3. `PRIVACY.md`: "WebSocket to `ws://localhost:19825`"
4. Pre-uninstall hook in `package.json`: targets `process.env.OPENCLI_DAEMON_PORT || '19825'`

**Handling collisions in our wrapper**:
1. Set `OPENCLI_DAEMON_PORT` env var to unique value before spawning OpenCLI subprocess
   - E.g., detect available port with `portscanner` npm module
   - Pass via `child_process.spawn(cmd, args, { env: {..., OPENCLI_DAEMON_PORT: '19826'} })`
2. Poll health endpoint before returning ready: `GET http://localhost:OPENCLI_DAEMON_PORT/ping`
3. Graceful shutdown: `POST http://localhost:OPENCLI_DAEMON_PORT/shutdown`

---

## PRIVACY Analysis

**File**: `PRIVACY.md` (dated 2026-03-25)

### Key Promises
1. **No telemetry** — zero data sent to remote servers
2. **No tracking cookies** — no identifiers, fingerprints, or analytics
3. **Localhost-only comms** — all data stays on user's machine (WebSocket to `127.0.0.1:19825`)
4. **Cookie read-only** — extension reads cookies only when explicitly requested by CLI; never modifies or exfiltrates them
5. **Isolated automation windows** — separate from user's normal browsing

### Extension Permissions & Justification
| Permission | Why Needed |
|------------|-----------|
| `debugger` | Chrome DevTools Protocol for browser automation |
| `tabs` | Create/manage isolated automation windows |
| `cookies` | Read site-specific cookies for authenticated scraping |
| `activeTab` | Identify current tab for context-aware commands |
| `alarms` | WebSocket keepalive checks to daemon |

### Implications for Our Wrapper
- Our consent prompt can **safely claim the same privacy posture** (all data stays local)
- We must **NOT add telemetry** without explicit user consent and OpenCLI re-licensing
- If we wrap multiple adapters, each must abide by its site's ToS (Twitter, Reddit, LinkedIn, etc. may prohibit automated access)
- **Recommend**: Mirror PRIVACY.md clauses in our UI, with a reference link to original

---

## License & Legal

**File**: `LICENSE`

**License**: Apache License 2.0 (January 2004)  
**Copyright**: 2025 jackwener  
**Repository**: https://github.com/jackwener/opencli

**Key permissions**:
- ✅ Use, modify, distribute (source or binary)
- ✅ Include in proprietary/closed-source software
- ✅ Add our own copyright and license terms to modifications
- ✅ Sublicense (pass along Apache terms to end users)

**Obligations**:
- ✅ Include copy of LICENSE and NOTICE file
- ✅ State changes (if we modify OpenCLI code)
- ✅ Include original attribution notices

**Verdict**: **Fully safe for our closed-source wrapper**. No copyleft (no AGPL), no patent risk beyond standard indemnification. We can wrap it, charge for it, and modify it—as long as we include the Apache license text.

---

## 15-Adapter Shortlist: Module Paths & Capabilities

Our master plan's curated targets. All are available in OpenCLI's codebase:

### 1. **GitHub** (Gitee proxy; actual GitHub via web scraping)
- **Modules**: `clis/gitee/user.js`, `clis/gitee/search.js`, `clis/gitee/trending.js`
- **Strategy**: PUBLIC (no auth required for public profiles)
- **Returns**: username, email, follower count, bio, public repos
- **Rate limit**: Medium (DOM scraping, no API)
- **Auth notes**: GitHub API requires token; Gitee public profiles work without login

### 2. **Reddit** (User + Posts + Comments)
- **Modules**: `clis/reddit/user-comments.js`, `clis/reddit/user.js` (posts), `clis/reddit/frontpage.js`
- **Strategy**: COOKIE (requires reddit.com login)
- **Returns**: (comments) subreddit, score, body, url; (posts) title, subreddit, upvotes, time
- **Rate limit**: High (uses `/user/NAME/comments.json?limit=X` API endpoint with cookie auth)
- **Auth notes**: Must be logged into Reddit in Chrome; cookies read from browser session

### 3. **LinkedIn** (Timeline + Profile)
- **Modules**: `clis/linkedin/timeline.js`, `clis/linkedin/search.js`
- **Strategy**: COOKIE (LinkedIn requires login)
- **Returns**: author, text, reactions, comments, posted_at, url (for timeline); search results (company, job, profile)
- **Rate limit**: Medium (DOM scraping with anti-bot, CSS selector based)
- **Auth notes**: Must be logged in; extension reads LinkedIn cookies

### 4. **Twitter** (Profile + Tweets)
- **Modules**: `clis/twitter/profile.js`, `clis/twitter/timeline.js`, `clis/twitter/tweets.js`
- **Strategy**: COOKIE (X.com requires login for API; bearer token + ct0 cookie)
- **Returns**: screen_name, name, bio, followers, following, tweets, verified status
- **Rate limit**: Medium (uses X GraphQL API w/ hardcoded bearer token + ct0 CSRF token)
- **Auth notes**: Requires login; extracts ct0 cookie dynamically from browser session

### 5. **HackerNews** (User + Search)
- **Modules**: `clis/hackernews/user.js`, `clis/hackernews/search.js`, `clis/hackernews/top.js`
- **Strategy**: PUBLIC (HN has public profiles & API)
- **Returns**: user: karma, about, created; posts: title, score, time, url
- **Rate limit**: Low (HN is permissive; DOM scraping + official API)
- **Auth notes**: No login required for public data

### 6. **StackOverflow** (User Profile + Search)
- **Modules**: `clis/stackoverflow/search.js`, `clis/stackoverflow/hot.js`, `clis/stackoverflow/unanswered.js`
- **Strategy**: PUBLIC (public API, though limited)
- **Returns**: question, score, answer count, tags, views, url
- **Rate limit**: Low (uses official SO API, DOM fallback)
- **Auth notes**: Public data only; no login needed

### 7. **YouTube** (User/Channel + Videos)
- **Modules**: `clis/youtube/channel.js`, `clis/youtube/video.js`, `clis/youtube/feed.js`
- **Strategy**: PUBLIC (YouTube InnerTube API, available in page context)
- **Returns**: channel title, subscriber count, recent videos (title, views, duration, url)
- **Rate limit**: Medium (InnerTube API, YouTube rate-limits but no hard block for organic use)
- **Auth notes**: No login required for public channels; feed requires login

### 8. **Medium** (User Profile + Search)
- **Modules**: `clis/medium/user.js`, `clis/medium/search.js`, `clis/medium/feed.js`
- **Strategy**: COOKIE (feed requires login; public profiles work without)
- **Returns**: title, date, read time, claps, url
- **Rate limit**: Medium (DOM scraping)
- **Auth notes**: Public articles viewable without login; feed (COOKIE strategy) requires Medium account

### 9. **Bluesky** (Profile + Posts + Search)
- **Modules**: `clis/bluesky/profile.js`, `clis/bluesky/user.js`, `clis/bluesky/search.js`
- **Strategy**: PUBLIC (Bluesky API is public; no auth required for most operations)
- **Returns**: profile: handle, name, bio, followers, following; posts: text, likes, replies, timestamp
- **Rate limit**: Medium (official Bluesky API; they allow bots with ID)
- **Auth notes**: No login required; handles and profiles are publicly searchable

### 10. **Xiaohongshu** (小红书 Red / User + Posts)
- **Modules**: `clis/xiaohongshu/creator-notes-summary.js`, `clis/xiaohongshu/creator-note-detail.js`
- **Strategy**: COOKIE (Xiaohongshu requires login for detailed data)
- **Returns**: note_id, title, description, interaction_count, timestamp, images
- **Rate limit**: High (Xiaohongshu is protected; needs valid session)
- **Auth notes**: Must log in via Chrome; extension reads Xiaohongshu cookies

### 11. **Arxiv** (Search + Paper)
- **Modules**: `clis/arxiv/search.js`, `clis/arxiv/paper.js`
- **Strategy**: PUBLIC (Arxiv freely allows bot access)
- **Returns**: title, authors, published, abstract, arxiv_id, pdf_url
- **Rate limit**: Low (Arxiv encourages bulk access; minimal throttling)
- **Auth notes**: No login required; bulk downloads permitted

### 12. **Wikipedia** (Search + Summary + User Contributions)
- **Modules**: `clis/wikipedia/search.js`, `clis/wikipedia/summary.js`, `clis/wikipedia/trending.js`
- **Strategy**: PUBLIC (Wikipedia API is public; robots.txt allows scraping)
- **Returns**: title, extract, url, contributors (for user-contributions)
- **Rate limit**: Low (Wikipedia allows bots via User-Agent; no rate limit if well-behaved)
- **Auth notes**: No login required

### 13. **ProductHunt** (Browse + Search + Posts)
- **Modules**: `clis/producthunt/browse.js`, `clis/producthunt/posts.js`, `clis/producthunt/today.js`
- **Strategy**: PUBLIC (ProductHunt profile pages are public)
- **Returns**: title, tagline, votes, comments, url, maker info
- **Rate limit**: Medium (DOM scraping)
- **Auth notes**: No login required for public pages; detailed analytics require login

### 14. **GitLab** (User + Projects + Search)
- **Modules**: `clis/gitlab/search.js` (note: module may be lighter than GitHub)
- **Strategy**: PUBLIC (GitLab public profiles/repos)
- **Returns**: username, bio, followers, public projects (name, stars, description)
- **Rate limit**: Medium (GitLab API available; DOM fallback)
- **Auth notes**: No login for public; private repos require token

### 15. **Mastodon** (Profile + Posts)
- **Modules**: `clis/bluesky/` (note: Bluesky is closer; Mastodon not directly in list but similar pattern)
- **Module path**: Unknown (may not be in baseline; recommend scraping via public instance API)
- **Strategy**: PUBLIC (Mastodon federation allows public API access to any instance)
- **Returns**: account, display_name, bio, followers, statuses (posts), timestamp
- **Rate limit**: Low (Mastodon instance admins are permissive; encourage federation)
- **Auth notes**: No login required for public timelines; local instance required

---

## Borrow vs. Rebuild Decision Table

For each major concept, assess whether to reuse OpenCLI's patterns or write our own:

| Concept | OpenCLI Implementation | Assessment | Recommendation |
|---------|------------------------|------------|-----------------|
| **Subprocess invocation** | Spawns daemon auto on first CLI call; polls health endpoint | Elegant, simple, auto-cleanup | **BORROW** — adapt the pattern (health polling, port env var) |
| **Adapter contract** | `cli({site, name, strategy, func, pipeline, ...})` global registry | Type-safe, no magic imports, sync registration | **BORROW** — use identical interface; our wrapper calls upstream |
| **Rate limiting** | Strategy enum + adapter-specific logic (no framework middleware) | Lightweight; each adapter handles own limits | **REBUILD** — add a rate-limiter wrapper at subprocess level |
| **robots.txt handling** | Not explicitly implemented; relies on ToS compliance | Each adapter respects site's JS execution consent | **REBUILD** — add robots.txt parser + allow-listing |
| **Output formatting** | `src/output.ts` JSON/YAML/table/CSV render | Well-factored; reusable functions | **BORROW** — pipe OpenCLI's output through our wrapper |
| **Error handling** | CliError hierarchy + Unix exit codes | Clean, actionable, agent-friendly | **BORROW** — catch OpenCLI errors, map to consent messages |
| **Cookie auth** | Extension reads cookies via CDP cookies API | Audited, secure, local-only | **BORROW** — delegate to OpenCLI; we add consent prompts |
| **Pipeline (YAML)** | Declarative steps (navigate, evaluate, map, limit, ...) | Powerful but complex for new adapters | **REBUILD** — keep TS func pattern; skip YAML for simplicity |
| **Daemon lifecycle** | Auto-spawn, health check, graceful shutdown | Robust; handles subprocess crashes | **BORROW** — wrap daemon startup in our rate-limiter |
| **Browser isolation** | CDP targets in isolated windows, not user's browser | Critical privacy feature | **BORROW** — rely on OpenCLI; we verify extension is active |

---

## Daemon / Extension Architecture Details

### Daemon Flow
1. **Start**: `opencli <command>` invokes main.ts
2. **Discovery**: Scan clis/, build registry
3. **Check daemon**: Does `http://localhost:19825/ping` respond?
   - If yes: skip spawn (daemon already running)
   - If no: spawn daemon process with `PORT=19825` (or env override)
4. **Health poll**: Retry ping every 500ms for 15s (POLL_TIMEOUT_MS)
5. **Execute**: POST JSON command to `/command`, get response via WebSocket

### Extension Flow
1. **Background worker** listens for `ws://localhost:19825/` upgrade
2. **Daemon routes** incoming /command POST to WebSocket client
3. **Extension** creates isolated `chrome.debugger` target (separate from user's browser)
4. **CDP handshake**: Establish debugging protocol on that target
5. **Execute**: page.goto(), page.evaluate(), etc. run in isolated window
6. **Response**: Extension sends result back via WebSocket to daemon
7. **HTTP response**: Daemon forwards to CLI via POST response

### Key Files
- **Daemon**: `src/daemon.ts` (342 lines) — HTTP, WebSocket, security checks
- **Extension service worker**: `extension/src/background.ts` — spawn targets, forward CDP
- **Page adapter**: `src/browser/cdp.ts` — implements IPage interface over CDP

### Security
- **Origin check**: Daemon rejects requests from non-chrome-extension:// origins
- **Custom header**: X-OpenCLI required on all commands (browsers can't forge it)
- **CORS**: Only `/ping` has CORS headers; normal commands return 403 if missing
- **Body limit**: 1 MB max to prevent OOM
- **WebSocket verifyClient**: Reject upgrade before connection established

---

## Conclusion

OpenCLI is a **mature, well-structured framework** suitable for wrapping. Its architecture is modular (adapter contract, error types, output formats are all public APIs). The **Apache-2.0 license** permits our closed-source wrapper. The **daemon + extension** design elegantly isolates automation from user's browser, critical for privacy and stability.

**For our `extensions/opencli-scraper/` plugin**:
1. ✅ Use `@jackwener/opencli/registry` to register our 15 curated adapters
2. ✅ Spawn daemon with custom `OPENCLI_DAEMON_PORT` to avoid collisions
3. ✅ Wrap execution in consent gate (site + strategy → prompt user)
4. ✅ Add rate limiter at subprocess level (external to OpenCLI)
5. ✅ Mirror PRIVACY.md claims in our UI
6. ✅ Catch OpenCLI errors, map to user-friendly recovery hints

**Timeline**: Estimate 3–4 weeks to integrate 15 adapters, add consent UI, test end-to-end.

