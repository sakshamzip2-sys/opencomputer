# PR-A: Steer Replan + Voice Wake + ACP Expansion — Design Spec

**Date:** 2026-05-07
**Branch:** `feat/pr-a-steer-wake-acp-2026-05-07`
**Driver docs:** `OpenComputer/docs/refs/openclaw/2026-05-06-deep-comparison.md` (S1, S3, B1) + `OpenComputer/docs/refs/hermes-agent/2026-05-06-deep-comparison.md` (cross-references).

## Why this scope

Three Tier-S/A features that compose architecturally and ship as one PR:

1. **Steer Replan-with-Context** — upgrade `/steer` from between-turn nudge to mid-tool-call cooperative cancel + buffer-drain + replan injection.
2. **Voice Wake** — always-on wake-word detection (openWakeWord) hands off to existing voice-mode loop.
3. **ACP Expansion** — `setSessionPermissions` + tier-aware `requestPermission` + lifecycle hooks bridged through ACP method calls.

These share infrastructure: (a) the same `asyncio.Event` cancellation primitive (steer + ACP cancel), (b) the same RuntimeContext typed extensions (ACP denylist + steer state), (c) the same hook-event taxonomy (ACP lifecycle bridges existing SESSION_START / SESSION_END / PRE_TOOL_USE).

## Scope (this PR)

### Feature 1 — Steer Replan-with-Context

**Module changes:**

#### 1.1 `opencomputer/agent/steer.py` (extended)

```python
class SteerRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}  # NEW
        self._lock = threading.Lock()

    def submit(self, session_id: str, nudge: str) -> None:
        # Existing latest-wins semantics + emit cancel event
        ...
        # NEW: signal cancel to in-flight tool dispatch
        event = self._cancel_events.get(session_id)
        if event is not None and not event.is_set():
            event.set()

    def cancel_event(self, session_id: str) -> asyncio.Event:
        """Return (lazily-creating) the per-session cancel event."""
        ...

    def reset_cancel(self, session_id: str) -> None:
        """Clear the cancel event after dispatch handles it."""
        ...
```

`format_nudge_message` updated to mark the post-interrupt case:

```python
def format_nudge_message(nudge: str, *, was_interrupted: bool = False) -> str:
    prefix = "<USER-INTERRUPT>" if was_interrupted else "<USER-NUDGE>"
    return (
        f"{prefix}: {nudge}\n"
        "(latest-wins; previous nudges discarded if any.)"
    )
```

#### 1.2 `opencomputer/agent/loop.py` (~30 LOC change around line 3785)

Wrap the existing `asyncio.gather(*(_run_one(c) for c in calls))` with a cancel-aware variant:

```python
# CURRENT (line 3785):
results = await asyncio.gather(*(_run_one(c) for c in calls))

# REFINED:
cancel_event = _steer_registry.cancel_event(session_id)
if cancel_event.is_set():
    cancel_event.clear()  # stale event; clear and proceed normally

tasks = [asyncio.create_task(_run_one(c)) for c in calls]
done, pending = await asyncio.wait(
    [*tasks, asyncio.create_task(cancel_event.wait())],
    return_when=asyncio.FIRST_COMPLETED,
)
if cancel_event.is_set():
    # Steer fired mid-dispatch — cancel pending tools cooperatively
    for t in pending:
        t.cancel()
    # Wait briefly for graceful cancellation; force-collect after timeout
    await asyncio.wait(pending, timeout=2.0)
    results = []
    for c, t in zip(calls, tasks):
        if t.done() and not t.cancelled():
            try:
                results.append(t.result())
            except Exception:
                results.append(_make_cancelled_result(c))
        else:
            results.append(_make_cancelled_result(c))
    cancel_event.clear()
else:
    results = [t.result() for t in tasks]
```

