# Browser Control — OpenClaw Port

Vertically-integrated browser automation. One agent-facing tool
(`Browser`) backed by a managed Chrome process driven via Playwright /
CDP. Ports OpenClaw's TypeScript browser plugin into Python; preserves
its security perimeter; opens a clean seam for Hermes-style alternate
providers (Browserbase / Firecrawl / Camoufox) that ship in v0.7+.

**v0.4 (Wave 4) adds adapter promotion** — turn a successful Browser
flow into a permanent, deterministic, agent-callable tool. See the
[curated starter adapter pack](adapters/README.md) and the
[adapter-author skill](skills/adapter-author/SKILL.md) for the full
playbook. Companion plugin: [`adapter-runner`](../adapter-runner/) —
discovers `@adapter`-decorated recipes and registers each as a synthetic
`<Site><Name>` tool.

**Always on.** `enabled_by_default: true` is honored at runtime via
Layer D in `opencomputer/plugins/registry.py` even when a profile uses
a narrow explicit `enabled: [...]` allowlist. Why: `WebFetch` returns
empty HTML shells for SPA / client-rendered pages, so the agent needs
this to read modern websites.

**Default OFF for setup.** Opt in to the runtime by:

1. `pip install opencomputer[browser]` — pulls Playwright + FastAPI +
   uvicorn + httpx + Pillow + send2trash.
2. `playwright install chromium` — one-time, ~150 MB.
3. Restart your agent — the Browser tool registers automatically.

The plugin is registered regardless of those steps; the doctor row
tells you what's missing.

## The single tool — `Browser`

A two-level discriminator surface. Outer `action` (16 values) for
page-level / lifecycle operations; inner `act.kind` (11 values) for
element-level ones. Token-efficient, OpenAI/Anthropic-compatible, and
matches OpenClaw's TypeScript surface verbatim.

| `action` | What it does |
|---|---|
| `status` / `start` / `stop` / `profiles` | Profile lifecycle reads + writes |
| `tabs` / `open` / `focus` / `close` | Tab management |
| `snapshot` | ARIA-or-AI tree of the current page + refs |
| `screenshot` | Base64 PNG (full-page or element-scoped) |
| `navigate` | Move an existing tab to a URL (≠ `open`, which makes a new tab) |
| `console` / `pdf` | Read buffered console messages / save page to PDF |
| `upload` / `dialog` | Arm a file chooser / arm an alert/confirm/prompt response |
| `act` | Run an inner element-level operation — see `act.kind` below |

| `act.kind` | What it does |
|---|---|
| `click` / `type` / `press` / `hover` | Pointer + keyboard primitives |
| `drag` / `select` / `fill` | Drag-drop / dropdown select / form fill |
| `resize` / `wait` / `evaluate` | Viewport resize / explicit wait / arbitrary JS |
| `close` | Close the focused tab from inside an act batch |

Example call shapes (LLM-facing):

```jsonc
{ "action": "navigate", "url": "https://example.com" }
{ "action": "snapshot", "targetId": "ABCDEF1234" }
{ "action": "act", "request": { "kind": "click", "ref": "e12" } }
{ "action": "act", "kind": "fill", "ref": "e7", "text": "hello" }   // flat form OK
```

The flat form is convenience: the runtime composes the inner
`ActRequest` from sibling `kind` + `ref`/`text`/etc.

## Profiles — `openclaw` (default) vs `user` (existing-session)

| Profile | Browser | Persistent? | Use when |
|---|---|---|---|
| `openclaw` (default) | Managed Chromium (bundled by Playwright) | Yes — per-profile dir under `~/.opencomputer/<oc-profile>/browser/openclaw/` | Agent should browse on its own without touching the user's logins |
| `user` | The user's existing Chrome | Yes — operates on the user's own profile via Chrome MCP | Agent needs the user's logged-in session (Gmail, internal tools, etc.). Host-only. **v0.2.** |

Storage convention: every profile lives at
`~/.opencomputer/<profile>/config.yaml` under the `browser:` key —
matches the rest of OpenComputer's per-profile layout.

## Architecture

