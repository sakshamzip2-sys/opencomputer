# Wave 4 — Adapter Promotion BLUEPRINT

> Authoritative design doc for v0.4.0 of the `browser-control` plugin: turning successful Browser-tool flows into deterministic, reusable, agent-callable tools (adapters). Mirrors OpenCLI's adapter model in Python.
>
> Reference: [/Users/architsakri/Desktop/opencli-plugin-learnx-atria/BUILD.md](/Users/architsakri/Desktop/opencli-plugin-learnx-atria/BUILD.md) is the ground-truth UX we're replicating. Everything in this BLUEPRINT was either explicitly used in that build or directly derived from it.
>
> Live coordination: top-level [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) — Wave 4 row.
>
> Deferred features (v0.5+): [DEFERRED.md](DEFERRED.md).
>
> Last updated: 2026-05-04 by s1.

---

## 1. Goal

When a user does a one-off task on a website and the agent figures it out, **let the agent crystallize that working flow into a permanent, deterministic tool the user can re-invoke without any LLM in the loop.**

```
Day 1, first task:
  User:   "Show me my Atria assignments"
  Agent:  [no AtriaAssignments tool exists → uses Browser tool to figure it out]
          [Browser(action="navigate") → resource_timing → finds tRPC API → fetches → returns]
          "Found 8 assignments. Want me to crystallize this into a permanent tool?"
  User:   yes

Day 2, same task:
  User:   "Show me my Atria assignments"
  Agent:  AtriaAssignments() → 200ms, deterministic, free.
```

This is the core of OpenCLI's value proposition translated to OpenComputer's plugin shape.

## 2. Locked architectural decisions

These were settled across multiple rounds of discussion + a deep audit of OpenCLI's source + the user's real-world LearnX adapter build. Do not flip without re-grounding in those discussions.

