# OpenComputer Context Quality Criteria — Detailed Rubric

This rubric expands the eight criteria in `SKILL.md`. Each section gives
the question to ask, examples that score high, and examples that score
low.

## 1. Run/build commands (15 pts)

**Ask:** Can a fresh agent run the test suite, the dev loop, and a build
without grepping?

| Score | Example |
|-------|---------|
| 15/15 | `pytest tests/`, `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`, `pip install -e . --no-cache-dir --no-deps` all listed with one-line context |
| 10/15 | `pytest` listed but no install / lint commands |
| 5/15  | Only "use the standard tools" — no commands |
| 0/15  | Nothing about how to run anything |

## 2. Architecture clarity (15 pts)

**Ask:** Can a fresh agent answer "where does X live?" without reading
every file?

| Score | Example |
|-------|---------|
| 15/15 | Directory tree with one-line purpose per top-level dir; key modules called out |
| 10/15 | Top-level dirs listed, no purposes |
| 5/15  | Vague "monorepo with packages and tools" |
| 0/15  | No architecture section at all |

## 3. Non-obvious gotchas (15 pts)

**Ask:** Are the things that bit you in the past explicitly written down
so they don't bite the next agent?

| Score | Example |
|-------|---------|
| 15/15 | Numbered list of burned-in lessons (e.g. "always `hash -r` after editable reinstall — `source ~/.zshrc` doesn't refresh exec cache") |
| 10/15 | A few gotchas, no rationale (the *why* is what makes the lesson stick) |
| 5/15  | "Watch out for parallel sessions" — too vague to act on |
| 0/15  | Nothing |

## 4. OC plugin/skill awareness (10 pts)

**Ask:** If this project ships or depends on OC plugins/skills, does the
doc say so?

| Score | Example |
|-------|---------|
| 10/10 | "Install `coding-harness` for Edit/MultiEdit/TodoWrite. `browser-harness` for headless web." with one-line *why* each |
| 6/10  | Plugins mentioned but no rationale |
| 3/10  | Only the project's own plugins — no recommendation for which OC plugins to load |
| 0/10  | No plugin/skill mention |

## 5. Standing-Orders blocks (10 pts, AGENTS.md only)

**Ask:** Does this AGENTS.md leverage `## Program: <name>` blocks?

| Score | Example |
|-------|---------|
| 10/10 | 3+ Programs covering the most-repeated multi-step workflows in this repo |
| 6/10  | 1–2 Programs |
| 3/10  | A `## Program:` heading exists but the body is a free-form paragraph (parser expects numbered steps) |
| 0/10  | No Programs (or this isn't an AGENTS.md — score N/A) |

For non-AGENTS.md files, this criterion is N/A and the 10 points are
redistributed proportionally to the others.

## 6. Conciseness (10 pts)

**Ask:** Does every line earn its context budget?

| Score | Example |
|-------|---------|
| 10/10 | Every line is information-dense; bullets over paragraphs; commands over prose |
| 6/10  | Some narrative paragraphs that could be bullets |
| 3/10  | Multiple paragraphs of motivation/history |
| 0/10  | Wall of text |

## 7. Currency (15 pts)

**Ask:** Does the doc reflect the code as it is *today*, not as it was
six months ago?

| Score | Example |
|-------|---------|
| 15/15 | Every command runs; no references to deleted modules; mentions current branch / recent PRs sparingly |
| 10/15 | Mostly current; one or two stale paths |
| 5/15  | "Last updated 2024-01-15" — likely stale |
| 0/15  | References a module that doesn't exist anymore; commands fail |

To check currency: pick 3 random commands from the doc and run them.
Pick 3 module paths and `ls` them.

## 8. Actionability (10 pts)

**Ask:** Are instructions runnable, not vague?

| Score | Example |
|-------|---------|
| 10/10 | Every "do X" has the exact command in a code fence |
| 6/10  | Most instructions actionable; some "use the build tool" |
| 3/10  | Mostly aspirational ("be careful with imports") |
| 0/10  | Pure prose with no commands |

## Edge cases

- **Empty file** → automatic F (0/100). Recommend creating from a starter
  template (`references/templates.md`).
- **File only contains another file's content (e.g. CLAUDE.md is a copy
  of OPENCOMPUTER.md)** → flag as duplicate; recommend deleting one.
- **File >2000 lines** → likely too long. Recommend extraction into
  per-subdirectory files.
- **File has secrets** → CRITICAL. Stop scoring, flag immediately, do
  not show the secrets in any output.
