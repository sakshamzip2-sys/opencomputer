# Two-phase plugin discovery

OpenComputer never imports a plugin's Python at startup. Instead it scans
cheap `plugin.json` metadata first, then activates a plugin only when
something wants to USE it (the CLI, the agent loop, a subagent).

## Phase 1 — Discovery (cheap)

`opencomputer/plugins/discovery.py::discover(search_paths, force_rescan=False)`

1. Walk each search path's top-level children only (no deep recursion).
2. Skip dotfiles, `__pycache__`, `.git`, `.venv`, `node_modules`,
   `.pytest_cache`, `.ruff_cache`, `dist`, `build`.
3. For each directory, look for `plugin.json`. If absent, skip silently.
4. Validate filesystem safety via
   `opencomputer/plugins/security.py::validate_plugin_root`. User plugins
   fail closed on symlink escapes / bad perms / suspicious ownership;
   bundled plugins under `extensions/` get relaxed rules.
5. Pydantic-validate the parsed JSON against `PluginManifestSchema`. Bad
   manifests log a WARNING and get dropped.
6. Construct a `PluginCandidate(manifest, root_dir, manifest_path,
   id_source)`. Id-collision detection: a second candidate with a
   previously-seen id is logged + skipped.

Results are cached in `_discovery_cache` for `_DISCOVERY_TTL_SEC = 1.0`
seconds, keyed on `tuple(search_paths) + (uid,)`. The 1-second window
collapses bursty CLI flows (doctor + plugins + chat in the same tick)
without hiding a freshly-installed plugin behind stale cache.

Discovery does NOT import any plugin Python. `opencomputer plugins`
runs this phase only, so the listing stays fast even with many plugins.

## Search-path order

`opencomputer/plugins/discovery.py::standard_search_paths()` returns, in
priority order:

1. Profile-local — `<profile>/plugins/` (only for named profiles).
2. Global — `~/.opencomputer/plugins/`.
3. Bundled — `<repo>/extensions/`.

The `discover` call dedupes by id, so higher-priority roots shadow
lower-priority ones. This lets a user override a bundled plugin with a
forked copy dropped into their profile's plugins dir.

## Phase 2 — Loading (on demand)

`opencomputer/plugins/loader.py::load_plugin(candidate, api, activation_source=None)`

Called by `PluginRegistry.load_all` after filtering candidates through
the active profile and preset enable-set. Per candidate:

1. If `manifest.single_instance` is true, acquire a PID lock at
   `~/.opencomputer/.locks/<id>.lock`. Raises `SingleInstanceError` if
   the lock is held by another running process.
2. Insert the plugin's root directory onto `sys.path`.
3. Clear common sibling module names from `sys.modules`:
   `("provider", "adapter", "plugin", "handlers", "hooks")`. Prevents
   cross-plugin contamination when two plugins both have `provider.py`.
4. Use `importlib.util.spec_from_file_location` with a synthetic module
   name `_opencomputer_plugin_<id>_<entry>` and execute the module.
5. Fetch `register` from the loaded module and call `register(api)`.
   Exceptions are logged but do not stop the registry; the plugin is
   dropped from the loaded list.
6. Snapshot `api` before and after the `register` call. The diff becomes
   a `PluginRegistrations` record stored on the `LoadedPlugin` so a
   later teardown can remove exactly what this plugin added.
7. Run the runtime-contract validator (Task I.5). Logs a WARNING if the
   manifest's declared `kind` doesn't match what the plugin actually
   registered, or if its declared `tool_names` don't overlap with the
   registered tool schemas.

## Activation sources

Every load happens with one of seven `PluginActivationSource` values
(see `plugin_sdk.core.PluginActivationSource`):

- `bundled` — shipped under `extensions/`.
- `global_install` — `opencomputer plugin install --global`.
- `profile_local` — installed into the active profile's plugins dir.
- `workspace_overlay` — enabled via `.opencomputer/config.yaml`.
- `user_enable` — explicit `opencomputer plugin enable <id>`.
- `auto_enable_default` — manifest `enabled_by_default: true` on first run.
- `auto_enable_demand` — demand-driven resolution of a tool-not-found signal.

Plugin code can branch on `api.activation_source` inside `register()` —
e.g. be chatty only on `user_enable`, quiet on `auto_enable_demand`.

## Why two phases

The cost of importing Python is not free. A plugin that pulls in
TensorFlow or opens a network connection would make `opencomputer
plugins` feel slow. Two-phase lets the listing command stay a cheap
JSON walk. Only the plugins actually needed for this run get loaded.

Matches the OpenClaw pattern (`sources/openclaw/src/plugins/loader.ts`)
we derived from. See also `opencomputer/plugins/CLAUDE.md` for the
internal loader's design notes.
