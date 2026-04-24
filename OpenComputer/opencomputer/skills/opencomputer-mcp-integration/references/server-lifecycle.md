# MCP server lifecycle

From config entry to live tool to teardown — what happens when an MCP
server is integrated with OpenComputer. Understanding the flow helps
debug connection issues and reason about startup cost.

## Config load

At CLI boot, `load_config()` parses `~/.opencomputer/<profile>/
config.yaml` and produces a `Config` value whose `.mcp.servers` is a
tuple of `MCPServerConfig` entries. At this point:

- Each entry has been schema-validated (transport must be one of the
  three; stdio requires command; sse/http require url).
- `${VAR}` substitutions in strings have happened.
- No connections have been attempted.

`MCPConfig.deferred` (default `True`) determines whether the CLI waits
for connections to come up before starting the chat prompt.

## Deferred connect

`MCPManager.schedule_deferred_connect(servers)` kicks off an
`asyncio.Task` wrapping `connect_all(servers)`. Per server:

1. Filter by `enabled` — disabled servers are silently skipped.
2. Construct an `MCPConnection(config=cfg)`.
3. Call `conn.connect()`:
   - Spawn the subprocess (stdio) or open the HTTP/SSE stream.
   - Enter the `ClientSession` async context.
   - Send `initialize()` and capture `serverInfo.version`.
   - Call `list_tools()` and wrap each returned tool in an `MCPTool`.
   - Set `conn.state = "connected"` and record `connect_time`.
4. Register every `MCPTool` in the global tool registry. A name
   collision (same `<server>__<tool>` already registered) is logged
   and that specific tool is skipped; sibling tools still register.

If `connect()` raises, the state flips to `"error"`, `last_error` is
populated with the formatted exception, and the manager continues to
the next server. The chat session starts even if zero servers
connected — users still get the built-in tools.

## Status snapshot

`MCPManager.status_snapshot()` returns a per-connection dict shape:

```python
{
    "name": str,
    "url": str,                                     # synth for stdio
    "version": str | None,                          # from initialize
    "tool_count": int,
    "tools": list[str],                             # tool names
    "connection_state": "connected" | "disconnected" | "error",
    "last_error": str | None,
    "uptime_sec": float | None,
}
```

`opencomputer mcp status` (IV.4) renders this as a Rich table. For
stdio servers, `url` is synthesized from `command + args` since those
connections don't have a real URL.

## The test command — dry-connect

`opencomputer mcp test <name>` runs a lightweight version of the
above flow without registering tools in the main registry. Output:

```
$ opencomputer mcp test github
Connecting to github (stdio: npx -y @modelcontextprotocol/server-github)...
  version: 2.1.0
  tools:
    - search_repos
    - get_file
    - create_issue
  ... (12 tools total)
OK.
```

Use this when adding a new server to validate it before starting a
chat. A failure here surfaces the underlying error (missing env var,
bad args, network error) clearly.

## Tool dispatch

When the model calls a namespaced tool (e.g. `github__search_repos`):

1. `ToolRegistry.dispatch(call)` looks up the `MCPTool` by name.
2. The tool's `execute(call)` calls `self.session.call_tool(
   name=self.tool_name, arguments=call.arguments)` — note it strips
   the `<server>__` prefix and uses the SHORT server-local tool name.
3. The MCP session's response is converted to a string: text blocks
   concatenated, images rendered as `[image]`, others str()ed.
4. Returned as `ToolResult(tool_call_id=call.id, content=..., is_error
   =mcp_result.isError)`.

MCP tools always have `parallel_safe = False` because each server has
its own state (a stdio server is a single stateful process).

## Teardown

`MCPManager.shutdown()` unwinds cleanly:

1. For each connection, unregister its tools from the tool registry.
2. Call `conn.disconnect()` — closes the async context, which:
   - Terminates the stdio subprocess (sends SIGTERM, waits for exit).
   - Closes the HTTP / SSE stream.
3. Clear `self.connections`.

Shutdown is called from the CLI's atexit chain. A hung server that
doesn't respond to shutdown gets force-killed after the event loop
teardown.

## Reconnection

Today there's no automatic reconnect on flaky servers. If a server
dies mid-session, subsequent tool calls return an error result.
Workaround: restart the CLI. A `mcp reconnect <name>` command is
parked (Phase 12m) until real use shows demand.

## Snapshot-based debugging

When a server "should be working but isn't", the troubleshooting flow:

```bash
# 1. Is it in the config at all?
opencomputer mcp list

# 2. Does it dry-connect?
opencomputer mcp test <name>

# 3. Is it connected in the live session?
opencomputer mcp status

# 4. What's the full error?
OPENCOMPUTER_LOG=DEBUG opencomputer
# (check the startup log for the connect() failure line)
```

Most issues fall into:
- **Missing env var** — `command` spawns but the server bails with
  "no credentials". Fix: add to `env:` or export in the parent env.
- **Wrong args** — `npx -y @modelcontextprotocol/server-github` works,
  `npx @modelcontextprotocol/server-github` (no `-y`) hangs on prompt.
- **Transport mismatch** — trying `http` against an `sse` server. Fix:
  check server docs.
- **Tool name collision** — two servers expose different tools but
  happen to be configured with the same `name`. The second server's
  tools fail to register. Fix: pick distinct server names.
