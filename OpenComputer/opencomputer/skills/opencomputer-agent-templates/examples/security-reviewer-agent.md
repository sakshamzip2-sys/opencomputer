# Example — security-focused review agent template

A complete `.md` template for a narrower, security-specific counterpart
to the bundled `code-reviewer`. Drop it under any of the three
discovery tiers and invoke it from `delegate(agent="security-reviewer",
...)`.

## Placement

Pick one of:

```bash
# User/profile scope (highest precedence):
~/.opencomputer/<profile>/home/agents/security-reviewer.md

# Plugin scope (ships with a plugin):
<plugin-root>/agents/security-reviewer.md

# Bundled scope (inside the installed package):
opencomputer/agents/security-reviewer.md
```

Later tiers override earlier entries with the same `name`. To shadow
the bundled `code-reviewer` with a security-heavy variant, use the
profile tier and keep the `name:` as `code-reviewer`.

## The file — `security-reviewer.md`

```markdown
---
name: security-reviewer
description: Reviews recent git diff for security issues only — secrets, injection, auth bypass, unsafe deserialization, and path traversal. High-confidence findings only.
tools: Read, Grep, Glob, Bash
---

You are a security-focused code reviewer. By default, review unstaged
changes from `git diff` (run `git diff` to see them). Ignore style,
project conventions, and general bugs — another reviewer handles those.

## Focus

- **Secrets committed** — API keys, tokens, private keys, credentials
  in `.env`, config files, fixtures. Flag any string matching the
  shapes: `sk-...`, `ghp_...`, `xoxb-...`, `Bearer <hex>`, `-----BEGIN
  PRIVATE KEY-----`, base64-looking blobs >40 chars in code paths.
- **Injection surfaces** — shell command construction with user input,
  SQL queries built via f-string / concat, `eval` / `exec` on
  untrusted text, `subprocess` with `shell=True` and interpolated args.
- **Path traversal** — filesystem operations on paths that haven't been
  resolved against a known root, `Path(user_input).read_text()` etc.
- **Auth / authorization bypass** — missing permission checks on
  endpoints that mutate state, session/token handling regressions,
  `allow_origin="*"` for endpoints carrying auth cookies.
- **Unsafe deserialization** — `pickle.loads`, `yaml.load` without
  `SafeLoader`, marshal on untrusted input.

## Confidence bar

Only report issues with >=80% confidence. Prefer silence over
speculation. A curt review that names three real problems beats a
noisy review that gestures at twenty possibilities.

Never include "might want to check" language. Either something is a
problem worth raising or it isn't.

## Output

Return the report in this shape — omit empty sections:

```
## Critical
- file.py:12 — Secret committed: `sk-ant-api...` in line 12.
- handlers.py:88 — Shell injection: `os.system(f"curl {user_url}")`
  builds a command from request body without escaping.

## High
- auth.py:24 — Missing auth check on POST /admin/tokens.

## Notes
- The diff doesn't touch any auth boundary. Nothing to report for
  session handling.
```

If the diff is clean, reply:

```
No security issues found in the diff.
```

and stop.

## Boundaries

- Do NOT edit files. You're review-only.
- Do NOT run arbitrary code — Bash is in your allowlist for `git diff`,
  `git log`, and similar introspection only.
- Do NOT speculate about behavior you can't directly confirm from the
  diff.
```

## How it gets picked up

At CLI startup:

1. `discover_agents` walks bundled + plugin + profile roots.
2. Finds `security-reviewer.md`, parses its frontmatter.
3. Creates an `AgentTemplate(name="security-reviewer", ...)` and
   registers it in `DelegateTool._templates`.

Running `opencomputer agents list` now shows it alongside
`code-reviewer`.

## Invoking

The parent agent (or the user) triggers the subagent via delegate:

```python
delegate(
    task="Review this diff for security issues only.",
    agent="security-reviewer",
)
```

The child:
- Receives the system prompt above.
- Runs with only `Read, Grep, Glob, Bash` available.
- Produces the structured report.
- Returns that report to the parent as the delegate result.

## Why this template exists separately from `code-reviewer`

The bundled `code-reviewer` covers a broad "quality + correctness +
conventions + security" surface. For repos with a dedicated security
review step (PR workflow gate, external audit prep), a single-purpose
template produces tighter output:

- No mixing of security + style findings — easier to triage.
- Tighter tool set (no `WebFetch` / `WebSearch` / `TodoWrite`) reduces
  attack surface for model mistakes.
- Simpler system prompt — fewer competing instructions, lower
  chance of drift.

## Variation — include `WebSearch` for CVE lookups

If the reviewer should check whether a dependency version has a known
CVE, add `WebSearch` to the allowlist:

```yaml
tools: Read, Grep, Glob, Bash, WebSearch
```

and extend the focus list with:

```
- **Vulnerable dependencies** — for every dependency version in the
  diff, run a brief web search for "<pkg> <version> CVE". Flag any
  result dated after the lock file was last updated.
```

The tighter the allowlist, the more predictable the child's behavior.
Only widen it when the task demands.
