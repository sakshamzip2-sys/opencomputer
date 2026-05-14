# MCP server catalog

Curated list of MCP servers known to work well with OpenComputer. **Nothing
here is bundled** — you opt in by running the `opencomputer mcp add ...`
snippet for the ones you want. That keeps OpenComputer's default install
small and avoids surprising users with auto-loaded servers.

Configured servers persist to `~/.opencomputer/config.yaml` and connect on
the next `opencomputer chat` / `opencomputer gateway` run.

## Startup mode + the `/mcp` slash (2026-05-14)

By default (`mcp.deferred: true` in `config.yaml`) MCP servers connect in
the background — `oc chat` opens instantly and tools register as each
server becomes ready. Chat startup prints a one-liner:

```
Connecting to N MCP server(s) in background — type /mcp for status.
```

The `/mcp` slash command gives live control inside a session:

| Slash | What it does |
|---|---|
| `/mcp` (or `/mcp status`) | Rich table: name / status / tool count / version / last error. In-flight servers show "connecting…". Re-run to refresh. |
| `/mcp connect <name>` | Bring up one server by name. Works even if `enabled: false` in config. Idempotent — reconnect replaces a stale session and waits for any in-flight deferred-connect of the same name. |
| `/mcp disconnect <name>` | Drop the server's session + unregister its tools. |
| `/mcp reload` | Full re-discover (alias for `/reload-mcp`). |

Switch to legacy block-on-startup with `mcp.deferred: false` — useful for
one-shot scripts where every tool must be registered before the first
user prompt.

### Implementation notes

- `MCPManager` owns a dedicated daemon-thread event loop. Every
  `stdio_client` / `ClientSession` is entered AND exited on that loop's
  per-connection owner task — satisfies anyio's same-task cancel-scope
  rule and eliminates the `RuntimeError: Attempted to exit cancel
  scope...` wall that previously dumped to stderr on every chat startup.
  Live regression test: `tests/test_mcp_cross_task_shutdown.py`.
- `MCPTool.execute` from a different event loop (per-turn `asyncio.run`
  in chat) dispatches through `asyncio.run_coroutine_threadsafe` +
  `asyncio.wrap_future`. Test doubles built via
  `MCPTool.__new__(MCPTool)` keep working through the class-level
  `session_loop: None` default that short-circuits the trampoline.
- `connect_all` runs in parallel via `asyncio.gather` — one slow `npx`
  fetch can't gate the others.

## Reference: `opencomputer mcp` subcommands

```bash
opencomputer mcp add NAME [options]   # add a server
opencomputer mcp list                 # see what's configured
opencomputer mcp test NAME            # connect + list tools, no register
opencomputer mcp enable NAME          # flip enabled = true
opencomputer mcp disable NAME         # flip enabled = false
opencomputer mcp remove NAME          # drop from config.yaml
```

Three transports:

| Transport | When | Required flags |
|---|---|---|
| `stdio` | Local subprocess (most servers) | `--command`, optional `--arg`/`--env` (repeatable) |
| `sse` | Remote MCP server (legacy SSE) | `--url`, optional `--header` (repeatable for auth) |
| `http` | Remote MCP server (modern Streamable HTTP, spec rev 2025-03+) | `--url`, optional `--header` |

---

## Recommended starter set

### filesystem — read/write files in a sandboxed root

Provides `read_file`, `write_file`, `list_directory`, `search_files`, etc.,
scoped to one directory you nominate. Good companion when the agent needs
project-aware file ops without granting Bash full disk access.

```bash
opencomputer mcp add filesystem \
  --transport stdio \
  --command npx \
  --arg -y --arg @modelcontextprotocol/server-filesystem \
  --arg /Users/you/projects
opencomputer mcp test filesystem
```

### git — stage / diff / log / branch operations

Lets the agent inspect git state without shelling out (safer in plan mode).

```bash
opencomputer mcp add git \
  --transport stdio \
  --command uvx \
  --arg mcp-server-git --arg --repository --arg /Users/you/projects/myrepo
```

### github — issues, PRs, repository metadata via the GitHub API

Requires a GitHub personal access token. Best for "look up issue 42",
"list my open PRs", etc.

```bash
opencomputer mcp add github \
  --transport stdio \
  --command npx \
  --arg -y --arg @modelcontextprotocol/server-github \
  --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
```

### sequential-thinking — structured multi-step reasoning helper

A one-tool MCP server that exposes a "think step by step" primitive the
agent can call when it wants to externalise its reasoning.

```bash
opencomputer mcp add sequential-thinking \
  --transport stdio \
  --command npx \
  --arg -y --arg @modelcontextprotocol/server-sequential-thinking
```

### fetch — generic HTTP fetcher (HTML stripper)

Overlaps with our built-in `WebFetch` but supports more aggressive content
extraction. Add only if `WebFetch` results are too noisy for your workload.

```bash
opencomputer mcp add fetch \
  --transport stdio \
  --command uvx \
  --arg mcp-server-fetch
```

### memory — graph-style knowledge memory (separate from our 3-pillar memory)

Useful as a *richer* memory backend during exploration; does NOT replace
our `MEMORY.md` declarative memory. Phase 11d's episodic-memory port
covers the in-tree path.

```bash
opencomputer mcp add memory \
  --transport stdio \
  --command npx \
  --arg -y --arg @modelcontextprotocol/server-memory
```

---

## Remote servers (sse / http)

Most public MCP services expose either SSE or modern Streamable HTTP. The
`--header` flag lets you pass auth tokens.

### Example: a hosted MCP service over SSE

```bash
opencomputer mcp add hosted-search \
  --transport sse \
  --url https://mcp.example.com/sse \
  --header "Authorization=Bearer YOUR_TOKEN"
```

### Example: a hosted MCP service over Streamable HTTP

```bash
opencomputer mcp add hosted-tools \
  --transport http \
  --url https://mcp.example.com/v1 \
  --header "Authorization=Bearer YOUR_TOKEN"
```

OAuth flows (per-MCP-server token persistence + browser callback) are NOT
yet implemented — expand this section once the OAuth client lands in a
follow-up Phase 11c PR. For now, paste static bearer tokens into `--header`.

---

## Troubleshooting

```bash
opencomputer mcp test NAME              # smoke test one server
opencomputer doctor                     # health check + MCP connectivity
opencomputer mcp list                   # see what's configured
```

Common failures:

- **"command not found"** (stdio) — the executable named in `--command` isn't
  on `PATH`. Try the absolute path: `--command /usr/local/bin/npx`.
- **`http` transport "Method Not Allowed"** — server probably only speaks
  legacy SSE. Try `--transport sse` instead.
- **401 / 403 from a remote server** — `--header` token is wrong or expired.
  Verify with `curl -H "Authorization: ..." <url>` first.
- **Tools don't show up after add** — they're connected lazily; restart the
  chat / gateway process to pick them up. `opencomputer mcp test NAME`
  confirms the server itself is healthy.

---

## Why curated, not bundled?

OpenComputer's principle: **don't default-install anything the user didn't
ask for**. Every server in this catalog needs explicit `opencomputer mcp
add` invocation to land in your config. The catalog grows as Phase 11e
collects more battle-tested servers — open an issue with one to add.
