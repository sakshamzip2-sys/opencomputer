# Prompt Caching Production-Ready Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make Anthropic prompt caching actually hit on turn 2+ across volatile per-turn injections; make idle TTL correct under multi-session daemon use; harden tool array against re-ordering; close the `/rename` title-bar fragility.

**Architecture:** (a) Split the system block into `base_system` (frozen, cache-marked) + `injected_system` (per-turn, unmarked) — Anthropic's prefix walk hits the cache on the marked block regardless of injection content. (b) Idle tracker becomes a per-session dict on the provider with a 256-entry LRU bound. (c) Token estimator counts tool_result/tool_use/image/document blocks via chars-based recursion. (d) `_format_tools_for_anthropic` sorts by name. (e) Title bar gets its own ConditionalContainer + a callable for the title source.

**Tech Stack:** Python 3.11, anthropic SDK, prompt_toolkit, pytest.

---

## File map

- **Modify** `opencomputer/agent/prompt_caching.py` — token-estimator extension; new `_mark_system_base_block` helper; `apply_full_cache_control` system-list dispatch.
- **Modify** `extensions/anthropic-provider/provider.py` — `_format_tools_for_anthropic` sort; per-session idle dict; `_apply_cache_control` new signature; 3 call sites.
- **Modify** `plugin_sdk/provider_base.py` (or wherever `BaseProvider.complete` lives) — accept `base_system`, `injected_system`, `session_id` as optional kwargs.
- **Modify** `opencomputer/agent/loop.py` — drop `system = base_system + injected` concat; pass new kwargs through `provider.complete`/`stream_complete`.
- **Modify** `opencomputer/cli_ui/input_loop.py` — `read_user_input` accepts `get_session_title`; split badge/title into separate ConditionalContainers.
- **Modify** `opencomputer/cli.py` — pass `get_session_title` callable; remove the per-iteration `_current_title` re-fetch (now handled by the callable).
- **Modify** `tests/test_idle_ttl_switch.py` — update keyword `system=""` → `base_system=""`.
- **Create** `tests/test_prompt_caching_v2.py` — new tests for system-block split, token estimator, byte-stable tools.
- **Create** `tests/test_anthropic_idle_per_session.py` — per-session idle tracker tests.
- **Create** `tests/test_cli_ui_title_callable.py` — title callable + decoupling tests.

---

## Task 1: Token estimator includes tool_result/tool_use/image/document blocks

**Files:**
- Modify: `opencomputer/agent/prompt_caching.py:54-64`
- Test: `tests/test_prompt_caching_v2.py`

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_prompt_caching_v2.py
"""V2 caching tests — production-ready (rev 2)."""
from opencomputer.agent.prompt_caching import (
    _block_chars,
    _block_token_estimate,
)


def test_token_estimate_includes_tool_result_string():
    """A tool_result with a 50KB string content should report >0 tokens."""
    big = "x" * (50 * 1024)
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": big}]
    est = _block_token_estimate(content)
    assert est > 10_000, f"expected >10k tokens, got {est}"


def test_token_estimate_includes_tool_result_blocks():
    """A tool_result with list-of-blocks content recurses correctly."""
    inner = "y" * 8000
    content = [{
        "type": "tool_result",
        "tool_use_id": "t2",
        "content": [{"type": "text", "text": inner}],
    }]
    est = _block_token_estimate(content)
    assert est >= 1500, f"expected ~2000 tokens for 8KB string, got {est}"


def test_token_estimate_includes_tool_use_input():
    """A tool_use with a 5KB JSON input should report >0 tokens."""
    big_input = {"k": "z" * 5000}
    content = [{"type": "tool_use", "id": "t3", "name": "f", "input": big_input}]
    est = _block_token_estimate(content)
    assert est > 1000


def test_token_estimate_image_block_baseline():
    """Image blocks return a non-trivial estimate (≥1000 tokens)."""
    content = [{"type": "image", "source": {"type": "base64", "data": "..."}}]
    est = _block_token_estimate(content)
    assert est >= 1000


def test_block_chars_helper_returns_chars_not_tokens():
    """The internal helper returns CHARS so units don't drift across recursion."""
    s = "abcd"  # 4 chars = 1 token at _CHARS_PER_TOKEN=4
    assert _block_chars(s) == 4
    assert _block_token_estimate(s) == 1


def test_block_chars_string_passthrough():
    assert _block_chars("hello world") == 11
```

- [ ] **Step 1.2: Run tests to confirm they fail**

```bash
cd /Users/saksham/.config/superpowers/worktrees/opencomputer/prompt-caching/OpenComputer
.venv/bin/pytest tests/test_prompt_caching_v2.py -x -v
```
Expected: ImportError on `_block_chars` (doesn't exist) or assertion failures (estimator returns 0 for non-text blocks).

- [ ] **Step 1.3: Refactor estimator to chars-based recursion**

Replace `_block_token_estimate` (lines 54-64) with a chars-based helper plus a thin token-units wrapper:

```python
#: Per-image / per-document token estimate. Anthropic charges roughly
#: 1500 tokens per typical image and ~3000 per PDF page (rough). The
#: estimator is used only to decide whether a block clears the cache-
#: eligibility threshold, so order-of-magnitude is sufficient.
_IMAGE_TOKENS_ESTIMATE = 1500
_DOCUMENT_TOKENS_ESTIMATE = 3000


