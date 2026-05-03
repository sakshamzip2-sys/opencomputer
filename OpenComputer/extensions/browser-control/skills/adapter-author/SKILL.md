---
name: adapter-author
description: |
  Use when authoring a new OpenComputer adapter for a site, or adding a new
  command to an existing site. Drives the recon → write → verify flow that
  turns a successful one-off Browser-tool flow into a permanent, deterministic
  agent-callable tool. Mirrors OpenCLI's opencli-adapter-author skill in
  shape; adapted for Python + the Browser tool surface.

  Trigger phrases: "make me a tool for X", "save this as a permanent tool",
  "I keep doing X — can you crystallize this", "I need a CLI for X",
  "promote this Browser flow into an adapter".
trigger_examples:
  - "Make me a tool that lists my Atria assignments"
  - "Save this Hacker News flow as an adapter"
  - "Crystallize this into a permanent tool I can re-invoke"
---

# adapter-author — turn a Browser flow into a permanent tool

## What this skill is for

The user just did a one-off task on a website with the `Browser` tool, and
wants the agent to **promote that flow into a deterministic, reusable tool**
that doesn't need an LLM in the loop on subsequent invocations. This skill
is the playbook.

> **Day 1, first task:** "Show me my Atria assignments" → Browser flow figures
> it out → 8 assignments returned. *"Want me to crystallize this into a
> permanent tool?"* — yes.
>
> **Day 2, same task:** `AtriaAssignments()` → 200ms, deterministic, free.

This is OpenCLI's adapter-promotion model translated to OpenComputer. The
mechanism: a Python module with a `@adapter`-decorated `async def run(args, ctx)`
that the `adapter-runner` plugin discovers + registers as a synthetic
`BaseTool` named `<Site><Name>` (PascalCase).

## The decision tree

```
START
  │
  ▼
Browser(action="status") → doctor green?
  │ no  → fix dispatcher first; rerun
  │ yes
  ▼
Read site memory: ~/.opencomputer/<profile>/sites/<site>/{endpoints.json, notes.md}
  │ hit  → jump to "Endpoint validation"; never skip authoring
  │ miss → continue
  ▼
Site recon — Browser(action="analyze", url=...)
  → returns Pattern A/B/C/D/E + best candidate endpoint(s)
  │
  ▼
API discovery — pick the cheapest pattern that works:
  §1 network        — page calls JSON API directly
                       (Browser action="network_list" or "resource_timing")
  §2 state          — data in window.__INITIAL_STATE__ / __NEXT_DATA__
                       (Browser action="act" kind="evaluate")
  §3 bundle         — data in JS bundle / inline <script>
  §4 token          — auth token from a separate endpoint, then JSON API
  §5 intercept      — must drive the browser end-to-end (Strategy.UI)
  │
  ▼
Endpoint validation:
  ctx.fetch(url) or ctx.fetch_in_page(url)
  → assert HTTP 200 + non-empty body + the data shape you expected
  │
  ▼
Field decode → column design → Browser(action="adapter_new", site=..., name=...)
  → fill in the run() body
  │
  ▼
Browser(action="adapter_validate", path=...)
  → static checks pass
  │
  ▼
Live verify: invoke the new tool; confirm output matches expectations
  │
  ▼
Write site memory: endpoints.json + field-map.json + append notes.md
  │
  ▼
DONE — the new tool persists across sessions
```

## The 5 patterns (in cost order)

### Pattern A — direct JSON API (cheapest, most common)

Page calls a JSON API on a predictable URL (often `/api/...`). You can hit
it with `ctx.fetch` (no browser) or `ctx.fetch_in_page` (with cookies).

**Recon command:**
```
Browser(action="resource_timing", url="https://target.example/path")
```

This reads `performance.getEntriesByType('resource')` from the page —
**THE killer recon move** per the LearnX BUILD.md. Works on already-loaded
pages where live `network_list` capture misses everything.

**Adapter shape:**
```python
@adapter(site="hackernews", name="top", ..., strategy=Strategy.PUBLIC)
async def run(args, ctx):
    ids = await ctx.fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    ...
```

### Pattern B — embedded state

Data is in `window.__INITIAL_STATE__` / `window.__NEXT_DATA__` etc. (SSR or
hydration payloads). Read it with `ctx.evaluate`.

**Recon:**
```
Browser(action="act", kind="evaluate",
        expression="JSON.stringify(window.__NEXT_DATA__).slice(0, 500)")
```