```
LLM → tool call ─►  Browser tool (_tool.py)
                    │
                    ▼
                client/actions.py (BrowserActions wrappers)
                    │
                    ▼
                client/fetch.py (dual-transport fork)
                    ├── HTTP (loopback only, auth-injected)
                    │       └─►  uvicorn → server/  (sandbox / remote)
                    │
                    └── in-process dispatcher
                            └─►  server/dispatcher.py → FastAPI app
                                    ├── auth + CSRF + body-limit middleware
                                    └── routes/  (~46 endpoints)
                                            └─►  server_context/  +  session/
                                                    ├── tools_core/ (act primitives)
                                                    ├── snapshot/ (ARIA tree builder)
                                                    └── chrome/ (Chrome process management)
                                                            └─►  Playwright over CDP
```

The dual transport is the load-bearing piece: the in-process
dispatcher (the path-only branch in `client/fetch.py`) makes the same
client API work without socket overhead in the typical local case;
the HTTP branch lights up for sandboxed / remote-controllable
deployments.

### Module layout

```
extensions/browser-control/
├── plugin.py                  # entry: register Browser + 11 shims + doctor row
├── plugin.json                # manifest
├── README.md                  # this file
├── _tool.py                   # Browser tool + 11 deprecation shims
├── schema.py                  # pydantic models + JSON Schema generator
│
├── client/                    # agent-side library
│   ├── fetch.py               # dual-transport request helper
│   ├── auth.py                # is_loopback_host, inject_auth_headers
│   ├── actions.py             # ~38 wrapper methods over the HTTP routes
│   ├── form_fields.py         # fill-payload normalizer
│   ├── proxy_files.py         # base64 decode + recursive path rewrite
│   └── tab_registry.py        # session-scoped tab tracking + cleanup
│
├── server/                    # FastAPI control surface (W2)
├── chrome/                    # Chrome process management (W0)
├── profiles/                  # config + profile resolver (W0)
├── session/                   # CDP + Playwright session (W1)
├── tools_core/                # act primitives — click/type/etc. (W2)
├── snapshot/                  # ARIA tree + screenshot grid (W1)
├── server_context/            # per-profile runtime state (W1)
├── _utils/                    # atomic_write, errors, trash, ...
│
├── adapters/                  # ⭐ v0.4 — curated starter adapter pack
│   ├── hackernews/top.py      #   PUBLIC: HN top stories
│   ├── arxiv/search.py        #   PUBLIC: arXiv search
│   ├── reddit/hot.py          #   PUBLIC: subreddit hot posts
│   ├── github/notifications.py # COOKIE/HEADER: GH notifications via token
│   ├── apple_podcasts/search.py # PUBLIC: iTunes Search API
│   ├── amazon/track_price.py  #   COOKIE: logged-in product price
│   ├── cursor_app/recent_files.py # INTERCEPT: Cursor.app via CDP
│   └── chatgpt_app/new_chat.py # INTERCEPT: ChatGPT desktop via CDP
│
└── skills/                    # ⭐ v0.4 — agent-side authoring helper
    └── adapter-author/SKILL.md
```

## v0.4 — adapter promotion (Wave 4)

Eight new actions on top of the v0.3 surface, plus a companion plugin:

| Action | Purpose |
|---|---|
| `network_start` / `network_list` / `network_detail` | Live capture + replay |
| `resource_timing` | Read `performance.getEntriesByType('resource')` from page (THE killer recon move) |
| `analyze` | One-shot site recon — pattern + candidate endpoints + neighbors |
| `adapter_new` / `adapter_save` | Scaffold or save a recipe |
| `adapter_validate` | Static checks |
| `verify` | Run an adapter against its `verify/<name>.json` fixture |

The `adapter-runner` plugin (sibling dir) discovers `@adapter`-decorated
modules and registers each as a synthetic `<Site><Name>` tool. The
[adapter-author skill](skills/adapter-author/SKILL.md) is the playbook;
the [bundled starter pack](adapters/README.md) is the reference.

## Migration from the legacy 5+6 tools

Until v0.X+1 the legacy names continue to work as deprecation shims:

| Legacy tool | New call |
|---|---|
| `browser_navigate(url)` | `Browser(action="navigate", url=...)` |
| `browser_click(url, selector)` | `Browser(action="act", kind="click", selector=..., url=...)` |
| `browser_fill(url, selector, value)` | `Browser(action="act", kind="fill", selector=..., text=..., url=...)` |
| `browser_snapshot(url)` | `Browser(action="snapshot", url=...)` |
| `browser_scrape(url, css_selector?)` | `Browser(action="snapshot", url=..., selector=...)` |
| `browser_scroll(url, direction?)` | `Browser(action="act", kind="press", key="PageDown" \| "PageUp" \| "Home" \| "End", url=...)` |
| `browser_back(url)` | `Browser(action="act", kind="press", key="Alt+ArrowLeft", url=...)` |
| `browser_press(url, key, selector?)` | `Browser(action="act", kind="press", key=..., selector=..., url=...)` |
| `browser_get_images(url, max_images?)` | `Browser(action="act", kind="evaluate", expression="document.images...", url=...)` |
| `browser_vision(url)` | `Browser(action="screenshot", url=...)` |
| `browser_console(url, max_messages?)` | `Browser(action="console", url=...)` |

Each shim emits a `DeprecationWarning` once per process the first
time it's called. Sunset: **one minor release.** Migrate skills and
docs that still use the legacy names.

## Doctor

Run `opencomputer doctor` to see the `browser-control` row. Probes:

1. `playwright` is importable — fails warn with the install hint if not.
2. `playwright.async_api` loads — warns on partial install.
3. If `OPENCOMPUTER_BROWSER_CONTROL_URL` is set, attempts a HEAD probe.
   Otherwise reports in-process dispatcher mode.

The chromium binary itself isn't probed (too heavy for a doctor pass);
runtime errors surface clearly when missing.

## Privacy contract

| Captured | Storage | Where it goes |
|---|---|---|
| Page URL + title + ARIA tree + visible text | In-process — RAM only | Tool result → agent loop |
| Cookies / localStorage / sessionStorage | Profile dir on disk (per the profile's persistence policy) | Stays in the browser profile |
| Screenshots | Profile media store (atomic write + fsync) | Tool result includes the local path |
| Console messages / network request log | RAM ring buffers (per profile) | Tool result |
| Auth tokens for the control service | In-process, never persisted in v0.1 | Loopback HTTP only |

**Network egress contract.** The agent-facing entry-point files
(`plugin.py`, `_tool.py`, `schema.py`) MUST NOT import HTTP clients
at module scope. Enforced by
`tests/test_browser_control_no_egress.py`. The `client/` and
`server/` subpackages legitimately use httpx / FastAPI / uvicorn /
websockets (BLUEPRINT.md §4) and are exempt.

The control service binds to `127.0.0.1` only; cross-host calls from
`client/fetch.py` are explicitly refused (`Refusing to call
non-loopback browser control URL`). The auth header is attached only
when the URL is loopback.

## Hooks + consent

Every Browser action is gated by `plugin_sdk.consent.CapabilityClaim`
through the F1 ConsentGate when present. Default tier is `EXPLICIT`
(navigation / click / fill / press); read-only operations
(`snapshot` / `scrape` / `console`) declare `IMPLICIT`. The hook
engine fires `PreToolUse` / `PostToolUse` for every dispatch — your
hooks (settings-declared or plugin-declared) can veto on the same
event surface as any other tool.

## CAUTION

- Browser actions can submit forms and interact with arbitrary site
  JS. Treat them as side-effecting. The loop's PreToolUse hook is the
  user's veto layer.
- Don't `fill` passwords or credit-card numbers into the agent — the
  Browser tool will type them into the page DOM where the page's JS
  can read them. Always staffed by a hook in production setups.
- The `existing-session` profile (`profile="user"`) operates on the
  user's actual Chrome. v0.2.

## See also

- `docs/browser-port/BLUEPRINT.md` — design doc (sections 1–10
  load-bearing).
- `docs/browser-port/IMPLEMENTATION_STATUS.md` — wave / sister-session
  coordination.
- `docs/refs/openclaw/browser/` — six subsystem deep dives that
  underpin the port.