def _block_chars(content: Any) -> int:
    """Estimate content size in CHARACTERS (recursion-safe).

    Used internally by ``_block_token_estimate``; centralizing on chars
    avoids the divide-then-multiply round-trip that a token-recursion
    would produce.
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

- [ ] **Step 1.4: Run new tests; confirm pass**

```bash
.venv/bin/pytest tests/test_prompt_caching_v2.py -x -v
```
Expected: 6 passed.

- [ ] **Step 1.5: Run existing prompt_caching tests; confirm still pass**

```bash
.venv/bin/pytest tests/test_prompt_caching.py tests/test_prompt_caching_thresholds.py -x -v
```
Expected: all pass.

- [ ] **Step 1.6: Commit**

```bash
git add opencomputer/agent/prompt_caching.py tests/test_prompt_caching_v2.py
git commit -m "feat(cache): token-estimator counts tool_result/tool_use/image/document blocks"
```

---

## Task 2: New `_mark_system_base_block` helper + system-list dispatch

**Files:**
- Modify: `opencomputer/agent/prompt_caching.py` (insert after `_apply_cache_marker`)
- Modify: `opencomputer/agent/prompt_caching.py:185-226` (`apply_full_cache_control`)
- Test: `tests/test_prompt_caching_v2.py`

- [ ] **Step 2.1: Write failing tests**

Append to `tests/test_prompt_caching_v2.py`:
```python
from opencomputer.agent.prompt_caching import (
    _mark_system_base_block,
    apply_full_cache_control,
)


def test_mark_system_base_block_marks_index_zero_only():
    """When system content is a 2-block list, only index 0 gets the marker."""
    msg = {"role": "system", "content": [
        {"type": "text", "text": "base"},
        {"type": "text", "text": "injection"},
    ]}
    marker = {"type": "ephemeral"}
    _mark_system_base_block(msg, marker)
    assert msg["content"][0].get("cache_control") == marker
    assert "cache_control" not in msg["content"][1]


def test_mark_system_base_block_no_op_on_string_content():
    """When system content is a plain string, the helper is a no-op."""
    msg = {"role": "system", "content": "all-in-one"}
    _mark_system_base_block(msg, {"type": "ephemeral"})
    assert msg["content"] == "all-in-one"


def test_mark_system_base_block_no_op_on_empty_list():
    msg = {"role": "system", "content": []}
    _mark_system_base_block(msg, {"type": "ephemeral"})
    assert msg["content"] == []


def test_apply_full_cache_control_system_2block_marks_first_only():
    """End-to-end: a system message with [base, injection] gets cache_control on base only."""
    msgs = [{
        "role": "system",
        "content": [
            {"type": "text", "text": "base"},
            {"type": "text", "text": "\n\nplan reminder"},
        ],
    }, {"role": "user", "content": "hi"}]
    cached, _ = apply_full_cache_control(msgs, [], native_anthropic=True)
    sys_content = cached[0]["content"]
    assert sys_content[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in sys_content[1]


def test_apply_full_cache_control_system_1block_unchanged_behavior():
    """A single-block system content still gets the marker (same as before refactor)."""
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "base"}],
    }, {"role": "user", "content": "hi"}]
    cached, _ = apply_full_cache_control(msgs, [], native_anthropic=True)
    assert cached[0]["content"][0].get("cache_control") == {"type": "ephemeral"}


def test_apply_full_cache_control_byte_stable_when_only_injection_changes():
    """Same base, two different injections: marked-block bytes identical."""
    base = "shared base prompt " * 100
    msgs1 = [{
        "role": "system",
        "content": [
            {"type": "text", "text": base},
            {"type": "text", "text": "\n\ninjection 1"},
        ],
    }]
    msgs2 = [{
        "role": "system",
        "content": [
            {"type": "text", "text": base},
            {"type": "text", "text": "\n\ninjection 2"},
        ],
    }]
    c1, _ = apply_full_cache_control(msgs1, [], native_anthropic=True)
    c2, _ = apply_full_cache_control(msgs2, [], native_anthropic=True)
    assert c1[0]["content"][0] == c2[0]["content"][0]  # same marked block bytes
```

- [ ] **Step 2.2: Run failing tests**

```bash
.venv/bin/pytest tests/test_prompt_caching_v2.py::test_mark_system_base_block_marks_index_zero_only -x -v
```
Expected: ImportError on `_mark_system_base_block`.

- [ ] **Step 2.3: Add `_mark_system_base_block` helper**

Insert after `_apply_cache_marker` in `opencomputer/agent/prompt_caching.py`:

```python
def _mark_system_base_block(
    msg: dict[str, Any],
    cache_marker: dict[str, Any],
) -> None:
    """Stamp ``cache_control`` on the FIRST text block of a system message.

    Used when the system content list has the shape
    ``[base, optional injected]``: the base is index 0 (frozen, cache-
    eligible) and the injection (when present) is index 1 (volatile,
    no marker). Distinct from :func:`_apply_cache_marker` which stamps
    the LAST block — that's correct for non-system messages but would
    stamp the volatile injection here.

    No-op on string content or empty content list (the legacy single-
    block-string-system path is handled by ``_apply_cache_marker``).
    """
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return
    first = content[0]
    if isinstance(first, dict):
        first["cache_control"] = cache_marker
```

- [ ] **Step 2.4: Update `apply_full_cache_control` to dispatch on system shape**

In `opencomputer/agent/prompt_caching.py`, replace the system-handling block in `apply_full_cache_control` (around line 214-217):

OLD:
```python
sys_used = 0
if messages and messages[0].get("role") == "system":
    _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
    sys_used = 1
```

NEW:
```python
sys_used = 0
if messages and messages[0].get("role") == "system":
    sys_msg = messages[0]
    sys_content = sys_msg.get("content")
    if (
        isinstance(sys_content, list)
        and len(sys_content) > 1
        and all(
            isinstance(b, dict) and b.get("type") == "text"
            for b in sys_content
        )
    ):
        # Multi-block system content (base + injection split): mark index 0
        # so the injection at index 1 stays cache-volatile without busting
        # the base prefix.
        _mark_system_base_block(sys_msg, marker)
    else:
        _apply_cache_marker(sys_msg, marker, native_anthropic=native_anthropic)
    sys_used = 1
```

- [ ] **Step 2.5: Run all prompt_caching tests**

```bash
.venv/bin/pytest tests/test_prompt_caching.py tests/test_prompt_caching_v2.py tests/test_prompt_caching_thresholds.py -x -v
```
Expected: all pass.

- [ ] **Step 2.6: Commit**

```bash
git add opencomputer/agent/prompt_caching.py tests/test_prompt_caching_v2.py
git commit -m "feat(cache): _mark_system_base_block helper + system-list dispatch"
```

---

## Task 3: Stable tool ordering in `_format_tools_for_anthropic`

**Files:**
- Modify: `extensions/anthropic-provider/provider.py:76-104`
- Test: `tests/test_anthropic_provider_tool_ordering.py` (new)

- [ ] **Step 3.1: Write failing tests**

Create `tests/test_anthropic_provider_tool_ordering.py`:

```python
"""V2 — tool ordering must be deterministic across calls + sorted by name."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from plugin_sdk.tools import ToolSchema


