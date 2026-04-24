# Designing an agent template's tool allowlist

The `tools:` frontmatter field on an agent template is a comma-
separated allowlist (or YAML list) that constrains which tools the
child subagent can call. Getting this set right is the difference
between a reliable delegation and one that accidentally edits files
you didn't want edited.

## How the allowlist is applied

At delegate-dispatch time (`opencomputer/tools/delegate.py:
DelegateTool.execute`):

1. If the caller passed an explicit `allowed_tools` list in the
   `delegate(...)` call, that wins. Always.
2. Otherwise, the template's `tools` tuple becomes the allowlist,
   converted to a `frozenset[str]`.
3. An empty tuple in the template means "inherit the parent's full
   tool set" — NO restriction. This is the default when the
   frontmatter omits `tools:` entirely.
4. An explicit empty list `tools: []` means "no tools" — a pure
   reasoning delegation.

The allowlist is then pushed onto the child loop as `allowed_tools`.
When the child dispatches a tool call, the registry refuses names not
in the set.

## Starting minimum

Most delegations only need these tools:

| Task class | Minimum allowlist |
|------------|-------------------|
| Read-only review | `Read, Grep, Glob, Bash` |
| Code search / exploration | `Read, Grep, Glob` |
| Web-aware research | `Read, Grep, Glob, WebFetch, WebSearch` |
| Documentation writer | `Read, Grep, Glob, Write` |
| Test runner | `Read, Grep, Glob, Bash, RunTests` |

`Bash` is often needed for read-only work too — `git diff`, `ls`, `cat`
— but it's the highest-risk tool in the set. Prefer `Read` + `Grep`
when they're sufficient.

## Tools to avoid by default

| Tool | Why |
|------|-----|
| `Edit` / `MultiEdit` | Mutates files. A review/exploration agent should never need these. |
| `Write` | Creates files. Keep out of allowlists except for documentation agents. |
| `StartProcess` / `KillProcess` | Background processes — long-running, hard to clean up. |
| `TodoWrite` | Mutates parent session state. Subagent TODOs are the wrong layer. |
| `PushNotification` | Sends external messages. Cross-session side effect. |
| `delegate` | Recursive delegation. Works but expensive and usually a sign of scope creep. |

Include these ONLY when the task genuinely needs them — and document
the reason in the template's system prompt so a future reader knows
why.

## Tools to almost always include

- `Read` — looking at file contents is the cheapest way to gather
  context. Excluding it forces the subagent to beg the parent for
  snippets.
- `Grep` — finding strings in code. Zero side effects.
- `Glob` — enumerating files. Zero side effects.

Even a strictly-reasoning agent benefits from being able to read the
files it's reasoning about.

## Reading the coding-harness patterns

The bundled `code-reviewer.md` uses:

```
tools: Read, Grep, Glob, Bash, WebFetch, WebSearch, TodoWrite
```

Note `TodoWrite` is in the set — this is a deliberate choice for
reviews that produce a concrete task list the parent can consume. For
a pure "report the findings" agent, drop `TodoWrite`.

`Bash` is included because code review often needs `git log`, `git
diff`, `git blame`. If your template doesn't run git, drop `Bash`.

## Decision tree

```
Does the task produce file mutations?
├── YES → include Edit, MultiEdit, Write as relevant, plus Read/Grep/Glob.
│         Write a prompt that tells the subagent to CONFIRM before writing.
└── NO
    ├── Does the task need to run commands? (tests, git, etc.)
    │   ├── YES → Read, Grep, Glob, Bash
    │   └── NO
    │       ├── Does it need the web?
    │       │   ├── YES → Read, Grep, Glob, WebFetch, WebSearch
    │       │   └── NO  → Read, Grep, Glob
    │       └── (if it doesn't even need to read files) → []
```

## Explicit-caller-wins semantic

Remember: an explicit `allowed_tools` in the `delegate(...)` call
OVERRIDES the template. A security-conscious user who knows their
template allows `Bash` can still call:

```python
delegate(
    task="Summarize the diff.",
    agent="code-reviewer",
    allowed_tools=["Read", "Grep", "Glob"],  # no Bash
)
```

This means you can design a permissive default template and let
specific callers lock it down. The converse (restrictive template,
permissive caller) also works — the template's tools don't apply when
the caller is explicit.

## Cross-checking your allowlist

After writing the template:

1. Read the system prompt.
2. List every verb that implies a tool ("read files" → Read, "search
   for X" → Grep, "run the tests" → Bash or RunTests).
3. Confirm each verb has an allowlisted tool.
4. Confirm every allowlisted tool has a prompt verb that uses it. If
   not, drop the tool.

A tool in the allowlist that the prompt never implies is dead weight
— remove it. A prompt verb without a matching tool is a bug — either
add the tool or reword the prompt.

## Testing

```bash
opencomputer agents list                    # see the allowlist in output
opencomputer --verbose                      # watch tool dispatches in chat
```

When you delegate via the subagent, you'll see tool calls tagged with
the subagent session id. If the child tries a tool not in the
allowlist, the dispatch returns a clear "tool not in allowlist" error
— that's usually a prompt/allowlist mismatch to fix.
