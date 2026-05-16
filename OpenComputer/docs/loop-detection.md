# Loop detection

An agent can fall into a degenerate loop — calling the same tool with the
same arguments over and over, or emitting the same message repeatedly —
making no progress while burning tokens. OpenComputer's **repetition
detector** watches for this and steps in.

The detector itself (`opencomputer/agent/loop_safety.py::LoopDetector`) is
the OpenClaw "1.C" anti-loop port and predates this milestone. Milestone 1
of the Hermes + OpenClaw parity work added four things on top: an
**observe / enforce mode switch**, **config-tunable thresholds**, an
**audit log** of detected loops, and a per-tool **`loop_safe` opt-out`**.

## Observe vs enforce mode

> **The default is `observe`.** Out of the box, loop detection **logs**
> trips to the audit log but does **not** halt the agent. It only halts
> once you opt into `enforce` mode.

This is a deliberate soft-launch (PART-2 §4.1 of the parity plan): the
3-in-8 threshold is design intuition, not measured. Running in observe
mode first produces real calibration data — every trip is logged to
`audit.db` — so you can confirm the detector isn't false-positiving on a
legitimate polling workload before you let it stop runs.

| Mode | On a detected loop |
|---|---|
| `observe` (default) | Writes one `tool_loop_trips` row per trip episode. The agent keeps running. |
| `enforce` | Writes the trip row **and** halts: the loop raises `LoopAbortError`, surfaces a clean `Agent loop stopped: …` message, and the `ConversationResult.stop_reason` is `StopReason.TOOL_LOOP`. |

## How it works

The detector keeps, per `(session_id, delegation_depth)` frame, a sliding
window of the most recent tool calls and assistant messages:

- Each tool call is fingerprinted as `(tool_name, hash(arguments))`.
- When the same fingerprint recurs `max_tool_repeats` times within the
  window, the frame is **flagged** — a `<system-reminder>` is appended to
  the conversation nudging the model to change approach. (The nudge is
  appended in both modes.)
- After `max_consecutive_flags` consecutive flagged records, the detector
  **must-stops**. In `enforce` mode the agent loop raises `LoopAbortError`
  and surfaces a single clean "Agent loop stopped" message instead of
  spinning to the iteration budget; in `observe` mode it only logs the
  trip and continues.
- Assistant-text repetition is tracked the same way on a separate window
  (`max_text_repeats`).

A unique tool call (or message) resets the consecutive-flag counter — the
ramp only fires on *sustained* repetition.

With the **shipped defaults** the detector halts (in `enforce` mode) on a
**3rd identical tool-call within an 8-call window**: `window_size: 8`,
`max_tool_repeats: 3` (the 3rd repeat flags), `max_consecutive_flags: 1`
(the first flag must-stops).

## Configuration

Mode and thresholds live under `loop.repetition` in `config.yaml`. The
block below shows the **shipped defaults** — a config that omits the block
behaves identically:

```yaml
loop:
  repetition:
    mode: observe             # observe (log only, default) | enforce (log + halt)
    max_tool_repeats: 3       # identical (tool, args) calls that flag a loop
    max_text_repeats: 2       # identical assistant messages that flag a loop
    window_size: 8            # how many recent calls / messages are tracked
    max_consecutive_flags: 1  # consecutive flags before the detector must-stops
```

Raise the threshold values to make detection more permissive; lower them
to make it more aggressive.

### Flipping to enforce

Once observe-mode logs confirm the detector only trips on genuine loops,
switch it on by setting `mode` in the active profile's `config.yaml`:

```yaml
loop:
  repetition:
    mode: enforce
```

From then on, a detected loop halts the agent instead of merely being
logged. The thresholds are unchanged — only the consequence of a trip is.

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

Every loop trip is recorded to the `tool_loop_trips` table of the profile's
`audit.db` — in **both** modes. (In observe mode the trip log is the *only*
effect; in enforce mode it accompanies the halt.) Exactly one row is
written per trip *episode*: while the agent keeps repeating, the trip stays
latched and is not re-logged until a unique call resets the streak.

| Column | Meaning |
|---|---|
| `ts` | Unix timestamp of the trip |
| `session_id` | session the loop occurred in |
| `depth` | delegation depth (0 = top-level agent, >0 = a subagent) |
| `kind` | `tool` or `text` — which repetition window tripped |
| `detail` | the human-readable warning message |

`tool_loop_trips` is part of the managed schema (created by the `audit.db`
migration chain in `opencomputer/agent/state.py`), not a table created
ad-hoc on first write — so it carries an index and migrates cleanly.

The write is best-effort: a database failure is logged at WARNING and
swallowed — loop-detection telemetry must never wedge the agent loop. The
log exists so detection thresholds can be tuned against real-world data —
which is exactly why observe mode logs without halting.