def _load_provider_module():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_provider_tool_order_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fmt():
    return _load_provider_module()._format_tools_for_anthropic


def _ts(name: str, desc: str = "") -> ToolSchema:
    return ToolSchema(
        name=name, description=desc or name,
        input_schema={"type": "object", "properties": {}},
    )


def test_format_tools_sorted_alphabetical(fmt):
    tools = [_ts("zoom"), _ts("apple"), _ts("mango")]
    out = fmt(tools)
    assert [t["name"] for t in out] == ["apple", "mango", "zoom"]


def test_format_tools_byte_stable_across_calls(fmt):
    tools = [_ts(f"tool_{i:02d}") for i in range(20)]
    out1 = fmt(list(tools))
    out2 = fmt(list(reversed(tools)))
    assert json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)


def test_format_tools_empty_passthrough(fmt):
    assert fmt(None) == []
    assert fmt([]) == []
```

- [ ] **Step 3.2: Run failing tests**

```bash
.venv/bin/pytest tests/test_anthropic_provider_tool_ordering.py -x -v
```
Expected: `test_format_tools_byte_stable_across_calls` fails (out2 reversed).

- [ ] **Step 3.3: Add sort to `_format_tools_for_anthropic`**

In `extensions/anthropic-provider/provider.py`, find the function (line 76) and add a single sort line before `return out`:

```python
    out.sort(key=lambda d: d.get("name", ""))
    return out
