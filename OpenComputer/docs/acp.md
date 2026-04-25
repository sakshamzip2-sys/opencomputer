# OpenComputer ACP Server

OpenComputer ships a built-in Agent Client Protocol (ACP) server.
When you run `opencomputer acp`, OpenComputer listens on stdio for
JSON-RPC messages and acts as the agent backend for ACP-aware IDEs.

## What is ACP?

The Agent Client Protocol is a JSON-RPC-over-stdio protocol that lets
editors and IDEs delegate AI agent work to an external process. The
editor spawns the agent process, sends `initialize` + session lifecycle
messages, and streams `prompt` requests to it. The agent streams back
notifications as it works.

Supported clients:
- **Zed** — built-in agent panel, custom agent server config
- **VS Code** — with a compatible ACP extension
- **Cursor** — via custom agent server config
- **Claude Desktop** — via MCP server config pointing to `opencomputer acp`

## Quick start

Make sure OpenComputer is installed and your provider key is set:

```bash
pip install opencomputer
export ANTHROPIC_API_KEY=sk-...
opencomputer acp
```

The server reads from stdin and writes to stdout (NDJSON — one JSON
object per line). You normally do not run this manually; your IDE
spawns it.

## Zed configuration

Add to `~/.config/zed/settings.json`:

```json
{
  "agent_servers": {
    "OpenComputer": {
      "type": "custom",
      "command": "opencomputer",
      "args": ["acp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-..."
      }
    }
  }
}
```

Open the Agent panel in Zed and select **OpenComputer** to start a thread.

To use a specific profile or model, pass additional arguments:

```json
{
  "agent_servers": {
    "OpenComputer (coder profile)": {
      "type": "custom",
      "command": "opencomputer",
      "args": ["--profile", "coder", "acp"],
      "env": {}
    }
  }
}
```

## VS Code configuration

With an ACP-compatible VS Code extension, add to `.vscode/settings.json`
or user settings:

```json
{
  "acp.agentServers": {
    "OpenComputer": {
      "command": "opencomputer",
      "args": ["acp"]
    }
  }
}
```

Consult your extension's documentation for the exact key names.

## Cursor configuration

Cursor supports custom agent servers via its settings UI or JSON config.
Point the command to `opencomputer` with argument `acp`. Example
`.cursor/settings.json` fragment:

```json
{
  "agentServer": {
    "command": "opencomputer",
    "args": ["acp"]
  }
}
```

## Claude Desktop configuration

Claude Desktop can use OpenComputer as an MCP-style agent backend.
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or the equivalent path on your OS:

```json
{
  "mcpServers": {
    "opencomputer-acp": {
      "command": "opencomputer",
      "args": ["acp"]
    }
  }
}
```

## Session management

### Creating a new session

The IDE sends `newSession`. By default each session gets a unique
`acp:<uuid>` identifier so IDE threads are isolated.

You can request a specific session key via the `_meta` field:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "newSession",
  "params": {
    "_meta": { "sessionKey": "my-project:main" }
  }
}
```

Rules (adapted from openclaw spec):
- `sessionKey` — use a specific session key. If the key already exists
  in memory, the call is rejected (use `loadSession` instead).
- Default (`_meta` absent or `sessionKey` absent) — mint a new
  `acp:<uuid>` key.

### Resuming a session

Use `loadSession` to reconnect to a previous session. OpenComputer
checks in-memory sessions first, then falls back to the SQLite
`SessionDB` (same store used by `opencomputer sessions` and
`opencomputer search`):

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "loadSession",
  "params": { "sessionId": "acp:abc-123" }
}
```

`loadSession` returns `{"loaded": "from-memory"}` or `{"loaded": "from-db"}`.
If the session is not found anywhere, a `ERR_SESSION_NOT_FOUND` (-32001)
error is returned.

