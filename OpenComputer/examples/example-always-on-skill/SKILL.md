---
name: example-always-on-skill
description: Minimal demonstration of the always_on frontmatter flag. Use as a copy-paste starting point for skills whose standing rules must reach the model on turn 0.
version: 0.1.0
always_on: true
---

# Example: always-on skill

This file demonstrates the `always_on: true` frontmatter flag. When a profile loads this skill, its body — everything below the frontmatter `---` fence — is rendered into Slot 4b of every system prompt. The model sees these rules from turn 0 without first invoking the Skill tool.

## When the rule fires

Every turn. That is the point of `always_on`. There is no per-turn condition, no plan-mode gate, no persona dispatch. The body is part of the standing prompt for as long as the skill is loaded.

## Cost

This body costs prompt tokens per turn. Authors should keep always-on bodies under ~8 KB even though the hard cap is 16 KB — every byte multiplies by every turn × every session × every profile that has the skill enabled.

## Composability

If this skill also declared `paths: ["my-project/**"]`, the body would only inject when the agent's cwd is inside (or below) a directory named `my-project/`. The cwd gate runs before the body loads.

If this skill also declared `disable_model_invocation: true`, the body would still inject — humans could invoke via `/example-always-on-skill` but the LLM could not auto-call it. The two flags are orthogonal: body is knowledge, invocation flag is a trigger guard.

## How to remove the rule

Either delete the `always_on: true` line (the skill stays loadable, just no longer auto-injected) or delete the skill directory entirely.
