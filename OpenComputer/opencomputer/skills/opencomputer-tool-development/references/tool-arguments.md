# Extracting arguments and handling errors

`ToolCall.arguments` is whatever the provider parsed from the model's
output. The schema guides the model but does NOT guarantee the shape.
Defensive extraction in `execute` keeps tool errors informative rather
than raising `KeyError` / `TypeError`.

## Idiomatic extraction

```python
async def execute(self, call: ToolCall) -> ToolResult:
    path = call.arguments.get("file_path")
    if not isinstance(path, str) or not path:
        return ToolResult(
            tool_call_id=call.id,
            content="Error: 'file_path' is required and must be a non-empty string.",
            is_error=True,
        )

    offset = call.arguments.get("offset", 0)
    if not isinstance(offset, int) or offset < 0:
        return ToolResult(
            tool_call_id=call.id,
            content=f"Error: 'offset' must be a non-negative integer; got {offset!r}.",
            is_error=True,
        )
    # ... safe to use path, offset below ...
```

Patterns:

- Use `get(..., default)` — never `[...]`. Missing keys are common.
- Check `isinstance` before coercing; the model sometimes sends a
  number as a string or vice versa.
- Normalize strings (`.strip()`) when whitespace matters.
- Give the error message enough detail to self-correct — include the
  bad value (`{value!r}`) so the model sees exactly what it sent.

## Why `is_error=True` matters

A tool result with `is_error=True`:

- Is surfaced in the assistant's next turn as "tool error" content.
- Tells the provider layer to route it through the error-retry
  accounting in the agent loop.
- Does NOT abort the conversation — the agent gets to see the message
  and try again (or give up gracefully).

A tool result with `is_error=False` carrying error text:

- Looks like a normal success to the model, so it trusts the string.
- Can lead the model down the wrong path if it thought the tool
  succeeded but actually it quietly failed.

Use `is_error=True` whenever your tool couldn't do what was asked.

## Never raise

The ABC's docstring says so: `execute` "Must handle its own errors —
never raise." The dispatcher at `opencomputer/tools/registry.py:dispatch`
wraps exceptions in a generic `ToolResult`, but you lose the ability to
write a domain-specific message:

```python
# Bad — dispatcher sees exception, returns "Error: FileNotFoundError: ..."
raise FileNotFoundError(path)

# Good — tool formats the user-facing message.
return ToolResult(
    tool_call_id=call.id,
    content=f"Error: no file at {path!r}. Check the absolute path.",
    is_error=True,
)
```

## Error message style

Model-friendly error strings:

- Start with `"Error: "` so the model's pattern-match for error
  handling catches it.
- Name the specific problem (which arg, which path, which rule).
- Suggest the correction when obvious ("pass an absolute path",
  "expected one of: fast, thorough, paranoid").

Avoid:

- Stack traces — those are for logs, not the model.
- Opaque codes ("E_INVALID_ARG") — the model doesn't know your code map.
- Silent success on partial data — if you couldn't do the whole job,
  say so.

## Returning structured data

`ToolResult.content` is `str`. If your tool produces structured data,
serialize it yourself:

```python
import json

return ToolResult(
    tool_call_id=call.id,
    content=json.dumps({"files": files, "count": len(files)}, indent=2),
)
```

Prefer JSON over YAML or TOML — every model handles JSON well, and some
tokenize it more cheaply. Use `indent=2` so it's human-readable.

For long lists, consider pagination or a summary + offset scheme rather
than dumping thousands of entries in one result.

## Truncation

`BaseTool.max_result_size` defaults to `100_000` chars. The dispatcher
truncates results longer than that with a notice appended. If you need
a different ceiling, override the class attribute:

```python
class BigReadTool(BaseTool):
    max_result_size = 500_000  # 5x default — justified by the tool's role
    ...
```

Bumping this carelessly fills the context window fast. Smaller is
usually better — design the tool to produce a summary and let the agent
call it again with an offset for more.