`_make_cancelled_result(call)` constructs a `ToolResult` with content:
- For `Bash`: `"<INTERRUPTED-BY-STEER> partial stdout: {captured_stdout_so_far}\n(remaining work cancelled by user steer)"`
- For all other tools: `"<INTERRUPTED-BY-STEER> tool '{name}' cancelled by user steer; no partial output captured"`

The between-turn consume (existing at line 1410-1442) gets a single-line update: `was_interrupted=True` flag passed to `format_nudge_message` if the cancel event was set during this iteration.

#### 1.3 `opencomputer/gateway/dispatch.py` (~80 LOC: per-session message buffer)

```python
class _SteerBuffer:
    """Per-session message buffer for steer drain.

    When a message arrives during in-flight tool dispatch, it's appended
    to this buffer instead of triggering a new agent run. On cancel,
    buffered messages are concatenated and become part of the steer
    nudge. Cap=5; drop-oldest; logged.
    """
    MAX = 5

    def append(self, session_id: str, text: str) -> int:
        """Returns the count of dropped older messages (0 if no drops)."""
        ...

    def drain(self, session_id: str) -> str:
        """Return concatenated buffer (separator '\n---\n'); clear."""
        ...
```

Dispatcher integration: when an inbound message lands and the session is mid-dispatch (detected via `_steer_registry.has_pending(sid)` OR an in-flight `asyncio.Lock` per-session check), the message goes to `_SteerBuffer` instead of triggering a new run. On cancel-event consumption in the loop, the drained buffer is concatenated to the steer text.

#### 1.4 `opencomputer/cli_ui/slash_handlers.py` (~5 LOC ack update)

```python
# Current ack (line 458):
return SlashResult.message(
    f"[green]steered[/green] — next turn will use: [dim]{preview}[/dim]"
)

# Refined ack:
status = "interrupted" if was_mid_dispatch else "steered"
return SlashResult.message(
    f"[green]{status}[/green] — next turn will use: [dim]{preview}[/dim]"
)
```

`was_mid_dispatch` is True if `_steer_registry.cancel_event(sid).is_set()` immediately after `submit()` returns (event-set tells us a dispatch is listening).

#### Cancellation scope — honest documentation

> **Sync-tool cancel:** tools backed by blocking syscalls (Read, Glob, Grep) finish their current syscall before honoring CancelledError; cancel only delivers at the next `await` boundary. For these tools, cancellation behaves as "skip the next call in the batch" rather than "interrupt the in-progress call."
>
> **Async-yielding tools** (Bash, WebFetch, WebSearch, browser-control, MCP) cancel at the next `await` checkpoint, typically <100ms.
>
> **Bash partial-stdout capture:** the existing Bash tool already accumulates stdout in a buffer; on CancelledError, the captured prefix is returned as part of the cancelled ToolResult. No other tool captures partial output.

#### Tests — `tests/agent/test_steer_replan.py`

- `test_steer_during_async_tool_cancels_cleanly` — Bash sleep 5s, steer at 100ms, asserts cancel within 200ms + partial-stdout in result.
- `test_steer_during_sync_tool_runs_to_completion` — Read on a 50KB file, steer mid-call, asserts read completes + cancel honored at next await.
- `test_steer_buffer_drain_concatenates` — submit steer text, then 2 buffered messages, assert next-turn nudge contains all 3 with `\n---\n` separator.
- `test_steer_buffer_drops_oldest_at_cap` — 6 buffered messages, assert oldest dropped + log emitted.
- `test_latest_wins_still_holds` — 2 steers in 50ms, assert second wins, first discarded.
- `test_no_pending_tools_fast_path` — steer fires when no tools running, assert it's handled by between-turn consume (existing behavior).
- `test_cancel_event_doesnt_kill_outer_loop` — verifies `return_exceptions`-equivalent semantics: outer turn loop survives.

---

### Feature 2 — Voice Wake

#### 2.1 `opencomputer/voice/wake_word.py` (new module, ~200 LOC)

