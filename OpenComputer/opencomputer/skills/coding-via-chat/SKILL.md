---
name: coding-via-chat
description: Use when the user messages OpenComputer from a phone/Telegram/Discord asking for code changes — formalize the existing pattern of "message -> agent edits repo -> opens PR".
---

# Coding via Chat

## When to use

- User messages from Telegram/Discord/Slack/iMessage asking for code changes
- User on a phone wants to "fix that import error" or "add a CHANGELOG entry"
- User wants async coding work without opening a terminal

## What this skill does

OC already supports this pattern via the existing tool surface. This
SKILL.md just makes the workflow discoverable.

The pattern:
1. User messages the agent with a coding request
2. Agent picks the right repo (from chat context, recent files, or asks)
3. Agent uses `Edit` / `MultiEdit` / `Write` / `Bash` tools to make changes
4. Agent runs tests via `Bash` if appropriate
5. Agent commits + pushes + opens a PR via `Bash` (gh CLI) or `SpawnDetachedTask` for the heavy work
6. Agent reports back: PR URL + summary of what changed

## Procedure

1. **Confirm repo + branch**:
   - From the user's message, identify which repo. If ambiguous: ask. Don't guess.
   - Default to creating a new branch (never push directly to main).

2. **Verify ability to make the change safely**:
   - Tests exist for the area being changed?
   - Branch protection rules in place (so PR is required)?

3. **Make the change**:
   - Read the existing code first (use `Read` + `Grep`).
   - Edit minimally — match existing style / naming.
   - Run any relevant tests via `Bash`.
   - If the change is large or risky, use `SpawnDetachedTask` so the user gets their chat back.

4. **Commit + push**:
   - Conventional commits format
   - `gh pr create --base main --head <branch>` with a clean PR body
   - Use `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` per OC convention

5. **Report back**:
   - Send a short reply via the channel: "Opened PR #X: <one-line>. <PR URL>"
   - If CI takes time, note "CI running — will ping when green" (Phase 2)

## CAUTION

- **Never auto-merge** — user explicitly merges via `gh pr merge` after CI green
- **Never push to main** — always a feature branch + PR
- **Never amend a published commit** — create new commits
- **Never skip tests** — if the existing code has tests, run them; if they fail, fix them or surface clearly

## Examples

User (Telegram): "fix the import error in extensions/voice-mode/stt.py"
Agent: [reads file, finds the import, fixes it, runs tests, commits, opens PR, replies with URL]

User (iMessage): "bump CHANGELOG with note about voice mode improvements"
Agent: [reads CHANGELOG, adds entry, commits + pushes, opens PR, replies]

User (Slack): "rebase feat/voice-mode on main"
Agent: [identifies branch, runs `git rebase`, force-push if clean, replies with status]

## Why this is a discoverable skill

The pattern works WITHOUT this skill — OC has all the primitives. But:
- New users don't realize the agent CAN do this from chat
- The skill name `coding-via-chat` makes it findable when the agent's planner sees a coding request from a non-terminal channel
- It documents the conventions (no auto-merge, branch always, tests always)

## Notes

- Repo selection: when not specified, try the most recently-touched repo per V2.B Spotlight indexing.
- For very large changes, prefer to schedule via `opencomputer cron` rather than handling synchronously in chat.
