# PluginManifest fields reference

Every field in `plugin.json` maps to a field on `plugin_sdk.core.PluginManifest`.
The pydantic schema at `opencomputer/plugins/manifest_validator.py` is the
source of truth for types and validation.

## Required fields

### `id` (string, 1-64 chars)

Stable unique identifier. Lowercase letters, digits, and hyphens only; must
start and end alphanumeric. Used for:

- The on-disk directory name (`plugin.json` must live at `<id>/plugin.json`).
- Install paths (`~/.opencomputer/plugins/<id>/`).
- Collision detection — a second plugin with the same id is logged and
  skipped at discovery.
- The lock file for `single_instance` plugins
  (`~/.opencomputer/.locks/<id>.lock`).

Set once. Never change. Renaming a plugin's id breaks users' enable-lists.

### `name` (string, 1-128 chars)

Human-readable display name. Shown in `opencomputer plugins`, scaffolder
output, and READMEs. Free-form — can include spaces, capitalization, etc.

### `version` (string, semver-ish)

Accepts `M`, `M.m`, or `M.m.p` with optional `-prerelease` / `-build`
metadata. Bump on every release. The SDK guarantees backwards compat
across minor versions.

### `entry` (string, Python module name)

Name (not path) of the entry module inside the plugin root. Usually
`"plugin"` — meaning the loader imports `<root>/plugin.py`. Must not
contain slashes or `.py` suffix (the validator rejects both). The loader
registers this module under a synthetic name
`_opencomputer_plugin_<id>_<entry>` in `sys.modules`.

## Optional classification fields

### `description` (string, default `""`)

One-sentence blurb. Shown in listing commands. Keep under ~160 chars.

### `author` (string, default `""`)

Free-form. Shown in scaffolded READMEs.

### `homepage` (string, default `""`)

URL. Optional.

### `license` (string, default `"MIT"`)

SPDX id. Free-form — the SDK does not validate it against a list.

### `kind` (enum, default `"mixed"`)

One of `channel`, `provider`, `tool`, `skill`, or `mixed`. The loader's
runtime-contract validator (Task I.5) logs a WARNING if a plugin
declares `kind=tool` but registers zero tools, `kind=provider` but zero
providers, etc. `kind=skill` plugins contribute filesystem content only
and are exempt from this check. `kind=mixed` is the catch-all for
multi-surface plugins like `coding-harness`.

Note: the CLI scaffolder accepts `--kind toolkit` for UX, but the
generated `plugin.json` records the SDK enum value `tool`.

## Runtime-behavior fields

### `profiles` (list or `null`, default `null`)

Profile-scope gate (Phase 14.C/D). Three values are meaningful:

- `null` or absent — permissive; the plugin loads in any profile.
- `["*"]` — same as `null`; permissive.
- `["coding", "research"]` — restrictive; plugin only loads in those
  named profiles. Trying to load in any other profile logs an INFO
  line with the allowed list in the reason string.

This is Layer A of the filtering stack in `PluginRegistry.load_all`;
Layer B is the user's preset-based enable set.

### `single_instance` (bool, default `false`)

Set `true` if the plugin owns an exclusive resource (a bot token, a UDP
port, an OS mutex) that only one profile at a time can hold. Enforced
by an atomic PID lock at `~/.opencomputer/.locks/<id>.lock` acquired
BEFORE the entry module is imported. A second profile attempting to load
the plugin raises `SingleInstanceError` in the loader, which the
registry catches and logs as a WARNING (other plugins keep loading).

Stale locks from crashed processes are stolen atomically (rename to
`.lock.stale`, unlink, retry O_EXCL). Release happens on process exit
via an `atexit` hook.

### `enabled_by_default` (bool, default `false`)

Sub-project A flag. When `true`, the plugin is auto-enabled on a fresh
install — the first-run wizard path adds its id to the default profile's
enable list. Today only `memory-honcho` uses this (so Honcho becomes
the default memory overlay when Docker is available). Leave `false`
unless you are shipping a core overlay that Saksham has approved.

### `tool_names` (list of strings, default `[]`)

Sub-project E declaration: the schema names of tools this plugin
registers via `api.register_tool`. Used by the demand-driven activation
tracker to resolve tool-not-found events to candidate plugins WITHOUT
importing them. If the demand tracker sees the model request a tool by
name and a disabled plugin's `tool_names` contains that name, the user
gets an auto-enable prompt.

Keep this in sync with what `register()` actually registers — the
drift-guard test on bundled extensions enforces equality for those.

## Example: the weather-example manifest

```json
{
  "id": "weather-example",
  "name": "Weather Example",
  "version": "0.1.0",
  "description": "Example provider plugin that returns hardcoded weather for demos",
  "author": "OpenComputer Contributors",
  "license": "MIT",
  "kind": "provider",
  "entry": "plugin",
  "profiles": ["*"],
  "enabled_by_default": false,
  "tool_names": []
}
```

## Example: the coding-harness manifest

```json
{
  "id": "coding-harness",
  "name": "Coding Harness",
  "version": "0.1.0",
  "description": "Coding agent toolkit — Edit, MultiEdit, TodoWrite, background process tools, plan mode.",
  "author": "OpenComputer Contributors",
  "license": "MIT",
  "kind": "mixed",
  "entry": "plugin",
  "tool_names": [
    "Edit", "MultiEdit", "TodoWrite", "ExitPlanMode",
    "StartProcess", "CheckOutput", "KillProcess",
    "Rewind", "CheckpointDiff", "RunTests"
  ]
}
```

Note `tool_names` lists ten schema names even though `coding-harness`
also ships hooks, injection providers, and slash commands. `tool_names`
is tool-schema-specific; other registrations are detected via the
manifest-contract drift check rather than declared here.
