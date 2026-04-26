---
name: code-review
description: Use when reviewing pull requests, diffs, or recently-changed files for correctness, security, and project conventions
---

# Code Review

## When to use

- Reviewing a PR before merge
- Auditing recently-modified files (`git diff main`)
- Pre-commit self-review on the agent's own changes

## Steps

1. **Read the diff first, not the whole files.** `git diff <base>...HEAD` to scope what actually changed.
2. **Test coverage check.** For each new/changed function, find the test that exercises it. If none exists, that's the first issue to flag.
3. **Project conventions.** Read `CLAUDE.md` and any `<dir>/CLAUDE.md` for the touched paths. Style mismatches (PascalCase vs snake_case tool names, frozen dataclasses, etc.) are real review comments.
4. **Security pass.** Untrusted input rendered without escape, secrets in code, broad `except Exception`, command injection via shell, weak crypto — flag explicitly.
5. **Behavioral correctness.** Trace at least one happy path AND one error path through the change. Does the error path actually return / raise / log?
6. **Suggest, don't rewrite.** Reviewers propose; authors decide. Use "consider X because Y" not "change to X".

## Notes

- Empty diff after `git diff` means nothing staged — check `git status` first.
- If the PR has 50+ files, ask for a logical split before diving in. Massive PRs hide real issues.
- Don't approve your own auto-generated changes without a human eye when the change touches security, auth, or money.
