---
name: OpenComputer Skill Authoring
description: This skill should be used when the user asks to "write an OpenComputer skill", "add a skill to a plugin", "SKILL.md frontmatter", "skill description that triggers well", "reference files for a skill", "skill examples dir", or needs guidance on authoring filesystem-based skills for OpenComputer agents.
version: 0.1.0
---

# OpenComputer Skill Authoring

Skills are filesystem-based agent playbooks. Each skill is a directory
with a `SKILL.md` file at its root; the frontmatter supplies metadata
and the body is prose the agent loads when triggered. Supporting
material goes in sibling `references/` and `examples/` directories,
loaded progressively as the agent needs deeper context.

The agent discovers skills through `MemoryManager.list_skills()` at
`opencomputer/agent/memory.py`. No registration code — filesystem is
the contract.

## Minimum shape

```
my-skill/
└── SKILL.md
```

With this body:

```markdown
---
name: My Skill Name
description: When to trigger — verbs-first phrasing the retrieval layer matches.
version: 0.1.0
---

# My Skill

Body prose here. This is what the agent reads when the skill fires.
```

Three frontmatter fields:

- `name` — human-readable. Shown in `opencomputer skills` listings.
- `description` — the retrieval query. See "Triggering" below — this
  is the most important field to get right.
- `version` — semver-ish string. Use `0.1.0` to start.

The rest of the file is the skill's prose. 60-120 lines is a good target.

## The extended layout

```
my-skill/
├── SKILL.md                     # In-context prose (60-120 lines)
├── references/                  # Deeper docs — loaded on demand
│   ├── alpha.md
│   └── beta.md
└── examples/                    # Working examples the agent can copy
    ├── simple.md
    └── advanced.md
```

`references/*.md` — structured documentation. Only `.md` files. Loaded
on demand when the agent's exploration reaches a detail the main
SKILL.md doesn't cover.

`examples/*` — worked examples. Any file type (`.md`, `.py`, `.json`,
`.yaml`, text anything). The agent can open these when it wants to copy
a real pattern. Non-text files that fail UTF-8 decode are skipped.

Both directories are enumerated at skill discovery time; each file
becomes a `SkillReference` on the `SkillMeta` record and is available
for injection. See `references/progressive-disclosure.md` for deciding
what goes where.

## Triggering — the description field

When the user's intent comes in, the retrieval layer scores each
skill's `description` field against the query. A good description is
**verbs-first** and includes **synonyms the user might type**:

Good:
```yaml
description: Use when the user hits a ModuleNotFoundError, ImportError, ImportError when running a Python script, circular import, "no module named X", or asks about fixing a broken Python import.
```

Bad:
```yaml
description: This is a skill.
```

The description is NOT the place for prose — it's a retrieval query.
Stuff it with the exact phrases users naturally say, the exact error
messages they'd paste, and the verb forms they'd use ("fixing",
"debug", "resolve"). See `references/description-writing.md` for five
good + five bad concrete examples.

A convention many of OpenComputer's built-in skills use: open with the
literal phrase **"This skill should be used when"**, then list triggers.
The test suite enforces this prefix for the plugin-dev skill library
specifically — it keeps descriptions honest and retrieval-friendly.

## Placement tiers

Skills can ship from three places, all picked up by the same discovery:

1. **Bundled** — `opencomputer/skills/<name>/` inside the core package.
   Always available. Examples: this skill, `debug-python-import-error`,
   `coding-standards`, the other imported curations.
2. **Plugin-shipped** — `extensions/<plugin>/skills/<name>/`. Loaded
   when the plugin's profile is active. Example:
   `extensions/coding-harness/skills/*`.
3. **User-added** — `~/.opencomputer/<profile>/home/skills/<name>/`.
   User-local; can shadow bundled skills of the same id.

Later tiers win when IDs collide — same rule as agent templates.

## When to split into references vs inline

- **SKILL.md body** — what the agent needs **in-context** to do the
  task. Short, focused, verbs-first.
- **`references/`** — deeper docs the agent reads only when the body's
  coverage runs out. Long-form reference material.
- **`examples/`** — concrete copy-paste material. Full code samples,
  sample configs, sample outputs.

If a piece of content is longer than ~80 lines and the agent can
complete 80% of uses without it, put it in `references/`. If it's a
concrete thing a user might want to adapt verbatim, put it in
`examples/`.

## Writing style

- **Imperative mood.** "Check the traceback", not "You should check..."
- **Numbered lists for checklists.** The agent walks them in order.
- **Code fences for commands.** Triple-backtick + language tag.
- **Short paragraphs.** Two to four sentences each.
- **Avoid hedging.** Skills that waffle ("you might want to consider
  maybe checking...") don't give the agent confidence to act.

## Existing bundled skill as template

`opencomputer/skills/debug-python-import-error/` is the canonical
example. Read its SKILL.md, common-causes.md, and stack-trace-example.md
to calibrate style and depth.

## See also

- `opencomputer-plugin-structure` skill — when a plugin ships skills
  from its `skills/` subdir.
- `references/description-writing.md` — five good + five bad
  description patterns.
- `references/progressive-disclosure.md` — SKILL.md body vs references/
  vs examples/ split.
- `opencomputer/agent/memory.py::list_skills` — the discovery
  implementation.
