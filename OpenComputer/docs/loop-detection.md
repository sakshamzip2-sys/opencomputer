# Loop detection

An agent can fall into a degenerate loop — calling the same tool with the
same arguments over and over, or emitting the same message repeatedly —
making no progress while burning tokens. OpenComputer's **repetition
detector** watches for this and steps in.

The detector itself (`opencomputer/agent/loop_safety.py::LoopDetector`) is
the OpenClaw "1.C" anti-loop port and predates this milestone. Milestone 1
of the Hermes + OpenClaw parity work added three things on top: **config-
tunable thresholds**, an **audit log** of detected loops, and a per-tool
**`loop_safe` opt-out**.

## How it works

The detector keeps, per `(session_id, delegation_depth)` frame, a sliding
window of the most recent tool calls and assistant messages:

- Each tool call is fingerprinted as `(tool_name, hash(arguments))`.
- When the same fingerprint recurs `max_tool_repeats` times within the
  window, the frame is **flagged** — a `<system-reminder>` is appended to
  the conversation nudging the model to change approach.
- After `max_consecutive_flags` consecutive flagged records, the detector
  **must-stops**: the agent loop raises `LoopAbortError` and surfaces a
  single clean "Agent loop stopped" message instead of spinning to the
  iteration budget.
- Assistant-text repetition is tracked the same way on a separate window
  (`max_text_repeats`).

A unique tool call (or message) resets the consecutive-flag counter — the
ramp only fires on *sustained* repetition.

## Configuration

Thresholds live under `loop.repetition` in `config.yaml`. The defaults match
the detector's historical hardcoded values, so an existing config that omits
the block behaves exactly as before:

```yaml
loop:
  repetition:
    max_tool_repeats: 3       # identical (tool, args) calls that flag a loop
    max_text_repeats: 2       # identical assistant messages that flag a loop
    window_size: 10           # how many recent calls / messages are tracked
    max_consecutive_flags: 2  # consecutive flags before the loop hard-stops
```

Raise the values to make detection more permissive; lower them to make it
more aggressive.

## Tools that legitimately repeat — `loop_safe`

Some tools are *supposed* to be called repeatedly with identical arguments —
a build-status poller, a sleep-then-retry tool. Such a tool would otherwise
trip the detector. A tool opts out by setting the `loop_safe` class
attribute:

```python
from plugin_sdk.tool_contract import BaseTool

class PollBuildStatusTool(BaseTool):
    loop_safe = True   # exempt from repetition detection
    ...
```

`loop_safe` defaults to `False`. A `loop_safe` tool's calls are skipped by
the detector entirely — they are never recorded into the window.

## Audit log

Every loop abort is recorded to the `tool_loop_trips` table of the profile's
`audit.db`:

| Column | Meaning |
|---|---|
| `ts` | Unix timestamp of the abort |
| `session_id` | session the loop occurred in |
| `depth` | delegation depth (0 = top-level agent, >0 = a subagent) |
| `kind` | `tool` or `text` — which repetition window tripped |
| `detail` | the human-readable warning message |

The write is best-effort: a database failure is logged at WARNING and
swallowed — loop-detection telemetry must never wedge the agent loop. The
log exists so detection thresholds can be tuned against real-world data.
