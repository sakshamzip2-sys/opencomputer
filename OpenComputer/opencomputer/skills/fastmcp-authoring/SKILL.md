---
name: FastMCP Authoring
description: Use this skill when the user asks to "write an MCP server", "build an MCP", "author a custom tool for OpenComputer", "FastMCP tutorial", "MCP server in Python", or wants to extend OpenComputer with their own tools without writing a full plugin. Covers the FastMCP decorator API, transports (stdio / sse / http), tool/resource/prompt primitives, and the `opencomputer mcp scaffold` workflow that materialises a working skeleton.
version: 0.1.0
---

# FastMCP Authoring

OpenComputer consumes any MCP (Model Context Protocol) server as an
ordinary tool surface. **FastMCP** is the decorator-based Python SDK
for writing those servers — `@server.tool()`, `@server.resource()`,
`@server.prompt()` — without hand-rolling the JSON-RPC envelopes the
spec requires.

This skill is the curated path for authoring a new MCP server,
running it locally, and registering it into OpenComputer. The faster
alternative — `opencomputer mcp scaffold <name>` (G.30) — generates a
working skeleton you can edit; this skill explains the parts.

## When to author your own MCP

Build an MCP server when:

- You have a **stable internal service** that the agent should call
  through a uniform tool interface (your company's billing API, a
  proprietary stock-screener you use daily, etc.).
- You want to **share tools across multiple agents/IDEs** — Claude
  Code, Cursor, and OpenComputer all speak MCP, so one server feeds
  all three.
- The functionality is **stateless or session-scoped** — MCP servers
  spawn fresh per agent connection (stdio) or accept multiple
  connections (http/sse).

Don't build an MCP when an OpenComputer plugin would be a better fit:

- You need the agent loop's hooks (`PreToolUse`, `PostToolUse`).
- You need to register channels, providers, or memory backends.
- The functionality is tightly coupled to OC's internals.

For those cases, write a plugin instead — see the
`opencomputer-plugin-structure` skill.

## Quickstart with the scaffolder

The fastest path: let `opencomputer mcp scaffold` generate a working
skeleton, then edit:

```bash
opencomputer mcp scaffold my-tools
cd my-tools
pip install -e .

# The skeleton ships with one demo tool (`echo`); run as-is to verify:
python -m my_tools.server

# Then register with OpenComputer:
opencomputer mcp add my-tools \
  --transport stdio \
  --command 'python -m my_tools.server'
```

The scaffold gives you `my_tools/server.py` with a single `@server.tool()`
on a `FastMCP` instance — replace the demo tool with your own.

## The three primitives

### `@server.tool()` — the agent calls you

The most common primitive. Takes a typed Python function, exposes it
as a JSON-callable tool the agent can invoke. Type hints become the
schema the agent's LLM sees.

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP(name="stocks-mcp")


@server.tool()
def get_quote(ticker: str) -> dict:
    """Return the latest quote for a stock ticker.

    The docstring is what the agent sees as the tool description —
    write it like you're briefing a smart colleague.
    """
    # ... your implementation
    return {"ticker": ticker, "price": 1234.56, "change_pct": 2.3}
```

Rules:

- Return `dict`, `list`, `str`, `int`, `float`, or `bool`. FastMCP
  serialises Python primitives. For richer return types, convert to a
  dict with stable keys.
- Type-hint EVERY parameter — that's the schema the LLM sees.
- The docstring is the description the LLM reads when deciding
  whether to call the tool. Be specific.
- **Don't** raise bare exceptions for normal failures (e.g. "ticker
  not found"). Return a structured `{"error": "..."}` so the agent
  can act on it. Reserve raises for genuine bugs.

### `@server.resource()` — context the agent can read

Resources are documents the agent can pull into its context without
calling a tool. Useful for "static-ish" data: a project's README, the
schema of a database, a list of available macros.

```python
@server.resource("docs://project-readme")
def project_readme() -> str:
    """Return the contents of the project README."""
    return Path("README.md").read_text()
```

The URI scheme is yours — `docs://`, `db://table-schema`, etc.

### `@server.prompt()` — reusable prompt templates

Less common in agent workflows but useful for IDEs that want to drop
in a curated "summarise this PR"-style prompt.

```python
@server.prompt()
def summarise_pr_diff(diff: str) -> str:
    """Render a prompt that asks the agent to summarise a PR diff."""
    return f"Summarise this PR diff in 3 bullets, focusing on risk:\n\n{diff}"
```

## Transports

`server.run(transport="stdio")` — what the scaffold defaults to. The
process reads JSON-RPC messages on stdin and writes responses on
stdout. Best for **local development + per-agent processes**.

`server.run(transport="sse", host="0.0.0.0", port=8765)` — legacy
HTTP Server-Sent Events. Older clients need this; new clients should
use http (below).

`server.run(transport="http", host="0.0.0.0", port=8765)` —
streamable HTTP per the post-2025-03 MCP spec. Best for **shared
servers** where multiple agents/IDEs connect to one process.

Pick one in your `main()`:

```python
def main() -> None:
    server.run(transport="stdio")  # or sse / http
```

## Registering with OpenComputer

Once your server runs locally, tell OC about it:

```bash
# stdio:
opencomputer mcp add stocks --transport stdio --command 'python -m stocks_mcp.server'

# http:
opencomputer mcp add stocks --transport http --url 'http://localhost:8765/mcp'

# Test the connection without registering for permanent use:
opencomputer mcp test stocks
```

After `add`, the server's tools surface as ordinary agent tools on the
next chat. Toggle on/off with `opencomputer mcp enable/disable`.

## Common gotchas

- **`stdio` servers MUST NOT print to stdout.** That stream is the
  RPC channel. Use `logging` to stderr; FastMCP captures stdout
  automatically but custom `print()` calls leak into the wire and
  break clients.
- **Tool names share a namespace inside the agent.** If two MCP
  servers both expose a `search` tool, the second one shadows the
  first. Pick distinctive names (e.g. `stocks_search`, `docs_search`).
- **Don't import heavy deps at module top level for stdio servers.**
  Each agent connection forks a process — heavy imports add latency
  to every chat startup. Defer them to inside the tool function.
- **Capability claims live in the OC plugin layer, not the MCP
  server.** If the user wants to consent-gate your tool, they need to
  wrap it via an OC plugin. MCP servers themselves don't carry F1
  metadata.

## Worked example

The `examples/` sibling folder contains a runnable mini-MCP that
exposes a single `add(a: int, b: int) -> int` tool. Use it as a
copy-paste starting point for your own server.

See `references/transports.md` for a deeper transport-comparison
table and `references/lifecycle.md` for the full FastMCP request
lifecycle (initialize → list_tools → call_tool → shutdown).
