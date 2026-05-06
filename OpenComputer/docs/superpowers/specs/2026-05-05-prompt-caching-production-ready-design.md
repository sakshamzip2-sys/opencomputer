# Prompt Caching — Production-Ready (2026-05-05, rev 2 post-audit)

## Summary

The Anthropic prompt-caching pipeline (PR #339, #473) ships the right
*shape* but is not production-ready: the system block is rebuilt with
volatile per-turn injections appended before the cache_control marker is
placed, so the prefix cache misses every turn whenever any injection
varies. Idle TTL tracking is per-provider-instance (not per-session),
making it incorrect for multi-session deployments. Token estimation
ignores tool_result/tool_use blocks, so large tool outputs (the fattest
content in tool-using sessions) silently fail the threshold filter and
waste breakpoint slots. Tool array order is caller-supplied, so dynamic
plugin/MCP discovery can reorder tools across turns and bust the
tools-array prefix.

This spec also makes the bottom-status `/rename` title indicator more
robust: the title is currently captured into a closure at
`read_user_input` call time and shares its visibility filter with the
permission-mode badge, so any future change that hides the badge also
hides the title even when it should be visible. We decouple the two
and use a callable so the title stays current.

## Goals

1. End-to-end cache hits on turn 2+ across every supported volatile
   condition (plan reminder flips, screen-awareness blocks, persona
   reclassification, multi-session daemon use).
2. Tool-array cache stability invariant under dynamic tool registration.
3. `/rename` updates the title bar on the next prompt; visibility no
   longer entangled with the permission-mode badge.

## Non-goals

- Per-block TTL differentiation. We already get 1h on all 4 breakpoints
  when idle ≥ 4 min.
- Replacing the legacy `apply_anthropic_cache_control` (used only by
  no-tools callers, kept for API compatibility).
- New telemetry surfaces (PR #473's e2e test already locks the
  hit/miss/write reporting).
- Eliminating `copy.deepcopy` on the hot path — performance-only, not
  correctness; punt.

## Confirmed bugs to fix (priority order)

### Bug 1 — Per-turn injection appended to frozen system busts cache (P0)

**Location:** `opencomputer/agent/loop.py:1045`
```python
system = base_system + ("\n\n" + injected if injected else "")
```

**Symptom:** `injection_engine.compose(InjectionContext)` returns
content that varies per turn. Once concatenated into `system`, the
bytes flowing into `_apply_cache_control` differ across turns, busting
the cached system prefix.

**Fix:** Pass `base_system` and `injected` separately into the provider
chain. The Anthropic provider builds the system content as TWO text
blocks: a frozen base block (with `cache_control`) followed by an
optional injection block (NO `cache_control`). The cache marker sits
at index 0 of the system content list, so Anthropic's prefix walk hits
the cache regardless of injection volatility.

**Detailed implementation:**

`AnthropicProvider._apply_cache_control` signature changes:
```python
def _apply_cache_control(
    self,
    anthropic_messages: list[dict[str, Any]],
    base_system: str,                # was: system
    injected_system: str = "",       # NEW (default "" preserves old behavior)
    api_tools: list[dict[str, Any]] | None = None,
    *,
    model: str = "",
    idle_seconds: float = 0.0,
    session_id: str | None = None,   # NEW (used for idle-tracker keying)
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
```

The unified-system synth becomes:
```python
unified: list[dict[str, Any]] = []
sys_blocks: list[dict[str, Any]] = []
if base_system:
    sys_blocks.append({"type": "text", "text": base_system})
if injected_system:
    sys_blocks.append({"type": "text", "text": "\n\n" + injected_system})
if sys_blocks:
    unified.append({"role": "system", "content": sys_blocks})
unified.extend(anthropic_messages)
```

Call `apply_full_cache_control` as today, but with a NEW dedicated
helper for system-block marker placement so we don't share semantics
with the legacy path:

```python
# In prompt_caching.py — NEW helper, isolated from _apply_cache_marker
def _mark_system_base_block(
    msg: dict[str, Any],
    cache_marker: dict[str, Any],
) -> None:
    """Stamp cache_control on the FIRST text block of a system message.

    Used when the system content list has the shape
    ``[base, optional injected]``: the base is index 0 (frozen, cache-
    eligible) and the injection (when present) is index 1 (volatile,
    no marker). Distinct from ``_apply_cache_marker`` which stamps the
    LAST block — that's correct for non-system messages but would
    stamp the volatile injection here.
    """
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return
    first = content[0]
    if isinstance(first, dict):
        first["cache_control"] = cache_marker
```

`apply_full_cache_control` is updated to dispatch on system role:
```python
if messages and messages[0].get("role") == "system":
    sys_msg = messages[0]
    sys_content = sys_msg.get("content")
    if isinstance(sys_content, list) and len(sys_content) > 1:
        _mark_system_base_block(sys_msg, marker)
    else:
        _apply_cache_marker(sys_msg, marker, native_anthropic=native_anthropic)
    sys_used = 1
```

This keeps the legacy path's single-block behavior untouched while
giving the new path the correct first-block placement. Audit Finding 1
+ 2 addressed.

`AgentLoop.run_conversation` changes:
- Compute `base_system` (snapshot) and `injected` (compose result)
  separately.
- Pass both to `provider.complete(... base_system=base_system,
  injected_system=injected, session_id=sid, ...)`.
- Drop the line `system = base_system + ...`.
- Where `provider.complete` is called via `**_extra_kwargs` today,
  add `base_system` + `injected_system` + `session_id` as explicit
  named arguments — they're not part of `runtime_extras`.

`BaseProvider.complete` and `BaseProvider.stream_complete` accept the
new kwargs as `Optional[str] = None` so non-Anthropic providers absorb
+ ignore them. The `system: str = ""` parameter is replaced by
`base_system: str = ""` + `injected_system: str = ""`. To keep
existing call sites and 3rd-party providers working, both `complete`
and `stream_complete` accept `system` as an alias: when `system` is
passed and `base_system` is not, `system` is treated as
`base_system` and `injected_system=""`. Update the docstring and add
a deprecation comment; remove `system` in a follow-up release.

### Bug 2 — Idle tracker per-provider-instance, not per-session (P0)

**Location:** `extensions/anthropic-provider/provider.py:818, 1245-1247`

**Symptom:** Single shared provider in long-running daemons →
cross-session timestamp pollution → wrong TTL.

**Fix:** Replace `self._last_call_ts: float` with
`self._last_call_ts: dict[str, float]` keyed by `session_id`. Apply at
all 3 call sites. New evict-by-oldest bound at 256 entries.

```python
# __init__
self._last_call_ts: dict[str, float] = {}
self._last_call_ts_max = 256

# at each call site (line 1245+, 1447+, 1557+)
import time as _time
_now = _time.monotonic()
sid_key = session_id or "_default"
_last = self._last_call_ts.get(sid_key, 0.0)
idle_s = (_now - _last) if _last > 0 else 0.0
self._last_call_ts[sid_key] = _now
# bound: drop oldest-write entry when above cap
if len(self._last_call_ts) > self._last_call_ts_max:
    oldest_key = min(self._last_call_ts.items(), key=lambda kv: kv[1])[0]
    self._last_call_ts.pop(oldest_key, None)
```

Sessions without a known id share the `"_default"` key (preserves
current single-session behavior in tests / scripts that don't pass
`session_id`).

### Bug 3 — Token estimator ignores tool_result + tool_use (P1)

**Location:** `opencomputer/agent/prompt_caching.py:54-64`

**Fix:** Refactor to count CHARS internally and only divide once at
the public-API boundary. This avoids the unit-bug Finding 8 caught.

```python
#: Per-image / per-document token estimate. Anthropic charges roughly
#: 1500 tokens per typical image and ~3000 per PDF page (rough). We
#: don't need exact — the estimator is used only to decide whether a
#: block clears the cache-eligibility threshold.
_IMAGE_TOKENS_ESTIMATE = 1500
_DOCUMENT_TOKENS_ESTIMATE = 3000


def _block_chars(content: Any) -> int:
    """Estimate content size in CHARACTERS (recursion-safe).

    Used internally by ``_block_token_estimate``; centralizing on
    chars avoids the divide-then-multiply round-trip that the
    naive token-recursion would produce.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                total += len(block.get("text", ""))
            elif t == "tool_result":
                total += _block_chars(block.get("content", ""))
            elif t == "tool_use":
                import json as _json
                try:
                    total += len(_json.dumps(block.get("input", {})))
                except (TypeError, ValueError):
                    pass  # un-encodable input; treat as 0 chars
            elif t == "image":
                total += _IMAGE_TOKENS_ESTIMATE * _CHARS_PER_TOKEN
            elif t == "document":
                total += _DOCUMENT_TOKENS_ESTIMATE * _CHARS_PER_TOKEN
        return total
    return 0


def _block_token_estimate(content: Any) -> int:
    """Cheap upper-bound token count for a message's content."""
    return _block_chars(content) // _CHARS_PER_TOKEN
```

### Bug 4 — Tool ordering is caller-supplied (P1)

**Location:** `extensions/anthropic-provider/provider.py:76-104`

**Fix:** Sort by `name` before returning from
`_format_tools_for_anthropic`. The cache marker on the user-tools
array's last entry now lands on the alphabetically-last tool every
turn — stable.

```python
out.sort(key=lambda d: d.get("name", ""))
return out
```

**Skills interaction:** `_augment_kwargs_for_skills` (line 218-222)
appends `code_execution_20250825` to `kwargs["tools"]` AFTER
`_apply_cache_control` has already placed the cache_control marker.
Concretely the wire payload is:
```
[user_tool_1, ..., user_tool_N_with_cache_control, code_execution_20250825]
```
The marker sits on the alphabetically-last user tool; the
unmarked `code_execution` sits AFTER the marker and so is NOT part of
the cached prefix region. Anthropic accepts marker placement anywhere
in the tools array, and the prefix cache hits whenever the marked-and-
prior bytes match — they do, byte-for-byte, regardless of skill toggle.
Add a regression test asserting the cache marker lands on the
alphabetically-last user tool when skills are enabled.

Also add a regression test asserting `_format_tools_for_anthropic` is
byte-stable across two calls with the same input, to catch dict-key-
ordering drift in `to_anthropic_format()`.

### Bug 5 — Dead-code role=tool branch (P2 cleanup)

**Location:** `opencomputer/agent/prompt_caching.py:79-82`

**Symptom:** By the time `apply_full_cache_control` runs from
`_apply_cache_control`, `_to_anthropic_messages` has converted every
`role == "tool"` message into `role == "user"` with a `tool_result`
content block. The branch is unreachable from the Anthropic native
path. Legacy callers without tools never construct
`role=="tool"` messages.

**Fix:** Leave it. Removing risks breaking a 3rd-party plugin that
hand-rolls `role=="tool"` messages and calls `apply_anthropic_cache_control`
directly. Cost of leaving is one branch never executed in our paths.
We document its purpose in a comment so future readers don't churn it.

### Bug 6 — Persona reclassification evicts snapshot (P3 deferred)

**Location:** `opencomputer/agent/loop.py:2469`

`persona_overlay` is rendered INTO the FROZEN base prompt
(`prompt_builder.py:459`, called from `loop.py:1012`), so a persona
flip mutates `base_system` and the eviction is genuinely required.
With Bug 1's split (base + injected), the persona overlay is still
part of the base. The evict at `loop.py:2469` remains correct. No
change needed.

### Bug 7 — `/rename` title bar robustness (P1 ux)

**Locations:**
- `opencomputer/cli_ui/input_loop.py:495` — `session_title: str | None`
  parameter (closure capture)
- `opencomputer/cli_ui/input_loop.py:939-950` — title sits inside the
  same ConditionalContainer as the badge, sharing
  `_badge_visible = runtime is not None and stdout.isatty()`

**Symptom (caveats):** I could not deterministically reproduce
"title invisible on turn 2 after `/rename`" in a code-only inspection
— the closure is rebuilt fresh every outer-loop iteration with a
freshly-read DB title, and `loop._runtime` is initialized at
`AgentLoop.__init__` (loop.py:400) so `_badge_visible` is True in any
TTY chat flow. The user-reported screenshot does suggest the title
is missing post-rename, but a code-only repro is inconclusive — the
likely scenarios are (a) the user is running a stale binary
(editable-install on a different worktree branch), (b) terminal
rendering is dropping the bottom row in their setup, or (c) some
edge case I haven't traced. Either way, the current code is
fragile in three ways worth fixing:

1. Title visibility shares a filter with the badge — any future change
   that hides the badge silently hides the title.
2. The title is captured into the `read_user_input` closure as a
   static string, so any future inline-update mechanism (e.g. mid-
   prompt rename via key binding) can't take effect.
3. The empty-title (None or "") case currently returns `[]` — fine,
   but couples to the bool-y check. A callable lets us cleanly
   delegate "is there a title?" to the title source.

**Fix:** Decouple the title from the badge and use a callable.

```python
# input_loop.py — read_user_input signature
async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
    session_title: str | None = None,                  # kept for back-compat
    get_session_title: Callable[[], str | None] | None = None,  # NEW (preferred)
    paste_folder: PasteFolder | None = None,
    memory_manager: object | None = None,
    runtime: object | None = None,
) -> str:
    ...
    # If a callable is provided, use it; else fall back to the static value.
    if get_session_title is None:
        _captured = session_title
        def get_session_title() -> str | None:  # noqa: F811
            return _captured

    def _title_text():
        title = get_session_title() or ""
        if not (1 <= len(title) <= 50):
            return []
        return [
            ("class:title.box", "┤ "),
            ("class:title.text", title),
            ("class:title.box", " ├"),
        ]

    def _title_visible() -> bool:
        title = get_session_title() or ""
        return 1 <= len(title) <= 50

    # SEPARATE ConditionalContainer for the title — independent of the badge.
    title_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_title_text),
            height=1,
            align=WindowAlign.RIGHT,
            dont_extend_width=True,
        ),
        filter=Condition(_title_visible),
    )

    # Badge container stays as before but loses the inner title window.
    badge_window = ConditionalContainer(
        content=Window(content=FormattedTextControl(_badge_text), height=1),
        filter=Condition(lambda: _badge_visible),
    )

    # Bottom row — VSplit with both, but each owns its own visibility.
    bottom_row = VSplit([badge_window, title_window])
    layout = Layout(HSplit([
        filler,
        dropdown_window,
        dropdown_divider,
        VSplit([prompt_window, input_window]),
        paste_hint_window,
        bottom_row,
    ]), focused_element=input_window)
```

Caller (`cli.py:1505-1514`) passes the callable instead of a value:
```python
def _fetch_session_title() -> str | None:
    try:
        from opencomputer.agent.state import SessionDB as _TitleDB
        return _TitleDB(cfg.session.db_path).get_session_title(session_id) or None
    except Exception:
        return None

async def _read_one() -> str:
    scope = TurnCancelScope()
    return await read_user_input(
        profile_home=profile_home,
        scope=scope,
        get_session_title=_fetch_session_title,
        paste_folder=paste_folder,
        memory_manager=loop.memory if loop is not None else None,
        runtime=loop._runtime if loop is not None else None,
    )
```

Note: `Condition(callable)` is evaluated by prompt_toolkit on each
render frame. Frames are triggered by key events / cursor moves /
explicit `app.invalidate()`. For the chat flow this is fine — by the
time `read_user_input` returns and a new one is built, the new
title is already captured by the next frame's `_title_visible()`
call. We're NOT claiming "real-time mid-prompt updates" — that
would need an external invalidate trigger which doesn't exist.

The `cli.py:1492-1500` block that re-fetches `_current_title` per
iteration becomes dead code; remove it (or convert to a no-op
fallback for callers that haven't migrated). Keep the
`session_title` parameter on `read_user_input` for any test fixture
or 3rd-party caller that hasn't migrated.

## Plumbing changes

### `BaseProvider.complete` and `stream_complete`

Add three optional keyword-only parameters:
```python
async def complete(
    self,
    *,
    model: str,
    messages: list[Message],
    system: str = "",
    base_system: str = "",
    injected_system: str = "",
    session_id: str | None = None,
    tools: list[ToolSchema] | None = None,
    ...,
) -> ProviderResponse:
```

When `base_system` is empty and `system` is non-empty, the provider
uses `system` as `base_system` (back-compat). When both are empty,
no system block is sent. New providers should prefer
`base_system` + `injected_system`.

### `AnthropicProvider`

Three call sites of `_apply_cache_control` (lines 1252, 1454, 1564)
update to pass `base_system`, `injected_system`, `session_id`.
`AnthropicProvider.complete` and `_do_complete` and the streaming
counterparts plumb the new params through.

### `AgentLoop.run_conversation`

Lines ~1045 and ~3056-3112:
- Drop `system = base_system + ...`.
- Where `provider.complete(...)` is called (line ~3105), pass
  `base_system=base_system, injected_system=injected, session_id=sid`
  as explicit keyword args (NOT through `runtime_extras`).
- Same for `provider.stream_complete(...)` at line ~3056.

## Test plan

### New tests

- `test_prompt_caching.py::test_mark_system_base_block_marks_first_block_only`
  — system content list with 2 text blocks, assert cache_control on
  index 0, none on index 1.
- `test_prompt_caching.py::test_mark_system_base_block_no_op_on_string`
  — system content as string, assert no change (the helper handles
  list-only).
- `test_prompt_caching.py::test_token_estimate_includes_tool_result_string`
  — tool_result with string content of 50KB, assert estimate >0.
- `test_prompt_caching.py::test_token_estimate_includes_tool_result_blocks`
  — tool_result with list-of-blocks content, assert recursion works.
- `test_prompt_caching.py::test_token_estimate_includes_tool_use_input`
  — tool_use with a 5KB JSON input, assert estimate >0.
- `test_prompt_caching.py::test_token_estimate_image_block_baseline`
  — single image block returns ≥1000 tokens (loose lower bound on
  the constant).
- `test_anthropic_provider.py::test_format_tools_sorted_alphabetical`
  — pass tools in random order, assert output is sorted by name.
- `test_anthropic_provider.py::test_format_tools_byte_stable_across_calls`
  — call twice with same input, assert JSON-equal output.
- `test_anthropic_provider.py::test_idle_tracker_isolated_per_session`
  — call A then B (10s gap), then A again (300s gap), assert A sees
  ~310s and B sees ~300s.
- `test_anthropic_provider.py::test_idle_tracker_lru_bound_at_256`
  — populate 257 sessions, assert one was evicted.
- `test_anthropic_provider.py::test_split_system_keeps_prefix_byte_stable_when_injection_changes`
  — two calls, same `base_system`, different `injected_system`, assert
  the cache_control'd block bytes are identical.
- `test_anthropic_provider.py::test_apply_cache_control_returns_string_system_when_no_injection`
  — `injected_system=""`, no marker promotion to list — back-compat
  returns string-shaped `system_for_sdk` when there's only a single
  block.
- `test_anthropic_provider.py::test_apply_cache_control_returns_list_system_when_injection_present`
  — `injected_system="reminder"`, returns `system_for_sdk` as a 2-block list.
- `test_anthropic_provider.py::test_apply_cache_control_skills_marker_position`
  — with skills enabled, cache_control lands on the alphabetically-
  last USER tool, not on `code_execution_20250825`.
- `test_cli_ui_input_loop.py::test_title_bar_uses_dynamic_callable`
  — pass a `get_session_title` whose return value changes between
  invocations; assert `_title_text` reflects the current value.
- `test_cli_ui_input_loop.py::test_title_visible_independent_of_badge`
  — `runtime=None` (forces `_badge_visible=False`), but
  `get_session_title=lambda: "foo"` — assert `_title_visible()` is True.

### Tests that need updating

- `tests/test_idle_ttl_switch.py` (60-61, 88-90) — currently passes
  `system=""`. Change to `base_system=""` (the new param). Two test
  bodies update.
- Any other `_apply_cache_control` direct call site — sweep with
  `grep -rn "_apply_cache_control" tests/`.

### Regression (existing tests must still pass)

- `tests/test_prompt_caching.py` (all)
- `tests/test_prompt_caching_thresholds.py` (all)
- `tests/test_cache_telemetry_e2e.py` (locks the v2 telemetry format)
- `tests/test_anthropic_pdf_caching.py`
- `tests/test_anthropic_files_cache.py`
- `tests/test_openrouter_cache_wiring.py`

### Manual verification

1. `oc chat`, send "hi", read assistant reply.
2. Send "hi again". Inspect `/usage` for `cache_read_input_tokens > 0`.
3. Toggle `/plan` mid-session, send a message — verify cache hit
   still happens (the plan-mode injection toggle no longer busts).
4. `/rename test-foo` after turn 1. On turn 2's prompt, verify
   `┤ test-foo ├` renders bottom-right.

## Backwards compatibility

- Legacy `apply_anthropic_cache_control` keeps its current signature
  and behavior. The shared `_apply_cache_marker` is unchanged.
  System-block-first marker is a NEW helper `_mark_system_base_block`
  used only by the new path's dispatch.
- `AnthropicProvider._apply_cache_control` parameter rename:
  `system` → `base_system` (positional position preserved at index 1).
  Kwarg-passing tests update; positional callers stay working.
- `BaseProvider.complete/stream_complete` accept BOTH `system=` and
  `base_system=` for one release; document `system=` as deprecated.
- `read_user_input` accepts BOTH `session_title=` (back-compat) and
  `get_session_title=` (preferred). Existing callers keep working.
- Empty/whitespace `base_system`: when `base_system == ""` and
  `injected_system != ""`, render only the injection block (no
  marker, since the prefix is non-stable anyway). Matches today's
  concatenation behavior of `"" + "\n\n" + injected` (which produced
  a leading blank-line-prefixed block — the new path drops the
  leading blank line, a small benign improvement).

## Self-review checklist

- [x] No placeholders / TODOs.
- [x] Each fix has a concrete file:line target and concrete code.
- [x] No contradictions between sections.
- [x] All 7 bugs listed have explicit accept/defer disposition.
- [x] Backwards-compatibility section names every breaking signature
      change AND lists `tests/test_idle_ttl_switch.py` as needing update.
- [x] Test plan covers each fix with a name + assertion shape.
- [x] Audit findings 1-15 each have a disposition: 1+2 → new helper
      `_mark_system_base_block`; 3 → skills layer documented + test;
      4 → explicit kwarg, not via `runtime_extras`; 5 → tests updated;
      6 → re-investigated and Bug 7 framing softened (we cannot
      definitively repro original symptom, but the fix is still the
      right architectural cleanup); 7 → documented; 8 → switched to
      char-based recursion to avoid divide-then-multiply roundtrip;
      9 → persona-overlay-in-base clarified; 10 → empty-base case
      handled; 11 → naming cleaned; 12 → byte-stability test added;
      13 → no-op; 14 → string-vs-list shape tests added; 15 → checklist
      ticked.
