---
name: model-route
description: Recommend the best model tier (haiku/sonnet/opus) for the current task by complexity and budget. Use when picking which model an agent should run a job on.
---

<!-- Source: everything-claude-code (MIT) — adapted for OpenComputer 2026-04-27 -->


# Model Route Command

Recommend the best model tier for the current task by complexity and budget.

## Usage

`/model-route [task-description] [--budget low|med|high]`

## Routing Heuristic

- `haiku`: deterministic, low-risk mechanical changes
- `sonnet`: default for implementation and refactors
- `opus`: architecture, deep review, ambiguous requirements

## Required Output

- recommended model
- confidence level
- why this model fits
- fallback model if first attempt fails

## Arguments

$ARGUMENTS:
- `[task-description]` optional free-text
- `--budget low|med|high` optional

## Tier Selection Detail

### Pick `haiku` when

- The task is a single, well-specified mechanical edit (rename a symbol, reformat a file, apply a known patch)
- There is a clean specification and the success criterion is verifiable by a fast deterministic check (lint, typecheck, exact-match test)
- Budget is `low` and the work is genuinely small
- You expect zero ambiguity in the request

### Pick `sonnet` when

- The task is normal feature implementation, bug fix, refactor, or test authoring
- It needs reasoning over multiple files but follows a known pattern
- You want a balance of cost and quality
- This is the default — choose this unless a stronger reason pushes you up or down

### Pick `opus` when

- The work involves architecture decisions or trade-off analysis
- The requirements are ambiguous and you need a model that pushes back and clarifies
- Code review needs to find subtle issues across many files
- A failed attempt would be expensive (e.g. would land in production, would block other work)
- Budget is `high` or unspecified for a clearly-important task

## Confidence Levels

- **HIGH**: The signals are unambiguous (obvious mechanical task -> haiku, obvious architecture call -> opus)
- **MEDIUM**: The task could plausibly fit two tiers; you're picking the lower one to save budget or the higher one to reduce risk
- **LOW**: You're guessing — say so and recommend the user override if the first attempt looks weak

## Fallback Recommendation

For each pick, name the next tier up as the fallback. If `haiku` produces a wrong answer, retry with `sonnet`. If `sonnet` looks confused, escalate to `opus`. If `opus` itself fails, the issue is in the spec, not the model.

## Output Format

```
Recommended model: <haiku|sonnet|opus>
Confidence: <HIGH|MEDIUM|LOW>
Reason: <one or two sentences>
Fallback: <next tier up + when to escalate>
```
