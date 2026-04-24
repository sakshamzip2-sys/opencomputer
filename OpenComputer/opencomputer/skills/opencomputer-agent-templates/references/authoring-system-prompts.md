# Authoring an agent template's system prompt

The body of an agent-template `.md` file is the child subagent's
**entire** system prompt. Unlike the parent agent, a delegated subagent
using a template does NOT receive:

- Declarative memory (`MEMORY.md`).
- User profile (`USER.md`).
- Personality (`SOUL.md`).
- Auto-injected skills.
- The default base prompt.

The template author owns the whole prompt. That's deliberate — it makes
subagent behavior reproducible and testable independent of whoever's
parent profile spawned it. It also means: your prompt must be
**self-contained**.

## Required elements

Every good template prompt has:

1. **Identity** — one sentence saying who the agent is. ("You are a
   security-focused code reviewer.")
2. **Task shape** — what the agent is expected to do. ("Review the
   unstaged diff from `git diff` by default.")
3. **Focus list** — what to look at, usually as a bulleted list.
4. **Confidence / signal-to-noise rule** — prevents the child from
   reporting speculation. ("Only report issues with >=80% confidence.")
5. **Output shape** — how to structure the answer. ("Return `##
   Blocking`, `## Suggested`, `## Notes` headings.")
6. **Exit condition** — when the child loop should stop and return.
   ("If the diff is clean, say so in one line and stop.")

Without an explicit exit condition, the child keeps looping until the
iteration budget runs out.

## Length

60-150 lines of body is a good target. Longer templates usually signal
scope creep — split into two templates with different names.

## Style

- **Imperative mood.** "Review X" not "You should consider reviewing X."
- **Concrete rules.** "Skip TODO comments" beats "don't get distracted."
- **Specific heuristics.** "Flag any regex that can't be parsed by
  `re.compile`" is more useful than "flag bad regex."
- **No meta language.** The child doesn't know it's running from a
  template; don't reference "the template" or "the delegation".

## Boundaries with tools

Your prompt talks about WHAT to do; the `tools` allowlist constrains
HOW. If your prompt says "read each file in the diff", make sure
`tools: Read, Grep` is in the frontmatter — otherwise the child has to
beg the parent for help, which breaks the isolation.

Cross-check: after writing the prompt, list every action it implies.
Every action needs a corresponding tool in the allowlist, OR a note
that the child should describe rather than do.

## Confidence and silence

Default-noisy agents produce reviews nobody reads. Default-silent
agents miss things. The sweet spot is calibrated silence:

> "Only report issues you have high confidence (>=80%) in. Prefer
> silence over false positives — a terse, correct review beats a noisy
> one."

Adapt the percentage to taste. The important thing is that the agent
knows to drop uncertain findings rather than cover itself with
hedging ("possibly", "might want to check", "not sure but").

## The exit-condition pattern

Every template should tell the child when it's DONE. Options:

- **Structured report** — "Return a report with three sections. Then
  stop." Paired with an explicit output shape.
- **Pass/fail verdict** — "Conclude with `VERDICT: pass` or `VERDICT:
  fail` and stop."
- **Empty-case short-circuit** — "If the diff is clean, say so in one
  line and stop."

Without one of these, the child sometimes produces work, reviews it,
revises, and loops until `max_iterations` kicks in. That's expensive
and the final answer isn't necessarily better than the third pass.

## Example structure

The bundled `opencomputer/agents/code-reviewer.md` follows a template
worth copying:

```
You are an expert code reviewer. By default, review unstaged changes from
`git diff`. Focus on:

- **Bug detection** — ...
- **Project conventions** — read `CLAUDE.md` / `AGENTS.md` ...
- **Security** — ...

Only report issues you have high confidence (>=80%) in. Prefer silence ...

Return a short structured report:

- `## Blocking` — ...
- `## Suggested` — ...
- `## Notes` — ...

If the diff is clean, say so in one line and stop.
```

That's identity + task + focus list + confidence rule + output shape +
exit condition in under 20 lines. Use it as a template for your own
template.

## Testing a template

Before shipping, invoke the template manually:

```bash
opencomputer agents list    # confirm it was discovered
opencomputer                # start a chat
# Then: "Please delegate this task using the security-reviewer agent."
```

Watch the child's output. Red flags:
- Child asks clarifying questions the prompt should have answered.
- Child reports obviously low-confidence things.
- Child never stops and hits the iteration budget.
- Child calls tools outside the allowlist (that can't happen — the
  allowlist is enforced — but if the prompt instructs it to, that's a
  prompt bug).

Iterate on the prompt until the child does the right thing without
hand-holding.