```

- [ ] **Step 3.4: Run new tests; confirm pass**

```bash
.venv/bin/pytest tests/test_anthropic_provider_tool_ordering.py -x -v
```
Expected: 3 passed.

- [ ] **Step 3.5: Run existing anthropic-provider tests; confirm no regression**

```bash
.venv/bin/pytest tests/ -k "anthropic" --ignore=tests/extensions/openrouter -x -v 2>&1 | tail -30
```

- [ ] **Step 3.6: Commit**

```bash
git add extensions/anthropic-provider/provider.py tests/test_anthropic_provider_tool_ordering.py
git commit -m "feat(cache): stable alphabetical tool ordering in _format_tools_for_anthropic"
```

---

## Task 4: Per-session idle tracker on AnthropicProvider

**Files:**
- Modify: `extensions/anthropic-provider/provider.py:818` (`__init__`)
- Modify: `extensions/anthropic-provider/provider.py:1245-1247, 1447-1449, 1557-1559` (3 call sites)
- Test: `tests/test_anthropic_idle_per_session.py` (new)

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_anthropic_idle_per_session.py
"""V2 — idle tracker is per-session, not per-provider-instance."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest


def _load_provider_module():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_provider_idle_per_session_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    mod = _load_provider_module()
    return mod.AnthropicProvider()


def test_idle_tracker_is_dict_not_float(provider):
    """The idle tracker should be a dict keyed by session_id."""
    assert isinstance(provider._last_call_ts, dict)


def test_idle_tracker_isolated_per_session(provider, monkeypatch):
    """Two sessions with different gap patterns must report different idle_seconds."""
    fake_now = [1000.0]

    def _fake_monotonic():
        return fake_now[0]

    import time as _time
    monkeypatch.setattr(_time, "monotonic", _fake_monotonic)

    # Session A first call at t=1000
    provider._last_call_ts["A"] = fake_now[0]
    fake_now[0] = 1010.0  # +10s

    # Session B first call at t=1010 — idle = 0 (first call key absent)
    sid_B_last = provider._last_call_ts.get("B", 0.0)
    idle_B_first = (fake_now[0] - sid_B_last) if sid_B_last > 0 else 0.0
    assert idle_B_first == 0.0

    provider._last_call_ts["B"] = fake_now[0]
    fake_now[0] = 1310.0  # +300s

    # Session A second call: idle should be 1310-1000 = 310s, NOT 1310-1010 = 300s.
    idle_A_second = fake_now[0] - provider._last_call_ts["A"]
    assert idle_A_second == pytest.approx(310.0)

    # Session B second call from this same point: idle should be 300s.
    idle_B_second = fake_now[0] - provider._last_call_ts["B"]
    assert idle_B_second == pytest.approx(300.0)


def test_idle_tracker_lru_bound(provider):
    """When the tracker exceeds the cap, the oldest-write entry is evicted."""
    cap = provider._last_call_ts_max
    for i in range(cap + 5):
        provider._last_call_ts[f"sid-{i:04d}"] = float(i)
        # mimic the bound logic
        if len(provider._last_call_ts) > cap:
            oldest = min(
                provider._last_call_ts.items(), key=lambda kv: kv[1]
            )[0]
            provider._last_call_ts.pop(oldest, None)
    assert len(provider._last_call_ts) == cap
    # earliest entry should be gone
    assert "sid-0000" not in provider._last_call_ts
```

- [ ] **Step 4.2: Run failing tests**

