---
name: oc-codebase-recommender
description: Analyze a codebase and recommend OpenComputer plugins, skills, hooks, and MCP servers tailored to its tech stack. Use when the user asks what OC plugins to install, how to set up OC for a codebase, what hooks would help, or for first-time OC setup. Read-only — never modifies files. Knows the bundled extensions, skills, hook catalogue, and native MCP catalogue.
version: 0.1.0
---

# OpenComputer Codebase Recommender

Analyze the project in the current working directory and recommend OC
plugins, skills, hooks, and MCP servers that fit its stack.

**Read-only.** This skill outputs recommendations and the install commands
to apply them. It does not edit any files. The user (or a follow-up agent
turn) does the install.

## Output rules

- 1–2 recommendations per category by default; 3–5 if the user explicitly
  asks for more in one category
- Every recommendation explains *why* (which signal in the codebase
  triggered it) and *how* (one-line install command)
- End with: "Want more recommendations for any specific category?"

## Workflow

### Phase 1: Codebase signals

```bash
# Project type
ls -la package.json pyproject.toml Cargo.toml go.mod pom.xml \
       Gemfile composer.json deno.json bun.lockb 2>/dev/null

# Frameworks (peek at deps)
grep -hE '"(react|vue|angular|next|svelte|nuxt|remix)"' package.json 2>/dev/null
grep -hE '(fastapi|django|flask|sanic|starlette)' pyproject.toml requirements*.txt 2>/dev/null
grep -hE '(spring|quarkus|micronaut)' pom.xml build.gradle* 2>/dev/null

# Infra signals
ls -la docker-compose*.yml Dockerfile* terraform/ k8s/ helm/ 2>/dev/null

# Existing OC config
ls -la OPENCOMPUTER.md AGENTS.md CLAUDE.md ~/.opencomputer/*/config.yaml 2>/dev/null

# Test runner
ls -la pytest.ini tox.ini jest.config.* vitest.config.* playwright.config.* 2>/dev/null

# CI
ls -la .github/workflows/ .gitlab-ci.yml .circleci/ 2>/dev/null

# Issue trackers (look at remote + recent commits)
git remote -v 2>/dev/null
git log --oneline -20 2>/dev/null | grep -iE 'linear|jira|TICKET-'
```

Capture: language, frameworks, DBs, external services, test runner, CI,
issue tracker, and whether a deployment surface (Docker/k8s/serverless)
exists.

### Phase 2: Recommendations by category

#### A. OC plugins (extensions/)

OC ships ~86 bundled plugins under `extensions/`. Map signals to plugins:

