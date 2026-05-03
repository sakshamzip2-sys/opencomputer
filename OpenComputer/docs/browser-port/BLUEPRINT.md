# OpenClaw browser port — BLUEPRINT

> Authoritative design doc for porting OpenClaw's browser plugin into OpenComputer's
> [extensions/browser-control/](../../extensions/browser-control/). This is the *contract* between the orchestrator
> session and any sister implementation sessions.
>
> Reference material: [docs/refs/openclaw/browser/](../refs/openclaw/browser/) — six subsystem deep-dives.
> Live coordination: [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
>
> Last updated: 2026-05-03

---

## 1. Goal

Replace the current 5+6 tool [extensions/browser-control/](../../extensions/browser-control/) with a vertically-integrated browser control plugin that ports OpenClaw's architecture (TS → Python), preserves OpenComputer's plugin SDK boundary, and leaves a clean seam for Hermes-style alternate providers (Browserbase / Firecrawl / Camoufox) and a future computer-use track.

This is not a port-by-line-translation. It's a port of *intent + invariants*. We faithfully reproduce the architectural decisions and security perimeter, fix bugs we found in OpenClaw along the way (see "Bugs we don't reproduce"), and idiomize to Python where TS-isms don't carry.

## 2. Locked architectural decisions

| Decision | Choice | Rationale |
|---|---|---|
| Scope of replacement | **Clean replace** of [extensions/browser-control/](../../extensions/browser-control/) — no v2 side-by-side | User direction. Simpler, no permanent surface bloat. |
| Tool surface | **Single `Browser` tool with two-level discriminator** (outer `action` 16 values + inner `act.kind` 11 values) | Matches OpenClaw verbatim. Token-efficient; clean provider seam; semantic split between page-level and element-level actions. |
| Migration of existing 5+6 tool names | **Deprecation shims** that dispatch to `Browser` and emit a one-time warning per name; sunset after one release | Non-breaking upgrade; clear migration signal; not permanent surface bloat. |
| Profile / browser config storage | Nest under `~/.opencomputer/<profile>/config.yaml` as a `browser:` key | Matches every other plugin's config convention in OpenComputer. |
| Control HTTP server | Subprocess managed by the plugin; **dual transport** (HTTP for sandbox/remote, in-process dispatcher for local) — mirrors OpenClaw's [client-fetch.ts](../refs/openclaw/browser/06-client-and-utils.md) dual path | Lets the plugin run in-process by default (cheaper) and as a remote-controllable HTTP service when needed (sandbox / multi-agent). |
| Auth | Bearer token + password modes; **distinct from** [browser-bridge/](../../extensions/browser-bridge/) listener token (different security perimeters) | The bridge is awareness ingestion (read-only); browser-control is bidirectional command. Don't conflate. |
| Hook integration | Every action fires `PreToolUse` / `PostToolUse` via OpenComputer's existing hook engine. SSRF/nav-guard runs *inside* the tool, additionally gated by `plugin_sdk.consent.CapabilityClaim` (`browser.navigate`, `browser.fill`, …) when F1 lands | Defense in depth: hooks are the user's veto layer; SSRF guard is the platform's. |
| Provider seam | Abstract `BaseBrowserProvider` ABC behind the tool. Default provider = local Playwright (the OpenClaw port). Browserbase / Firecrawl / Camoufox plug in later as separate provider classes without touching tool code. | Hermes-style. Costs ~50 LOC of indirection upfront; saves a refactor later. |
| Computer-use boundary | `Browser` tool covers URL-scoped, sandboxable interactions inside the page DOM. **OS-level input** ([point_click.py](../../opencomputer/tools/point_click.py), [system_click.py](../../opencomputer/tools/system_click.py), [system_keystroke.py](../../opencomputer/tools/system_keystroke.py), [applescript_run.py](../../opencomputer/tools/applescript_run.py)) stays where it is. No accidental overlap. | Browser actions are deterministic relative to refs; OS input is screen-space. Different abstractions; keep them separate. |

## 3. Module layout

Final shape of [extensions/browser-control/](../../extensions/browser-control/) after the port:

```
extensions/browser-control/
├── plugin.py                  # entry: register(api) → registers Browser tool + service
├── plugin.json                # manifest
├── README.md                  # rewrite: what this is now
│
├── _tool.py                   # the single Browser tool + deprecation shims
│                              # NOTE: leading-underscore singular filename, not
│                              # `tools.py`, to dodge the sys.modules['tools']
│                              # race against coding-harness/tools/ — same
│                              # lesson PR #394 burned in for the legacy
│                              # _tools.py predecessor (W3, 2026-05-03).
├── schema.py                  # pydantic models for action params (flat schema for OpenAI compat)
│
├── server/                    # HTTP control surface — subsystem 05
│   ├── __init__.py
│   ├── app.py                 # FastAPI app factory
│   ├── auth.py                # bearer + password + timing-safe compare
│   ├── csrf.py                # loopback-only CSRF middleware
│   ├── middleware.py          # abort signal, json parser
│   ├── lifecycle.py           # startup / shutdown ordering
│   ├── dispatcher.py          # in-process dispatcher (the dual-transport other half)
│   ├── policy.py              # request-policy: which paths a profile can hit
│   └── routes/
│       ├── __init__.py
│       ├── basic.py           # /, /start, /stop, /profiles
│       ├── tabs.py            # /tabs, /tabs/open, /tabs/focus, /tabs/close
│       ├── agent.py           # /navigate, /snapshot, /screenshot, /pdf, /act
│       ├── storage.py         # /storage/cookies, /storage/local, /storage/session
│       └── observe.py         # /console, /errors, /requests
│
├── chrome/                    # Chrome process — subsystem 01
│   ├── __init__.py
│   ├── executables.py         # cross-platform binary detection
│   ├── launch.py              # spawn-and-wait
│   ├── lifecycle.py           # stop / kill / status
│   └── decoration.py          # Preferences JSON mutation (atomic write WITH fsync)
│
├── profiles/                  # config + profile resolution
│   ├── __init__.py
│   ├── config.py              # ResolvedBrowserConfig / ResolvedBrowserProfile dataclasses
│   ├── resolver.py            # resolve_browser_config / resolve_profile (pull-based, per-request)
│   ├── capabilities.py        # BrowserProfileCapabilities (uses_chrome_mcp, etc.)
│   └── service.py             # create / delete / reset profile
│
├── session/                   # CDP + Playwright — subsystem 02
│   ├── __init__.py
│   ├── cdp.py                 # connect_browser (dedup + retry + proxy bypass)
│   ├── helpers.py             # url redaction, cdp timeouts, target id
│   ├── playwright_session.py  # Browser/Context/Page wrapper + role-ref cache
│   ├── target_id.py           # page_target_id with /json/list HTTP fallback
│   └── nav_guard.py           # SSRF guard (page.route + ip block + post-nav re-validation)
│
├── tools_core/                # the workhorse — subsystem 03
│   ├── __init__.py
│   ├── interactions.py        # click/type/press/hover/drag/select/fill/scroll/wait/evaluate/close
│   ├── snapshot.py            # snapshot orchestration
│   ├── refs.py                # role-ref + aria-ref → Locator resolution
│   ├── downloads.py           # arm / trigger / capture / store
│   ├── dialog.py              # dialog arm
│   ├── file_chooser.py        # file chooser arm
│   ├── activity.py            # last-action timestamps
│   ├── storage.py             # cookies / localStorage / sessionStorage
│   ├── trace.py               # playwright tracing wrapper
│   ├── responses.py           # http response body reader (NOT envelope normalizer; see 03 deep-dive)
│   ├── state.py               # emulation knobs (offline / headers / geo / locale / timezone / device)
│   └── shared.py              # timeout clamps, scroll-into-view, common helpers
│
├── snapshot/                  # AI / Chrome MCP / role snapshot — subsystem 04
│   ├── __init__.py
│   ├── role_snapshot.py       # build_role_snapshot_from_aria_snapshot, RoleNameTracker
│   ├── snapshot_roles.py      # 46 ARIA role constants (17 interactive / 10 content / 19 structural)
│   ├── chrome_mcp.py          # MCP client for chrome-devtools-mcp subprocess
│   ├── chrome_mcp_snapshot.py # MCP tree → unified snapshot
│   └── screenshot.py          # 7-side × 6-quality compression grid
│
├── client/                    # client side — subsystem 06
│   ├── __init__.py
│   ├── fetch.py               # fetch_browser_json — dual transport (HTTP or in-process)
│   ├── auth.py                # is_loopback_host, header injection
│   ├── actions.py             # action wrapper class (BrowserActions)
│   ├── form_fields.py         # normalize fill payloads
│   ├── proxy_files.py         # decode + persist; recursive path rewrite (fixing OpenClaw shallow walk)
│   └── tab_registry.py        # session-scoped tab tracking + cleanup
│
├── server_context/            # orchestrator state — subsystem 05 cont'd
│   ├── __init__.py
│   ├── state.py               # BrowserServerState, ProfileRuntimeState dataclasses
│   ├── lifecycle.py           # per-profile startup / shutdown
│   ├── selection.py           # last_target_id fallback chain
│   └── tab_ops.py             # open / focus / close tab logic
│
├── providers/                 # Hermes-style provider seam (post-v0.1)
│   ├── __init__.py
│   ├── base.py                # BaseBrowserProvider ABC
│   └── playwright_local.py    # default: OpenClaw port (everything above wired together)
│   # later: browserbase.py, firecrawl.py, camoufox.py
│
├── _utils/                    # shared helpers
│   ├── __init__.py
│   ├── atomic_write.py        # write+fsync+rename (fixing OpenClaw's no-fsync gap)
│   ├── url_pattern.py         # exact / glob / substring (NO `?` wildcard — it's not implemented)
│   ├── safe_filename.py       # sanitize download filenames
│   ├── trash.py               # send2trash wrapper (replacing OpenClaw's buggy Linux fallback)
│   └── errors.py              # BrowserServiceError + status mapping
│
└── tests/
    ├── test_chrome_*.py
    ├── test_session_*.py
    ├── test_tools_core_*.py
    ├── test_server_*.py
    ├── test_client_*.py
    ├── test_snapshot_*.py
    └── test_e2e_*.py           # full integration: launch → navigate → snapshot → act
```

**Why this layout:** each top-level directory maps 1:1 to one of the six subsystem deep-dives in [docs/refs/openclaw/browser/](../refs/openclaw/browser/). A sister session implementing subsystem N reads exactly one deep-dive + works in exactly one directory. Cross-cuts go through `_utils/`.

## 4. Dependency stack

Add to [pyproject.toml](../../pyproject.toml) under an opt-in extra `[browser]`:

```toml
[project.optional-dependencies]
browser = [
    "playwright>=1.45",         # Chrome via CDP
    "fastapi>=0.110",           # control HTTP server
    "uvicorn>=0.27",            # ASGI runner for FastAPI
    "httpx>=0.27",              # client transport (+ retries, abort, redirect hooks)
    "mcp>=1.0",                 # Chrome MCP subprocess client (existing-session profile)
    "Pillow>=10",               # screenshot resize + JPEG compression
    "send2trash>=1.8",          # cross-platform trash (replacing OpenClaw's buggy fallback)
    "pydantic>=2.7",            # action schemas
]
```

**Already present in OpenComputer:** `pydantic` (used elsewhere), `httpx` (used by providers). The new ones are `playwright`, `fastapi`, `uvicorn`, `mcp`, `Pillow`, `send2trash`.

**Runtime requirement** (one-time, gated by `opencomputer doctor browser`):
- `playwright install chromium` — downloads bundled Chromium (~150 MB)
- For `existing-session` profile only: Node ≥ 18 on PATH (so `npx chrome-devtools-mcp@latest` works)

## 5. Tool surface — the `Browser` tool

**One tool registered.** Name: `Browser`. Two-level discriminator:

```python
# Outer action discriminator (16 values)
class BrowserAction(str, Enum):
    STATUS = "status"
    START = "start"
    STOP = "stop"
    PROFILES = "profiles"
    TABS = "tabs"
    OPEN = "open"
    FOCUS = "focus"
    CLOSE = "close"
    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"
    NAVIGATE = "navigate"
    CONSOLE = "console"
    PDF = "pdf"
    UPLOAD = "upload"
    DIALOG = "dialog"
    ACT = "act"   # nested kind discriminator below

# Inner act.kind discriminator (11 values, used only when action == ACT)
class BrowserActKind(str, Enum):
    CLICK = "click"
    TYPE = "type"
    PRESS = "press"
    HOVER = "hover"
    DRAG = "drag"
    SELECT = "select"
    FILL = "fill"
    RESIZE = "resize"
    WAIT = "wait"
    EVALUATE = "evaluate"
    CLOSE = "close"
```

**Schema shape.** Like OpenClaw, we deliberately use a **flat pydantic model** (not a discriminated union) because OpenAI's function-tool spec rejects nested `anyOf`. All fields optional; the runtime validates required fields per-action. See [browser-tool.schema.ts:87](../../../Downloads/Harnesses/openclaw-main/extensions/browser/src/browser-tool.schema.ts) for the precedent and rationale.

```python
class BrowserParams(BaseModel):
    action: BrowserAction
    target: Literal["sandbox", "host", "node"] | None = None
    profile: str | None = None
    target_url: str | None = Field(None, alias="targetUrl")
    target_id: str | None = Field(None, alias="targetId")
    # ... all the other optional fields ...
    request: ActRequest | None = None  # OR flat act params (legacy)
```

**Tool description** — short. The model needs to know "use this for browsing"; it learns the actions from the enum. Cribbed from [browser-tool.ts:381-390](../../../Downloads/Harnesses/openclaw-main/extensions/browser/src/browser-tool.ts):

> Control the browser via OpenComputer's browser control server (status/start/stop/profiles/tabs/open/snapshot/screenshot/navigate/act/...). Profile defaults to `openclaw` (isolated, agent-managed). Use `profile="user"` for the user's logged-in Chrome (host-only; existing-session). When using refs from snapshot (e.g. `e12`), keep the same tab: pass `targetId` from the snapshot response into subsequent actions.

## 6. Migration plan for existing 5+6 tool names

[extensions/browser-control/tools.py](../../extensions/browser-control/tools.py) currently registers 11 tools. We don't know all 11 names without reading that file, but the README cites the 5 base: `browser_navigate`, `browser_click`, `browser_fill`, `browser_snapshot`, `browser_scrape`.

**Strategy:**

1. New `Browser` tool ships with the discriminator surface.
2. Old tool names become **deprecation shims** in the same `tools.py`, each ~5 LOC:
   ```python
   @deprecated_alias(replacement="Browser(action='navigate', url=...)")
   class BrowserNavigate(BaseTool):
       async def execute(self, url: str, **kw) -> ToolResult:
           return await Browser().execute(action="navigate", url=url, **kw)
   ```
3. Each shim emits a *one-time-per-process* warning (so we don't spam logs).
4. Sunset window: **one minor release**. Mark in CHANGELOG: "0.X: deprecation; 0.X+1: removal."
5. Skills + documentation searched for old names; updated where they live.

This is a soft cutover. Nothing breaks day one; everyone migrates at their own pace; we don't carry the wrappers permanently.

## 7. Bugs we don't reproduce

The deep dives surfaced bugs / gaps in OpenClaw the Python port should *fix* rather than mirror:

| OpenClaw bug | OpenClaw file | Python fix |
|---|---|---|
| `chrome.profile-decoration.ts` writes `Preferences` JSON non-atomically (crash mid-write corrupts user profile) | [01-chrome-and-profiles](../refs/openclaw/browser/01-chrome-and-profiles.md) | `_utils/atomic_write.py` does `os.replace()` after writing tmp + `os.fsync()` |
| `output-atomic.ts` doesn't `fsync` before rename | [06-client-and-utils](../refs/openclaw/browser/06-client-and-utils.md) | Same `_utils/atomic_write.py`; fsync is mandatory |
| `trash.ts` Linux fallback is buggy | [06-client-and-utils](../refs/openclaw/browser/06-client-and-utils.md) | Use `send2trash` library |
| `proxy-files.ts` shallow-walks the result tree (deeply-nested results don't get rewritten) | [06-client-and-utils](../refs/openclaw/browser/06-client-and-utils.md) | `client/proxy_files.py` recurses |
| `url-pattern.ts` docstring claims `?` is a single-char wildcard — it isn't implemented | [05-server-and-auth](../refs/openclaw/browser/05-server-and-auth.md) | Either implement properly or drop the claim from the docstring |
| `roleRefsByTarget` is FIFO-by-insertion despite "LRU" naming | [02-cdp-and-session](../refs/openclaw/browser/02-cdp-and-session.md) | Use `collections.OrderedDict` with proper LRU semantics, OR keep FIFO but name it correctly |

## 8. Subsystem map ↔ deep dives

| Subsystem | Code dir | Deep dive | Brief (forthcoming) |
|---|---|---|---|
| Chrome process + profiles + config | [chrome/](../../extensions/browser-control/chrome/) + [profiles/](../../extensions/browser-control/profiles/) | [01-chrome-and-profiles.md](../refs/openclaw/browser/01-chrome-and-profiles.md) | BRIEF-01 |
| CDP + Playwright session + nav guard | [session/](../../extensions/browser-control/session/) | [02-cdp-and-session.md](../refs/openclaw/browser/02-cdp-and-session.md) | BRIEF-02 |
| Tools core (action workhorse) | [tools_core/](../../extensions/browser-control/tools_core/) | [03-pw-tools-core.md](../refs/openclaw/browser/03-pw-tools-core.md) | BRIEF-03 |
| AI / Chrome MCP / snapshot / screenshot | [snapshot/](../../extensions/browser-control/snapshot/) | [04-ai-and-snapshot.md](../refs/openclaw/browser/04-ai-and-snapshot.md) | BRIEF-04 |
| HTTP server + auth + routes + SSRF + lifecycle | [server/](../../extensions/browser-control/server/) + [server_context/](../../extensions/browser-control/server_context/) | [05-server-and-auth.md](../refs/openclaw/browser/05-server-and-auth.md) | BRIEF-05 |
| Client transport + tab registry + utilities | [client/](../../extensions/browser-control/client/) + [_utils/](../../extensions/browser-control/_utils/) | [06-client-and-utils.md](../refs/openclaw/browser/06-client-and-utils.md) | BRIEF-06 |

## 9. Dependency graph + parallelization plan

```
                  _utils/  ─────────────────────────────────────────┐
                                                                    │
                  profiles/  ←── chrome/  ←── session/  ←── tools_core/  ←── snapshot/  ←── server/  ←── tools.py + plugin.py
                                                  ↑                                    ↑
                                                  └──────── server_context/ ───────────┘
                                                                                       │
                                                                              client/  ┘
                                                                              (parallel; uses dispatcher)
```

**Parallelization waves:**

| Wave | Sessions in parallel | Modules | Rough effort |
|---|---|---|---|
| 0 — Foundation | 1 session | `_utils/`, `profiles/`, `chrome/` | 3–5 days |
| 1 — Core | 3 parallel | A: `session/` + `nav_guard` · B: `snapshot/` + Chrome MCP · C: `server_context/` | 1 week |
| 2 — Surface | 2 parallel | D: `tools_core/` (depends on `session/`) · E: `server/` routes (depends on session, snapshot, server_context) | 1 week |
| 3 — Wiring | 1 session | `client/`, `tools.py`, `plugin.py`, e2e tests | 4–5 days |
| 4 — Provider seam (post-v0.1) | 1 session | `providers/base.py` + retrofit `providers/playwright_local.py` | 2 days |

**Critical path:** profiles → chrome → session → tools_core → server → wiring. Anything else is parallel.

**Total**: ~4 weeks with 3-session peak parallelism. Solo: ~6–7 weeks.

## 10. Phasing — what ships when

**v0 (internal alpha)** — Foundation + Core (Waves 0+1 done):
- Can launch managed Chrome, attach CDP, snapshot a page, return role tree.
- No nav-guard, no tool surface, no auth. **Internal use only.**

**v0.1 (first usable release)** — Wave 2+3 done:
- Single `Browser` tool registered, all 16 actions working
- HTTP server with auth + SSRF guard
- Default `openclaw` profile (managed Chrome)
- `existing-session` profile (Chrome MCP) **deferred** (gates on Node availability + npx)
- Deprecation shims for old tool names
- Doctor row passes

**v0.2** — provider seam + existing-session:
- `BaseBrowserProvider` ABC stabilized
- Chrome MCP path (`existing-session` profile) shipped
- Provider stubs: Browserbase, Firecrawl, Camoufox (registration boilerplate; not full implementations)

**v0.3+** — provider implementations:
- Browserbase provider (cloud Chrome with anti-bot)
- Firecrawl provider (scrape-only fast path)
- Camoufox provider (stealth Firefox for detection-robust tasks)

## 11. Decisions deferred / open questions

These don't block writing the BLUEPRINT but need answers before coding the relevant module:

1. **Hot-reload of browser config** — OpenClaw is pull-based per-request (cheap; no watcher). We default to the same. Confirm? (Q for owner of profiles/.)
2. **Per-profile credential isolation** — OpenComputer's Phase 14.F is parked. Browser tokens currently live in the profile's `config.yaml`. Acceptable for v0.1?
3. **`_snapshot_for_ai` vs public `aria_snapshot()`** — playwright-python may not expose `_snapshot_for_ai`. Plan: ship "role" mode using `aria_snapshot()` for v0.1; add "aria" mode if the underscore API turns out to be reachable. (Q for owner of snapshot/.)
4. **Trace recording on day one** — heavy, gated behind a config flag? Default off seems right. Confirm.
5. **Sandbox / node target modes** — OpenClaw supports three targets: `host`, `sandbox`, `node`. We ship `host` for v0.1; the other two need OpenComputer's sandbox infra mature first. The schema accepts all three so we don't break later. Acceptable?
6. **Tool name** — `Browser` (PascalCase, matches OpenComputer convention) or keep OpenClaw's lowercase `browser`? Recommend `Browser`. Confirm.

## 12. How sister sessions consume this doc

Read in this order:

1. This file (BLUEPRINT.md) — get the architectural picture
2. [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) — see what's claimed, what's free
3. The deep-dive for your subsystem (`docs/refs/openclaw/browser/0X-*.md`) — read end-to-end
4. The brief for your subsystem (`BRIEF-0X-*.md`, forthcoming) — concrete to-do list
5. Begin work in `extensions/browser-control/<your-subsystem>/`. Open a branch `feat/browser-port-<subsystem>`. Update STATUS as you go.

You should not need to read the OpenClaw TS source to do your subsystem — the deep dive captures everything load-bearing, with file:line refs for when you do want the original.