```python
"""Wake-word detection for hands-free OC activation.

Uses openWakeWord (Apache 2.0, ONNX, CPU). Default model: hey_jarvis
(bundled with openwakeword). Always-on capture loop runs in a dedicated
thread; on detection (score >= threshold), a callback fires that hands
off to the voice-mode loop.

Default OFF — must be invoked via `oc voice wake`.
"""

class WakeWordError(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class WakeDetection:
    word: str
    score: float
    timestamp: float

class WakeWordDetector:
    def __init__(
        self,
        *,
        word: str = "hey_jarvis",
        threshold: float = 0.5,
        model_path: Path | None = None,
        on_detect: Callable[[WakeDetection], Awaitable[None]] | None = None,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def __aenter__(self): ...
    async def __aexit__(self, *exc): ...

    @property
    def state(self) -> Literal["IDLE", "DETECTED", "SPEAKING"]: ...
```

State machine: `IDLE → DETECTED → SPEAKING → IDLE`. Wake re-engages on transition to IDLE. Mic singleton enforced via PID-file at `<profile_home>/voice_wake.pid`.

Graceful degrade if `openwakeword` is not installed:

```python
try:
    import openwakeword
except ImportError:
    raise WakeWordError(
        "openwakeword not installed; "
        "install with `pip install opencomputer[wake]`"
    )
```

#### 2.2 `opencomputer/cli_voice.py` — new `wake` subcommand

```python
@voice_app.command("wake")
def voice_wake(
    word: str = typer.Option("hey_jarvis", "--word", help="Wake-word model"),
    threshold: float = typer.Option(0.5, "--threshold", help="Detection threshold (0.0-1.0)"),
    model_path: Path | None = typer.Option(None, "--model", help="Custom ONNX model path"),
) -> None:
    """Listen for a wake-word and hand off to the voice loop on detection."""
    ...
```

CLI prints: `[listening for 'hey_jarvis'... press Ctrl+C to stop]` + a `[heard]` indicator on each detection event. Exits with code 4 if openwakeword is not installed (matches `oc doctor` convention).

#### 2.3 `opencomputer/doctor.py` — new `wake` health check

`oc doctor wake` initializes the detector once, runs a single 80ms PCM zero-buffer through it, and reports OK/FAIL. Used to validate ONNX runtime works on this platform.

#### 2.4 `pyproject.toml` — new `[wake]` extra

```toml
[project.optional-dependencies]
wake = [
    "openwakeword>=0.6.0",
    "onnxruntime>=1.17",  # Apple Silicon stable
    "pyaudio>=0.2.13",  # already in voice-mode but listed here for clarity
]
```

#### Tests — `tests/voice/test_wake_word.py`

- `test_detector_init_and_close` — clean lifecycle.
- `test_detection_callback_fires_above_threshold` — mock predict scores, assert callback at threshold cross.
- `test_no_callback_below_threshold` — sub-threshold scores never call callback.
- `test_state_transitions` — `IDLE → DETECTED → SPEAKING → IDLE` cycle.
- `test_singleton_pid_file_blocks_second_instance` — second start fails fast.
- `test_graceful_degrade_when_openwakeword_missing` — patched ImportError, asserts CLI exit code 4 + helpful message.

---

### Feature 3 — ACP Expansion

#### 3.1 `opencomputer/acp/server.py` — new method handler

```python
self._handlers["setSessionPermissions"] = self._handle_set_session_permissions
```

`setSessionPermissions(sessionId, allowedTools?, deniedTools?)` updates the per-session denylist on `ACPSession`. Race-safe: applies to *future* tool dispatches only; in-flight tools complete unaffected.

```python
async def _handle_set_session_permissions(self, params: dict[str, Any]) -> dict[str, Any]:
    session_id = params.get("sessionId")
    if session_id not in self._sessions:
        raise ACPError(ERR_SESSION_NOT_FOUND, f"unknown session: {session_id}")
    session = self._sessions[session_id]
    allowed = frozenset(params.get("allowedTools") or [])
    denied = frozenset(params.get("deniedTools") or [])
    session.update_permissions(allowed=allowed, denied=denied)
    return {"sessionId": session_id, "allowedTools": list(allowed), "deniedTools": list(denied)}
```

