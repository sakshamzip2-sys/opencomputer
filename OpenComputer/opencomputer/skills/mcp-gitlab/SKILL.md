---
name: mcp-gitlab
description: Use GitLab MCP for issues, merge requests, pipelines, repository search on GitLab.com or self-hosted GitLab. Use when the user mentions a GitLab project, asks for an MR, comments on a GL issue, checks pipeline status, or wants to search a GitLab-hosted repo. Auths via personal access token (PAT) — set GITLAB_TOKEN before adding the server.
version: 0.1.0
---

# GitLab MCP

The GitLab MCP server gives the agent typed access to a GitLab
instance — issues, merge requests, pipelines, repo search, comments,
labels, milestones — without raw API calls.

## Install (one-time)

```bash
# Generate a Personal Access Token at:
#   GitLab.com:    https://gitlab.com/-/user_settings/personal_access_tokens
#   Self-hosted:   https://<your-gitlab>/-/user_settings/personal_access_tokens
# Required scopes: api, read_repository (write_repository if you need MR creation)

export GITLAB_TOKEN="glpat-..."

oc mcp add gitlab \
  --command npx \
  --args "-y,@modelcontextprotocol/server-gitlab" \
  --env "GITLAB_PERSONAL_ACCESS_TOKEN=$GITLAB_TOKEN" \
  --env "GITLAB_API_URL=https://gitlab.com"   # change for self-hosted
```

Confirm: `oc mcp list | grep gitlab`.

For self-hosted GitLab, set `GITLAB_API_URL` to your instance — e.g.
`https://gitlab.mycorp.internal/api/v4`.

## Common operations

| User asks | Tool to call |
|-----------|--------------|
| "Show me issue 123 in `group/proj`" | `gitlab.get_issue(project_id, issue_iid=123)` |
| "List open MRs for me in `proj`"    | `gitlab.list_merge_requests(state="opened", author_id=<me>)` |
| "Search for `foo` in `group/proj`"  | `gitlab.search_repositories(search="foo", project_id=...)` |
| "Comment on MR 42"                  | `gitlab.create_merge_request_note(...)` |
| "What's the pipeline status for HEAD?" | `gitlab.list_pipelines(project_id, sha=<sha>)` |
| "Open an MR from feature branch X to main" | `gitlab.create_merge_request(...)` |

## When to use GitLab MCP (vs alternatives)

| Repo lives on | Use |
|--------------|-----|
| GitHub       | `gh` CLI (already universally installed) |
| GitLab.com   | GitLab MCP |
| Self-hosted GitLab | GitLab MCP (with `GITLAB_API_URL` set) |
| Bitbucket    | (no dedicated MCP — fall back to REST + `curl`) |

## Auth & safety

- The PAT in `GITLAB_TOKEN` is the agent's identity. Treat it as a
  user credential — actions on GitLab show up under the token's
  owner.
- **Scope minimally.** If you don't need to create MRs or push
  branches, do NOT include `write_repository` in the PAT. `api +
  read_repository` covers most agent use cases.
- The token lives in your shell env (`oc auth login` style). Rotate
  with `oc mcp env set gitlab GITLAB_PERSONAL_ACCESS_TOKEN=<new>`.

## See also

- `gh` CLI for GitHub — same surface, no MCP needed
- `opencomputer/skills/github-pr-workflow/` — for the GitHub side
- `oc mcp` — list / remove / inspect connected MCP servers
