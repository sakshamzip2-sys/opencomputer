# Starter templates for OC context files

Use these as scaffolds when a file is missing or scored F. Replace the
placeholders, then run the curator again to score the new draft.

## OPENCOMPUTER.md (primary, recommended for new repos)

```markdown
# <Project> — OpenComputer context

## What this is
One paragraph: what the project does, who uses it, why it exists.

## Run / dev / test
```bash
# install
<install command>

# run
<run command>

# test
<test command>

# lint
<lint command>
```

## Architecture
Short directory tree with one-line purposes:

```
my-project/
├── src/         — application code
├── tests/       — pytest suites
└── scripts/     — operational helpers
```

## Non-obvious gotchas
1. **<Gotcha 1>** — <one-sentence why> + <how to handle>
2. **<Gotcha 2>** — …

## OC plugins/skills used
- `<plugin>` — <why this project needs it>

## See also
- `AGENTS.md` for standing orders
- `docs/` for design notes
```

## AGENTS.md (Standing-Orders focused)

```markdown
# <Project> — Standing Orders for OC

## Program: ship-it

When the user says "ship it" or "open the PR":
1. Run `pytest tests/ -x`. Stop if any test fails.
2. Run `ruff check src/ tests/`.
3. Run `git status` — verify clean working tree apart from staged work.
4. Create a single commit with a Conventional Commit subject.
5. Push and open a PR with the commit subject as the PR title.

## Program: quick-fix

When the user says "quick fix" plus a one-line problem statement:
1. Search the codebase with Grep for the relevant symbol.
2. Make the minimal change.
3. Add or update a single regression test.
4. Run only the affected test file.
5. Stop and report — do NOT create a commit.
```

The parser at `opencomputer/agent/standing_orders.py` requires:
- A `##` heading starting with `Program:` (case-sensitive; one space after the colon)
- A blank line, then the body
- Numbered steps for executable Programs (1. 2. 3. — or "When… 1. 2.")

## CLAUDE.md (Claude-Code-compat fallback)

Same shape as OPENCOMPUTER.md. Use only if the repo also wants to be
loadable by stock Claude Code with no prior knowledge of OC.

If both OPENCOMPUTER.md and CLAUDE.md exist, OC reads OPENCOMPUTER.md
first and CLAUDE.md gets loaded second; content overlap is wasted
budget. The curator will flag duplicates.
