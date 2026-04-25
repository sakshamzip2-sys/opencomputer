# FastMCP request lifecycle

The MCP protocol's three-phase handshake, written from the server's
point of view. Useful when debugging a "tools don't show up in OC"
problem.

## 1. Initialize

The client (OpenComputer's `MCPManager`) opens the transport and
sends an `initialize` request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "clientInfo": {"name": "opencomputer", "version": "..."},
    "capabilities": {}
  }
}
```

The server responds with its own `serverInfo` + supported
capabilities. FastMCP handles this automatically — you don't write
any code for it.

## 2. List tools / resources / prompts

After `initialize`, the client requests the available primitives:

```
list_tools  → returns every @server.tool()
list_resources → every @server.resource()
list_prompts → every @server.prompt()
```

This is when the client builds the tool schemas the LLM sees. If a
tool you defined doesn't show up:

- Check the tool function imports correctly — module-level errors
  prevent FastMCP from registering it.
- Check the function has type hints — FastMCP skips untyped params
  to avoid generating an invalid schema.
- Check the decorator is `@server.tool()` (with parens), not
  `@server.tool` (without).

## 3. Call tool

When the agent's LLM picks a tool, the client sends:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {"name": "get_quote", "arguments": {"ticker": "GUJALKALI"}}
}
```

FastMCP looks up your decorated function, calls it with the parsed
arguments, and wraps the return value as a JSON-RPC response. Errors
that bubble up as Python exceptions become structured MCP errors
the agent sees as a tool-failure.

## 4. Shutdown

stdio: the client closes its end of the pipe. The server should
exit promptly.

http/sse: the client may stay connected indefinitely; explicit
`shutdown` is rare. Process supervisors handle the lifecycle.

## Debugging tips

- Run the server manually first: `python -m my_server.server`. If it
  exits immediately or prints a traceback, fix that before testing
  with OC.
- Use `opencomputer mcp test <name>` to connect + list tools without
  starting a chat — fast feedback loop for "is the tool registered?"
- Watch stderr in another terminal: `tail -f ~/.opencomputer/agent.log`.
  FastMCP logs to stderr by default; OC pipes stderr through to its
  log.
- For http servers: hit the URL with curl + `tools/list` body to
  verify the wire shape independently of OC.
