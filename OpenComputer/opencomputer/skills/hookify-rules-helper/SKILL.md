---
name: hookify-rules-helper
description: Translate plain-English rules into OpenComputer hook YAML or plugin-registered HookSpec entries. Use when the user says add a hook for X, warn me when X, block X, remind me before deleting Y, always run Y after Z, or hookify this. Knows the full event catalogue (PreToolUse, PostToolUse, UserPromptSubmit, Stop, PreLLMCall, …) and both paths — YAML hooks block in profile config or plugin register(api). Shows snippet, explains matcher, offers to save.
version: 0.1.0
---

# Hookify Rules Helper

Convert plain-English behavioral rules into the right OpenComputer hook
configuration. Two registration paths, both supported:

1. **Settings YAML** (no code) — for shell-command hooks: edit
   `~/.opencomputer/<profile>/config.yaml`, add a `hooks:` block. Best for
   one-off rules and team-shared rules that should ride in dotfiles.
2. **Plugin `register(api)`** — for Python-callable hooks with full access
   to `HookContext`. Best for rules that need to inspect typed structures
   or call OC internals.

## OC hook events (the 28-event catalogue)

The full list lives at `opencomputer/hooks/__init__.py`:`ALL_HOOK_EVENTS`.
The most-useful subset for end-user rules:

| Event | When | Common rules |
|-------|------|--------------|
| `PreToolUse` | Before any tool runs | Block dangerous commands, warn on file edits |
| `PostToolUse` | After a tool finishes | Auto-format edited files, log to disk |
| `UserPromptSubmit` | When the user sends a prompt | Inject context, warn on secrets |
| `PreLLMCall` | Before each LLM call | Append context, swap models on conditions |
| `PreCompact` / `PostCompact` | Around compaction | Save snapshots, log compaction events |
| `Stop` | Agent finished a turn | Cleanup, notify, run a follow-up |
| `Notification` | Agent surfaces a notification | Forward to Slack/Telegram |
| `SessionStart` / `SessionEnd` | Lifecycle | Banner, log, archive |
| `SubagentStop` | Subagent finished | Aggregate results |

**Matchers** (PreToolUse / PostToolUse): regex against tool name. Examples:
`Edit|Write|MultiEdit` (file mutations), `Bash` (shell), `.*` (everything).

## Workflow

### Step 1: Pin down the rule

When the user describes a rule in English, extract:

1. **Trigger event** — which of the 28 events? Default to `PreToolUse` for
   "block/warn before X" and `PostToolUse` for "after X happens".
2. **Matcher** — which tools? Default to `.*` if unspecified, but ask if
   the rule sounds tool-specific.
3. **Action** — block, warn, or run-and-continue?
4. **Body** — the actual check (regex over command, file path, content).

### Step 2: Pick the registration path

Show the user both options when there's a meaningful choice:

| Use settings YAML when | Use plugin when |
|------------------------|------------------|
| The check is a regex or shell condition | The check needs typed access to `HookContext` |
| The user wants per-profile control | The rule should ship with a plugin |
| The check fits in 1-3 lines of bash/python | The handler is > 30 lines |
| User says "just add it to my config" | User says "make this part of the plugin" |

### Step 3: Show the snippet (and only then save)

Always print the full snippet first. Save only after explicit "yes/save it".

#### Settings YAML path

Add to `~/.opencomputer/<profile>/config.yaml`:

```yaml
hooks:
  PreToolUse:
    - matcher: "Edit|Write|MultiEdit"
      command: "python3 ~/.opencomputer/<profile>/hooks/warn_on_env_edit.py"
      timeout_seconds: 5
```

Hook script (`warn_on_env_edit.py`):

```python
#!/usr/bin/env python3
"""Warn whenever the agent edits a .env file."""
import json, sys

data = json.load(sys.stdin)
path = data.get("tool_input", {}).get("file_path", "")
if path.endswith(".env") or "/.env" in path:
    print(json.dumps({
        "decision": "block",
        "reason": "Refusing to edit .env directly. Edit .env.example or "
                  "use a per-key SecretRef instead.",
    }))
    sys.exit(0)

# Default: pass
print(json.dumps({}))
```