### Pattern C — JS bundle

Data is in an inline `<script>` tag or a fetched JS bundle. Less common;
often a sub-case of Pattern B.

### Pattern D — token-bearing JSON API

Page calls a JSON API but the auth is a token issued from a separate
endpoint. Adapter does two fetches: one for the token, one for the data.

**Adapter shape:**
```python
@adapter(site="github", name="notifications", ..., strategy=Strategy.COOKIE)
async def run(args, ctx):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise AuthRequiredError("set GITHUB_TOKEN env var")
    return await ctx.fetch(
        "https://api.github.com/notifications",
        headers={"Authorization": f"token {token}"},
    )
```

### Pattern E — intercept / drive the browser

No usable API; you must click and read DOM. Strategy.UI. Slowest tier.
Use only when nothing else works.

**Adapter shape:**
```python
@adapter(site="amazon", name="track_price", ..., strategy=Strategy.UI, browser=True)
async def run(args, ctx):
    await ctx.navigate(args["url"])
    snapshot = await ctx._actions().browser_snapshot(...)
    # ... walk the snapshot tree for the price node
```

## tRPC sites are a goldmine

If `resource_timing` reveals URLs like `/api/trpc/<procedure>?batch=1&input=...`
you've hit the jackpot. Use `ctx.trpc_query`:

```python
runs = await ctx.trpc_query("run.getAllForUser")
items = await ctx.trpc_query("activityWindow.getForRun", input={"runId": "77"})
```

The helper handles the URL encoding + the `[ { result: { data: { json } } } ]`
unwrapping automatically. Mirrors the user's LearnX `trpcQuery` helper.

## Authoring runbook

### 1. Health check

```
Browser(action="status")
```

Should return `{ok: true, ...}`. If not, run `opencomputer doctor` first.

### 2. Site recon

```
Browser(action="analyze", url="https://target.example/path")
```

Returns: `{pattern, candidate_endpoints, neighbor_adapters, anti_bot}`.

If `neighbor_adapters` is non-empty, **read those first** — they encode
field meanings + auth quirks specific to this site.

### 3. Endpoint validation

For Pattern A:
```
Browser(action="act", kind="evaluate",
  expression="""
    fetch('https://target.example/api/path', {credentials: 'include'})
      .then(r => r.status)
  """)
```

Returns `200`? Good. `401`/`403`? You're not logged in — log in first.
Anything else → check the URL.

### 4. Scaffold

```
Browser(action="adapter_new", site="<lowercase>", name="<lowercase>",
        description="<one-line>", domain="<host>",
        strategy="public|cookie|ui|intercept")
```

Writes a stub at `~/.opencomputer/<profile>/adapters/<site>/<name>.py`.

### 5. Fill in `run()`

Edit the stub. The signature is **always** `async def run(args, ctx)`.
Return a `list[dict]` whose dicts have the keys you declared in `columns`.

Use `ctx.fetch` for PUBLIC, `ctx.fetch_in_page` for COOKIE, `ctx.navigate`
+ `ctx.evaluate` for UI/INTERCEPT.

### 6. Validate

```
Browser(action="adapter_validate", path="<full-path-to-file>")
```

Returns `{ok, errors, warnings, tool_name}`. Fix any errors before
proceeding.

### 7. Live verify

The new tool is registered automatically on the next agent boot. To verify
without a restart, you can directly invoke `run` from a Python REPL:

```python
from extensions.adapter_runner import get_adapter
spec = get_adapter("<site>", "<name>")
ctx = AdapterContext.create(spec=spec, profile_home=Path.home() / ".opencomputer" / "default")
rows = await spec.run({...}, ctx)
```

Confirm row count and column shape match expectations.

### 8. Write a verify fixture

Once happy with output:

```
Browser(action="verify", site="<site>", name="<name>")
```

If a fixture exists at `<profile>/sites/<site>/verify/<name>.json`,
this asserts the adapter still produces matching output. **Critical** for
catching site changes (the LMS API redesign scenario from BUILD.md §11).

A minimal fixture:
```json
{
  "args": {"limit": 5},
  "rowCount": {"min": 5, "max": 5},
  "columns": ["rank", "title", "score", "author", "comments"],
  "patterns": {"rank": "^\\d+$", "title": ".+"},
  "notEmpty": ["title", "author"]
}
```

