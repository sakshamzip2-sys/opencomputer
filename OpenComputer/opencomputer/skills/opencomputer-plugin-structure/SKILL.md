---
name: opencomputer-plugin-structure
description: This skill should be used when the user asks to "create an OpenComputer plugin", "scaffold a plugin", "understand plugin structure", "set up plugin.py", "what is register(api)", "plugin discovery", "plugin manifest", or needs guidance on OpenComputer plugin directory layout, the register(api) function, PluginManifest fields, or plugin-discovery behavior.
version: 0.1.0
---

# OpenComputer Plugin Structure

OpenComputer plugins are Python packages discovered at startup via a tiny
`plugin.json` manifest and activated lazily by running their `register(api)`
function. Every plugin follows the same two-file baseline (`plugin.json` +
`plugin.py`), then layers on components in conventional subdirectories.

## The two files every plugin has

```
my-plugin/
├── plugin.json          # Cheap manifest — parsed at discovery time
└── plugin.py            # Entry module — defines register(api) -> None
```

`plugin.json` carries ONLY metadata. The runtime behavior lives in the
`register(api)` function inside the entry module, where the plugin calls
methods on `PluginAPI` to add tools, providers, channels, hooks, slash
commands, injection providers, doctor contributions, and memory providers.

**There is no YAML manifest.** Unlike some frameworks, OpenComputer does
not parse `manifest.yaml` or `manifest.toml`. The JSON manifest is the
cheap side of the two-phase loader; everything else is declared in Python
inside `register`.

## Minimal plugin.json

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "0.1.0",
  "description": "One-sentence blurb shown in `opencomputer plugins`.",
  "author": "You",
  "license": "MIT",
  "kind": "tool",
  "entry": "plugin"
}
```

The `kind` field is one of `tool`, `skill`, `channel`, `provider`, `memory`,
or `mixed`. The loader uses it for drift detection — a `kind=provider`
plugin that registers zero providers logs a contract warning. See
`references/manifest-fields.md` for every field and its semantics.

## Minimal plugin.py

```python
from __future__ import annotations

def register(api) -> None:  # PluginAPI is duck-typed
    # api.register_tool(MyTool())
    # api.register_hook(HookSpec(event=..., handler=...))
    # api.register_provider("my-llm", MyProvider)
    ...
```

The plugin loader imports this module under a synthetic name
(`_opencomputer_plugin_<id>_<entry>`) and calls `register(api)` once. The
`api` parameter is a `PluginAPI` instance carrying the narrow surface
plugins are allowed to write into.

## Two-phase discovery

OpenComputer splits plugin lifecycle into a cheap scan and a lazy activate:

1. **Discovery** (`opencomputer/plugins/discovery.py`) walks
   `extensions/`, `~/.opencomputer/plugins/`, and the active profile's
   `plugins/` directory, reads each `plugin.json`, and returns
   `PluginCandidate` records. No Python imports. Cached for 1 second.
2. **Activation** (`opencomputer/plugins/loader.py`) imports the entry
   module on demand and runs `register(api)`. Synthetic module naming
   prevents sibling-file collisions (multiple plugins both having a
   `provider.py`).

This lets `opencomputer plugins` (listing) stay fast even with 100+
plugins on disk. See `references/two-phase-discovery.md` for the full
lifecycle and what gets cached.

## Standard plugin directory

Beyond `plugin.json` + `plugin.py`, plugins may include:

```
my-plugin/
├── plugin.json
├── plugin.py
├── README.md
├── tools/              # Optional — extra tool modules (flat layout works too)
├── skills/             # Optional — SKILL.md directories shipped with the plugin
│   └── my-skill/SKILL.md
├── agents/             # Optional — .md subagent templates (see III.5)
│   └── my-reviewer.md
├── hooks/              # Optional — hook handler modules
├── provider.py         # For kind=provider plugins — BaseProvider subclass
└── tests/              # Pytest tests the scaffolder generates
```

Flat layout (e.g. `my_tool.py` at the plugin root, not under `tools/`) is
also supported and often preferable — the loader clears
`(provider, adapter, plugin, hooks, handlers)` from `sys.modules` between
loads, but NOT `tools`, so putting tools at the root avoids a
tools-module collision with another plugin.

## Scaffolding a new plugin

Don't write all this by hand. Use the built-in scaffolder:

```bash
opencomputer plugin new my-thing --kind toolkit
```

Supported `--kind` values: `channel`, `provider`, `toolkit`, `mixed`.
(CLI uses `toolkit`; the manifest records the SDK value `tool`.) The
command writes a complete working skeleton under the active profile's
`plugins/` directory, runs a smoke test that imports the plugin through
the real loader, and prints the path where the plugin was created.

## Kinds at a glance

| Kind | Register via | Inspired-by example |
|------|--------------|---------------------|
| `tool` | `api.register_tool(MyTool())` | `extensions/dev-tools/` |
| `provider` | `api.register_provider("name", MyProvider)` | `extensions/anthropic-provider/` |
| `channel` | `api.register_channel("name", adapter)` | `extensions/telegram/` |
| `memory` | `api.register_memory_provider(provider)` | `extensions/memory-honcho/` |
| `skill` | Filesystem only — ship `skills/<id>/SKILL.md` | (inline with mixed plugins) |
| `mixed` | Any combination of the above | `extensions/coding-harness/` |

## Real bundled examples

All seven bundled plugins live under `OpenComputer/extensions/`:

- `weather-example/` — smallest possible provider (hardcoded responses).
- `dev-tools/` — three tools (GitDiff, Browser, Fal).
- `telegram/` + `discord/` — channel adapters.
- `anthropic-provider/` + `openai-provider/` — LLM providers.
- `coding-harness/` — largest `kind=mixed` plugin: 10 tools, 5 hooks,
  4 injection providers, 5 slash commands, 2 skills, modes, permissions.
- `memory-honcho/` — `kind=memory` with a self-hosted Honcho overlay.

Copy their layouts rather than inventing your own.

## See also

- `opencomputer-tool-development` skill — writing `BaseTool` subclasses.
- `opencomputer-hook-authoring` skill — `register(api)` path AND the
  settings YAML path for shell hooks.
- `opencomputer-skill-authoring` skill — shipping skills inside a plugin.
- `opencomputer-agent-templates` skill — shipping `agents/*.md` templates.
- `docs/plugin-authors.md` — the comprehensive author's guide.
- `docs/sdk-reference.md` — type-by-type SDK reference.