The hook protocol:
- **stdout JSON** preferred. `{"decision": "block", "reason": "..."}` blocks.
  `{"action": "approve"}` passes. `{"context": "..."}` on `PreLLMCall`
  appends to the user message.
- **Exit code** fallback: `0` pass, `2` block, anything else fail-open.
- **Timeouts/crashes** fail open.
- Env vars available: `OPENCOMPUTER_EVENT`, `OPENCOMPUTER_TOOL_NAME`,
  `OPENCOMPUTER_SESSION_ID`, `OPENCOMPUTER_PROFILE_HOME`,
  `CLAUDE_PLUGIN_ROOT` (alias of profile home — Claude-Code hooks drop in
  unchanged).

#### Plugin path

In `extensions/<plugin>/plugin.py`:

```python
from plugin_sdk import HookEvent, HookSpec
from plugin_sdk.hooks import HookContext, HookResult

async def on_pre_tool_use(ctx: HookContext) -> HookResult:
    if ctx.tool_name not in ("Edit", "Write", "MultiEdit"):
        return HookResult.passthrough()
    path = (ctx.tool_input or {}).get("file_path", "")
    if path.endswith(".env"):
        return HookResult.block(
            reason="Refusing to edit .env. Use SecretRef.",
        )
    return HookResult.passthrough()

def register(api) -> None:
    api.register_hook(HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=on_pre_tool_use,
    ))
```

### Step 4: Suggest a follow-up rule

After saving one rule, ask "want to add another? Common companions to this
rule are…". For PreToolUse Edit hooks the classic companion is a
PostToolUse formatter — offer it.

## Common rules cookbook

| English | Event | Matcher | Body |
|---------|-------|---------|------|
| "warn me before `rm -rf`" | PreToolUse | `Bash` | regex `rm\s+-rf` on `tool_input.command` |
| "block edits to `.env`" | PreToolUse | `Edit\|Write\|MultiEdit` | check `tool_input.file_path` ends `.env` |
| "auto-format Python after edits" | PostToolUse | `Edit\|Write\|MultiEdit` | run `ruff format $FILE` |
| "remind to run tests before pushing" | PreToolUse | `Bash` | regex `git\s+push` → warn message |
| "scan secrets before sending to LLM" | PreLLMCall | (n/a) | run secret-scan, return `{"context": "..."}` if found |
| "log every Bash command" | PostToolUse | `Bash` | append to `~/.opencomputer/<profile>/logs/bash.log` |
| "block edits to migrations after Friday" | PreToolUse | `Edit\|Write\|MultiEdit` | path matches `migrations/.*sql` AND weekday > 4 |

## Validating a rule

Before saving, OC offers `oc hooks test` and `oc hooks doctor`:

```bash
oc hooks doctor       # syntax + matcher sanity over the whole profile
oc hooks test --event PreToolUse --tool-name Bash --tool-input '{"command":"rm -rf /"}'
```

Always recommend running these after saving. The hook system fails open
on errors, so a broken rule won't wedge the loop — but it also won't fire
silently if the matcher is wrong.

## Anti-patterns

- **`matcher: "*"`** — that's not a regex; use `.*`.
- **`matcher` on non-tool events** — `UserPromptSubmit` / `Stop` ignore
  matchers. Don't include one.
- **Long-running hooks** — anything > `timeout_seconds` is killed. Keep
  hook scripts under 1s p99; defer slow work to background processes.
- **Side effects without logging** — if a hook fails open, you'll never
  know it failed. Log every failure-open path to
  `~/.opencomputer/<profile>/logs/hooks.log`.

## See also

- `opencomputer-hook-authoring` skill — full reference for plugin-authored hooks
- `opencomputer/hooks/__init__.py` — `ALL_HOOK_EVENTS` (the 28-event catalogue)
- `oc hooks list` / `oc hooks test` / `oc hooks doctor` — diagnostics