| # | Decision | Choice |
|---|---|---|
| 1 | Recipe format | **Python module with `@adapter` decorator + `async def run(args, ctx)`**. NOT a pipeline DSL. Imperative Python wins for non-trivial flows (matches the user's `trpcQuery` helper pattern from LearnX). |
| 2 | Strategy enum | **`PUBLIC \| COOKIE \| UI \| INTERCEPT`** (4 values, not the previously-mistaken `HEADER`). `UI` means "drive the browser like a human"; `HEADER` is a sub-case of COOKIE. |
| 3 | Network capture | First-class. **`Browser(action="resource_timing")` is the killer recon move**, not live `network_list` — works on already-loaded pages where live capture misses everything (your LearnX BUILD.md Phase 2 dead-end). |
| 4 | Parameter extraction | **Explicit, agent-declared at adapter authoring time.** No auto-detect. `args=[{"name": "course", "type": "string", ...}]` in the decorator. |
| 5 | Tool registration | **Separate generated tools per adapter.** Names PascalCase (`AtriaAssignments`, `HackernewsTop`). Adapter-runner plugin discovers + registers at boot. |
| 6 | Sharing path | **Plugin packaging template ships in v0.4** (not deferred). `opencomputer plugin new --template adapter-pack` scaffolds; `opencomputer plugin install file://` symlinks; `opencomputer plugin install github:user/repo` clones+installs. |
| 7 | Origin pre-warm | **`ctx.fetch_in_page` auto-pre-warms the origin** before page.evaluate. Solves the LearnX "cannot find default execution context" dead-end transparently. |
| 8 | Site memory | **Mirrors OpenCLI's directory structure verbatim**: `~/.opencomputer/<profile>/sites/<site>/{endpoints.json, field-map.json, notes.md, verify/, fixtures/}`. |

## 3. Module layout

Final shape after Wave 4:

```
extensions/browser-control/
├── _tool.py                      # EXISTING — adds 8 new actions (see §5)
├── _dispatcher_bootstrap.py      # EXISTING — unchanged
├── client/                       # EXISTING — unchanged
├── server/                       # EXISTING — unchanged
├── session/                      # EXISTING — unchanged
├── snapshot/                     # EXISTING — unchanged
├── chrome/                       # EXISTING — unchanged
├── profiles/                     # EXISTING — unchanged
├── server_context/               # EXISTING — unchanged
├── _utils/                       # EXISTING — adds new typed errors
│   └── errors.py                 #   adds AuthRequiredError, AdapterError, etc.
│
├── adapters/                     # NEW — bundled curated starter pack
│   ├── hackernews/
│   │   └── top.py
│   ├── arxiv/
│   │   └── search.py
│   ├── reddit/
│   │   └── hot.py
│   ├── github/
│   │   └── notifications.py
│   ├── apple_podcasts/
│   │   └── search.py
│   ├── amazon/
│   │   └── track_price.py
│   ├── cursor_app/
│   │   └── recent_files.py
│   └── chatgpt_app/
│       └── new_chat.py
│
└── skills/                       # NEW — authoring helper skill
    └── adapter-author/
        └── SKILL.md              # ~500 lines docs; mirrors OpenCLI's opencli-adapter-author

extensions/adapter-runner/        # NEW PLUGIN
├── plugin.py                     # discovery + registration on boot
├── plugin.json                   # manifest
├── README.md
├── _decorator.py                 # @adapter decorator + AdapterSpec dataclass
├── _strategy.py                  # Strategy enum (PUBLIC|COOKIE|UI|INTERCEPT)
├── _ctx.py                       # AdapterContext: fetch, fetch_in_page, evaluate, navigate, network, site_memory
├── _runner.py                    # invokes adapter.run(args, ctx); handles errors, traces
├── _site_memory.py               # endpoints.json / field-map.json / notes.md handling
├── _trace.py                     # adapter run trace recording
├── _verify.py                    # verification fixture engine
├── _discovery.py                 # walks ~/.opencomputer/<profile>/adapters/**, plus extensions/*/adapters/**
└── _validation.py                # static checks (required fields, importability)

opencomputer/cli_plugin_scaffold.py  # EXISTING — extend to register adapter-pack template
```

## 4. Recipe / adapter format

A Python module exporting an `@adapter`-decorated `async def run(args, ctx)`:

```python
# Example: extensions/browser-control/adapters/hackernews/top.py

from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="hackernews",
    name="top",
    description="Hacker News top stories",
    domain="news.ycombinator.com",
    strategy=Strategy.PUBLIC,
    browser=False,
    args=[
        {"name": "limit", "type": "int", "default": 20, "help": "Number of stories"},
    ],
    columns=["rank", "title", "score", "author", "comments"],
)
async def run(args, ctx):
    ids = await ctx.fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    ids = ids[: min(args["limit"] + 10, 50)]
    results = []
    for idx, item_id in enumerate(ids):
        item = await ctx.fetch(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if not item.get("title") or item.get("deleted") or item.get("dead"):
            continue
        results.append({
            "rank": idx + 1,
            "title": item["title"],
            "score": item["score"],
            "author": item["by"],
            "comments": item.get("descendants", 0),
            "url": item.get("url"),
        })
        if len(results) >= args["limit"]:
            break
    return results
```

For a browser-required adapter (mirrors the user's LearnX `assignments.js`):

```python
# Example: ~/.opencomputer/<profile>/adapters/atria/assignments.py

from extensions.adapter_runner import adapter, Strategy
from extensions.adapter_runner.errors import AuthRequiredError

@adapter(
    site="atria",
    name="assignments",
    description="Atria University LMS assignments",
    domain="learnx.atriauniversity.in",
    strategy=Strategy.COOKIE,
    browser=True,
    args=[
        {"name": "course", "type": "string", "default": "", "help": "Filter by course"},
    ],
    columns=["course", "type", "title", "due", "max_marks", "status"],
)
async def run(args, ctx):
    # ctx.fetch_in_page auto-pre-warms the origin (no manual page.goto needed)
    runs = await ctx.trpc_query("run.getAllForUser")  # helper provided by ctx
    activities = []
    for r in runs:
        items = await ctx.trpc_query(
            "activityWindow.getForRun",
            input={"runId": r["id"]},
        )
        for item in items:
            if item["isLocked"] or item["isDisabled"]:
                continue
            activities.append({
                "course": r["name"],
                "type": item["type"],
                "title": item["title"],
                "due": item["windowEnd"],
                "max_marks": item.get("maxMarks", 0),
                "status": "graded" if item.get("gradePublished") else "pending",
            })
    if args["course"]:
        activities = [a for a in activities if args["course"].lower() in a["course"].lower()]
    return sorted(activities, key=lambda a: a["due"])
```

The `@adapter` decorator does the following:
- Registers an `AdapterSpec` in a module-level registry
- Validates the metadata (required fields, valid Strategy, etc.)
- Wraps the `run` function with the runner (so it can be invoked from a generated tool)
- Records the adapter's source path so trace artifacts can reference it

## 5. New `Browser` tool actions

8 additions on top of v0.3's 16 actions:

| Action | Purpose | Approximate LOC |
|---|---|---|
| `network_start` | Begin capturing network requests on the active page | ~30 |
| `network_list` | Return captured requests (URL, method, status, request/response body shapes) | ~60 |
| `network_detail` | Get full body for one specific request by index/key | ~40 |
| **`resource_timing`** | Read `performance.getEntriesByType('resource')` from page context — primary recon tool | ~40 |
| `analyze` | One-shot recon: navigates to URL, runs state + resource_timing + neighbor-adapter lookup, returns "use Pattern X, endpoint Y, similar to adapter Z" | ~120 |
| `adapter_new` | Scaffold adapter file `<site>/<name>.py` from a template | ~80 |
| `adapter_save` | Replay last successful flow as a recipe; write Python module to disk | ~120 |
| `adapter_validate` | Static checks: required fields, valid Strategy, no duplicate (site, name), importability | ~80 |

Plus a verify path (used by adapters once authored):

| Action | Purpose | LOC |
|---|---|---|
| `verify` | Run an adapter against its `verify/<name>.json` fixture; report mismatches | ~150 |

Total: ~720 LOC across the existing `_tool.py` and new helpers.

## 6. `adapter-runner` plugin

The new plugin that turns adapter files into registered OpenComputer tools.

### Discovery

On `register(api)`:
1. Walk `~/.opencomputer/<profile>/adapters/**/*.py` (user-authored)
2. Walk `extensions/browser-control/adapters/**/*.py` (bundled starter pack)
3. Walk `extensions/<other-plugin>/adapters/**/*.py` (installed adapter-pack plugins)
4. Import each module; find `@adapter`-decorated functions; collect `AdapterSpec`s
5. For each spec, generate a synthetic `BaseTool` class:
   ```python
   class AdapterTool(BaseTool):
       def __init__(self, spec: AdapterSpec) -> None:
           self._spec = spec
       
       @property
       def schema(self) -> ToolSchema:
           return ToolSchema(
               name=f"{spec.site_pascal}{spec.name_pascal}",  # e.g. "AtriaAssignments"
               description=spec.description,
               parameters=spec_to_json_schema(spec),
           )
       
       async def execute(self, call: ToolCall) -> ToolResult:
           ctx = AdapterContext.create(spec=self._spec, profile=...)
           try:
               result = await spec.run(call.arguments, ctx)
           except AuthRequiredError as e:
               return ToolResult(is_error=True, content=str(e), code="auth_required")
           except AdapterError as e:
               return ToolResult(is_error=True, content=str(e), code=e.code)
           return ToolResult(content=spec.format_output(result))
   ```
6. `api.register_tool(AdapterTool(spec))` for each one

### `AdapterContext` (the `ctx` parameter passed to `run`)

```python
class AdapterContext:
    site: str
    spec: AdapterSpec
    site_memory: SiteMemory  # read/write API to ~/.opencomputer/<profile>/sites/<site>/

    async def fetch(self, url: str, *, method: str = "GET", headers=None, body=None) -> Any:
        """Plain HTTP. No browser. Strategy.PUBLIC adapters use this exclusively."""

    async def fetch_in_page(self, url: str, *, method: str = "GET", headers=None, body=None) -> Any:
        """HTTP via page.evaluate. Pre-warms origin if needed. Strategy.COOKIE path."""

    async def navigate(self, url: str) -> dict:
        """Browser.navigate equivalent. Strategy.UI / INTERCEPT use this."""

    async def evaluate(self, js: str) -> Any:
        """Runtime.evaluate in the active page."""

    async def network_list(self, url_pattern: str | None = None) -> list[dict]:
        """All captured network requests."""

    async def trpc_query(self, procedure: str, input: dict | None = None) -> Any:
        """Convenience helper for tRPC sites (matches the user's LearnX trpcQuery helper)."""

    async def click(self, ref: str) -> dict:
        """For Strategy.UI adapters."""

    # ... and others mirroring the Browser tool's act-kinds
```

### Lifecycle: when does `adapter-runner` boot?

- Loaded as a plugin during normal OpenComputer plugin discovery (no special hooks)
- `register(api)` runs at agent startup; discovery + tool registration happens there
- For Strategy.PUBLIC adapters, the dispatcher / Browser server doesn't even spawn — `ctx.fetch` uses plain `httpx` directly
- For Strategy.COOKIE / UI / INTERCEPT, ctx falls through to `Browser(action=...)` calls under the hood, which lazily-bootstraps the dispatcher (Wave 3 hotfix path) on first use

## 7. Site memory

Per-site, per-profile knowledge base. **Persists across sessions** — this is what makes the second adapter for the same site near-instant.

```
~/.opencomputer/<profile>/sites/<site>/
├── endpoints.json
│   { "topstories": {
│       "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
│       "method": "GET",
│       "params": { "required": [], "optional": [] },
│       "response": { "type": "array<int>", "shape_summary": "..." },
│       "verified_at": "2026-05-04",
│       "notes": "Returns up to ~500 IDs; most active stories at the front"
│     }, ... }
│
├── field-map.json
│   { "play": {
│       "meaning": "view count",
│       "verified_at": "2026-04-28",
│       "source": "Bilibili API field"
│     }, ... }
│
├── notes.md          # running agent notes; manual append-only
│
├── verify/<name>.json  # verification fixtures per adapter command
│   { "args": {"limit": 5},
│     "rowCount": {"min": 5, "max": 5},
│     "columns": ["rank", "title", "score", "author", "comments"],
│     "patterns": { "rank": "^\\d+$", "title": ".+" },
│     "notEmpty": ["title", "author"]
│   }
│
└── fixtures/<name>-<timestamp>.json  # full sample API responses (PII-stripped)
```

`SiteMemory.read(key) -> Any` and `SiteMemory.write(key, value)` for the agent during authoring. Concurrency: per-file locks on writes (use existing `_utils/file_lock.py`).

## 8. Plugin scaffolding template (promoted from v0.5)

`opencomputer plugin new --template adapter-pack <name>` extends the existing scaffolder:

```
extensions/<plugin-name>/
├── plugin.json             { "id": "<plugin-name>", "kind": "adapter-pack", ... }
├── plugin.py               from extensions.adapter_runner import register_adapter_pack
│                           def register(api):
│                               register_adapter_pack(api, adapters_dir=Path(__file__).parent / "adapters")
├── adapters/<site>/*.py    the actual adapter files
├── README.md               template README
└── pyproject.toml          for pip-installability
```

Then:

- `opencomputer plugin install file:///path/to/dir` — symlinks into `~/.opencomputer/<profile>/plugins/`
- `opencomputer plugin install github:user/repo` — clones + installs
- adapter-runner picks up the new plugin's adapters automatically on next boot

## 9. Authoring helper skill

`extensions/browser-control/skills/adapter-author/SKILL.md` — markdown skill the agent reads when asked "make me a CLI for site X." Mirrors OpenCLI's [`opencli-adapter-author/SKILL.md`](https://github.com/jackwener/OpenCLI/blob/main/skills/opencli-adapter-author/SKILL.md) decision tree:

```
START
  │
  ▼
Browser(action="status") → doctor green?
  │ yes
  ▼
Read site memory: ~/.opencomputer/<profile>/sites/<site>/{endpoints.json, notes.md}
  │ hit → jump to endpoint validation; never skip authoring
  │ miss → continue
  ▼
Site recon (Pattern detection):
  Browser(action="analyze", url=...) → returns Pattern A/B/C/D/E + best endpoint
  │
  ▼
API discovery (5 patterns, in cost order):
  §1 network        — page calls JSON API directly (Browser network_list / resource_timing)
  §2 state          — data in __INITIAL_STATE__ (Browser evaluate)
  §3 bundle         — data in JS bundle / <script src>
  §4 token          — auth token from separate endpoint
  §5 intercept      — must drive browser end-to-end
  │
  ▼
Endpoint validation (memory hit OR fresh):
  ctx.fetch() the candidate; assert 200 + non-empty data
  │
  ▼
Field decode → column design → Browser(action="adapter_new") → fill in run()
  │
  ▼
Browser(action="adapter_validate") → static checks pass
  │
  ▼
Live verify: agent invokes the new tool itself, confirms output matches expectations
  │
  ▼
Browser(action="verify_write_fixture") → save expected shape
  │
  ▼
Write site memory: endpoints.json + field-map.json + notes.md
  │
  ▼
DONE
```

The skill is the agent's playbook. It triggers when:
- User asks "make a CLI for X" / "save this as a permanent tool"
- Agent recognizes high repeatability score after a successful Browser flow

## 10. Curated starter adapter pack

8 adapters bundled with `extensions/browser-control/adapters/`, demonstrating every Strategy tier:

| Adapter | Strategy | Demonstrates | Approx LOC |
|---|---|---|---|
| `hackernews/top` | PUBLIC | Pure-API, no browser, plain httpx | 50 |
| `arxiv/search` | PUBLIC | Search-API with query parameters | 60 |
| `reddit/hot` | PUBLIC | Subreddit list with optional auth | 70 |
| `github/notifications` | COOKIE/HEADER | Token-auth via env var | 70 |
| `apple_podcasts/search` | PUBLIC | iTunes Search API | 50 |
| `amazon/track_price` (US) | COOKIE | Browser-required (logged-in cart) | 90 |
| `cursor_app/recent_files` | INTERCEPT | Electron app control via CDP | 80 |
| `chatgpt_app/new_chat` | INTERCEPT | Another Electron app | 80 |

Total: ~550 LOC + ~150 LOC of fixtures (verify/<name>.json each).

## 11. Tier S add-ons (folded into v0.4)

Three features that move the experience from "good" to "wow":

### `Browser(action="analyze", url=...)`

One-shot site recon. Equivalent to running:
```
navigate(url) → wait_for_load → state → resource_timing → 
search neighbor adapters in registry → detect anti-bot signals
```

Returns:
```json
{
  "pattern": "A",  // §1 network — page calls API directly
  "candidate_endpoints": [
    {"url": "https://api.bilibili.com/x/web-interface/popular?...",
     "method": "GET", "response_shape": "array<obj>", "auth": "cookie"},
    ...
  ],
  "neighbor_adapters": ["bilibili/dynamic", "bilibili/feed"],
  "anti_bot": {"detected": false, "indicators": []}
}
```

Collapses the agent's discovery loop from ~10 turns to 1.

### Verification fixtures

`verify/<name>.json` per adapter command. `Browser(action="verify", adapter="atria/assignments")` runs the adapter and asserts:
- `rowCount` is in expected range
- `columns` match exactly
- `types` per column match
- `patterns` regexes hold for each row's values
- `notEmpty` fields are non-empty

Failed verifications leave a diff report. **This is what catches "did the LMS change their API?" automatically** — runs in CI or on-demand.

### Trace artifacts

Every adapter run can be invoked with `--trace on`. Dumps to `~/.opencomputer/<profile>/traces/<adapter>-<timestamp>/`:
- `trace.zip` — Playwright trace viewer compatible
- `summary.md` — agent-readable: which step ran, network calls observed, errors if any
- `screenshots/` — captured during run if `Browser(action="screenshot")` was called

Foundation for v0.5's autofix flow.

## 12. Typed errors

In `extensions/browser-control/_utils/errors.py`:

```python
class AuthRequiredError(BrowserServiceError):
    """Adapter detected the user isn't logged in / auth expired."""
    code = "auth_required"
    exit_code = 77  # EX_NOPERM

class AdapterEmptyResultError(BrowserServiceError):
    """Adapter ran without error but returned no rows."""
    code = "empty_result"
    exit_code = 66  # EX_NOINPUT

class AdapterTimeoutError(BrowserServiceError):
    """Adapter exceeded its time budget."""
    code = "timeout"
    exit_code = 75  # EX_TEMPFAIL

class AdapterConfigError(BrowserServiceError):
    """Adapter or site memory is misconfigured."""
    code = "config"
    exit_code = 78  # EX_CONFIG

class AdapterNotFoundError(BrowserServiceError):
    """Adapter file exists but no @adapter-decorated function found."""
    code = "not_found"
```

The runner translates these to `ToolResult(is_error=True, code=..., content=str(exc))`.

## 13. Acceptance criteria

The Wave 4 PR is mergeable when ALL of:

- [ ] All 8 new Browser actions implemented + unit tests
- [ ] `adapter-runner` plugin: discovery + decorator + ctx + runner + tests
- [ ] Site memory module: read/write + locking + tests
- [ ] Plugin packaging template: `opencomputer plugin new --template adapter-pack` works end-to-end + test
- [ ] Authoring helper skill markdown file present at `extensions/browser-control/skills/adapter-author/SKILL.md`
- [ ] All 8 starter-pack adapters present + fixture files + each runs successfully
- [ ] Tier S: `analyze`, `verify`, `--trace` working + tests
- [ ] Typed errors: 5 new exception classes + ToolResult mapping + tests
- [ ] **Real e2e test**: agent uses `Browser(action="adapter_new")` → fills in `run()` → `adapter_validate` → invokes the generated tool → verifies output. End-to-end without manual hand-editing.
- [ ] **Real smoke test against a public site**: e.g. agent authors a tiny `news/headlines` adapter against a real public news API; bundled adapter `hackernews/top` actually returns data.
- [ ] No `from opencomputer` imports in plugin code (SDK boundary)
- [ ] All existing browser-port tests still pass
- [ ] Ruff clean

## 14. Subagent brief (workflow)

This is the brief the Wave 4 subagent will follow:

1. **Read first**:
   - This BLUEPRINT end-to-end
   - `/Users/architsakri/Desktop/opencli-plugin-learnx-atria/BUILD.md` — ground truth UX
   - One sample OpenCLI adapter for shape: `clis/hackernews/top.js` (in repo `jackwener/OpenCLI`)
   - The user's actual LearnX plugin under `/Users/architsakri/Desktop/opencli-plugin-learnx-atria/`
   - Existing browser-control plugin code to understand the existing tool surface

2. **Branch**: `git checkout main && git pull && git checkout -b feat/browser-port-wave4-adapters`

3. **Order of implementation** (depends-on order):
   1. Typed errors in `_utils/errors.py` (smallest, leaf)
   2. `adapter-runner` plugin scaffold: `_strategy.py`, `_decorator.py`, `_site_memory.py`
   3. `adapter-runner` core: `_ctx.py`, `_runner.py`, `_discovery.py`, `_validation.py`
   4. `adapter-runner` `plugin.py` registering with OpenComputer
   5. New Browser actions in `_tool.py` (8 of them) + supporting helpers
   6. Tier S: `_trace.py`, `_verify.py`, `analyze` action
   7. Plugin scaffolding template extension in `cli_plugin_scaffold.py`
   8. Authoring helper skill markdown
   9. 8 starter-pack adapters + fixtures
   10. Tests for everything

4. **Tests progressively**: write tests as you go, not at the end. After each module, run:
   ```
   cd /Users/architsakri/Documents/GitHub/opencomputer/OpenComputer
   source .venv/bin/activate
   python -m pytest tests/test_browser_port_*.py tests/test_adapter_runner_*.py -q --tb=short
   ruff check extensions/browser-control/ extensions/adapter-runner/ tests/
   ```

5. **Real smoke test before opening PR**: write an inline Python program that:
   - Imports the bundled `hackernews/top` adapter
   - Calls it directly (no agent)
   - Verifies it returns ≥5 stories with the expected columns
   
   AND a test that exercises `Browser(action="adapter_new", site="test", name="probe")` → `adapter_validate` → instantiate the generated tool → execute → verify the discovered tool appears in `opencomputer plugins` listing.

6. **Commit progressively** with `browser-port: wave4: <subsystem>: <message>` prefix.

7. **Open PR** titled `Browser port — Wave 4 (adapter promotion + starter pack + plugin packaging)`. Don't merge — orchestrator (s1) reviews first.

8. **If you discover** anything in this BLUEPRINT got something wrong, **amend the doc in the same PR** rather than working around it.

## 15. What this wave does NOT include

See [DEFERRED.md](DEFERRED.md) for the full v0.5+ roadmap. Quick summary of explicit non-goals:

- ❌ **Autofix flow** (auto-repair broken adapters when sites change) → v0.5
- ❌ **External CLI registration** (`opencomputer external register gh`) → v0.5
- ❌ **Electron-app control beyond the 2 starter pack apps** → v0.5
- ❌ **OpenCLI shell-out backend** (use opencli's 100+ adapters via subprocess) → v0.5
- ❌ **Adapter eject / reset** (override built-in adapters) → v0.6
- ❌ **Smart-search across adapters** → v0.6
- ❌ **Adapter sharing via opencomputer's plugin registry** (more than file:// + github:) → v0.6
- ❌ **Hermes provider seam** (Browserbase / Firecrawl / Camoufox alternate backends) → v0.7+

---

## How to consume this BLUEPRINT (sister sessions)

Read in this order:
1. This file (BLUEPRINT.md) — sections 1–14
2. [DEFERRED.md](DEFERRED.md) — what NOT to build in this wave
3. The user's [LearnX BUILD.md](/Users/architsakri/Desktop/opencli-plugin-learnx-atria/BUILD.md) — operational ground truth
4. `extensions/browser-control/` existing code — the foundation you build on

You should NOT need to read OpenCLI's source directly — section 4–11 distills everything from it.

If anything is ambiguous, surface it as a `**Question:**` line in the PR description rather than guessing.
