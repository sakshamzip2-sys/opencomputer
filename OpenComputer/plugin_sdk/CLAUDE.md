# `plugin_sdk/` — public contract for OpenComputer plugins

This directory is the **only** module surface plugins are allowed to import
from. Any third-party plugin must do `from plugin_sdk.X import Y` exclusively;
imports from `opencomputer.*` are forbidden inside `plugin_sdk` itself and
discouraged in plugin code.

## Hard rules (enforced by CI)

1. **No imports from `opencomputer.*` inside `plugin_sdk/`**.
   Enforced by `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`.
   This is what lets the core change without breaking plugins.

2. **Public re-exports go through `plugin_sdk/__init__.py`**.
   If a new type belongs in the public API, add it to `__all__` AND the
   from-import block. Plugins consume `from plugin_sdk import X`, never
   `from plugin_sdk.specific_module import X`.
   - `SlashCommand` + `SlashCommandResult` — for plugin-authored slash
     commands (Phase 12b6).

3. **Every public class/dataclass is `@dataclass(frozen=True, slots=True)`**
   unless it must be subclassed (the contract ABCs:
   `BaseTool`, `BaseProvider`, `BaseChannelAdapter`,
   `DynamicInjectionProvider`). Frozen prevents drive-by mutation by buggy
   plugin code; slots prevents accidental attribute drift.

4. **Backwards compatibility across minor versions.** Removing or renaming
   any name in `plugin_sdk/__init__.py:__all__` is a major-version break.
   Adding new names is fine. Adding new fields to existing dataclasses is
   fine if they have defaults.

## What lives here vs. `opencomputer/`

| Lives in `plugin_sdk/` | Lives in `opencomputer/` |
|---|---|
| Type contracts (BaseTool, BaseProvider, ...) | Implementations (BashTool, AnthropicProvider) |
| Dataclasses for messages (Message, ToolCall, ToolResult) | The agent loop, registries, dispatchers |
| Hook + injection ABCs | The hook engine + injection engine that runs them |
| Plugin manifest dataclass | Manifest validator + loader (uses internals) |

If you find yourself wanting to reach into `opencomputer.*` from a plugin,
the answer is almost always: **add a hook event** or **expose a new method
on `PluginAPI`** rather than coupling to internals.

## When in doubt

- A plugin needs to react to something happening? → register a `HookSpec`
  for the appropriate `HookEvent`.
- A plugin needs to inject text into the system prompt conditionally?
  → implement `DynamicInjectionProvider`.
- A plugin needs to expose a new channel? → subclass `BaseChannelAdapter`.
- A plugin needs to expose a new model backend? → subclass `BaseProvider`.
- A plugin needs to do something `PluginAPI` doesn't expose? → file a
  request to extend `PluginAPI` rather than reaching into the core.
