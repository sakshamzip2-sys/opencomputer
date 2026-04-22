# `opencomputer/plugins/` — plugin loader internals

This is the loader for both bundled (`extensions/*`) and user-installed
(`~/.opencomputer/plugins/*`) plugins. Plugin **authors** read
`plugin_sdk/CLAUDE.md`; this file is for editors of the loader itself.

## Layout

```
opencomputer/plugins/
├── discovery.py            # Phase 1: cheap manifest scan, no imports
├── loader.py               # Phase 2: lazy import + register(api)
├── manifest_validator.py   # pydantic schema for plugin.json (Phase 12g)
└── registry.py             # PluginRegistry singleton + PluginAPI
```

## Two-phase model (do NOT collapse)

- **Discovery is cheap.** No imports, no side effects, just JSON reads. The
  CLI uses this to print `opencomputer plugins` without paying activation
  cost. This is openclaw's `manifest-first` pattern — the only thing that
  scales to 100+ plugins on disk.
- **Loading is on-demand.** `loader.load_plugin(candidate, api)` imports
  the entry module + calls its `register()` exactly when a plugin is
  needed. Loaded plugins are cached on `PluginRegistry.loaded`; a
  re-load on a busy registry raises tool-name collisions.

## Sibling module name collisions — the gotcha

Plugins typically name their entry `plugin.py` and have siblings named
`provider.py`, `adapter.py`, `hooks.py`. Python's `sys.modules` cache
returns the FIRST module loaded under those names for every subsequent
import. Without our defence, two plugins with `provider.py` siblings
share the first plugin's classes.

`loader._clear_plugin_local_cache` clears those names from `sys.modules`
before each plugin loads. **Do not remove this** — it's the single
source of truth fix for the `_PLUGIN_LOCAL_NAMES` tuple gotcha.

## Synthetic module names

Each entry module is registered under a synthetic name
`_opencomputer_plugin_{plugin_id}_{entry}` so two plugins with the same
entry filename don't share their top-level module. See
`loader.load_plugin` for the `importlib.util.spec_from_file_location`
incantation.

## What manifest_validator does

`manifest_validator.py` is the typed schema for `plugin.json`. It runs
inside `discovery._parse_manifest` to reject malformed manifests with a
useful message before they ever reach the loader. Catches:

- Missing required fields (id, name, version, entry)
- Wrong types (`enabled: "yes"` instead of `true`)
- Unknown `kind` values (only `channel | provider | tool | skill | mixed`)
- Empty entry path (a common copy-paste bug)

Adding a new manifest field? Update `manifest_validator.PluginManifestSchema`
AND `plugin_sdk.core.PluginManifest` AND `_parse_manifest` in one PR.

## Boundary

- This package MAY import from `plugin_sdk.*` (it consumes the contracts)
  and from `opencomputer.tools.registry` / `opencomputer.hooks.engine` /
  `opencomputer.agent.injection` (the registries plugins write to).
- **Plugins themselves** must NOT import from this package. The plugin
  loader passes a `PluginAPI` instance — that's the only contact surface.
