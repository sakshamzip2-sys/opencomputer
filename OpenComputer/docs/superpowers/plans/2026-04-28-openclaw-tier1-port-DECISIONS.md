# OpenClaw Tier 1 Port — Phase 0 Pre-flight DECISIONS

**Date:** 2026-04-28
**Branch:** `feat/openclaw-tier1-port`
**Scope:** 10 verification tasks from AMENDMENTS.md, run grep-verified against `main`@`04c74b72` (latest origin/main).

**Headline:** **Audit was correct on every claim.** Plus 1 new defect surfaced (D-NEW-1).

---

## Task 0.1 — PreLLMCall hook: is `modified_message` honored?

**Evidence:**
- `opencomputer/agent/loop.py:1825-1841`: PRE_LLM_CALL emit is `_hook_engine.fire_and_forget(...)`. Comment: "fire-and-forget so handlers can read the message list and model name before we hit the wire. Hook returns are intentionally ignored: this is an observation event, not a gate."
- `opencomputer/agent/loop.py:560-564` (BEFORE_PROMPT_BUILD, same pattern): "modified_message support for appending a system reminder is documented in the SDK; the loop does NOT consume it today (template author owns the body). A future PR can splice modified_message into the rendered snapshot per the plan."

**Verdict:** Outcome B — modified_message NOT injected. Audit C2 confirmed.

**Implication for plan:** Sub-project 1.B-alt's pivot to `RecallTool-prepend` (core, not plugin) is validated as the correct direction. Avoiding hook engine surgery means we ship in S effort instead of L.

---

## Task 0.2 — Channel adapter streaming surface

**Evidence:**
- `opencomputer/gateway/dispatch.py::_do_dispatch` (lines 346-456) calls `loop.run_conversation(...)` and awaits the complete result; line 431 returns `result.final_message.content or None` — a single complete string, NOT a stream of deltas.
- Adapter usage in dispatch is: `on_processing_start` (line 378), `on_processing_complete` (line 453), `send_typing` (line 550). NO `edit_message`, NO delta-chunking, NO stream callback.

**Verdict:** Audit C7 partially correct (re: send vs edit_message for streaming) — but DEEPER: dispatch has no streaming surface at all. **NEW DEFECT D-NEW-1.**

**Implication for plan:** Sub-project 1.A's wrapper cannot be at dispatch-layer. The correct insertion point is `loop._call_provider`'s `stream_callback` parameter (`loop.py:1843`). Plan AMENDMENTS need updating before any 1.A code is written.

---

## Task 0.3 — SessionDB read API

**Evidence:**
- `opencomputer/agent/state.py:480`: `def create_session(self, session_id: str, platform: str = "cli", model: str = "", title: str = "") -> None`
- `opencomputer/agent/state.py:540`: `def get_session(self, session_id: str) -> dict[str, Any] | None`
- `opencomputer/agent/state.py:545`: `def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]`
- `opencomputer/agent/state.py:811`: `def get_messages(self, session_id: str) -> list[Message]:` — **no `limit` parameter**
- `get_session_summary` — searched entire state.py: **does not exist**.

**Verdict:** Audit C5 fully correct.

**Implication for plan:** SessionsHistory slices client-side (`messages[-limit:]`). SessionsStatus uses `get_session()` (returns row dict with title, message_count, input_tokens, output_tokens, vibe).

---

## Task 0.4 — Outgoing queue API

**Evidence:**
- `opencomputer/gateway/outgoing_queue.py:142-172`: `def enqueue(self, *, platform: str, chat_id: str, body: str, attachments: list[str] | None = None, metadata: dict[str, Any] | None = None) -> OutgoingMessage`
- File-wide grep: `put_send` absent, `put_session_send` absent, `peer` absent, `message` absent.

**Verdict:** Audit C1 fully correct.

