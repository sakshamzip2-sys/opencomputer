# Hookify (OpenComputer port)

Auto-loading hook rules from markdown files. Drop a `.md` rule into
`~/.opencomputer/<profile>/hookify/` (or `.opencomputer/hookify/`
inside a project) and Hookify registers it as a `PreToolUse`,
`PostToolUse`, `Stop`, or `UserPromptSubmit` hook automatically.

Port of Anthropic's `hookify` plugin. Same frontmatter shape as the
upstream; rule files are byte-for-byte portable.

## File format

```markdown
---
name: warn-rm-rf            # required: kebab-case identifier
enabled: true               # required: bool
event: bash                 # required: bash | file | all | post | stop | prompt
action: warn                # optional: warn (default) | block
pattern: 'rm\s+-rf'         # optional shorthand: regex over the obvious field
                            #   bash → command, file → new_text, etc.
conditions:                 # optional: explicit field/operator/pattern triples
  - field: command
    operator: regex_match
    pattern: 'rm\s+-rf'
tool_matcher: Bash          # optional: override the default tool match
                            #   (Bash, Edit|Write|MultiEdit, *, etc.)
---

The body of the markdown file is the message shown when the rule
fires. Markdown formatting works.
```

### Event families

| Event family | Maps to OC HookEvent | Tool matcher default |
|--------------|---------------------|----------------------|
| `bash`       | `PRE_TOOL_USE`      | `Bash` |
| `file`       | `PRE_TOOL_USE`      | `Edit\|Write\|MultiEdit` |
| `all`        | `PRE_TOOL_USE`      | `.*` (every tool) |
| `post`       | `POST_TOOL_USE`     | `.*` |
| `stop`       | `STOP`              | n/a |
| `prompt`     | `USER_PROMPT_SUBMIT`| n/a |

### Operators

| Operator | Meaning |
|----------|---------|
| `regex_match` (default) | Python `re.search` (case-insensitive) |
| `contains`    | substring present |
| `not_contains`| substring absent |
| `equals`      | exact match |
| `starts_with` | prefix match |
| `ends_with`   | suffix match |

### Action semantics

- `warn` — message surfaces to the user; tool call proceeds.
- `block` — message surfaces to the user; tool call is refused.

If multiple rules match the same event, blocking rules win and all
matching messages are concatenated under their `**[name]**` headers.

## Where rules live

Searched in order; project rules shadow profile rules on name collision:

1. `$OPENCOMPUTER_PROFILE_HOME/hookify/*.md` — per-profile rules
2. `<cwd>/.opencomputer/hookify/*.md` — per-project rules (optional)

## Live reload

Rules are read on every hook invocation (not cached at register
time). Edit a rule file, save it, and the next tool call honours the
change. Toggle a rule off with `enabled: false` — no need to delete.

## Examples shipped

Five starter rules are in `examples/`. Copy any of them into
`~/.opencomputer/<profile>/hookify/` to activate:

- `warn-dangerous-rm.md` — flag `rm -rf` invocations
- `block-env-edits.md` — refuse direct `.env` edits
- `warn-prod-deploy.md` — heads-up before `*deploy*prod*` commands
- `warn-eval.md` — flag `eval(` introductions in source
- `remind-tests-before-push.md` — nudge `git push` to be preceded by tests

## Companion skill

The `hookify-rules-helper` skill (under `opencomputer/skills/`)
teaches the agent how to translate plain-English rule descriptions
into the file format above. Together: skill writes the rule file,
plugin runs it.

## Failure modes

- Parse error in a rule file → file is skipped with a WARN log;
  other rules still load.
- `enabled: false` → file is skipped silently.
- Hook handler exception → fail-open (OC contract); the call
  proceeds. Logged at WARN.
- Per-rule timeout: 2 seconds. Hookify rules should be regex-only;
  if you need slower work, write a proper plugin.

## License

MIT (matches the upstream Anthropic plugin).