### Listing sessions

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "listSessions",
  "params": {}
}
```

Returns `{"sessions": [{"sessionId": "acp:..."}]}` for all sessions
currently held in memory by this server process.

## Protocol reference

OpenComputer's ACP server speaks JSON-RPC 2.0 over stdio (one message
per line, UTF-8).

### Methods

| Method | Description |
|---|---|
| `initialize` | Handshake. Send first. Returns `serverCapabilities`. |
| `newSession` | Create a new agent session. Returns `sessionId`. |
| `loadSession` | Resume an existing session by ID. |
| `prompt` | Send a user message to a session. Returns when complete. |
| `cancel` | Cancel the in-flight prompt for a session. |
| `listSessions` | List sessions held in memory. |

### Server capabilities

```json
{
  "promptStreaming": true,
  "sessionPersistence": true,
  "tools": true,
  "cancel": true
}
```

### Notifications emitted during `prompt`

The server emits JSON-RPC notifications (no `id`) during an active prompt:

| Method | Params | When |
|---|---|---|
| `session/promptStart` | `{sessionId}` | Prompt begins |
| `session/promptDone` | `{sessionId}` | Prompt ends (success or cancel) |
| `session/cancelled` | `{sessionId}` | Prompt was cancelled |
| `session/promptError` | `{sessionId, error}` | Prompt failed with exception |

## Limitations

The following ACP features from the openclaw reference implementation are
not yet supported in OpenComputer's ACP server:

- **Per-session MCP servers** — `mcpServers` in session params is
  silently ignored. Configure MCP servers at the OpenComputer config
  layer instead (`opencomputer config` / `opencomputer mcp`).
- **Client filesystem methods** (`fs/read_text_file`, etc.) — the server
  does not call ACP client filesystem methods. Tools run on the server
  side via OC's existing PluginAPI.
- **Client terminal methods** (`terminal/*`) — not exposed.
- **Plan / thought streaming** — the server emits `session/promptDone`
  when the full response is ready; it does not yet stream intermediate
  plan steps or reasoning tokens to the client.
- **Session forking** — `forkSession` is not implemented in v1.
- **Model switching via ACP** — the IDE cannot switch the model
  mid-session via a protocol method yet. Change the model with
  `opencomputer config set model.model <name>` and restart the server.

## Troubleshooting

**No response from the server**

Ensure the IDE is sending messages as UTF-8 NDJSON (one JSON object per
line, newline-terminated). The server ignores blank lines and responds
with an error if JSON cannot be parsed.

**`server not initialized` error**

Send `initialize` first before any other method.

**`session not found` error (-32001)**

The session ID is not held in memory. Use `loadSession` to try to restore
it from the database, or create a new session with `newSession`.

**Auth / API key errors**

OpenComputer uses the same provider and API key configured for normal
chat. Set `ANTHROPIC_API_KEY` (or the relevant key for your provider)
in the environment before starting the server, or configure it via
`opencomputer config set model.provider anthropic`.

**Verbose logging**

Logging goes to stderr (never stdout, which is the JSON-RPC transport):

```bash
opencomputer acp 2>acp-debug.log
```

Set `PYTHONPATH` or configure logging to `DEBUG` for more detail:

```bash
OPENCOMPUTER_LOG_LEVEL=debug opencomputer acp 2>acp-debug.log
```

## Related docs

- `opencomputer --help` — all available commands
- `opencomputer sessions` — list past sessions
- `opencomputer search <query>` — search past conversations
- `opencomputer mcp` — manage MCP server connections
- `docs/plugin-authors.md` — extend OpenComputer with plugins

## Implementation notes

The ACP server is implemented in `opencomputer/acp/`:

| File | Role |
|---|---|
| `server.py` | JSON-RPC dispatcher + stdio transport |
| `session.py` | Per-session state; wraps `AgentLoop` |
| `tools.py` | Stub; future ACP-to-PluginAPI bridge |

Session persistence uses the same `SessionDB` SQLite store as the
interactive chat command, so sessions started via ACP are visible in
`opencomputer sessions` and searchable via `opencomputer search`.

Spec reference: openclaw 2026.4.23 `docs.acp.md` (MIT).
Tool routing patterns adapted from hermes-agent 2026.4.23 `acp_adapter/`.
