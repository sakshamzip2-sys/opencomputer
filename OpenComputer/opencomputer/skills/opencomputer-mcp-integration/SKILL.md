---
name: opencomputer-mcp-integration
description: This skill should be used when the user asks to "add MCP servers", "configure MCP in OpenComputer", "MCPServerConfig", "stdio MCP", "http MCP", "MCP server status", "mcp install-from-preset", or wants to integrate Model Context Protocol servers into OpenComputer.
version: 0.1.0
---

# OpenComputer MCP Integration

MCP (Model Context Protocol) servers expose external tool surfaces that
OpenComputer connects to and surfaces as ordinary agent tools. The
integration is implemented in `opencomputer/mcp/client.py` (`MCPTool`
and `MCPManager`) and configured via the `mcp:` block in
`config.yaml`.

## Three transports

Declared in `MCPServerConfig.transport` (at
`opencomputer/agent/config.py`):

- `stdio` — local subprocess. Configure `command` + `args` + `env`.
- `http` — modern streamable HTTP (spec rev 2025-03+). Configure `url`
  + optional `headers`.
- `sse` — legacy HTTP Server-Sent Events. Configure `url` + `headers`.
  Use only for older servers that haven't migrated.

See `references/transport-types.md` for concrete examples of each.

## `MCPServerConfig` shape

```python
@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str = ""                       # used as the tool prefix
    transport: str = "stdio"             # "stdio" | "sse" | "http"
    command: str = ""                    # stdio: executable
    args: tuple[str, ...] = ()           # stdio: argv
    url: str = ""                        # sse/http: endpoint
    env: dict[str, str] = field(default_factory=dict)     # stdio env
    headers: dict[str, str] = field(default_factory=dict)  # sse/http auth
    enabled: bool = True
```

`MCPConfig.servers: tuple[MCPServerConfig, ...]` lives inside
`Config.mcp`. Add entries via the `mcp` CLI subgroup or hand-edit
`~/.opencomputer/<profile>/config.yaml`.

## The tool-name shape

Every tool exposed by an MCP server is surfaced as an `MCPTool`
instance with its `ToolSchema.name` set to `<server_name>__<tool_name>`
(double underscore). Example: `github__search_repos`,
`mongodb__find`. The prefix prevents collisions when multiple servers
expose identical tool names.

Since `ToolRegistry.register` raises `ValueError` on a duplicate
`schema.name`, the prefix is load-bearing — don't configure two
servers with the same `name`.

## Deferred loading

`MCPConfig.deferred = True` (default). At CLI startup,
`MCPManager.schedule_deferred_connect(servers)` returns an
`asyncio.Task` that connects each server in the background. Startup
does NOT wait. If a server fails to spin up, the CLI still starts with
the rest of its tools — you can check which servers are healthy via:

```bash
opencomputer mcp status
```

The `status` command (IV.4) prints a per-server snapshot from
`MCPManager.status_snapshot()`: connection state, reported version,
tool count, uptime, last error.

## Adding a server

### Via CLI

```bash
# stdio — local subprocess (e.g. GitHub MCP server).
opencomputer mcp add github \
    --transport stdio \
    --command npx \
    --arg "-y" \
    --arg "@modelcontextprotocol/server-github" \
    --env "GITHUB_TOKEN=ghp_..."

# http — remote server.
opencomputer mcp add hosted-tools \
    --transport http \
    --url https://tools.example.com/mcp \
    --header "Authorization=Bearer $TOKEN"

# Then:
opencomputer mcp list          # see it in the servers table
opencomputer mcp test github   # dry-connect + list its tools
opencomputer mcp status        # live status snapshot
```

### Via YAML

Direct edit of `~/.opencomputer/<profile>/config.yaml`:

```yaml
mcp:
  deferred: true
  servers:
    - name: github
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: ${GITHUB_TOKEN}
      enabled: true
```

The CLI path is recommended because it validates each field; YAML
editing is for bulk edits and version-controlled config.

## Disabling without removing

`enabled: false` keeps the entry in config but skips connection. Fast
way to quarantine a flaky server:

```bash
opencomputer mcp disable flaky-server
opencomputer mcp status          # confirm it's no longer tracked
```

## Plugin-shipped MCP configs

A plugin that depends on an MCP server can append to the user's MCP
config from its `register(api)`. The pattern is: read the current
config via `api.session_db_path`'s parent (the profile home), merge
your server entry, write it back. Prefer this over requiring the user
to hand-edit `config.yaml`. See `extensions/memory-honcho/plugin.py`
for a write-to-config pattern (writes to the memory: block rather
than mcp:, but the mechanics are identical).

Better: prompt the user via first-run UX rather than silently
mutating their config. Unexpected `mcp.servers` changes are a surprise
worth avoiding.

## When MCP is the wrong answer

Skip MCP for:
- **Tools that need tight OpenComputer integration.** If the tool
  reads `RuntimeContext`, mutates session state, or participates in
  the plan-mode gate, write a native `BaseTool` instead. MCP tools
  don't see OpenComputer internals.
- **Fast, dependency-free functionality.** Spawning a subprocess or
  opening an HTTP connection is 10-50ms per session startup. Native
  tools are zero-cost.
- **Anything you'd ship in the main repo.** MCP is for third-party /
  user-local / cross-language tools. In-repo functionality lives in
  `opencomputer/tools/` or an extension plugin.

Use MCP for: existing MCP servers (GitHub, Filesystem, Postgres,
Playwright), polyglot tools (Go / Rust CLIs), and tools whose
lifecycle the user wants to control independently.

## Related

- `references/transport-types.md` — stdio / http / sse examples.
- `references/server-lifecycle.md` — connect, test, snapshot, teardown.
- `examples/github-mcp-config.md` — full working config for the
  GitHub MCP server.
- `opencomputer/mcp/client.py` — `MCPManager` + `MCPTool` source.
- `opencomputer/agent/config.py` — `MCPServerConfig` + `MCPConfig`.
- `opencomputer/cli_mcp.py` — every `mcp` subcommand.
