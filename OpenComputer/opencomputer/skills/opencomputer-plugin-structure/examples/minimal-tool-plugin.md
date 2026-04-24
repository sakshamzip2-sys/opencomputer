# Minimal tool plugin — "reverse-string"

The smallest possible OpenComputer plugin: one tool, one file of code,
one manifest. Works as a drop-in copy under the active profile's
`plugins/` directory.

## Directory layout

```
reverse-string/
├── plugin.json
└── plugin.py
```

## `plugin.json`

```json
{
  "id": "reverse-string",
  "name": "Reverse String",
  "version": "0.1.0",
  "description": "Agent-callable tool that reverses a string.",
  "author": "Example",
  "license": "MIT",
  "kind": "tool",
  "entry": "plugin",
  "tool_names": ["ReverseString"]
}
```

## `plugin.py`

```python
"""Reverse-string plugin — entry module + tool class."""

from __future__ import annotations

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ReverseStringTool(BaseTool):
    parallel_safe = True  # pure function, no shared state

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ReverseString",
            description="Reverse the characters of the input string.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The string to reverse.",
                    }
                },
                "required": ["text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        text = call.arguments.get("text", "")
        if not isinstance(text, str):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'text' argument must be a string.",
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=text[::-1])


def register(api) -> None:  # PluginAPI is duck-typed
    api.register_tool(ReverseStringTool())
```

## Install and verify

```bash
# 1. Drop the directory under the active profile's plugins root.
mkdir -p ~/.opencomputer/plugins
cp -r reverse-string ~/.opencomputer/plugins/

# 2. List plugins — `reverse-string` should appear.
opencomputer plugins

# 3. Enable it if your profile uses an enable-list preset.
opencomputer plugin enable reverse-string

# 4. Start a chat and ask the agent to "use the ReverseString tool on 'hello'".
opencomputer
```

## What the loader does

When the CLI starts and this plugin is enabled:

1. `discover` reads `plugin.json` and produces a `PluginCandidate`.
2. `load_plugin` inserts the plugin root onto `sys.path`, synthesizes the
   module name `_opencomputer_plugin_reverse_string_plugin`, and imports
   `plugin.py`.
3. `register(api)` runs once. `ReverseStringTool` lands in the global
   `ToolRegistry` under the schema name `ReverseString`.
4. The runtime-contract validator confirms `kind=tool` matches (one tool
   registered), and that `tool_names=["ReverseString"]` matches the
   registered schema name. No warnings — clean load.

## Next steps

- To add more tools, drop each in its own file at the plugin root
  (`my_tool.py`) and call `api.register_tool(MyTool())` in `register`.
- To ship a skill alongside, create `reverse-string/skills/<skill-id>/
  SKILL.md` — it will be picked up by the skill discovery hierarchy. See
  the `opencomputer-skill-authoring` skill for details.
- To add a hook, see the `opencomputer-hook-authoring` skill.

Or skip the hand-write and run `opencomputer plugin new <id>
--kind toolkit` for a richer scaffold with tests and docs.
