# Example — integrating the GitHub MCP server

A full walkthrough of wiring the official `@modelcontextprotocol/
server-github` into OpenComputer. Produces agent-callable tools like
`github__search_repos`, `github__get_file`, `github__create_issue`.

## Prereqs

- `node` + `npx` on PATH (for `npx -y @modelcontextprotocol/server-
  github`).
- A GitHub personal access token with the scopes you want the agent
  to use (`repo`, `read:org`, etc.). Treat this as a secret — do NOT
  paste it directly into `config.yaml`.

Export the token:

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

Optional: persist it in `~/.zshrc` / `~/.bashrc` for every future
OpenComputer session.

## Step 1 — Add via CLI

```bash
opencomputer mcp add github \
    --transport stdio \
    --command npx \
    --arg "-y" \
    --arg "@modelcontextprotocol/server-github" \
    --env "GITHUB_TOKEN=$GITHUB_TOKEN"
```

This writes the server into `~/.opencomputer/<profile>/config.yaml`:

```yaml
mcp:
  deferred: true
  servers:
    - name: github
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: "ghp_xxxxxxxxxxxxxxxxxxxx"
      enabled: true
```

Note: `--env "GITHUB_TOKEN=$GITHUB_TOKEN"` captures the CURRENT shell
value into config — the token is now persisted in YAML. For real use,
template it:

```yaml
env:
  GITHUB_TOKEN: "${GITHUB_TOKEN}"
```

(hand-edit after the CLI `add` if you care about keeping the token
out of the file).

## Step 2 — Dry-connect to verify

```bash
$ opencomputer mcp test github
Connecting to github (stdio: npx -y @modelcontextprotocol/server-github)...
  version: 2.1.0
  tools:
    - search_repos
    - get_file_contents
    - create_or_update_file
    - push_files
    - create_issue
    - list_issues
    - create_pull_request
    - ...
  14 tools total.
OK.
```

A failed test usually means the token is missing / invalid — re-check
the env var and re-run.

## Step 3 — Start a chat + use the tools

```bash
$ opencomputer
> Find my most recent 5 open PRs across my GitHub repos and summarize them.
```

The model sees tools prefixed with `github__` in its schema list and
calls them directly:

```
[dispatch] github__search_pull_requests({"q":"author:@me state:open", ...})
[dispatch] github__get_pull_request({"owner":"...", ...})
[response] Here are your 5 most recent open PRs: ...
```

Check live status any time:

```bash
$ opencomputer mcp status
┌────────┬────────┬───────────┬───────┬──────────┬───────┐
│ server │ state  │ version   │ tools │ uptime   │ error │
├────────┼────────┼───────────┼───────┼──────────┼───────┤
│ github │ conn.. │ 2.1.0     │ 14    │ 00:12:34 │ —     │
└────────┴────────┴───────────┴───────┴──────────┴───────┘
```

## Step 4 — Disable when not needed

GitHub MCP spins up a `node` subprocess and adds 14 tools to the
registry. If a given session doesn't need it, disable:

```bash
opencomputer mcp disable github
opencomputer                         # starts without the GitHub server
```

Re-enable with `opencomputer mcp enable github`. The config entry
persists either way.

## Notes

- **Rate limits.** The MCP server inherits your PAT's rate limit. For
  heavy-use sessions, create a token scoped narrowly to the repos the
  agent needs and keep another one for manual work.
- **Write access.** Tools like `create_or_update_file` and
  `push_files` can mutate your repos. The token's scopes are the
  only gate — there's no per-tool allowlist at the MCP layer. To
  restrict, provision a read-only token or add a `PreToolUse` hook
  that refuses `github__(create|update|push|delete)_.*` (see the
  `opencomputer-hook-authoring` skill).
- **Pin the version.** `npx -y @modelcontextprotocol/server-github`
  resolves to the latest. For reproducible behavior:

  ```yaml
  args: ["-y", "@modelcontextprotocol/server-github@2.1.0"]
  ```

## Why this is MCP-appropriate

This is a canonical MCP use case because:

- The GitHub server already exists and is actively maintained — no
  reason to rewrite its 14 tools natively.
- It bundles a non-trivial dependency (node + `@octokit/rest`) that
  would be awkward in the main OpenComputer process.
- The tools are self-contained — they don't need to touch
  OpenComputer's internals (no `RuntimeContext` reads, no session
  DB writes).
- Users want independent control over enabling / disabling it per
  profile.

Compare to `extensions/dev-tools/diff_tool.py`, which is a native tool
because `git diff` is trivial, has no external dependency footprint,
and benefits from being parallel-safe out of the box.
