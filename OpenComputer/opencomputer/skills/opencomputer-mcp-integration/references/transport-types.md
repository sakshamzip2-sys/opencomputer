# MCP transports

OpenComputer supports three MCP transports, selected via
`MCPServerConfig.transport`. Each has its own connection-time contract
and its own set of required fields.

## stdio — local subprocess

The most common transport. OpenComputer spawns the server as a child
process and speaks MCP over stdin / stdout. Use for:
- Official reference servers (Filesystem, GitHub, Postgres).
- Local CLIs that bundle an MCP server.
- Scripts installed via `npx` / `uvx` / `pipx`.

### Fields

- `command` (required) — the executable. Can be an absolute path or a
  name resolved via `PATH`.
- `args` (optional) — tuple of argv elements. Passed as a list in
  YAML, stored as a tuple in the dataclass for hashability.
- `env` (optional) — dict of env-var overrides. Merges on top of the
  parent env rather than replacing it. Use this for tokens / API keys.
- `url` — ignored for stdio. Leave as `""`.
- `headers` — ignored for stdio. Leave empty.

### YAML example

```yaml
mcp:
  servers:
    - name: github
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: ${GITHUB_TOKEN}
      enabled: true
```

The `npx -y` form installs the server on first run and caches it —
subsequent runs are fast. For production use, pin a specific version:

```yaml
args: ["-y", "@modelcontextprotocol/server-github@2.1.0"]
```

### CLI example

```bash
opencomputer mcp add github \
    --transport stdio \
    --command npx \
    --arg "-y" \
    --arg "@modelcontextprotocol/server-github" \
    --env "GITHUB_TOKEN=ghp_xxx"
```

Every `--arg X` appends one element to `args`; every `--env K=V`
inserts one key into `env`.

## http — streamable HTTP (modern)

For remote MCP servers that speak the spec rev 2025-03+ streamable-
HTTP transport. Use for:
- Hosted services exposing MCP endpoints.
- Cloud-run MCP servers your organization hosts.
- Any endpoint whose docs say "streamable HTTP" or "HTTP transport".

### Fields

- `url` (required) — full endpoint URL, including `/mcp` path suffix
  if the server expects it.
- `headers` (optional) — dict of HTTP headers. Typically carries
  `Authorization: Bearer <token>`.
- `command`, `args`, `env` — ignored for http.

### YAML example

```yaml
mcp:
  servers:
    - name: hosted-tools
      transport: http
      url: https://tools.example.com/mcp
      headers:
        Authorization: "Bearer ${TOOLS_API_KEY}"
      enabled: true
```

### CLI example

```bash
opencomputer mcp add hosted-tools \
    --transport http \
    --url https://tools.example.com/mcp \
    --header "Authorization=Bearer $TOOLS_API_KEY"
```

### Env var substitution

`${VAR}` in `url` / `headers` / `env` values IS substituted at config
load time. Keep tokens in env vars, not committed to YAML:

```bash
export TOOLS_API_KEY=...
opencomputer            # reads ${TOOLS_API_KEY} from env
```

## sse — legacy Server-Sent Events

For MCP servers that haven't migrated to the streamable-HTTP spec
(pre-2025-03). Same fields as `http` — `url` + `headers`. Internally
uses `mcp.client.sse.sse_client` instead of
`mcp.client.streamable_http.streamablehttp_client`.

### When to use sse vs http

- **Prefer `http`** for anything new or recently updated.
- **Use `sse`** only when the server's docs or responses show SSE
  framing. A quick way to tell: try `http` first; if `mcp test` fails
  with a transport-level error mentioning `event-stream`, switch to
  `sse`.

### YAML example

```yaml
mcp:
  servers:
    - name: legacy-service
      transport: sse
      url: https://legacy.example.com/mcp/sse
      headers:
        X-API-Key: "${LEGACY_KEY}"
      enabled: true
```

## Validation + testing before enabling

After adding any server, test it end-to-end BEFORE starting a chat:

```bash
opencomputer mcp test <name>     # dry-connect + list tools
opencomputer mcp status          # confirms state is "connected"
```

A failed test keeps the server in config but skipped at connect-all
time. Fix the underlying issue (missing env var, bad URL, wrong
transport) and re-test.

## Common configuration mistakes

- **Forgetting `-y` on `npx`** — the first run of a package prompts
  "OK to install?" which hangs the subprocess. Always include `-y`.
- **Interpolated vars that aren't exported** — `${GITHUB_TOKEN}` only
  substitutes if `GITHUB_TOKEN` is in the env when the CLI starts.
- **Using `http` for an `sse` server** — produces obscure stream-parse
  errors. Check the server's docs for which transport it expects.
- **Reusing a server `name`** — `ToolRegistry.register` raises
  `ValueError` on duplicate schema names (`<server>__<tool>`), which
  aborts the plugin load. Make each server name distinct.
- **Setting `enabled: false` temporarily and forgetting** — the
  server doesn't show up in `mcp status` (only tracks connected /
  recently-failed). Use `mcp list` to see every configured server
  including disabled ones.
