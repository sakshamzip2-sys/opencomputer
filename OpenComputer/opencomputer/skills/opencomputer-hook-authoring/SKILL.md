---
name: OpenComputer Hook Authoring
description: This skill should be used when the user asks to "add a hook to an OpenComputer plugin", "write a PreToolUse hook", "block dangerous commands", "log tool calls", "SessionStart hook", "HookSpec", "register_hook", or wants to add automation at OpenComputer's lifecycle events.
version: 0.1.0
---

# OpenComputer Hook Authoring

Hooks are lifecycle handlers fired at well-defined points in the agent
loop. Two authoring paths share the same `HookEvent` vocabulary and the
same `HookDecision` contract:

1. **Python path** — a plugin's `register(api)` constructs a
   `HookSpec(event, handler, matcher, fire_and_forget)` and passes it to
   `api.register_hook(spec)`. The handler is an `async` callable.
2. **Settings YAML path** — users declare shell-command hooks under the
   top-level `hooks:` key in `config.yaml`. III.6 wraps each command in a
   generated async handler that pipes a JSON blob of the `HookContext` to
   stdin and translates the exit code back into a `HookDecision`.

Both paths coexist; both fire for the matching event. Plugin-declared
hooks fire first, then settings-declared hooks.

## The nine events

Declared in `plugin_sdk/hooks.py::HookEvent`:

| Event | When | Blocking? |
|-------|------|-----------|
| `PreToolUse` | Before every tool dispatch | Yes — decision gates the call |
| `PostToolUse` | After every tool dispatch | No — fire-and-forget |
| `Stop` | Model stops requesting tools | Yes — can force continue |
| `SessionStart` | New session opened | No |
| `SessionEnd` | Session closed | No |
| `UserPromptSubmit` | User sends a message | Yes — can inject context |
| `PreCompact` | Before `CompactionEngine.summarize` | No |
| `SubagentStop` | Delegated subagent finishes | No |
| `Notification` | `PushNotification` dispatches a message | No |

See `references/event-catalog.md` for one-paragraph-each semantics.

## HookSpec

```python
@dataclass(frozen=True, slots=True)
class HookSpec:
    event: HookEvent
    handler: HookHandler                  # async (ctx) -> HookDecision | None
    matcher: str | None = None            # regex over tool name (Pre/PostToolUse)
    fire_and_forget: bool = True          # False for blocking gates
```

`matcher` is a Python `re.search` regex run against `ctx.tool_call.name`
for `PreToolUse` / `PostToolUse`. Use `"Edit|Write|MultiEdit"` to match
any of several tools, `"^Bash$"` for an exact match, or `None` to match
every call.

## HookDecision

```python
@dataclass(frozen=True, slots=True)
class HookDecision:
    decision: Literal["approve", "block", "pass"] = "pass"
    reason: str = ""
    modified_message: str = ""  # injected as a system reminder
```

Returning `None` from a handler is equivalent to `HookDecision(decision="pass")`.
Only `decision="block"` gates a PreToolUse dispatch. `"approve"` is a
positive ack used by yolo-mode flows. Post-action hooks ignore the
return value.

## The Python path — building a PreToolUse gate

```python
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


async def block_workspace_escape(ctx: HookContext) -> HookDecision | None:
    if ctx.tool_call is None:
        return None
    path = ctx.tool_call.arguments.get("file_path") or ""
    if not isinstance(path, str) or not path.startswith(str(Path.cwd())):
        return HookDecision(
            decision="block",
            reason=f"{ctx.tool_call.name} refused: {path!r} is outside the workspace.",
        )
    return None  # pass


def register(api) -> None:
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=block_workspace_escape,
            matcher="Edit|MultiEdit|Write",
            fire_and_forget=False,  # blocking — we want the decision
        )
    )
```

`ctx.runtime` (optional, `None` for pre-6a hooks) exposes `plan_mode`
and `yolo_mode`. The coding-harness plan-mode hook at
`extensions/coding-harness/hooks/plan_block.py` is the canonical model.

## Fire-and-forget vs blocking

- **`PreToolUse` + `Stop` + `UserPromptSubmit`** — can return a
  decision; the hook engine's `fire_blocking` returns the first non-pass
  decision. Set `fire_and_forget=False` for these.
- **Everything else** — post-action observation. The loop continues
  without waiting. Set `fire_and_forget=True` (the default). Exceptions
  in these handlers are caught and logged; they cannot break the loop.

## The settings YAML path

Declare shell-command hooks under the top-level `hooks:` block in
`~/.opencomputer/<profile>/config.yaml`:

```yaml
hooks:
  PreToolUse:
    - matcher: "Edit|Write|MultiEdit"
      command: "python3 /usr/local/bin/oc-linter"
      timeout_seconds: 10
  SessionStart:
    - command: "bash ~/.opencomputer/hooks/bootstrap.sh"
```

Each entry becomes a `HookCommandConfig` (see
`opencomputer/agent/config.py`). The factory in
`opencomputer/hooks/shell_handlers.py::make_shell_hook_handler` wraps it
in an async handler that enforces a hard `timeout_seconds` budget.

### Exit-code contract

Matches Claude Code:
- `0` → `HookDecision(decision="pass")` (tool runs).
- `2` → `HookDecision(decision="block", reason=<stderr>)` — stderr is
  the reason fed back to the model.
- Any other exit / crash / timeout → logged at WARNING, returns `pass`
  (fail-open — a broken hook must never brick the CLI).

### Env vars + stdin payload

The command runs under `asyncio.create_subprocess_exec` (no shell) with
these env vars set on top of the parent env:

- `OPENCOMPUTER_EVENT` — event name (`"PreToolUse"`, etc.).
- `OPENCOMPUTER_TOOL_NAME` — tool name for Pre/PostToolUse, `""` else.
- `OPENCOMPUTER_SESSION_ID` — current session id.
- `OPENCOMPUTER_PROFILE_HOME` — active profile home dir.
- `CLAUDE_PLUGIN_ROOT` — aliased to the profile home so Claude-Code
  hook scripts drop in unchanged.

Stdin receives a JSON blob with `hook_event_name`, `session_id`,
`tool_name`, `tool_input`, `tool_result`, `message`, `runtime` (keys
absent rather than null when the context field is `None`).

## See also

- `references/event-catalog.md` — per-event semantics and ctx fields.
- `references/shell-hook-patterns.md` — three copy-paste recipes.
- `examples/python-hook-plugin.md` — full plugin shipping one hook.
- `examples/settings-yaml-hook.md` — same behavior via `config.yaml`.
- `opencomputer-tool-development` skill — mode gates belong in hooks,
  not inside tools.
- `extensions/coding-harness/hooks/` — six production hooks to copy.
