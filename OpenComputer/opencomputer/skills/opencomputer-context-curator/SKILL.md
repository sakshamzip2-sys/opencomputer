---
name: opencomputer-context-curator
description: Audit, evaluate, and improve OpenComputer context files (OPENCOMPUTER.md, AGENTS.md, CLAUDE.md) across a project. Use when the user asks to "audit OPENCOMPUTER.md", "improve AGENTS.md", "fix the project context docs", "score my CLAUDE.md", "check what context Claude/OC is missing", "curate my project memory", or "what should be in OPENCOMPUTER.md". Scores files A-F against OC-specific quality criteria, presents a diff-based proposal, and updates files only after explicit user approval per file.
version: 0.1.0
---

# OpenComputer Context Curator

Audit and improve the **three** project-context files OpenComputer reads at
startup, in the canonical OC precedence order:

| Priority | File | Purpose |
|----------|------|---------|
| 1 (highest) | `OPENCOMPUTER.md` / `opencomputer.md` | OC-native primary context (V3.A-T8 convention) |
| 2 | `.hermes.md` / `HERMES.md` | Hermes-compat surface |
| 3 | `AGENTS.md` / `agents.md` | Standing-orders + cross-tool agent doc |
| 4 | `CLAUDE.md` / `claude.md` | Claude-Code-compat surface (still loaded if present) |
| 5 | `.cursorrules` | Cursor-compat surface |

The agent loop reads these from the cwd and ancestors at startup
(`opencomputer/agent/prompt_builder.py::load_workspace_context`) and
lazily as the agent navigates into subdirectories
(`opencomputer/agent/subdirectory_hints.py`). Every file you curate gets
loaded into the prompt — so brevity matters.

**This skill writes to context files only after user approval per file.**
Show the proposed diff, wait for explicit "yes" / "no" per file. Never
auto-apply.

## Workflow

### Phase 1: Discovery

Find every context file in the repository:

```bash
# Project root + subdirectories — match OC's actual reader
find . -type f \( \
  -name 'OPENCOMPUTER.md' -o -name 'opencomputer.md' -o \
  -name '.hermes.md'      -o -name 'HERMES.md'       -o \
  -name 'AGENTS.md'       -o -name 'agents.md'       -o \
  -name 'CLAUDE.md'       -o -name 'claude.md'       -o \
  -name '.cursorrules' \
\) 2>/dev/null | head -50
```

Also check:
- `~/.claude/CLAUDE.md` — global Claude defaults (also surfaces in `oc memory audit`)
- `~/.opencomputer/<profile>/MEMORY.md` and `USER.md` — profile-scoped memory (curated separately by `oc memory audit`, NOT this skill)

### Phase 2: Quality Assessment

For every file found, score against OC's quality criteria:

| Criterion | Weight | Check |
|-----------|--------|-------|
| **Run/build commands** | High | Are `pytest`, `ruff`, install/dev-loop commands documented? |
| **Architecture clarity** | High | Can a fresh agent understand the codebase shape (layout + key modules)? |
| **Non-obvious gotchas** | High | Burned-in lessons (env quirks, parallel-session pitfalls, lock files) explicitly listed? |
| **OC plugin/skill awareness** | Med | If repo ships OC plugins/skills, does the doc mention which to load and why? |
| **Standing-Orders blocks** | Med | If using AGENTS.md — are there `## Program: <name>` blocks? (Parsed by `opencomputer/agent/standing_orders.py`) |
| **Conciseness** | Med | Every line earns context budget — no boilerplate, no obvious info |
| **Currency** | High | Reflects current code state (no references to deleted modules / shipped TODOs) |
| **Actionability** | High | Instructions are runnable commands, not vague advice |

**Quality grades:**

| Grade | Score | Meaning |
|-------|-------|---------|
| A | 90–100 | Production-ready; minor polish only |
| B | 70–89  | Good coverage; targeted gaps |
| C | 50–69  | Basic info; missing key sections |
| D | 30–49  | Sparse / outdated |
| F | 0–29   | Missing or actively wrong |

### Phase 3: Quality Report

**Output the report BEFORE making any edits.** Format:

```
## OpenComputer Context Quality Report

### Summary
- Files found: X
- Average score: X/100
- Files needing update: X
- Standing-orders coverage: X/X AGENTS.md files have `## Program:` blocks

### File-by-File Assessment

#### 1. ./OPENCOMPUTER.md (project primary)
**Score: XX/100 (Grade: X)**

| Criterion              | Score  | Notes |
|------------------------|--------|-------|
| Run/build commands     | X/15  | …     |
| Architecture clarity   | X/15  | …     |
| Non-obvious gotchas    | X/15  | …     |
| OC plugin awareness    | X/10  | …     |
| Standing-Orders        | X/10  | (N/A for non-AGENTS.md) |
| Conciseness            | X/10  | …     |
| Currency               | X/15  | …     |
| Actionability          | X/10  | …     |

**Issues:**
- …

**Recommended additions:**
- …
```

### Phase 4: Targeted Updates (approval-gated, per file)

After the report, for each file that scored < B (i.e. needs work):

1. **Show the proposed diff** as a unified-diff block.
2. **Ask the user to approve.** No bulk approvals — one file at a time.
3. **Only edit on explicit "yes" / "approve" / "apply".** "Looks good" is approval; "interesting" is not.
4. **Skip silently** on "skip" / "no" / "later".

**Diff format:**

````
### Proposed update: ./AGENTS.md

**Why:** Standing-Orders block missing — opens up the
`## Program: <name>` parser in `opencomputer/agent/standing_orders.py`
and lets `oc` load behavioral programs without code changes.

```diff
+## Program: quick-pr
+
+When the user says "ship it" or "open the PR":
+1. Run `pytest tests/ -x` and stop on first failure.
+2. Run `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`.
+3. Stage tracked changes and create a single commit.
+4. Push current branch and open a PR with the title from HEAD.
```

Apply? (yes / no / show full file first)
````

### Phase 5: Update Guidelines

Keep additions tight (every line costs prompt-context budget):

**DO add:**
- Commands or workflows discovered during the session that future agents will need
- Burned-in gotchas (e.g. "always `hash -r` after editable reinstall")
- Plugin/skill recommendations specific to this repo
- AGENTS.md `## Program:` blocks for repeated multi-step workflows

**DON'T add:**
- Generic best practices already covered by user-global CLAUDE.md
- One-off fixes unlikely to recur
- Anything obvious from the code itself (file tree, imports, type signatures)
- Marketing copy ("This project is amazing")

**Format conventions:**
- One-line per concept where possible
- Headed sections (`## Run`, `## Architecture`, `## Gotchas`) so the agent's
  retrieval can target sub-sections
- Code fences for every command — not bare prose

## Reference files

See `references/quality-criteria.md` for the full rubric and edge cases.
See `references/templates.md` for starter templates per file type.
See `references/oc-vs-claude-md.md` for how OC's reading differs from
Claude Code's (subdirectory walking, three-doc precedence, AGENTS.md
Standing-Orders parser).

## See also

- `oc memory audit` — separate CLI for `~/.opencomputer/<profile>/MEMORY.md`
  and `USER.md` curation. Do NOT use this skill to edit those files.
- `opencomputer/agent/subdirectory_hints.py` — the live reader that loads
  these files on tool calls; useful to understand what gets injected when.
- `opencomputer/agent/standing_orders.py` — the parser for `## Program:`
  blocks in AGENTS.md.