#### 3.2 `opencomputer/acp/session.py` — per-session permission storage

```python
@dataclass
class ACPSession:
    session_id: str
    ...
    allowed_tools: frozenset[str] = frozenset()
    denied_tools: frozenset[str] = frozenset()

    def update_permissions(
        self,
        *,
        allowed: frozenset[str] | None = None,
        denied: frozenset[str] | None = None,
    ) -> None: ...
```

#### 3.3 `plugin_sdk/runtime_context.py` — typed denylist field

```python
@dataclass
class RuntimeContext:
    ...
    acp_denied_tools: frozenset[str] = field(default_factory=frozenset)
```

#### 3.4 `opencomputer/agent/loop.py` — consult denylist in tool dispatch

In `_dispatch_tool_calls`, before invoking each tool:

```python
if c.name in self._runtime.acp_denied_tools:
    results.append(ToolResult(
        call_id=c.id,
        content=f"<DENIED-BY-ACP> tool '{c.name}' is denied for this ACP session.",
        is_error=True,
    ))
    continue
```

#### 3.5 `opencomputer/acp/permissions.py` — tier parameter

```python
def make_approval_callback(
    session_id: str,
    gate: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    *,
    default_tier: str = "PER_ACTION",  # NEW
) -> Callable[..., str]: ...
```

`default_tier` is used when the IDE doesn't specify one in `requestPermission`. Acceptable values mirror `ConsentTier` enum: `PER_ACTION`, `SESSION`, `ALWAYS`.

#### 3.6 ACP lifecycle hook bridging — in `acp/server.py`

On `newSession`: fire `HookEvent.SESSION_START` (existing).
On session disconnect / EOF: fire `HookEvent.SESSION_END` (existing).
On `prompt` start: fire `HookEvent.USER_PROMPT_SUBMIT` (existing).
On `prompt` complete: no new event needed; existing `Stop` hook fires from the loop.

(No new hook events are added — we just bridge ACP method calls to existing events.)

#### Tests — `tests/acp/test_acp_expansion.py`

- `test_set_session_permissions_round_trip` — JSON-RPC roundtrip, assert session denylist updated.
- `test_set_session_permissions_unknown_session_returns_error` — ERR_SESSION_NOT_FOUND.
- `test_denied_tool_returns_denied_result` — agent loop with ACP denylist set, tool call returns `<DENIED-BY-ACP>` error.
- `test_in_flight_tool_completes_despite_new_denial` — denylist updated mid-dispatch, in-flight tool finishes normally.
- `test_request_permission_with_tier_param` — IDE passes `tier=SESSION`, gate sees correct tier.
- `test_acp_lifecycle_fires_session_hooks` — newSession fires SESSION_START; disconnect fires SESSION_END.

---

## Ship-with-callsite checklist (per memory rule)

| Module | Callsite |
|---|---|
| `steer.py::cancel_event` | `loop.py::_dispatch_tool_calls` (Feature 1.2) |
| `steer.py::SteerBuffer` (new) | `gateway/dispatch.py` per-session ingress (Feature 1.3) |
| `format_nudge_message(was_interrupted=...)` | `loop.py::_run_one_step` between-turn consume (Feature 1.2) |
| `wake_word.py::WakeWordDetector` | `cli_voice.py::voice_wake` command (Feature 2.2) |
| `cli_voice.py::voice_wake` | wired into existing `voice_app` Typer (Feature 2.2) |
| `doctor.py::wake_check` | wired into `oc doctor` registry (Feature 2.3) |
| `acp/server.py::_handle_set_session_permissions` | self-registered in `__init__` handler dict (Feature 3.1) |
| `RuntimeContext.acp_denied_tools` | consulted in `loop.py::_dispatch_tool_calls` (Feature 3.4) |