| Signal | Recommend | Why |
|--------|-----------|-----|
| Any code editing | `coding-harness` | Edit, MultiEdit, TodoWrite, plan-mode, checkpoints |
| Frontend / scraping / form-fill | `browser-harness` | Playwright-driven 5-tool browser; default-on |
| Headless deployment / Pi / systemd | `api-server` + `dev-tools` | OpenAI-compat /v1/* + Hermes /api/* aliases |
| Anthropic Claude API or proxy | `anthropic-provider` | Native + bearer-mode (claude-router compat) |
| OpenAI / OpenRouter / Gemini / Groq / Cerebras / DeepSeek / Codex | matching `<vendor>-provider` | One per vendor; OC has ~28 providers |
| Discord bot / Telegram bot / Slack | `discord` / `telegram` / `slack` | Channel adapter; credentials via `oc auth login` |
| Voice in/out | `voice` | Whisper local + Edge TTS |
| Persistent cross-session memory | `memory-honcho` | Self-hosted Honcho overlay |
| Skill drift / auto-evolution | `skill-evolution` | Auto-detect → extract → stage skills |
| Observability | `langfuse` | Trace contextvars + langfuse plumbing |
| Home Assistant integration | `homeassistant` | OC ↔ HA bridge |
| Email triage | `email` (channel) + `inbox-triage` (skill) | Pair |
| iMessage on macOS | `imessage` | Channel |
| Matrix federation | `matrix` | Channel |

For installation, point the user to: `oc plugins` (list active), or
manually clone an extension into `~/.opencomputer/<profile>/plugins/`.

#### B. OC skills (opencomputer/skills/)

OC ships ~80+ skills. The high-value ones to surface based on signal:

| Signal | Skill |
|--------|-------|
| Editing OPENCOMPUTER.md / AGENTS.md / CLAUDE.md | `opencomputer-context-curator` |
| Writing OC plugins | `opencomputer-plugin-structure`, `opencomputer-tool-development`, `opencomputer-skill-authoring` |
| Need PR workflow | `github-pr-workflow`, `commit-message-craft` |
| Building / testing | `coding-standards`, `tdd-workflow`, `silent-failure-hunter` |
| Front-end design | `html-artifact-design`, `popular-web-designs` |
| Browser tasks | `webapp-testing` |
| Data work / SQL | `database-schema-design` |
| Hooks | `hookify-rules-helper`, `opencomputer-hook-authoring` |
| MCP integration | `opencomputer-mcp-integration`, `fastmcp-authoring`, `native-mcp` |
| Math/proof verification | `math-olympiad` (also a generalisable adversarial-verifier pattern) |
| Session usage report | `session-report` |
| Legacy migration | `code-modernization` (separate plugin once installed) |
| LSP integration | `lsp-bridge` |

#### C. Hooks

Recommend YAML hooks for `~/.opencomputer/<profile>/config.yaml` matched
to common workflows:

| Signal | Hook |
|--------|------|
| Has formatters (ruff/prettier/eslint) | PostToolUse on Edit\|Write\|MultiEdit → run formatter |
| Has migrations | PreToolUse on Edit\|Write\|MultiEdit → block changes to merged migrations |
| Sensitive files in repo | PreToolUse → block edits to `.env`, secrets, certs |
| Production-critical | PreLLMCall → run secret-scan, append context warning |
| Custom workflow | UserPromptSubmit → inject `## Program: <name>` from AGENTS.md |

The `hookify-rules-helper` skill turns plain English into the right YAML.

#### D. MCP servers

| Signal | MCP server | Install |
|--------|-----------|---------|
| Postgres / Supabase | `mongodb` (atlas) / `supabase` | `claude mcp add` or OC's `oc mcp add` |
| Browser automation | `playwright` / `chrome-devtools-mcp` | one-time install |
| Library docs (any framework) | `context7` | always recommend if writing any frontend/backend code |
| GitHub integration | `github` (already-installed plugins use OAuth) | n/a |
| Linear issue tracker | `linear` | OAuth |
| Notion docs | `Notion` | OAuth |
| Vercel deploy | `vercel` | OAuth |
| Firebase | `firebase` | OAuth |
| AWS infra | `aws-serverless` / `deploy-on-aws` | already in OC |
| Stock/finance | `investor-agent`, `stockflow` | already installed for Saksham |
| Pinecone vectors | `pinecone` | OAuth |

### Phase 3: One-page action plan

Format:

```markdown
## OC setup recommendation for <project-name>

**Detected stack:** <list>

### Top picks (install first)
1. **<plugin/skill/hook/mcp>** — <why> — `<install command>`
2. **<plugin/skill/hook/mcp>** — <why> — `<install command>`

### Hooks (paste into `~/.opencomputer/<profile>/config.yaml`)
```yaml
hooks:
  PostToolUse:
    - matcher: "Edit|Write|MultiEdit"
      command: "ruff format $OPENCOMPUTER_TOOL_INPUT_PATH"
      timeout_seconds: 5
```

### Optional / nice-to-have
- ...

### Want more?
Ask "more <category>" for additional recommendations in any of:
plugins, skills, hooks, MCP.
```

## See also

- `oc plugins` — list installed plugins
- `oc skills` — list installed skills
- `oc hooks list` — show active hooks
- `oc mcp list` — show MCP servers
- `oc doctor` — multi-layer health check across all four