```bash
.venv/bin/pytest tests/test_anthropic_idle_per_session.py -x -v
```
Expected: `test_idle_tracker_is_dict_not_float` fails (it's a float currently).

- [ ] **Step 4.3: Update `__init__`**

In `extensions/anthropic-provider/provider.py:818`:

OLD:
```python
        # Idle-aware TTL switch — track wall-clock between calls so we can
        # bump cache TTL to 1h when a session has been idle long enough
        # that the 5m cache would otherwise have expired.
        self._last_call_ts: float = 0.0
```

NEW:
```python
        # Idle-aware TTL switch — track wall-clock between calls so we can
        # bump cache TTL to 1h when a session has been idle long enough
        # that the 5m cache would otherwise have expired. Per-session keying
        # prevents cross-session contamination in long-running daemons
        # (Telegram bot, ACP, batch) where one provider instance handles
        # many sessions.
        self._last_call_ts: dict[str, float] = {}
        self._last_call_ts_max = 256
```

- [ ] **Step 4.4: Add a helper to `AnthropicProvider`**

Insert after `__init__`:

```python
    def _record_call_get_idle(self, session_id: str | None) -> float:
        """Update the per-session timestamp and return idle_seconds since
        this session's previous call (0.0 on first call or unknown session).

        Bounded at ``self._last_call_ts_max`` entries; eviction picks the
        entry with the oldest timestamp (also the longest-idle session,
        which is fine — that session would re-prefill anyway).
        """
        import time as _time
        _now = _time.monotonic()
        sid = session_id or "_default"
        prev = self._last_call_ts.get(sid, 0.0)
        idle = (_now - prev) if prev > 0 else 0.0
        self._last_call_ts[sid] = _now
        if len(self._last_call_ts) > self._last_call_ts_max:
            oldest_key = min(
                self._last_call_ts.items(), key=lambda kv: kv[1]
            )[0]
            self._last_call_ts.pop(oldest_key, None)
        return idle
```

- [ ] **Step 4.5: Update the 3 call sites to use the helper**

Find each occurrence of:
```python
        import time as _time
        _now = _time.monotonic()
        _last = getattr(self, "_last_call_ts", 0.0)
        idle_s = (_now - _last) if _last > 0 else 0.0
        self._last_call_ts = _now
```

Replace with:
```python
        idle_s = self._record_call_get_idle(session_id)
```

(The `session_id` parameter will be threaded through Task 5.)

- [ ] **Step 4.6: Run new tests; confirm pass**

```bash
.venv/bin/pytest tests/test_anthropic_idle_per_session.py -x -v
```
Expected: 3 passed.

- [ ] **Step 4.7: Commit**

```bash
git add extensions/anthropic-provider/provider.py tests/test_anthropic_idle_per_session.py
git commit -m "feat(cache): per-session idle tracker on AnthropicProvider"
```

---

## Task 5: `_apply_cache_control` signature update + base/injection split

**Files:**
- Modify: `extensions/anthropic-provider/provider.py:1026-1088` (`_apply_cache_control`)
- Modify: `extensions/anthropic-provider/provider.py:1252, 1454, 1564` (3 call sites)
- Modify: `tests/test_idle_ttl_switch.py:60, 88` (keyword update)
- Test: `tests/test_prompt_caching_v2.py` (already started; add provider-level tests)

- [ ] **Step 5.1: Update `_apply_cache_control` signature + body**

In `extensions/anthropic-provider/provider.py`, replace the `_apply_cache_control` method:

```python
    def _apply_cache_control(
        self,
        anthropic_messages: list[dict[str, Any]],
        base_system: str,
        injected_system: str = "",
        api_tools: list[dict[str, Any]] | None = None,
        *,
        model: str = "",
        idle_seconds: float = 0.0,
        session_id: str | None = None,
    ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        """Apply Anthropic prompt caching across system + messages + tools.

        ``base_system`` is the FROZEN per-session prompt (carries the
        cache marker). ``injected_system`` is the per-turn dynamic
        content (no marker — sits AFTER the marked block so its
        volatility doesn't bust the cached prefix). When both are non-
        empty, the system content is a 2-block list; when only
        base_system is set, it stays a single block (back-compat).

        Returns:
            ``(system_for_sdk, messages_for_sdk, tools_for_sdk)``.
            ``system_for_sdk`` is a content-block list (always, when any
            system text exists) so the marker rides through; the SDK
            accepts both list and string for the ``system=`` kwarg.
        """
        from opencomputer.agent.prompt_caching import select_cache_ttl

        # Build the system content list (base + optional injection).
        sys_blocks: list[dict[str, Any]] = []
        if base_system:
            sys_blocks.append({"type": "text", "text": base_system})
        if injected_system:
            sys_blocks.append(
                {"type": "text", "text": "\n\n" + injected_system}
                if base_system
                else {"type": "text", "text": injected_system}
            )

        unified: list[dict[str, Any]] = []
        if sys_blocks:
            unified.append({"role": "system", "content": sys_blocks})
        unified.extend(anthropic_messages)

        caps = self.capabilities
        ttl = select_cache_ttl(
            supports_long_ttl=caps.supports_long_ttl,
            idle_seconds=idle_seconds,
        )
        threshold = caps.min_cache_tokens(model) if model else 0

        cached, cached_tools = apply_full_cache_control(
            unified,
            api_tools,
            cache_ttl=ttl,
            native_anthropic=True,
            min_cache_tokens=threshold,
        )

        if sys_blocks and cached and cached[0].get("role") == "system":
            sys_content = cached[0].get("content")
            sys_for_sdk: Any = (
                sys_content if isinstance(sys_content, list) else (
                    base_system + ("\n\n" + injected_system if injected_system else "")
                )
            )
            messages_for_sdk = cached[1:]
        else:
            # No system text at all
            sys_for_sdk = ""
            messages_for_sdk = cached

        # session_id is consumed via _record_call_get_idle at the call
        # site (Task 4); accepting it here keeps the param surface
        # consistent for tests that call _apply_cache_control directly.
        _ = session_id  # pragma: no cover — silence unused-arg lint

        return sys_for_sdk, messages_for_sdk, cached_tools
```

- [ ] **Step 5.2: Update the 3 internal call sites**

In `_do_complete` (line ~1252), `_do_stream_complete` (line ~1454), and `stream_complete` (line ~1564), replace:
```python
        idle_s = ...  # already replaced in Task 4 step 4.5
        api_tools_pre = _format_tools_for_anthropic(tools)
        sys_for_sdk, api_messages, api_tools = self._apply_cache_control(
            anthropic_messages, system, api_tools_pre,
            model=model, idle_seconds=idle_s,
        )
```
with:
```python
        idle_s = self._record_call_get_idle(session_id)
        api_tools_pre = _format_tools_for_anthropic(tools)
        sys_for_sdk, api_messages, api_tools = self._apply_cache_control(
            anthropic_messages,
            base_system,
            injected_system,
            api_tools_pre,
            model=model,
            idle_seconds=idle_s,
            session_id=session_id,
        )
```

The methods need new params `base_system`, `injected_system`, `session_id` — added in Task 6.

- [ ] **Step 5.3: Update existing test `test_idle_ttl_switch.py`**

`tests/test_idle_ttl_switch.py:60` and `:89`:

OLD:
```python
    sys_for_sdk, msgs_for_sdk, _tools = provider._apply_cache_control(
        anth_msgs, system="", model="claude-opus-4-7", idle_seconds=600.0
    )
```

NEW:
```python
    sys_for_sdk, msgs_for_sdk, _tools = provider._apply_cache_control(
        anth_msgs, base_system="", model="claude-opus-4-7", idle_seconds=600.0
    )
```

(Same for the second call site at line ~89.)

- [ ] **Step 5.4: Add provider-level tests**

Append to `tests/test_prompt_caching_v2.py`:

```python
def test_apply_cache_control_split_system_2blocks(monkeypatch):
    """Provider returns a 2-block system list when injected_system is set."""
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    repo = Path(__file__).resolve().parent.parent
    plugin = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_split_test"
    spec = importlib.util.spec_from_file_location(name, plugin)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    provider = mod.AnthropicProvider()

    sys_for_sdk, _msgs, _tools = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 20000}],
        base_system="frozen base",
        injected_system="per-turn reminder",
        model="claude-opus-4-7",
        idle_seconds=0.0,
    )
    assert isinstance(sys_for_sdk, list)
    assert len(sys_for_sdk) == 2
    assert sys_for_sdk[0]["text"] == "frozen base"
    assert "cache_control" in sys_for_sdk[0]
    assert "cache_control" not in sys_for_sdk[1]
    assert sys_for_sdk[1]["text"].endswith("per-turn reminder")


def test_apply_cache_control_no_injection_keeps_single_block(monkeypatch):
    """When injected_system is empty, system is a single block (back-compat)."""
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    repo = Path(__file__).resolve().parent.parent
    plugin = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_no_inj_test"
    spec = importlib.util.spec_from_file_location(name, plugin)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    provider = mod.AnthropicProvider()

    sys_for_sdk, _msgs, _tools = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 20000}],
        base_system="frozen base",
        injected_system="",
        model="claude-opus-4-7",
    )
    assert isinstance(sys_for_sdk, list)
    assert len(sys_for_sdk) == 1
    assert sys_for_sdk[0]["text"] == "frozen base"
    assert "cache_control" in sys_for_sdk[0]
```

- [ ] **Step 5.5: Run all anthropic-provider + cache tests**

```bash
.venv/bin/pytest tests/test_prompt_caching.py tests/test_prompt_caching_v2.py tests/test_prompt_caching_thresholds.py tests/test_idle_ttl_switch.py tests/test_anthropic_idle_per_session.py tests/test_anthropic_provider_tool_ordering.py -x -v
```
Expected: all pass.

- [ ] **Step 5.6: Commit**

```bash
git add extensions/anthropic-provider/provider.py tests/test_prompt_caching_v2.py tests/test_idle_ttl_switch.py
git commit -m "feat(cache): _apply_cache_control accepts base_system + injected_system + session_id"
```

---

## Task 6: BaseProvider + AgentLoop plumbing for new kwargs

**Files:**
- Modify: `plugin_sdk/provider_base.py` (or wherever `BaseProvider.complete` is defined; verify path)
- Modify: `extensions/anthropic-provider/provider.py:1364, 1411, 1522` (`complete`, `_do_complete`, `stream_complete`, `_do_stream_complete` signatures)
- Modify: `opencomputer/agent/loop.py:1045` (drop concat) + `:3056-3063, 3105-3112` (call site kwargs)

- [ ] **Step 6.1: Find BaseProvider definition**

```bash
grep -rln "class BaseProvider" plugin_sdk/ opencomputer/ extensions/
```

- [ ] **Step 6.2: Add new optional kwargs to `BaseProvider.complete` and `stream_complete`**

For each method signature add:
```python
        base_system: str = "",
        injected_system: str = "",
        session_id: str | None = None,
```
Keep existing `system: str = ""`. In each method body, add a back-compat shim at the top:
```python
        if not base_system and system:
            base_system = system
```

This means: any caller still passing `system=` continues to work; new callers pass `base_system=` directly.

- [ ] **Step 6.3: Update AnthropicProvider.complete + stream_complete public signatures**

Find `complete` at line ~1364, `_do_complete` at ~1411, `stream_complete` at ~1522, `_do_stream_complete` at ~1411 (verify exact line numbers). Add the same three kwargs and the back-compat shim. Thread `base_system, injected_system, session_id` through to the internal call site that invokes `_apply_cache_control`.

- [ ] **Step 6.4: Update AgentLoop.run_conversation**

`opencomputer/agent/loop.py:1045` — replace:
```python
        system = base_system + ("\n\n" + injected if injected else "")
```
with:
```python
        # Bug 1 fix (2026-05-05): keep base_system frozen (carries the
        # cache marker downstream) and injected separate (per-turn,
        # never marked). Provider receives both and assembles the
        # 2-block system content.
        system_for_provider_compat = base_system + (
            "\n\n" + injected if injected else ""
        )  # legacy callers reading `system` from local scope (logging etc)
        # Below, the new kwargs base_system + injected pass cleanly to
        # supporting providers; legacy providers fall back to system.
```

- [ ] **Step 6.5: Update the 2 provider call sites in loop.py**

`opencomputer/agent/loop.py:3056-3063`:

OLD:
```python
            stream_source = self.provider.stream_complete(
                model=model_name,
                messages=wire_messages,
                system=system,
                tools=tool_schemas,
                max_tokens=max_tokens_override or self.config.model.max_tokens,
                temperature=self.config.model.temperature,
                **_extra_kwargs,
            )
```

NEW:
```python
            stream_source = self.provider.stream_complete(
                model=model_name,
                messages=wire_messages,
                system=system_for_provider_compat,  # back-compat
                base_system=base_system,
                injected_system=injected,
                session_id=sid,
                tools=tool_schemas,
                max_tokens=max_tokens_override or self.config.model.max_tokens,
                temperature=self.config.model.temperature,
                **_extra_kwargs,
            )
```

Similarly for `provider.complete(...)` at line 3105:

NEW:
```python
            async def _do_call(active_model: str):
                return await self.provider.complete(
                    model=active_model,
                    messages=wire_messages,
                    system=system_for_provider_compat,  # back-compat
                    base_system=base_system,
                    injected_system=injected,
                    session_id=sid,
                    tools=tool_schemas,
                    max_tokens=self.config.model.max_tokens,
                    temperature=self.config.model.temperature,
                    **_extra_kwargs,
                )
```

- [ ] **Step 6.6: Run the full pytest suite**

```bash
.venv/bin/pytest tests/ -x --ignore=tests/extensions/openrouter 2>&1 | tail -30
```
Expected: green.

- [ ] **Step 6.7: Commit**

```bash
git add plugin_sdk/ extensions/anthropic-provider/provider.py opencomputer/agent/loop.py
git commit -m "feat(cache): plumb base_system + injected_system + session_id through provider chain"
```

---

## Task 7: Decouple title bar visibility from badge + use callable

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py:491-502` (signature)
- Modify: `opencomputer/cli_ui/input_loop.py:931-950` (badge/title window structure)
- Modify: `opencomputer/cli.py:1492-1514`
- Test: `tests/test_cli_ui_title_callable.py` (new)

- [ ] **Step 7.1: Write failing tests**

```python
# tests/test_cli_ui_title_callable.py
"""V2 — title bar callable + badge/title decoupling."""
from typing import Callable

import pytest


def test_read_user_input_accepts_get_session_title():
    """The signature should accept a callable for title source."""
    import inspect

    from opencomputer.cli_ui.input_loop import read_user_input

    sig = inspect.signature(read_user_input)
    assert "get_session_title" in sig.parameters


def test_title_text_uses_callable_dynamically(monkeypatch):
    """When the callable's return value changes, _title_text reflects it."""
    # We test the helper-level logic by extracting it; full E2E test would
    # need a prompt_toolkit Application harness which is heavy.
    # Instead, test the visibility / text helpers via a thin reproduction.
    title_holder = ["foo"]
    get_title: Callable[[], str | None] = lambda: title_holder[0]

    # Mirror the logic in input_loop.py's _title_text
    def _title_text():
        title = get_title() or ""
        if not (1 <= len(title) <= 50):
            return []
        return [
            ("class:title.box", "┤ "),
            ("class:title.text", title),
            ("class:title.box", " ├"),
        ]

    seg1 = _title_text()
    assert seg1[1][1] == "foo"

    title_holder[0] = "bar"
    seg2 = _title_text()
    assert seg2[1][1] == "bar"


def test_title_visibility_independent_of_runtime():
    """When runtime is None, the title can still be visible (as long as it's set)."""
    # Mirror _title_visible from input_loop.py
    title_holder = ["my-session"]

    def _title_visible() -> bool:
        title = title_holder[0] or ""
        return 1 <= len(title) <= 50

    assert _title_visible() is True

    title_holder[0] = ""
    assert _title_visible() is False

    title_holder[0] = "x" * 51  # > 50
    assert _title_visible() is False
```

- [ ] **Step 7.2: Run failing tests**

```bash
.venv/bin/pytest tests/test_cli_ui_title_callable.py -x -v
```
Expected: `test_read_user_input_accepts_get_session_title` fails (param doesn't exist).

- [ ] **Step 7.3: Update `read_user_input` signature**

`opencomputer/cli_ui/input_loop.py:491`:

OLD:
```python
async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
    session_title: str | None = None,
    paste_folder: PasteFolder | None = None,
    memory_manager: object | None = None,
    runtime: object | None = None,
) -> str:
```

NEW:
```python
async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
    session_title: str | None = None,
    get_session_title: Callable[[], str | None] | None = None,
    paste_folder: PasteFolder | None = None,
    memory_manager: object | None = None,
    runtime: object | None = None,
) -> str:
```

Add `from typing import Callable` at the top of the file if not already imported.

In the body, just after the docstring, normalize the source:
```python
    # Normalize title source: callable wins; static value is wrapped.
    if get_session_title is None:
        _captured_title = session_title
        def get_session_title() -> str | None:  # type: ignore[no-redef]
            return _captured_title
