# Claude Code inventory (for Phase 11)

Source repo: `/Users/saksham/Vscode/claude/sources/claude-code/`
Walked: `src/tools/`, `src/plugins/`, `src/extensions/`, `src/mcp/`, `src/channels/`, `src/connectors/`, the bundled plugin sets.
Date: 2026-04-22.

| Item | Kind | OC equivalent (or "missing") | Value if ported | Cost | Verdict | Destination |
|---|---|---|---|---|---|---|
| Read | tool | Read | — | — | already-have | core |
| Write | tool | Write | — | — | already-have | core |
| Edit | tool | Edit (coding-harness) | — | — | already-have | coding-harness |
| MultiEdit | tool | MultiEdit (coding-harness) | — | — | already-have | coding-harness |
| Bash | tool | Bash | — | — | already-have | core |
| Grep | tool | Grep | — | — | already-have | core |
| Glob | tool | Glob | — | — | already-have | core |
| WebFetch | tool | WebFetch | — | — | already-have | core |
| WebSearch | tool | WebSearch | — | — | already-have | core |
| TodoWrite | tool | TodoWrite (coding-harness) | — | — | already-have | coding-harness |
| Task / Agent | tool | Delegate | — | — | already-have | core |
| NotebookEdit | tool | missing | high | M | port | core |
| NotebookRead | tool | covered by Read (ipynb support) | — | — | already-have | core |
| ExitPlanMode | tool | missing | high | S | port | coding-harness |
| AskUserQuestion | tool | missing | high | M | port | core |
| PushNotification | tool | missing | med | S | port | core |
| Monitor | tool | partial — Background tool | high | S | port | coding-harness |
| BashOutput / KillShell | tool | covered by Background | high | S | port | coding-harness |
| Skill (invocable) | tool | partial — SkillManage handles CRUD only | med | S | port | core |
| SlashCommand | tool | n/a — slash commands are user-driven | — | — | skip | n/a |
| security-guidance | hook plugin | missing | med | S | port | core |
| commit-commands | command set | missing | high | S | port | coding-harness |
| code-review | command set | missing | high | M | port | new:extensions/dev-tools |
| pr-review-toolkit | plugin | missing | high | M | port | new:extensions/dev-tools |
| feature-dev | plugin (sub-agents) | missing | high | L | port | new:extensions/dev-tools |
| frontend-design | skill plugin | missing | med | M | port | new:extensions/dev-tools |
| ralph-wiggum (loop runner) | plugin | missing | med | S | port | new:extensions/dev-tools |
| plugin-dev (create-plugin etc.) | plugin | partial — Phase 10c covers `plugin new` | high | M | port-overlap | core (10c) |
| skill-development / hook-development / mcp-integration | skills | missing | med | M | port | new:extensions/dev-tools |
| learning-output-style / explanatory-output-style | output styles | missing | low | S | skip | n/a |
| MCP `.mcp.json` config (oauth, env-substitution, tool-naming) | MCP pattern | partial — stdio works, http/oauth missing | high | M | port | mcp-bundle (Phase 11c) |
| Hooks: SessionStart, PreToolUse, PostToolUse | hook events | already-have | — | — | already-have | core |
| Hooks: PreCompact, SubagentStop, Notification, UserPromptSubmit | hook events | partial — only 6 of 9 events implemented | med | S | port | core |

## Notes

**Plugin-dev overlap with Phase 10c.** Claude Code's `plugin-dev` plugin offers `create-plugin` + a suite of skills (mcp-integration, hook-development, command-development, agent-development, plugin-structure, plugin-settings, skill-development) along with `agent-creator`, `plugin-validator`, `skill-reviewer` agents. OpenComputer's Phase 10c is shipping `opencomputer plugin new`; the rest of `plugin-dev` (validators, reviewers, skill scaffolders) is a natural follow-up bundled into a `dev-tools` plugin.

**MCP integration is more mature than ours.** Claude Code's `.mcp.json` supports stdio + SSE + HTTP + WebSocket transports, OAuth flows (with token persistence + localhost callback), env var substitution, and consistent tool naming (`mcp__plugin_<name>_<server>__<tool>`). OpenComputer has stdio only (line 119 of `mcp/client.py` is `NotImplementedError("http MCP transport — Phase 4.1")`). Phase 11c is the natural home for closing this gap.

**Behaviour-modifying plugins are distinct from tool plugins.** `security-guidance`, `learning-output-style`, `explanatory-output-style` work via SessionStart / PreToolUse hooks that inject system-prompt fragments — they don't expose new tools. The pattern is worth documenting in the `docs/plugin-authors.md` Phase 10c doc as a third plugin shape ("instruction-modifying plugin" alongside tool/channel/provider).

**Hook event count.** Claude Code has 9 hook events; OpenComputer has 6 (PreToolUse, PostToolUse, Stop, SessionStart, SessionEnd, UserPromptSubmit). The missing events (PreCompact, SubagentStop, Notification) were on the original Phase 13 `13a` list — re-surface them in Phase 11b if there's appetite, otherwise leave them to 13.
