# Bundled adapter pack — v0.4 starter set

Eight curated adapters demonstrating the `@adapter`-decorator surface
across all four `Strategy` tiers. Each is intentionally small (~50 LOC)
so you can read it end-to-end and use as a template for your own.

| File | Strategy | Demonstrates |
|---|---|---|
| [`hackernews/top.py`](hackernews/top.py) | PUBLIC | Pure HTTP via `ctx.fetch`; no auth, no browser |
| [`arxiv/search.py`](arxiv/search.py) | PUBLIC | Search API + minimal XML parsing (stdlib only) |
| [`reddit/hot.py`](reddit/hot.py) | PUBLIC | UA-spoofed JSON feed |
| [`github/notifications.py`](github/notifications.py) | COOKIE (header token) | Bearer-token auth via env var |
| [`apple_podcasts/search.py`](apple_podcasts/search.py) | PUBLIC | iTunes Search API — search/podcasts |
| [`amazon/track_price.py`](amazon/track_price.py) | COOKIE | Logged-in browser session |
| [`cursor_app/recent_files.py`](cursor_app/recent_files.py) | INTERCEPT | Electron app over CDP |
| [`chatgpt_app/new_chat.py`](chatgpt_app/new_chat.py) | INTERCEPT | Another Electron app |

## Tool names

The `adapter-runner` plugin registers each as a synthetic `BaseTool`
named `<Site><Name>` (PascalCase). So:

  - `hackernews/top` → `HackernewsTop`
  - `arxiv/search` → `ArxivSearch`
  - `cursor_app/recent_files` → `CursorAppRecentFiles`
  - `apple_podcasts/search` → `ApplePodcastsSearch`

## Verification fixtures

Each adapter has a `verify/<name>.json` fixture next to it that
declares expected row count, column shape, regex patterns per column,
etc. Run via:

```
Browser(action="verify", site="<site>", name="<name>")
```

Returns `{ok: bool, failures: [...], rows_returned: int}`. Use this in
CI to catch site-side breakage.

## Customizing or replacing a bundled adapter

v0.4 ships read-only — bundled files are the source of truth. v0.6
adds `opencomputer adapter eject <site>/<name>` (DEFERRED.md §E) which
copies the bundled adapter to your user-local `~/.opencomputer/<profile>/
adapters/` where local edits override.

For now: copy the file by hand into your user-local adapters dir, edit
freely, and the discovery walk will prefer your local copy on the next
boot.

## Writing your own

Use the [adapter-author skill](../skills/adapter-author/SKILL.md) for
the full runbook, or:

1. `Browser(action="adapter_new", site="mysite", name="mycommand", strategy="public")` — scaffolds a stub
2. Edit the `async def run(args, ctx)` body
3. `Browser(action="adapter_validate", path="<path>")` — static checks
4. Restart the agent — the new tool registers automatically

## See also

- The [BLUEPRINT](../../../docs/browser-port/wave4-adapters/BLUEPRINT.md) — Wave 4 architectural design
- The [LearnX BUILD log](../../../docs/refs/) — ground-truth UX from the OpenCLI side
- [`adapter-runner`](../../adapter-runner/) — the discovery + registration plugin