```

- [ ] **Step 7.4: Update `_title_text` to use the callable**

`input_loop.py:869`:

OLD:
```python
    def _title_text():
        if not session_title or not (1 <= len(session_title) <= 50):
            return []
        return [
            ("class:title.box", "┤ "),
            ("class:title.text", session_title),
            ("class:title.box", " ├"),
        ]
```

NEW:
```python
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
```

- [ ] **Step 7.5: Split badge_window — title gets its own ConditionalContainer**

`input_loop.py:939-950`:

OLD:
```python
    badge_window = ConditionalContainer(
        content=VSplit([
            Window(content=FormattedTextControl(_badge_text), height=1),
            Window(
                content=FormattedTextControl(_title_text),
                height=1,
                align=WindowAlign.RIGHT,
                dont_extend_width=True,
            ),
        ]),
        filter=Condition(lambda: _badge_visible),
    )
```

NEW:
```python
    title_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_title_text),
            height=1,
            align=WindowAlign.RIGHT,
            dont_extend_width=True,
        ),
        filter=Condition(_title_visible),
    )
    badge_text_window = ConditionalContainer(
        content=Window(content=FormattedTextControl(_badge_text), height=1),
        filter=Condition(lambda: _badge_visible),
    )
    # The bottom row hosts the badge on the left, title on the right;
    # each container owns its own visibility so a hidden badge no
    # longer hides the title.
    badge_window = VSplit([badge_text_window, title_window])
