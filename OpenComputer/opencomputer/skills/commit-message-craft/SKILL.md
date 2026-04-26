---
name: commit-message-craft
description: Use when writing git commit messages, conventional commit headers, or curating a clean diff history
---

# Commit Message Craft

## When to use

- About to run `git commit`
- Squashing a series of WIP commits
- Reviewing your own diff before push

## Steps

1. **Subject line: imperative + 70 chars max.** "Add X" / "Fix Y" / "Refactor Z". Not "Added", not "Fixes the bug where…".
2. **Type prefix when relevant.** `feat:` `fix:` `chore:` `docs:` `refactor:` `test:` `perf:`. Conventional Commits work even outside formal CC repos.
3. **Why, not what.** The diff shows what. The message explains why now / why this approach / what was rejected.
4. **Body wraps at 72.** Real paragraphs. Bullet points OK for change lists, but prose explains motivation better.
5. **One commit = one logical change.** Mixed concerns get separate commits. `git add -p` to split.
6. **Reference, don't duplicate.** `Closes #42`, `See RFC-007`. Don't paste issue body.
7. **Avoid these:**
   - "WIP" / "checkpoint" / "fix typo" in the final history (squash them away)
   - "minor changes" / "various improvements" (specify what)
   - Author signature at the end (git already tracks that)

## Notes

- Reading your last 10 commits is the best test of message quality. If you can't tell what each one did, write better.
- Co-authors via `Co-Authored-By: Name <email>` line at the end (preserved by GitHub PR squash).
- Don't bypass hooks (`--no-verify`) without a real reason.
