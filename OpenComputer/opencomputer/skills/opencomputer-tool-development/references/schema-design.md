# Designing a ToolSchema

The `parameters` dict on `ToolSchema` is JSON Schema (draft 2020-12).
OpenComputer passes it verbatim to Anthropic as `input_schema` and to
OpenAI (via `to_openai_format`) as `function.parameters`. A well-written
schema steers the model toward correct calls; a sloppy one leads to
argument-shape errors you have to catch and apologize for in `execute`.

## Anatomy of a good `parameters` block

```python
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Absolute path to the file. Must exist."
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "description": "Zero-indexed line to start reading from."
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 2000,
      "description": "Max lines to read. Default 2000."
    }
  },
  "required": ["file_path"],
  "additionalProperties": false
}
```

Keys the model reads:

- `type` — always `"object"` at the top. Providers reject non-object
  schemas for tool parameters.
- `properties` — every argument. Always include a `description` — the
  model treats this as direct guidance.
- `required` — list of mandatory keys. Skip optional args from this
  list even if you default them in `execute`.
- `additionalProperties: false` — makes the model stop inventing
  arguments. Recommended for every tool.

## Describe arguments like you're writing a prompt

Bad (vague):
```json
{"path": {"type": "string", "description": "a path"}}
```

Good (concrete + constraint):
```json
{
  "path": {
    "type": "string",
    "description": "Absolute filesystem path. Must start with '/'. Tilde expansion is not supported."
  }
}
```

The model will feed back your description when reasoning about the call.
A constraint that's in the description gets honored; a constraint that's
only in your `execute` code surfaces as a tool-error retry loop.

## Tool name conventions

- PascalCase for names (`MyTool`, `CheckoutRepo`).
- Verb-leading for action tools (`Edit`, `RunTests`, `StartProcess`).
- Match Claude Code's built-ins when you're adding an analog — `Read`,
  `Write`, `Glob`, `Grep` are standard.
- MCP-exposed tools are auto-namespaced as `<server>__<tool>` by
  `MCPTool.schema` in `opencomputer/mcp/client.py` — avoid double
  namespacing in your own tools.

## Useful JSON Schema patterns

**Enum** — restrict to a fixed set:

```json
{
  "mode": {
    "type": "string",
    "enum": ["fast", "thorough", "paranoid"],
    "description": "Check strictness."
  }
}
```

**Array of strings** — common for batch tools:

```json
{
  "files": {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "maxItems": 64,
    "description": "Paths to process."
  }
}
```

**Object argument** — when a tool takes a structured record:

```json
{
  "todo": {
    "type": "object",
    "properties": {
      "content": {"type": "string"},
      "status": {
        "type": "string",
        "enum": ["pending", "in_progress", "completed"]
      }
    },
    "required": ["content", "status"]
  }
}
```

**Union / anyOf** — usually a smell. The model handles enums better than
anyOf. Prefer two separate tools if the branches are substantial.

## Keep descriptions aligned with behavior

If your tool refuses files outside the workspace, say so:

> "Reads a file. Must be inside the current workspace — absolute paths
> pointing outside will return an error."

Don't hide safety rules in code and then blame the model for hitting
them. Put the rule where the model can read it.

## What NOT to include

- No `$schema` — providers don't validate against it and Anthropic
  rejects some draft versions.
- No `$ref` / `$defs` — Anthropic's schema validator doesn't always
  resolve them. Inline.
- No `examples` — some providers quietly strip them.
- No `default` — providers don't inject defaults; handle them in
  `execute`.

## Validating your schema

Compare against a working built-in to sanity-check. `opencomputer/tools/
read.py`, `write.py`, `edit.py`, and the coding-harness tools are
battle-tested shapes. Copy their structure when in doubt.