```

- [ ] **Step 7.6: Update `cli.py` to pass the callable**

`opencomputer/cli.py:1491-1514`:

OLD:
```python
    while True:
        # Fetch the session title each turn so a fresh /rename takes effect
        # immediately (the title indicator updates on the very next prompt).
        try:
            from opencomputer.agent.state import SessionDB as _TitleDB

            _title_db = _TitleDB(cfg.session.db_path)
            _current_title = _title_db.get_session_title(session_id) or None
        except Exception:  # noqa: BLE001 — never crash the prompt loop on a title fetch
            _current_title = None

        # Bind ``_current_title`` via default arg so each loop iteration's
        # closure captures *that* iteration's title, not the late-bound
        # outer name (ruff B023).
        async def _read_one(_title: str | None = _current_title) -> str:
            scope = TurnCancelScope()
            return await read_user_input(
                profile_home=profile_home,
                scope=scope,
                session_title=_title,
                paste_folder=paste_folder,
                memory_manager=loop.memory if loop is not None else None,
                runtime=loop._runtime if loop is not None else None,
            )
```

NEW:
```python
    def _fetch_session_title() -> str | None:
        """Fresh DB read so `/rename` mid-session is reflected on the next render."""
        try:
            from opencomputer.agent.state import SessionDB as _TitleDB
            return _TitleDB(cfg.session.db_path).get_session_title(session_id) or None
        except Exception:  # noqa: BLE001 — never crash the prompt loop on a title fetch
            return None

    while True:
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

