# dev-tools — bundled OpenComputer plugin

Three tools for developer workflows that don't fit the chat-only core.

| Tool | Purpose | Setup |
|---|---|---|
| `Diff` | `git diff` (working / staged / vs ref) | `git` on PATH (already required for any dev box) |
| `Browser` | Fetch JS-rendered pages via Playwright | `pip install playwright && playwright install chromium` (~300MB) |
| `Fal` | Call any fal.ai model (image / video / audio / etc.) | `export FAL_KEY=...` from <https://fal.ai/dashboard/keys> |

## When to use each

### Diff

Use BEFORE `Edit` / `MultiEdit` / `Bash` to understand the current state of the repo. Read-only, never mutates anything. Three modes:

- **Working diff** (default): unstaged changes vs HEAD
- **Staged diff** (`staged=true`): index vs HEAD
- **Ref diff** (`against="main"`): working tree vs `<ref>`

Cheap. Always safe to call.

### Browser

Use **after** `WebFetch` returns empty or near-empty content — that's the signal that the page is a single-page app that builds the DOM with JS. `WebFetch` (httpx + BeautifulSoup) is faster and lighter; don't reach for `Browser` until you need it.

Cost: first run downloads ~300MB of Chromium. Page loads are 1–10s typical.

### Fal

Generic wrapper over `https://fal.run/<model_id>`. Works with any fal.ai model since they all share one POST shape. Pass:

- `model` — model id (e.g. `"fal-ai/flux/schnell"`, `"fal-ai/whisper"`)
- `payload` — model-specific JSON (e.g. `{"prompt": "a red apple"}`)

The tool doesn't validate the payload against any model's schema — that's fal.ai's job. Check the model's page on <https://fal.ai/models> for the input shape.

## Manifest

```json
{
  "id": "dev-tools",
  "name": "Developer Tools",
  "version": "0.1.0",
  "kind": "tool",
  "entry": "plugin"
}
```

Loaded automatically by `opencomputer chat` / `opencomputer gateway` since it ships in `extensions/`. To disable: rename or remove the folder.

## Why a plugin (not core)?

These tools have non-trivial dependencies (Playwright, FAL_KEY) or domain assumptions (git on PATH) that a chat-only user doesn't need. Bundling them as a plugin matches OpenComputer's "core stays small, capabilities ride in plugins" rule from the project README.
