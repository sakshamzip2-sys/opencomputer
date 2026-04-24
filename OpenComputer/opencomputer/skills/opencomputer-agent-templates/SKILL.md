---
name: OpenComputer Agent Templates
description: This skill should be used when the user asks to "create a subagent template", "add an .md agent", "register a named agent", "code-reviewer subagent", "DelegateTool agent parameter", or wants to ship a reusable subagent shape.
version: 0.1.0
---

# OpenComputer Agent Templates

Agent templates are reusable subagent shapes declared as `.md` files
with YAML frontmatter. When the parent agent calls `delegate` with an
`agent` argument, OpenComputer looks up the named template, applies its
system prompt + tool allowlist + model override to the spawned child
loop, and returns the child's final answer.

Mirrors Claude Code's `plugins/<plugin>/agents/*.md` convention,
implemented in `opencomputer/agent/agent_templates.py`.

## Minimum shape

```markdown
---
name: security-reviewer
description: Review recent git diff for security issues only — secrets, injection, auth bypass.
tools: Read, Grep, Glob, Bash
---

You are a security-focused code reviewer. Only report issues where you
have high confidence (>=80%). Focus on:

- Secrets accidentally committed.
- Shell injection / SQL injection / path traversal.
- Auth / authorization bypass.
- Unsafe deserialization.

If the diff is clean, say so in one line and stop.
```

Four frontmatter fields (per `AgentTemplate` in
`opencomputer/agent/agent_templates.py`):

- `name` — template identifier used in `delegate(agent=...)`. Required.
- `description` — one-line summary. Shown by `opencomputer agents list`.
  Required.
- `tools` — comma-separated allowlist OR YAML list. Optional; omitted
  means "inherit parent's full tool set". Empty list means "no tools".
- `model` — optional model override (reserved — threaded through but
  not yet consumed by the child loop).

The markdown body after the frontmatter block is the child's **entire**
system prompt.

## Three-tier discovery

`discover_agents` in `agent_templates.py` walks three roots. Later
tiers override earlier entries with the same `name`:

1. **Bundled** — `opencomputer/agents/*.md` inside the installed
   package. Ships `code-reviewer.md` as the canonical example.
2. **Plugin** — each enabled plugin's `<plugin-root>/agents/*.md`.
3. **Profile / user** — `~/.opencomputer/<profile>/home/agents/*.md`.

Malformed files are logged at WARNING and skipped. A single broken
template never takes down the registry.

## `opencomputer agents list`

Prints every discovered template, grouped by source tier:

```
$ opencomputer agents list
code-reviewer (bundled) — Reviews recent git diff for bugs, ...
  tools: Read, Grep, Glob, Bash, WebFetch, WebSearch, TodoWrite
  source: /.../opencomputer/agents/code-reviewer.md
security-reviewer (profile) — Review recent git diff for security ...
  tools: Read, Grep, Glob, Bash
  source: /home/saksham/.opencomputer/default/home/agents/security-reviewer.md
```

Use this to verify a new template was discovered before invoking it
from a conversation.

## Calling a template from the agent

The model invokes `delegate` with an `agent` parameter:

```python
delegate(
    task="Review the security implications of this diff: ...",
    agent="security-reviewer",
)
```

`DelegateTool.execute` in `opencomputer/tools/delegate.py` looks up
`"security-reviewer"` in `_templates`, applies:

1. **System prompt** — the markdown body becomes the child's system
   prompt via `run_conversation(system_prompt_override=...)`. This
   REPLACES the default prompt — no declarative memory, no skills,
   no USER.md, no SOUL.md injection on top. The template author owns
   the whole prompt.
2. **Tool allowlist** — `template.tools` becomes the child's
   `allowed_tools` frozenset, UNLESS the caller passed an explicit
   `allowed_tools` array in the delegate call. Explicit beats template.
3. **Model override** — reserved; future work will thread
   `template.model` into the child loop's provider resolution.

An unknown template name returns an error `ToolResult` listing the
available names.

## Writing a good template

### System prompt rules

- Keep it focused. A template exists to avoid the overhead of the full
  parent prompt for a specific task. If the child needs general
  assistant behavior, don't use a template.
- Spell out constraints the child can't infer (high-confidence bar,
  output format, what to skip).
- State the exit condition ("if clean, say so and stop") so the child
  loop finishes instead of looping.
- No self-reference to "the template" — the child just sees prose.

### Tool allowlist rules

- Start MINIMAL. `Read, Grep, Glob` is enough for a reviewer.
- Avoid destructive tools (`Edit`, `Write`, `Bash`, `MultiEdit`) unless
  the delegation's task genuinely requires them.
- Every additional tool is more attack surface for model mistakes.

See `references/tool-allowlist-design.md` for the decision tree.

### Description field

One line, model-readable. Shown in `agents list` and — potentially —
used by future retrieval logic. Aim for "Use when the user wants X"
phrasing. See `references/authoring-system-prompts.md`.

## Bundled example — code-reviewer

`opencomputer/agents/code-reviewer.md` is the canonical model:

```markdown
---
name: code-reviewer
description: Reviews recent git diff for bugs, logic errors, and project-convention violations. Reports only high-confidence issues.
tools: Read, Grep, Glob, Bash, WebFetch, WebSearch, TodoWrite
---

You are an expert code reviewer. ...
```

Copy its structure for any "review" style template.

## Related

- `references/authoring-system-prompts.md` — writing the body prose.
- `references/tool-allowlist-design.md` — picking the `tools` set.
- `examples/security-reviewer-agent.md` — full working template.
- `opencomputer-plugin-structure` skill — plugins can ship templates
  via `<plugin>/agents/`.
- `opencomputer/tools/delegate.py::DelegateTool.execute` — the
  dispatching code.
