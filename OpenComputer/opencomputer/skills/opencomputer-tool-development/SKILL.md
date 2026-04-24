---
name: OpenComputer Tool Development
description: This skill should be used when the user asks to "create an OpenComputer tool", "subclass BaseTool", "write a ToolSchema", "add a tool to a plugin", "tool arguments", "parallel-safe tools", "how to return ToolResult", or needs guidance on OpenComputer's BaseTool contract, ToolSchema JSON schema fields, parallel_safe semantics, or ToolCall / ToolResult dataclasses.
version: 0.1.0
---

# OpenComputer Tool Development

A tool is any callable the agent can invoke during a turn. OpenComputer
tools are `BaseTool` subclasses declared in `plugin_sdk/tool_contract.py`.
Each tool has a JSON schema the provider uses to advertise the tool to
the model, and an async `execute` method that runs when the model calls it.

## The minimum contract

```python
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class MyTool(BaseTool):
    parallel_safe = False  # safe default — opt in only when provably race-free

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="MyTool",
            description="What the tool does and when to use it.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "..."},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        query = call.arguments.get("query", "")
        # ... do the work ...
        return ToolResult(tool_call_id=call.id, content="result text")
```

That's it. Register it from a plugin's `register(api)`:

```python
def register(api) -> None:
    api.register_tool(MyTool())
```

## `ToolSchema`

Three fields (see `plugin_sdk/tool_contract.py`):

- `name` — PascalCase identifier. Must be unique across the whole
  registry (schema-name uniqueness is the collision guard — duplicate
  names raise `ValueError` in `ToolRegistry.register`).
- `description` — free-form text the model reads to decide when to call
  this tool. Write it like a prompt.
- `parameters` — JSON Schema object describing the args. OpenComputer
  adapts this to Anthropic's `input_schema` and OpenAI's
  `function.parameters` formats automatically.

Existing built-in names to avoid conflicting with: `Read`, `Write`,
`Edit`, `MultiEdit`, `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch`,
`TodoWrite`, `Memory`, `Recall`, `SkillManage`, `SkillTool`,
`ExitPlanMode`, `AskUserQuestion`, `PushNotification`, `NotebookEdit`,
`delegate`.

## `ToolCall` and `ToolResult`

Input (`ToolCall`):

```python
@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str                         # opaque id the provider assigned
    name: str                       # must match your schema.name
    arguments: dict[str, Any]       # already-parsed JSON
```

Output (`ToolResult`):

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str               # ALWAYS echo call.id
    content: str                    # string — serialize structured data yourself
    is_error: bool = False          # True → agent sees this as a retryable error
```

**Always echo `call.id` as `tool_call_id`.** Anthropic's API rejects the
response turn if tool_call_ids don't match.

**Use `is_error=True` for recoverable problems** the agent should see
and potentially retry (bad args, network blip, file not found). Never
raise exceptions out of `execute` — the dispatcher wraps them in an
error `ToolResult` anyway, but you lose the ability to format the
message the model sees.

## `parallel_safe`

Class attribute, default `False`. When true, the agent loop may fire
this tool concurrently with other parallel-safe tools from the same
turn. Only opt in if your tool has no shared state and no side effects
that can race (pure reads are the typical case).

**Do NOT set `parallel_safe = True` on:**

- Tools that write files, delete resources, or run shell commands.
- Tools that mutate TODO lists or session state.
- Tools that prompt the user.

The agent loop stacks two additional guards on top of your flag — see
`references/parallel-safety.md` for the full three-layer gate. Don't
try to opt out of those by lying on the flag; the core name-whitelist
will veto you anyway.

## `max_result_size`

Class attribute, default `100_000` chars. Results longer than this get
truncated with a notice. Bump it for tools that naturally produce large
output (e.g. a full-file reader); shrink it for tools that should never
flood context.

## Argument extraction

Keep it defensive. `call.arguments` is whatever JSON the model produced
— it may not match your schema. Validate before using:

```python
async def execute(self, call: ToolCall) -> ToolResult:
    path = call.arguments.get("file_path")
    if not isinstance(path, str) or not path:
        return ToolResult(
            tool_call_id=call.id,
            content="Error: 'file_path' is required and must be a string.",
            is_error=True,
        )
    # ... safe to use `path` as str below ...
```

See `references/tool-arguments.md` for idiomatic patterns.

## Reading runtime context

Tools often need to know whether plan mode is active, whether YOLO mode
is set, or what the current session id is. Two paths:

1. **For delegate-style tools** — `DelegateTool._current_runtime` is a
   class attribute set by the parent loop. See
   `opencomputer/tools/delegate.py` for the pattern.
2. **For most tools** — use a `PreToolUse` hook to enforce the mode
   constraint, not the tool itself. Plan mode blocking, yolo mode
   auto-approval, and scope checks are hook logic; the tool stays
   simple. See the `opencomputer-hook-authoring` skill.

## Registering from `register(api)`

`PluginAPI.register_tool(tool)` is the entry. It delegates to
`ToolRegistry.register` which enforces schema-name uniqueness:

```python
def register(api) -> None:
    api.register_tool(GitDiffTool())
    api.register_tool(BrowserTool())
    api.register_tool(FalTool())
```

If you register the same `schema.name` twice (across plugins or within
one plugin), the second `register_tool` call raises `ValueError` and
aborts the whole plugin load. Pick unique PascalCase names.

## See also

- `opencomputer-plugin-structure` skill — how `register(api)` fits into
  the plugin lifecycle.
- `opencomputer-hook-authoring` skill — enforcing mode gates via hooks
  rather than inside tools.
- `extensions/dev-tools/` — concrete three-tool plugin.
- `plugin_sdk/tool_contract.py` — canonical ABC + `ToolSchema`.
- `opencomputer/tools/registry.py` — dispatch + name-collision check.
