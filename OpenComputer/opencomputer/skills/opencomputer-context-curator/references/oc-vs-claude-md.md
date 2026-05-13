# How OC reads context files vs how Claude Code reads CLAUDE.md

Five concrete differences worth knowing when curating these files in an
OC repo.

## 1. Three doc names, not one

Claude Code reads `CLAUDE.md` (and `.claude.md` / `.claude.local.md`).

OC reads, in priority order:

```python
# opencomputer/agent/subdirectory_hints.py:50–56
_HINT_FILENAMES = [
    "OPENCOMPUTER.md", "opencomputer.md",
    ".hermes.md", "HERMES.md",
    "AGENTS.md", "agents.md",
    "CLAUDE.md", "claude.md",
    ".cursorrules",
]
```

Within a single directory the **first match wins**. Across directories,
the agent walks up to 5 ancestors collecting hints.

## 2. AGENTS.md has a parser — `## Program:` blocks become executable

`opencomputer/agent/standing_orders.py` scans AGENTS.md for headings
matching `## Program: <name>` and registers each block as a *Standing
Order* the agent can be reminded of by saying the program name. This is
specific to OC — Anthropic's `claude-md-improver` is unaware of it.

A correctly-formatted Program:

```markdown
## Program: ship-it

When the user says "ship it" or "open the PR":
1. Run `pytest tests/ -x`.
2. Run `ruff check src/ tests/`.
3. ...
```

Heading must be exactly `## Program:` (case-sensitive, one space after
the colon).

## 3. Subdirectory hints are loaded LAZILY on tool calls

Claude Code loads CLAUDE.md from cwd + ancestors at startup.

OC ALSO loads context files lazily when the agent navigates into a
subdirectory via Read/Bash/Grep. So a subdirectory-scoped
OPENCOMPUTER.md inside `packages/api/` only enters the prompt when the
agent first touches that directory — keeping the system prompt small.

This means: **per-subdirectory context files are cheap in OC** — write
them. The curator should encourage adding them where a subdirectory has
non-obvious structure.

## 4. SOUL.md is separate (persona, not context)

OC has SOUL.md for persona/identity. SOUL.md is loaded by the persona
system, not by the context-file reader. **Do not use this curator on
SOUL.md** — see `oc evolution dashboard` for persona curation.

## 5. ~/.claude/CLAUDE.md is read at the user-global level

OC inherits Claude-Code's user-global CLAUDE.md if it exists at
`~/.claude/CLAUDE.md`. The curator should mention it but generally not
edit it (user-global settings are best owned by the user).

## Practical implication for the curator

When you find both OPENCOMPUTER.md and CLAUDE.md in the same repo:
- If content is duplicated, recommend keeping ONLY OPENCOMPUTER.md.
- If they're complementary (e.g. CLAUDE.md is Claude-Code-specific
  workflow), keep both but cross-reference them.
- Never silently merge — the user may have a reason for the split.