### 9. Persist site memory

After the adapter works, write down what you learned:

```python
ctx.site_memory.write_endpoint(
    "topstories",
    {
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
        "method": "GET",
        "response": {"type": "array<int>"},
        "notes": "Returns up to ~500 IDs",
    },
)
ctx.site_memory.append_note("Pattern A — pure-API; no auth")
```

The next agent extending this site jumps straight to step 3 + skips recon.

## Lessons from the LearnX build (BUILD.md §5)

1. **tRPC sites are a goldmine.** If `/api/trpc/<procedure>?batch=1&input=...`
   appears in `resource_timing`, recon is essentially over.

2. **`performance.getEntriesByType('resource')` > live network capture.**
   The browser already has the resource-timing buffer; it survives the
   page's initial render. Use `Browser(action="resource_timing")`.

3. **Use the server's classification, not your own.** If the API returns
   `isGradeable: true` or similar, prefer that over reinventing the rule
   yourself — robust to schema changes.

4. **Site memory is leverage.** Writing `endpoints.json` after recon
   takes ~1 min and makes every subsequent extension trivial.

5. **Pre-warm the origin.** `ctx.fetch_in_page` does this for you
   automatically — don't fight it.

6. **Plugin = portable adapter, but command names live in `@adapter`.**
   The plugin's package name is just an npm-style identifier; the
   user-facing tool name comes from `<site><name>` PascalCased.

## Anti-patterns (don't)

- ❌ **Don't use `ctx.fetch` for COOKIE-strategy sites.** Cookies don't
  ride along with httpx. Use `ctx.fetch_in_page`.

- ❌ **Don't assume the API endpoint is stable.** Always write a
  `verify/<name>.json` fixture so the adapter self-tests.

- ❌ **Don't skip the validation step.** `Browser(action="adapter_validate")`
  catches typos, bad strategy values, missing fields.

- ❌ **Don't write secrets into the adapter source.** Read from env vars
  or `ctx.site_memory.read("token")` (which reads the per-profile
  `endpoints.json`).

- ❌ **Don't use `:contains()` selectors.** That's jQuery, not standard
  CSS. Use indexed refs from `Browser(action="snapshot")` instead.

## When to share

Once an adapter is solid, package it as an adapter-pack plugin so others
(or your future self on another machine) can install it with one command:

```
opencomputer plugin new --template adapter-pack <name>
# move adapters/ into <name>/adapters/, edit metadata
opencomputer plugin install file:///path/to/<name>
```

Or push to GitHub and run:

```
opencomputer plugin install github:user/repo
```

The adapter-runner plugin auto-discovers any installed adapter-pack
plugin's `adapters/` dir on next boot.

## Reference card

| Action | Purpose |
|---|---|
| `Browser(action="status")` | Health check |
| `Browser(action="analyze", url=...)` | One-shot site recon |
| `Browser(action="resource_timing", filter=...)` | List requests since page load |
| `Browser(action="network_start")` | Arm live request capture |
| `Browser(action="network_list")` | Read captured requests |
| `Browser(action="adapter_new", site, name, ...)` | Scaffold stub |
| `Browser(action="adapter_validate", path)` | Static checks |
| `Browser(action="adapter_save", site, name, run_body)` | Save successful flow |
| `Browser(action="verify", site, name)` | Run against verify fixture |
| `ctx.fetch(url)` | Plain HTTP (PUBLIC strategy) |
| `ctx.fetch_in_page(url)` | HTTP with cookies (COOKIE strategy) |
| `ctx.navigate(url)` | Drive the browser |
| `ctx.evaluate(js)` | Runtime.evaluate in page |
| `ctx.trpc_query(procedure, input=...)` | tRPC convenience |
| `ctx.site_memory.read(key)` | Per-site memory |
| `ctx.site_memory.write_endpoint(key, entry)` | Persist endpoint |

## See also

- The bundled curated adapter pack at `extensions/browser-control/adapters/` —
  8 adapters across all 4 strategies, each ~50 LOC. Read `hackernews/top.py`
  first (PUBLIC) then `arxiv/search.py` for an idiomatic pure-API pair.
- `docs/browser-port/wave4-adapters/BLUEPRINT.md` for the architectural design.
- The user's `~/Desktop/opencli-plugin-learnx-atria/BUILD.md` for the
  ground-truth UX this skill mirrors.
