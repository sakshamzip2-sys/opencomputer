# adapter-runner

Companion plugin to [`browser-control`](../browser-control/) — discovers
`@adapter`-decorated Python recipes and registers each as a synthetic
`<Site><Name>` tool. The mechanism behind v0.4 adapter promotion.

## How it works

On `register(api)`:

1. Walks three source roots in priority order (later wins on dup):
   - **Bundled** — `extensions/browser-control/adapters/**/*.py`
   - **Adapter-pack plugins** — `extensions/<plugin>/adapters/**/*.py`
   - **User-authored** — `~/.opencomputer/<profile>/adapters/**/*.py`

2. Imports each `.py` under a synthetic unique module name (so the
   bundled, plugin, and user copies of e.g. `hackernews/top.py` don't
   collide in `sys.modules`).

3. Each `@adapter`-decorated function registers an `AdapterSpec` in the
   process-global registry.

4. For each spec, builds a synthetic `BaseTool` with:
   - `schema.name` = `<Site><Name>` PascalCase (e.g. `AtriaAssignments`).
   - `schema.description` = the `description` keyword arg.
   - `schema.parameters` = JSON Schema derived from the `args` keyword.
   - `execute(call)` invokes the runner.

5. Adds a doctor row reporting count + import errors.

## Authoring an adapter

```python
from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="hackernews",
    name="top",
    description="Hacker News top stories",
    domain="news.ycombinator.com",
    strategy=Strategy.PUBLIC,
    args=[{"name": "limit", "type": "int", "default": 20}],
    columns=["rank", "title", "score", "author", "comments"],
)
async def run(args, ctx):
    ids = await ctx.fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    ...
```

See [`browser-control/skills/adapter-author/SKILL.md`](../browser-control/skills/adapter-author/SKILL.md)
for the full authoring playbook.

## Strategy enum

| Value | When to use | Browser? |
|---|---|---|
| `PUBLIC` | Pure HTTP, no auth | No |
| `COOKIE` | Needs the user's logged-in browser session (cookies or header tokens) | Yes |
| `UI` | Drive the browser like a human (clicks, fills, snapshot) | Yes |
| `INTERCEPT` | Need full CDP / Electron-app control | Yes |

## `AdapterContext` (`ctx`)

```python
async def run(args, ctx):
    # Pure HTTP (PUBLIC strategy)
    data = await ctx.fetch("https://api.example.com/...")

    # HTTP with cookies (COOKIE / UI / INTERCEPT) — auto-warms origin
    data = await ctx.fetch_in_page("https://lms.example/api/...")

    # tRPC convenience
    rows = await ctx.trpc_query("run.getAllForUser")

    # Page evaluation
    title = await ctx.evaluate("document.title")

    # Drive the browser
    await ctx.navigate("https://example.com")
    await ctx.click("e12")  # ref from snapshot

    # Site memory
    ctx.site_memory.write_endpoint("topstories", {"url": "...", "method": "GET"})
    note = ctx.site_memory.read("topstories")
```

## Sharing as a plugin

```bash
opencomputer plugin new --kind adapter-pack my-adapters
# move adapter files into my-adapters/adapters/
opencomputer plugin install file:///path/to/my-adapters
```

The runner picks up the new pack on next agent boot.

## Errors

The runner translates these typed errors from `extensions.browser_control._utils.errors`
into model-readable `ToolResult(is_error=True)`:

| Class | `code` | `exit_code` |
|---|---|---|
| `AuthRequiredError` | `auth_required` | 77 |
| `AdapterEmptyResultError` | `empty_result` | 66 |
| `AdapterTimeoutError` | `timeout` | 75 |
| `AdapterConfigError` | `config` | 78 |
| `AdapterNotFoundError` | `not_found` | 1 |

## See also

- [BLUEPRINT](../../docs/browser-port/wave4-adapters/BLUEPRINT.md)
- [DEFERRED](../../docs/browser-port/wave4-adapters/DEFERRED.md) — what's NOT in v0.4
- [`browser-control`](../browser-control/) — the foundation
- [bundled starter pack](../browser-control/adapters/) — 8 sample adapters
