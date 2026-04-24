# Progressive disclosure — SKILL.md vs references/ vs examples/

The III.4 skill hierarchy gives you three layers of depth. Use them
deliberately — the goal is that the agent finds what it needs fast
without drowning in detail.

## Layer 1 — SKILL.md body

**What belongs here:** everything an agent needs to do the 80% case.
A numbered checklist. The decision tree. The short version of the
patterns. Targeted prose that's actionable without needing to open
other files.

**Size target:** 60-120 lines. This is what the agent always has
available after the skill triggers — budget it for context.

**Style:** imperative mood, short paragraphs, code fences for
commands. Think "one page you'd tape to the wall next to the
workstation".

## Layer 2 — references/*.md

**What belongs here:** depth for the 20% case. Long-form reference
material. Full API documentation. Edge cases and gotchas that don't
fit the short checklist. Background concepts a new reader might need
before the SKILL.md makes sense.

**Size target:** 30-80 lines each. Multiple focused files beat one
giant file — the agent can load ONE to answer its question.

**Naming:** kebab-case, content-describing. `common-causes.md`,
`event-catalog.md`, `schema-design.md`. Not `alpha.md` / `beta.md`.

## Layer 3 — examples/*

**What belongs here:** concrete, copy-paste-ready material. Real
examples of the pattern the skill teaches. Actual code, actual
configs, actual output. The file types allowed are any — `.md`,
`.py`, `.json`, `.yaml`, `.sh`, whatever makes sense.

**Size target:** 20-60 lines each. Big enough to be realistic, small
enough to be graspable.

**Distinguishing from references/:** if a user would copy it and
modify it to suit their need, it's an example. If they'd read it to
understand something, it's a reference.

## Decision tree for placement

```
Is this content >80% of uses?
├── YES → SKILL.md body
└── NO
    ├── Is it prose explaining HOW or WHY?
    │   └── references/
    └── Is it copy-paste code / config / data?
        └── examples/
```

## What NOT to do

**Don't duplicate between layers.** If the main SKILL.md already has
a checklist, don't repeat it in `references/`. Cross-reference
instead: "See references/common-causes.md for deeper diagnostics."

**Don't make SKILL.md a pointer index.** If every section is "See
references/X.md", the SKILL.md fails its job. The body should be
complete for common cases; references/ is a backup.

**Don't scatter one topic across many tiny files.** Each
`references/*.md` should be self-contained on its topic. If you need
three files to cover one concept, they should cross-reference each
other OR collapse into one.

## Example breakdown — `debug-python-import-error`

Look at how the shipped skill splits content:

- `SKILL.md` (~60 lines): five-step checklist, four most common
  causes with one-line fix each, OpenComputer-plugin-specific notes,
  "verify and save" closer. Enough for the 80% case.
- `references/common-causes.md` (~100 lines): deep diagnostics per
  cause. Opens with the exact error string signature, lists multiple
  diagnostic commands, explains less-obvious variants. Loaded only
  when SKILL.md's short version doesn't resolve.
- `examples/stack-trace-example.md` (~40 lines): a single worked
  trace with the full diagnostic path the skill would produce. Used
  to calibrate detail level.

That's the shape to aim for.

## Cross-linking between files

Inside a skill, use relative paths in markdown links:

```markdown
See [common causes](references/common-causes.md) for deep diagnostics.
```

Relative paths from the SKILL.md root. The tests verify these resolve
to real files — a broken link fails CI. Don't link to files that
don't exist yet.

## When to add a new references/ file vs. extend an existing one

New file when:

- The topic is genuinely distinct (different trigger, different
  audience, different failure mode).
- You'd want to load it alone, without the sibling files.

Extend existing when:

- The new content elaborates on something already in the file.
- It's the same topic at more depth.

Err on the side of more files. Smaller is easier to load surgically.