**Implication for plan:** Sub-project 1.H is dropped (PR #222 collision). Sub-project 1.F's SessionsSend is deferred. So this defect is sidestepped.

---

## Task 0.5 — Credential pool

**Evidence:**
- Module location: `opencomputer/agent/credential_pool.py` — NOT `plugin_sdk/`.
- `opencomputer/agent/credential_pool.py:23`: `ROTATE_COOLDOWN_SECONDS: float = 60.0`
- `opencomputer/agent/credential_pool.py:54`: `def __init__(self, *, keys: Sequence[str], max_rotation_attempts: int = 3, rotate_cooldown_seconds: float = ROTATE_COOLDOWN_SECONDS)`
- `opencomputer/agent/credential_pool.py:89`: `async def report_auth_failure(self, key: str, *, reason: str = "401") -> None`
- Public surface: `acquire() -> str`, `report_auth_failure(key, *, reason)`, `with_retry(fn, *, is_auth_failure)`, `stats() -> dict`. No `cooldown()`, no `available_profiles()`, no profile concept.

**Verdict:** Audit C3 fully correct.

**Implication for plan:** Sub-project 1.E is correctly reframed in AMENDMENTS as "thin config + monitor surface" over the existing key-based quarantine. No new abstraction. `auth_monitor_loop` calls `provider.ping(key)` and on failure calls existing `report_auth_failure(key, reason="health_check_failed")`.

---

## Task 0.6 — F1 ConsentGate `capability_claims`

**Evidence (canonical pattern across 7 tools):**
- `opencomputer/tools/point_click.py:46`: `capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (...)`
- Same pattern at `voice_transcribe.py:23`, `spawn_detached_task.py:45`, `voice_synthesize.py:26`, `applescript_run.py:53`, `python_exec.py:49`, `cron_tool.py:60`.

**Verdict:** Required attribute is `capability_claims: ClassVar[tuple[CapabilityClaim, ...]]` — class-level tuple of `CapabilityClaim` instances.

**Implication for plan:** New tools in Sub-projects F/G must construct `CapabilityClaim(...)` instances, not raw strings. Engineer must check `plugin_sdk/consent.py` for the `CapabilityClaim` constructor signature before writing tools.

---

## Task 0.7 — PluginAPI surface

**Evidence (`opencomputer/plugins/loader.py:589-868`):**

| Attribute | Type | Line |
|---|---|---|
| `tools` | tool registry | 605 |
| `hooks` | hook engine | 606 |
| `providers` | `dict[str, Any]` | 607 |
| `channels` | `dict[str, Any]` | 608 |
| `injection` | engine \| None | 609 |
| `doctor_contributions` | `list[Any]` | 612 |
| `memory_provider` | `Any` (= None for built-in only) | 615 |
| `session_db_path` | `Path \| None` | 621 |
| `slash_commands` | `dict[str, Any]` (plain dict) | 625 |
| `activation_source` | property → `PluginActivationSource` | 666 |
| `request_context` | property → `RequestContext \| None` | 680 |
| `outgoing_queue` | property → `Any \| None` | 699 |

**MISSING (used in plan but absent in reality):**
- `provider` (real: `providers[name]`)
- `memory` (real: `memory_provider`)
- `config`
- `list_enabled_channels()`

**`slash_commands` is a `dict[str, Any]`, NOT callable.** Plan code `api.slash_commands().add(...)` raises TypeError.

**Verdict:** Audit H2 fully correct.

**Implication for plan:** Sub-project 1.B-alt as core (not plugin) sidesteps this. Sub-project 1.G ClarifyTool registers via `registry.register(...)` in `cli.py`, not via PluginAPI. No plan code currently needs PluginAPI access for any new pick.

---

## Task 0.8 — DelegateTool scoping

**Evidence:**
- `opencomputer/tools/delegate.py:323`: `subagent_loop = self._factory()` — fresh AgentLoop instance per subagent.
- Class docstring at line 54: "Inject a callable that returns a fresh AgentLoop."
- Child has own config (line 332-338 via `dataclasses.replace`), own allowed_tools (line 342), own runtime (line 317-320, depth incremented).

**Verdict:** Confirmed — fresh AgentLoop per subagent. Audit H5 correct.

**Implication for plan:** LoopDetector must scope by `(session_id, delegation_depth)`. Sub-project 1.C must wire detector instantiation per AgentLoop instance + use depth from runtime. Effort estimate stays S+ (~1.5d).

---

## Task 0.9 — ToolCall / ToolResult shape

**Evidence:**
- `plugin_sdk/core.py:62-68`: `@dataclass(frozen=True, slots=True) class ToolCall: id: str; name: str; arguments: dict[str, Any]`
- `plugin_sdk/core.py:71-77`: `@dataclass(frozen=True, slots=True) class ToolResult: tool_call_id: str; content: str; is_error: bool = False`

**Verdict:** Audit C4 fully correct.

**Implication for plan:** Global rename in F + G:
- `call.input[...]` → `call.arguments[...]`
- `ToolResult(output=str(X), is_error=Y)` → `ToolResult(tool_call_id=call.id, content=str(X), is_error=Y)`
- Test fixtures: `ToolCall(id="t-1", name=..., arguments=...)`

---

## Task 0.10 — File-collision check

**Evidence (filesystem `ls`):**

| File | Status |
|---|---|
| `plugin_sdk/streaming/__init__.py` | MISSING |
| `plugin_sdk/streaming/block_chunker.py` | MISSING |
| `opencomputer/agent/active_memory.py` | MISSING |
| `opencomputer/agent/loop_safety.py` | MISSING |
| `opencomputer/gateway/replay_sanitizer.py` | MISSING |
| `opencomputer/cli_auth.py` | MISSING |
| `opencomputer/tools/sessions.py` | MISSING |
| `opencomputer/tools/clarify.py` | MISSING |

**Verdict:** All 8 planned files absent on `feat/openclaw-tier1-port`. No file-collision risk on this branch for these picks. PR #222's `send_message.py` collision (audit C6) remains the reason 1.H is dropped.

---

## NEW defect — must update AMENDMENTS

### D-NEW-1: Dispatch has no streaming delta surface

**Where:** Sub-project 1.A — chunker wrapper insertion point.

**Defect:** The plan (and even the AMENDMENTS) assumed `gateway/dispatch.py` has a delta-dispatch path that the chunker would wrap. It does not. `_do_dispatch` (lines 346-456) calls `loop.run_conversation(...)` once, awaits the complete result string, and forwards it to the adapter's `on_processing_complete`. There is no per-delta callback at the dispatch layer.

**Why it matters:** The chunker needs to see provider deltas as they arrive. The only place that surfaces them is `loop._call_provider` (`agent/loop.py:1843+`), which already accepts a `stream_callback` parameter for streaming providers.

**Fix:** Sub-project 1.A's correct insertion point is `loop._call_provider`'s stream_callback. The chunker becomes a callable wrapper around the user-supplied stream_callback (or a default no-op). The wrapper buffers deltas, emits blocks, and calls `adapter.send(chat_id, block.text)` per emit. **The wrapper still calls `send` per block (one new message per paragraph) — that IS the desired UX, replacing the current "long single message edited many times" pattern that doesn't actually exist either since dispatch isn't streaming today.**

This means: today, `oc gateway` and `oc chat` deliver the *complete* reply as one big message at the end of the turn (no streaming UX on Telegram at all — verify this once on a Telegram smoke test). The chunker won't fix robotic streaming because there isn't streaming today; it will introduce paragraph-paced delivery as a NEW capability.

**Updated effort:** Sub-project 1.A scope is unchanged in code complexity (~M, ~2d) but the design is different — it adds streaming-with-pacing rather than smoothing existing streaming.

---

## Decision: proceed with execution gating

After Phase 0:

| Pick | Pre-execution status | Notes |
|---|---|---|
| 1.A Block chunker | **PROCEED** with revised insertion (stream_callback in loop._call_provider). | D-NEW-1 changes design intent but not effort. |
| 1.B-alt Active Memory (RecallTool-prepend, core) | **PROCEED**. | Validated by 0.1 — pivot is correct path. |
| 1.C Anti-loop detector | **PROCEED**. | Per-(session,depth) scoping confirmed needed. Cleanest pick to execute first. |
| 1.D Replay sanitization | **PROCEED** with M effort + schema migration. | Audit H6 confirmed. |
| 1.E Auth cooldown surface | **PROCEED** with thin-surface design. | Audit C3 confirmed. |
| 1.F-read Sessions trio | **PROCEED** with C4/C5 fixes. | Audit C4+C5 confirmed. |
| 1.G ClarifyTool | **PROCEED** with C4 fix. | Audit C4 confirmed. |
| ~~1.H SendMessage~~ | **DROPPED**. | Audit C6 confirmed (PR #222 collision). |

**Recommended execution order (simplest first, riskiest last):**

1. **1.C Anti-loop** — internal loop logic, no SDK changes, no plugin loading, just one new module + integration. Best first execution target.
2. **1.G Clarify** — single new tool, well-known shape, just needs C4 fix.
3. **1.F-read Sessions** — three tools in one file, simple SessionDB reads, C4/C5 fixes.
4. **1.E Auth surface** — read-only monitor over existing pool.
5. **1.B-alt Active Memory** — touches `agent/loop.py`; needs careful integration.
6. **1.A Block chunker** — needs stream_callback wiring; design re-validated per D-NEW-1.
7. **1.D Replay sanitization** — biggest scope (schema migration + writers + reader).

Each pick ships as one PR. Subagent dispatch one at a time (per AMENDMENTS gating); spec/code review before next pick begins.
