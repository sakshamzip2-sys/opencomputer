# Screen-Awareness

Event-driven screen OCR for OpenComputer. **Default OFF — opt-in only.**

## What this does

When enabled and consent granted, the sensor captures + OCRs the primary
screen at three event triggers:

| Trigger | When |
|---|---|
| `BEFORE_MESSAGE_WRITE` (filter: role=user) | User submits a message |
| `PRE_TOOL_USE` (filter: GUI-mutating tools only) | Agent about to invoke a screen-mutating tool |
| `POST_TOOL_USE` (filter: GUI-mutating tools only) | The tool returned |

Captures land in a per-session ring buffer (last 20). A
`DynamicInjectionProvider` reads the latest entry and emits
`<screen_context>...</screen_context>` into the next agent step.

## What this does NOT do

| Thing | Status |
|---|---|
| Continuous polling daemon | ❌ event-driven only |
| Persist image bytes | ❌ OCR text only by default |
| Send any data to a network destination | ❌ AST egress guard |
| Capture when sensitive app is in foreground | ❌ filter to no-capture |
| Capture when screen is locked / asleep | ❌ skip |
| Capture without F1 consent grant | ❌ EXPLICIT tier required |

## Enable

Two gates are required:

```python
# 1. Per-profile state file (mirrors ambient-sensors pattern)
from extensions.screen_awareness.state import ScreenAwarenessState, save_state
from pathlib import Path
save_state(
    Path("~/.opencomputer/<profile>").expanduser(),
    ScreenAwarenessState(enabled=True),
)
```

```bash
# 2. F1 capability grant
oc consent grant introspection.ambient_screen --tier explicit
```

`oc doctor` will flag missing macOS Screen Recording permission.