## Risk register (post-audit)

15 audit findings, 11 produced design changes, 2 documented honestly, 2 accepted as risk:

| # | Finding | Disposition |
|---|---|---|
| A1 | `asyncio.gather` propagates CancelledError up | Use `asyncio.wait(..., return_when=FIRST_COMPLETED)` instead |
| A2 | Sync tools can't cancel mid-syscall | Documented; cancellation only at await boundaries for sync tools |
| A3 | onnxruntime aarch64 instability | Pin >=1.17; add `oc doctor wake` health check |
| A4 | `runtime.custom` denylist gets stomped | Promoted to typed `RuntimeContext.acp_denied_tools` field |
| B1 | Gateway lock + steer cancel deadlock | Verified safe — locks guard ingest, not in-flight tools |
| B2 | Multi-platform latest-wins non-determinism | **Accepted as risk** — latest-wins by lock-arrival is acceptable UX |
| B3 | Wake state machine | Documented: `IDLE → DETECTED → SPEAKING → IDLE` |
| B4 | ACP cancel ≠ Steer cancel intent | Distinct post-cancel behavior; shared event mechanism only |
| C1 | Task.cancel() vs explicit cancel_token | Stick with Task.cancel(); document tool author contract |
| C2 | openWakeWord vs Vosk | **Accepted as risk** — openWakeWord wins on latency |
| D1 | Steer ack copy on interrupt | Updated to "interrupted" status |
| D2 | Wake CLI feedback | "[listening...]" + "[heard]" indicators |
| D3 | ACP permission tier UX | Documented; tier values surface via initialize response |
| F1 | Buffer-drain real scope | Bumped to ~80 LOC; explicit overflow policy |
| F2 | Total session estimate | 6-8 hours focused; explicit exit ramps |
| G1 | ACP method name precedent | No precedent at PROTOCOL_VERSION 0.9.0; we define it |
| G2 | Wake CLI flag stability | Locked as v1; documented |
| H1 | Wake mic singleton | PID-file lock |
| H2 | openWakeWord not installed | Graceful CLI exit code 4 |
| H3 | Steer cancel during streaming LLM | Cancel watched only during tool dispatch, not streaming |
| H4 | ACP denylist race | Future dispatches only; in-flight tools unaffected |
| I1 | `getServerStatus` YAGNI | **Dropped from v1** |
| I2 | Wake `--model PATH` | Kept (real power-user need) |
| I3 | Partial-output for non-Bash tools | **Dropped** — only Bash captures partial stdout |

## Out of scope (deferred — explicit)

- **Lobster (typed workflows with resumable approvals)** — scope = 2-3 days dedicated PR; defer until concrete multi-day workflow use case.
- **Native iOS app** — separate PR-B; multi-week stream.
- **Voice Wake custom-word training** — defer; openWakeWord supports custom words but training is its own UX.
- **`getServerStatus` ACP method** — YAGNI; no real IDE caller.
- **MCP Porter / mcporter bridge** — we speak modern MCP spec directly.
- **Live Canvas / A2UI** — no UI surface to render into.
- **Dreaming** — Honcho already covers this.
- **More channel adapters** — 13 is enough; defer until demand.

## Self-review (post-write)

- [x] No "TBD" or vague placeholders.
- [x] Each module has a documented callsite.
- [x] Tests scoped per feature; total ~20 new tests.
- [x] Risk register integrates audit findings.
- [x] Cancellation scope honestly documented (sync vs async tools).
- [x] All optional deps gated behind extras (`[wake]`).
- [x] Schema migration: none needed (no new SQLite tables).
- [x] Honest scope — 6-8 hours focused, not "1 day."
- [x] Composability: shared cancel event, distinct post-cancel behavior.
- [x] Default OFF for wake; opt-in for ACP gating; steer-replan on by default but only fires when event set.