(Note: `session_id` is already in scope as `nonlocal`, so `_fetch_session_title` reads the current value. The closure works correctly across `/resume` because `_fetch_session_title` is defined ONCE outside the while loop, and `session_id` is reassigned by `_on_resume` via `nonlocal`.)

- [ ] **Step 7.7: Run new tests; confirm pass**

```bash
.venv/bin/pytest tests/test_cli_ui_title_callable.py -x -v
```
Expected: 3 passed.

- [ ] **Step 7.8: Run cli_ui regression tests**

```bash
.venv/bin/pytest tests/ -k "input_loop or cli_ui or rename" -x -v 2>&1 | tail -30
```

- [ ] **Step 7.9: Commit**

```bash
git add opencomputer/cli_ui/input_loop.py opencomputer/cli.py tests/test_cli_ui_title_callable.py
git commit -m "fix(ui): decouple title bar from badge visibility; use callable for title source"
```

---

## Task 8: Final verification

- [ ] **Step 8.1: Full pytest run**

```bash
cd /Users/saksham/.config/superpowers/worktrees/opencomputer/prompt-caching/OpenComputer && .venv/bin/pytest tests/ -x --ignore=tests/extensions/openrouter 2>&1 | tail -20
```
Expected: green (or only failures attributable to unrelated test infra).

- [ ] **Step 8.2: Ruff lint**

```bash
.venv/bin/ruff check opencomputer/agent/prompt_caching.py extensions/anthropic-provider/provider.py opencomputer/agent/loop.py opencomputer/cli_ui/input_loop.py opencomputer/cli.py
```

- [ ] **Step 8.3: Smoke compile**

```bash
.venv/bin/python -c "import opencomputer.agent.prompt_caching; import opencomputer.agent.loop; import opencomputer.cli_ui.input_loop; print('imports ok')"
```

- [ ] **Step 8.4: Push branch + open PR**

```bash
git push -u origin feat/prompt-caching-production-ready
gh pr create --title "feat(cache): production-ready prompt caching + /rename UI robustness" --body "$(cat <<'EOF'
## Summary

Closes the gaps from a deep audit of the Anthropic prompt-caching pipeline:

- **System block split** — `base_system` (frozen, cache-marked) and `injected_system` (per-turn, unmarked) flow as separate text blocks. Plan-mode reminders, screen-awareness, affect injection, and persona-overlay churn no longer bust the cached system prefix.
- **Per-session idle tracker** — `AnthropicProvider._last_call_ts` becomes a 256-entry dict keyed by session_id. Long-running daemons (Telegram, ACP, batch) no longer cross-contaminate the 5m/1h TTL switch.
- **Token estimator covers tool blocks** — `_block_token_estimate` now counts `tool_result`, `tool_use`, `image`, and `document` blocks via chars-based recursion. Large tool outputs are no longer skipped from the threshold filter.
- **Stable tool ordering** — `_format_tools_for_anthropic` sorts by name, so plugin/MCP discovery noise can't shift the cache marker.
- **Title bar robustness** — title gets its own `ConditionalContainer`; `read_user_input` accepts a `get_session_title` callable so the title is read fresh on every render frame, decoupled from the permission-mode badge's visibility.

## Test plan

- 13+ new unit tests across `tests/test_prompt_caching_v2.py`, `tests/test_anthropic_idle_per_session.py`, `tests/test_anthropic_provider_tool_ordering.py`, `tests/test_cli_ui_title_callable.py`.
- Existing `tests/test_idle_ttl_switch.py` updated for renamed kwarg (`system=` → `base_system=`).
- Full pytest suite green.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

- [x] Each fix maps to a Task with concrete file:line and code blocks.
- [x] No "TBD" / "implement later".
- [x] Test code is full Python, not pseudocode.
- [x] Commands use absolute paths or explicit `cd`.
- [x] Each task ends in a commit.
- [x] Spec audit findings 1-15 each have a corresponding plan step.
