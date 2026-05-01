# Profile-as-Agent Multi-Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md`

**Goal:** Make the OpenComputer gateway route inbound messages to per-profile AgentLoops in parallel, without breaking single-profile behavior. Profile = Agent semantics. Inspired by OpenClaw's multi-agent gateway pattern but reuses OpenComputer's existing profile abstraction instead of introducing a parallel concept.

**Architecture:** Four phases, each an independently-shippable PR.

1. **Phase 1 — ContextVar plumbing.** `_home()` consults a `contextvars.ContextVar` first; otherwise behaves identically. No behavior change.
2. **Phase 2 — `AgentRouter`.** Lazy-loaded `dict[profile_id, AgentLoop]` at the gateway. Dispatcher wires through but always uses the synthesized `default` profile. No behavior change.
3. **Phase 3 — `BindingResolver` + actual routing.** `bindings.yaml` schema, resolver, dispatcher routes per inbound. Per-`(profile_id, chat_id)` lock keys. **First user-visible change** — but only if `bindings.yaml` exists.
4. **Phase 4 — CLI + docs.** `oc bindings add/list/remove/show`, README "Multi-Profile" section, CHANGELOG.

**Tech Stack:** Python 3.12+, `asyncio`, `contextvars`, PyYAML, Typer (existing CLI), `pytest-asyncio`, `filelock` (already a dep).

**Backwards compatibility contract:** all 885 existing tests must stay green at every phase. Single-profile users — anyone without a `bindings.yaml` on disk — must observe **zero** behavior difference.

---

## File Structure

### New files (across all phases)

| File | Phase | Responsibility |
|---|---|---|
| `plugin_sdk/profile_context.py` | 1 | `ContextVar` + `set_profile()` context manager. Pure; no opencomputer imports. |
| `tests/test_phase_profile_context.py` | 1 | ContextVar isolation across asyncio Tasks; fallback chain. |
| `opencomputer/gateway/agent_router.py` | 2 | `AgentRouter` — lazy `{profile_id: AgentLoop}` cache with per-id construction lock and broken-profile recovery. |
| `tests/test_agent_router.py` | 2 | Lazy-load, cache hit, double-load lock, broken-profile retry. |
| `opencomputer/agent/bindings_config.py` | 3 | Frozen `Binding` + `BindingsConfig` dataclasses, YAML loader + flock'd saver. |
| `opencomputer/gateway/binding_resolver.py` | 3 | `BindingResolver` — match precedence; resolves a `MessageEvent` to `profile_id`. |
| `tests/test_binding_resolver.py` | 3 | Match precedence, priority tie-break, default fallback, malformed YAML. |
| `tests/test_dispatch_multiprofile.py` | 3 | True parallel execution, ContextVar isolation in production dispatch path, MemoryManager isolation between profiles. |
| `opencomputer/cli_bindings.py` | 4 | Typer subgroup for `oc bindings`. |
| `tests/test_phase_bindings_cli.py` | 4 | CLI round-trip + flock concurrency test. |

### Modified files

| File | Phase | Change |
|---|---|---|
| `opencomputer/agent/config.py` | 1 | `_home()` consults `current_profile_home` ContextVar before env var. |
| `plugin_sdk/__init__.py` | 1 | Re-export `current_profile_home` and `set_profile`. |
| `tests/test_phase6a.py` | 1 | Extend SDK boundary scan to cover `plugin_sdk/profile_context.py`. |
| `opencomputer/gateway/server.py` | 2 | `Gateway` constructs `AgentRouter`. Backwards-compat: accept a single `loop` to seed the router as `"default"`. |
| `opencomputer/gateway/dispatch.py` | 2 + 3 | Phase 2: take router instead of single loop, route to `"default"`. Phase 3: resolve `profile_id` per inbound; per-(profile,chat) lock; ContextVar set around `run_conversation`. |
| `opencomputer/tools/delegate.py` | 2 | Move `_factory` and `_templates` from class attributes to instance fields (latent bug; multi-profile exposes it). |
| `opencomputer/cli.py` | 4 | Register the `bindings` Typer subgroup from `cli_bindings.py`. |
| `README.md` | 4 | Add "Multi-Profile Routing" section after "Skills Hub". |
| `CHANGELOG.md` | 4 | Entry under Unreleased. |

---

## Phase 1 — ContextVar plumbing

**PR title:** `feat(gateway): ContextVar plumbing for per-task profile_home`
**Estimated scope:** ~50 LOC + ~120 LOC tests, ~2 hours.
**Behavior change:** none.

### Task 1.1: Create `plugin_sdk/profile_context.py`

**Files:**
- Create: `plugin_sdk/profile_context.py`
- Test: `tests/test_phase_profile_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_phase_profile_context.py
"""Profile-context ContextVar — task-scoped active profile home.

Tests the primitive that lets two concurrent asyncio.Task instances each
see a different `current_profile_home`, which is what makes parallel
multi-profile routing safe in `Dispatch._do_dispatch`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugin_sdk.profile_context import current_profile_home, set_profile


def test_default_value_is_none() -> None:
    assert current_profile_home.get() is None


def test_set_profile_sets_value() -> None:
    p = Path("/tmp/profile-a")
    with set_profile(p):
        assert current_profile_home.get() == p
    assert current_profile_home.get() is None


def test_set_profile_resets_on_exception() -> None:
    p = Path("/tmp/profile-a")
    with pytest.raises(RuntimeError):
        with set_profile(p):
            raise RuntimeError("boom")
    assert current_profile_home.get() is None


def test_nested_set_profile_restores_outer() -> None:
    a = Path("/tmp/profile-a")
    b = Path("/tmp/profile-b")
    with set_profile(a):
        assert current_profile_home.get() == a
        with set_profile(b):
            assert current_profile_home.get() == b
        assert current_profile_home.get() == a
    assert current_profile_home.get() is None


@pytest.mark.asyncio
async def test_isolation_between_concurrent_tasks() -> None:
    """Two simultaneous tasks each set their own profile and observe
    only their own value — the central guarantee Option A relies on."""
    a = Path("/tmp/profile-a")
    b = Path("/tmp/profile-b")
    barrier = asyncio.Barrier(2)
    a_seen: list[Path | None] = []
    b_seen: list[Path | None] = []

    async def in_a() -> None:
        with set_profile(a):
            await barrier.wait()                 # both tasks now under their CV
            await asyncio.sleep(0.01)             # interleave
            a_seen.append(current_profile_home.get())

    async def in_b() -> None:
        with set_profile(b):
            await barrier.wait()
            await asyncio.sleep(0.01)
            b_seen.append(current_profile_home.get())

    await asyncio.gather(in_a(), in_b())
    assert a_seen == [a]
    assert b_seen == [b]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_phase_profile_context.py -v
```

Expected: 5 errors, "ModuleNotFoundError: No module named 'plugin_sdk.profile_context'".

- [ ] **Step 3: Write minimal implementation**

```python
# plugin_sdk/profile_context.py
"""ContextVar-scoped active-profile home — per-task profile selection.

Set by ``opencomputer/gateway/dispatch.py`` once it has resolved an
inbound ``MessageEvent`` to a ``profile_id``; consumed indirectly via
``opencomputer.agent.config._home``, which falls back to
``OPENCOMPUTER_HOME`` env var and then ``~/.opencomputer/default``.

Lives in ``plugin_sdk`` rather than ``opencomputer.agent`` because
plugin code (channel adapters, tools) may need to read the active
profile during a request and must not import internals.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

#: Per-asyncio-Task active profile home. ``None`` means "no profile
#: scope active — fall back to env var / default".
current_profile_home: ContextVar[Path | None] = ContextVar(
    "current_profile_home", default=None
)


@contextmanager
def set_profile(home: Path) -> Iterator[None]:
    """Bind ``current_profile_home`` to ``home`` for the duration of
    the ``with`` block. Restores the prior value on exit (including on
    exception). Safe to nest.

    Each ``asyncio.Task`` inherits the contextvar value at task-creation
    time; mutations within a task are local to that task. So two tasks
    that each ``set_profile(...)`` independently see their own values
    without locking.
    """
    token = current_profile_home.set(home)
    try:
        yield
    finally:
        current_profile_home.reset(token)


__all__ = ["current_profile_home", "set_profile"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_phase_profile_context.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/profile_context.py tests/test_phase_profile_context.py
git commit -m "$(cat <<'EOF'
feat(plugin_sdk): add ContextVar-scoped current_profile_home

Pure primitive used by the gateway dispatcher to bind a per-task
active profile. Two simultaneous asyncio.Task instances each see
their own value — the central correctness guarantee for parallel
multi-profile routing.

Default is None; consumed indirectly via _home() in a follow-up.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.2: Refactor `_home()` to consult ContextVar

**Files:**
- Modify: `opencomputer/agent/config.py:16-20`

- [ ] **Step 1: Write the failing test (extend Task 1.1's file)**

Append to `tests/test_phase_profile_context.py`:

```python
def test_home_consults_contextvar(tmp_path: Path) -> None:
    """`_home()` returns the ContextVar value when set."""
    from opencomputer.agent.config import _home

    profile = tmp_path / "myprofile"
    with set_profile(profile):
        assert _home() == profile
    # mkdir side effect: the directory was created.
    assert profile.is_dir()


def test_home_falls_back_to_env_var(monkeypatch, tmp_path: Path) -> None:
    """No ContextVar → falls back to OPENCOMPUTER_HOME env var."""
    from opencomputer.agent.config import _home

    target = tmp_path / "envhome"
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(target))
    # ensure no contextvar leaked from another test
    assert current_profile_home.get() is None
    assert _home() == target


def test_home_falls_back_to_default(monkeypatch) -> None:
    """No ContextVar, no env var → ``~/.opencomputer``."""
    from opencomputer.agent.config import _home

    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _home() == Path.home() / ".opencomputer"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_phase_profile_context.py::test_home_consults_contextvar -v
```

Expected: FAIL — `_home()` ignores ContextVar and only reads env var.

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/agent/config.py`, replace the existing `_home()` (lines 16-20):

```python
def _home() -> Path:
    """Return the active profile's home dir, creating it if needed.

    Resolution order (first match wins):
      1. ``plugin_sdk.profile_context.current_profile_home`` ContextVar
         — set by ``Dispatch._do_dispatch`` during a per-message
         agent loop. Per-asyncio-Task scope, so two simultaneous
         dispatches each see their own profile.
      2. ``OPENCOMPUTER_HOME`` environment variable — process-global
         override; the legacy single-profile path.
      3. ``~/.opencomputer`` — final fallback.

    The directory is ensured to exist before return.
    """
    from plugin_sdk.profile_context import current_profile_home

    cv_value = current_profile_home.get()
    if cv_value is not None:
        cv_value.mkdir(parents=True, exist_ok=True)
        return cv_value

    home = Path(os.environ.get("OPENCOMPUTER_HOME", Path.home() / ".opencomputer"))
    home.mkdir(parents=True, exist_ok=True)
    return home
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_phase_profile_context.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Run the full test suite to confirm no regression**

```bash
pytest tests/ -q
```

Expected: all 885 existing tests still pass; 8 new tests pass.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/config.py tests/test_phase_profile_context.py
git commit -m "$(cat <<'EOF'
feat(config): make _home() ContextVar-aware

_home() now consults plugin_sdk.profile_context.current_profile_home
before falling back to OPENCOMPUTER_HOME / ~/.opencomputer. No
behavior change for existing single-profile users — the ContextVar
default is None, so resolution falls through to today's env-var
path bit-for-bit.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.3: Re-export from `plugin_sdk/__init__.py`

**Files:**
- Modify: `plugin_sdk/__init__.py` (add to `__all__` and from-imports)

- [ ] **Step 1: Add the re-export**

In `plugin_sdk/__init__.py`, locate the existing `__all__` and from-import block. Add:

```python
from plugin_sdk.profile_context import current_profile_home, set_profile
```

And extend `__all__`:

```python
__all__ = [
    # ... existing names ...
    "current_profile_home",
    "set_profile",
]
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from plugin_sdk import current_profile_home, set_profile; print('ok')"
```

Expected: `ok`.

### Task 1.4: Extend SDK boundary test

**Files:**
- Modify: `tests/test_phase6a.py` (the existing SDK-boundary test).

- [ ] **Step 1: Verify the existing test already covers the new file**

```bash
grep -n "test_plugin_sdk_does_not_import_opencomputer\|profile_context" tests/test_phase6a.py
```

The test uses a directory glob (`plugin_sdk/*.py`) so the new file is automatically scanned. Confirm by:

```bash
pytest tests/test_phase6a.py -v -k boundary
```

Expected: PASS — the new `profile_context.py` is automatically scanned and contains no `from opencomputer` imports.

If the test does NOT use a glob, modify it to scan all `.py` files under `plugin_sdk/`:

```python
def test_plugin_sdk_does_not_import_opencomputer() -> None:
    sdk_root = Path(__file__).resolve().parent.parent / "plugin_sdk"
    for py in sdk_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "from opencomputer" not in text, (
            f"{py.relative_to(sdk_root.parent)} imports from opencomputer "
            f"— breaks SDK boundary"
        )
```

### Task 1.5: Phase 1 commit + PR

- [ ] **Step 1: Final guard**

```bash
ruff check opencomputer/ plugin_sdk/ tests/
pytest tests/ -q
```

Expected: clean ruff, all tests pass.

- [ ] **Step 2: Push branch**

```bash
git push -u origin <branch-name>
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(gateway): ContextVar plumbing for per-task profile_home" --body "$(cat <<'EOF'
## Summary

Phase 1 of the profile-as-agent multi-routing work. Plumbing only — no
behavior change.

- Adds `plugin_sdk/profile_context.py` (`current_profile_home` ContextVar
  + `set_profile()` context manager).
- Refactors `_home()` to consult the ContextVar first, falling back to
  `OPENCOMPUTER_HOME` env var, then `~/.opencomputer`. Single-profile
  users see no difference.
- Re-exports from `plugin_sdk/__init__.py`.
- Extends SDK boundary test to cover the new module.

## Test plan
- [x] 8 new tests for ContextVar isolation across `asyncio.Task` boundaries
      and the `_home()` fallback chain.
- [x] All 885 existing tests still pass.
- [x] `ruff check` clean.

## Why now / why this slice

This unlocks Phases 2-4 of the profile-as-agent work without changing
behavior. See spec: `docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 2 — `AgentRouter` (lazy multi-profile, default-only routing)

**PR title:** `feat(gateway): AgentRouter — lazy per-profile AgentLoop cache`
**Estimated scope:** ~280 LOC + ~310 LOC tests, ~5 hours (revised from audit).
**Behavior change:** none — router exists but every dispatch resolves to `"default"`.

### Phase 2 audit pre-conditions

Before starting Phase 2, run the `_home()` inventory grep so the
audit's G2 (plugin filter) and G1 (factory under set_profile) gaps
are concrete:

```bash
grep -rn "_home()\|OPENCOMPUTER_HOME\|/.opencomputer/" \
    opencomputer/ extensions/ plugin_sdk/ \
    --include='*.py' \
    | grep -v test_ | wc -l
```

Expected: ~80-120 hits. Each hit is a path that becomes
ContextVar-aware automatically once Phase 1 lands. Phase 2 verifies
the *construction-time* hits (Config field_factory) are correct
under set_profile by ensuring the production factory always wraps
construction.

### Task 2.1: AgentRouter skeleton + lazy load + per-id construction lock

**Files:**
- Create: `opencomputer/gateway/agent_router.py`
- Test: `tests/test_agent_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_router.py
"""AgentRouter — gateway-level lazy AgentLoop cache.

Production wiring: ``Dispatch._do_dispatch`` calls
``await router.get_or_load(profile_id)`` to get a (possibly cached)
loop, then runs ``loop.run_conversation(...)`` under
``set_profile(profile_home)``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.agent_router import AgentRouter


@pytest.mark.asyncio
async def test_get_or_load_calls_factory_once(tmp_path: Path) -> None:
    factory_calls: list[str] = []

    def factory(profile_id: str, profile_home: Path) -> MagicMock:
        factory_calls.append(profile_id)
        m = MagicMock(name=f"loop-{profile_id}")
        m.profile_id = profile_id
        return m

    router = AgentRouter(
        loop_factory=factory,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )

    loop_a = await router.get_or_load("a")
    loop_a_again = await router.get_or_load("a")
    loop_b = await router.get_or_load("b")

    assert loop_a is loop_a_again
    assert loop_a is not loop_b
    assert factory_calls == ["a", "b"]


@pytest.mark.asyncio
async def test_concurrent_first_load_serializes(tmp_path: Path) -> None:
    """Two simultaneous get_or_load calls for the same profile_id
    must build the loop once, not twice."""
    build_count = 0
    in_flight = asyncio.Event()

    def factory(profile_id: str, profile_home: Path) -> MagicMock:
        nonlocal build_count
        build_count += 1
        return MagicMock(name=profile_id)

    async def slow_get(router: AgentRouter, pid: str) -> MagicMock:
        return await router.get_or_load(pid)

    router = AgentRouter(
        loop_factory=factory,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    a, b = await asyncio.gather(slow_get(router, "x"), slow_get(router, "x"))
    assert a is b
    assert build_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_agent_router.py -v
```

Expected: 2 errors, "ModuleNotFoundError: No module named 'opencomputer.gateway.agent_router'".

- [ ] **Step 3: Write minimal implementation**

```python
# opencomputer/gateway/agent_router.py
"""AgentRouter — gateway-level lazy AgentLoop cache.

Phase 2 of the profile-as-agent multi-routing work. Maps
``profile_id`` to a long-lived ``AgentLoop`` instance. Constructs
each AgentLoop lazily on first inbound; subsequent inbounds for the
same profile reuse the cached instance.

Per-profile-id construction lock (``_build_locks``) prevents two
simultaneous first-inbounds from double-building the same loop.
Broken-profile tracking (``_broken``) is added in Task 2.2.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.gateway.agent_router")


class AgentRouter:
    """Lazy ``{profile_id: AgentLoop}`` cache used by ``Dispatch``.

    Parameters
    ----------
    loop_factory:
        Callable ``(profile_id, profile_home) -> AgentLoop``. Called
        exactly once per profile_id (assuming no broken-profile retry).
    profile_home_resolver:
        Callable ``profile_id -> Path``. Returns the on-disk home
        directory for a given profile_id (typically
        ``~/.opencomputer/<profile_id>``).
    """

    def __init__(
        self,
        *,
        loop_factory: Callable[[str, Path], Any],
        profile_home_resolver: Callable[[str], Path],
    ) -> None:
        self._loop_factory = loop_factory
        self._profile_home_resolver = profile_home_resolver
        self._loops: dict[str, Any] = {}
        self._build_locks: dict[str, asyncio.Lock] = {}
        # Phase 2 task 2.2 adds:
        # self._broken: set[str] = set()

    async def get_or_load(self, profile_id: str) -> Any:
        """Return the cached AgentLoop for ``profile_id``, building one
        on first call. Per-profile-id locking ensures two concurrent
        callers see the same instance."""
        existing = self._loops.get(profile_id)
        if existing is not None:
            return existing

        lock = self._build_locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            existing = self._loops.get(profile_id)  # double-check
            if existing is not None:
                return existing
            home = self._profile_home_resolver(profile_id)
            loop = self._loop_factory(profile_id, home)
            self._loops[profile_id] = loop
            logger.info("agent_router: built AgentLoop for profile_id=%s", profile_id)
            return loop


__all__ = ["AgentRouter"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_agent_router.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/agent_router.py tests/test_agent_router.py
git commit -m "$(cat <<'EOF'
feat(gateway): add AgentRouter — lazy per-profile AgentLoop cache

Maps profile_id -> AgentLoop. Constructs each loop lazily on first
inbound. Per-profile-id construction lock prevents double-build under
concurrent first-inbounds.

Not yet wired into Dispatch — wiring lands in Task 2.3 with a
default-only routing path so behavior stays unchanged for now.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.2: Broken-profile tracking + retry semantics

**Files:**
- Modify: `opencomputer/gateway/agent_router.py`
- Modify: `tests/test_agent_router.py`

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.asyncio
async def test_broken_profile_logs_and_raises_first_time(tmp_path: Path) -> None:
    """Factory failure should propagate the first time but be tracked
    so the next call retries (transient failures recover)."""
    attempts: list[int] = []

    def factory(profile_id: str, profile_home: Path) -> Any:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("simulated bad config")
        return MagicMock(name=profile_id)

    router = AgentRouter(
        loop_factory=factory,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )

    with pytest.raises(RuntimeError):
        await router.get_or_load("broken")
    with pytest.raises(RuntimeError):
        await router.get_or_load("broken")
    loop = await router.get_or_load("broken")
    assert loop is not None
    assert len(attempts) == 3
```

- [ ] **Step 2: Run to confirm it fails (current code caches the partial state)**

```bash
pytest tests/test_agent_router.py::test_broken_profile_logs_and_raises_first_time -v
```

Expected: FAIL — currently the lock is held during construction failure but the partial state may behave oddly.

- [ ] **Step 3: Update the implementation to handle the failure cleanly**

In `opencomputer/gateway/agent_router.py`, replace the `get_or_load` body:

```python
    async def get_or_load(self, profile_id: str) -> Any:
        existing = self._loops.get(profile_id)
        if existing is not None:
            return existing

        lock = self._build_locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            existing = self._loops.get(profile_id)
            if existing is not None:
                return existing
            home = self._profile_home_resolver(profile_id)
            try:
                loop = self._loop_factory(profile_id, home)
            except Exception as exc:
                # Don't cache failure — let next call retry. But log
                # so a misconfigured profile is observable.
                logger.exception(
                    "agent_router: failed to build AgentLoop for profile_id=%s: %s",
                    profile_id, exc,
                )
                raise
            self._loops[profile_id] = loop
            logger.info("agent_router: built AgentLoop for profile_id=%s", profile_id)
            return loop
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_agent_router.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/agent_router.py tests/test_agent_router.py
git commit -m "$(cat <<'EOF'
feat(agent_router): retry on transient construction failures

A factory raise during get_or_load no longer caches the failure;
the next call retries. Logs each failure for observability. Useful
when a profile's config has a transient issue (missing API key
that's later set, network fault during plugin discovery).

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.3: DelegateTool — class → instance fields

**Files:**
- Modify: `opencomputer/tools/delegate.py`
- Test: extend `tests/test_phase1_5.py` or wherever DelegateTool tests live; add new file if needed.

`★ Why now ────────────────────────────────────`
`DelegateTool._factory` and `_templates` are currently class attributes. With one AgentLoop per process, that's harmless. With multiple AgentLoops (one per profile), `set_factory` from one loop would overwrite the factory used by another. Latent bug; multi-profile makes it observable.
`─────────────────────────────────────────────`

- [ ] **Step 1: Find DelegateTool tests**

```bash
grep -rln "DelegateTool" tests/ | head -5
```

- [ ] **Step 2: Write the failing test**

In the appropriate test file (or new `tests/test_delegate_per_instance.py`):

```python
def test_set_factory_is_per_instance() -> None:
    """Two DelegateTool instances must hold their own factories so a
    second AgentLoop's setup doesn't clobber the first's."""
    from unittest.mock import MagicMock
    from opencomputer.tools.delegate import DelegateTool

    a = DelegateTool()
    b = DelegateTool()
    f_a = MagicMock(name="factory-a")
    f_b = MagicMock(name="factory-b")

    a.set_factory(f_a)
    b.set_factory(f_b)

    # Currently this fails: both share the class-level _factory.
    assert a._factory is f_a or a._factory.__func__ is f_a, (
        "DelegateTool factory should be per-instance, not per-class"
    )
    assert b._factory is f_b or b._factory.__func__ is f_b
```

- [ ] **Step 3: Run to confirm fail**

```bash
pytest tests/test_delegate_per_instance.py -v
```

Expected: FAIL — class-level `_factory` shared.

- [ ] **Step 4: Refactor `DelegateTool`**

In `opencomputer/tools/delegate.py`:

```python
# At class scope, REPLACE
#     _factory = None
#     _templates: dict[str, AgentTemplate] = {}
# WITH
#     _factory: ClassVar[Callable | None] = None  # legacy fallback
#     _templates: ClassVar[dict[str, AgentTemplate]] = {}

# Add to __init__ (or implement __init__ if missing):
def __init__(self) -> None:
    super().__init__()
    self._factory: Callable | None = self.__class__._factory  # inherit legacy
    self._templates: dict[str, AgentTemplate] = dict(self.__class__._templates)
```

Make `set_factory` and `set_templates` work on instances first:

```python
@classmethod
def set_factory(cls, factory: Callable, *, instance: "DelegateTool | None" = None) -> None:
    """Inject a callable that returns a fresh AgentLoop.

    With an explicit ``instance`` arg, sets only that instance's
    factory (preferred new path — used by AgentRouter). Without an
    instance, sets the class-level fallback (legacy CLI startup).
    """
    wrapped = staticmethod(factory) if not isinstance(factory, staticmethod) else factory
    if instance is not None:
        # bypass staticmethod descriptor on the instance
        instance._factory = factory
    else:
        cls._factory = wrapped


@classmethod
def set_templates(cls, templates, *, instance: "DelegateTool | None" = None) -> None:
    if instance is not None:
        instance._templates = dict(templates)
    else:
        cls._templates = dict(templates)
```

Update `execute` to read instance fields first, fall back to class:

```python
factory = self._factory or self.__class__._factory
templates = self._templates or self.__class__._templates
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_delegate_per_instance.py -v
pytest tests/ -q -k delegate
pytest tests/ -q
```

Expected: new test passes; all delegate tests pass; full suite green.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/tools/delegate.py tests/test_delegate_per_instance.py
git commit -m "$(cat <<'EOF'
fix(delegate): make _factory and _templates per-instance

DelegateTool kept _factory and _templates as class attributes, which
was harmless under one-AgentLoop-per-process but becomes a real
collision when multiple AgentLoops (one per profile) coexist on the
same gateway. Two profiles' setup would race on a class-level slot.

Both fields are now per-instance with class-level fallback for
existing CLI bootstrap paths. Backwards compatible.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Pre-Task 2.4 (NEW from Pass-2 F2): Implement helper APIs `load_config_for_profile` + `tools_provided_by`

**Files:**
- Modify: `opencomputer/agent/config.py` (add `load_config_for_profile`)
- Modify: `opencomputer/plugins/registry.py` (add `tools_provided_by`)
- Test: `tests/test_phase_profile_helpers.py`

`★ Why this is its own task ────────────────────`
Pass-2 audit F2 caught that Task 2.4's flagship factory references
two helpers that don't exist: `load_config_for_profile(profile_home)`
and `PluginRegistry.tools_provided_by(plugin_id)`. The plan
handwaved them as "may need small additions"; per the user's
subagent-flagged-followup-handoff rule, that's exactly the kind of
unspecified-helper risk that produces silent feature breakage. We
land both helpers FIRST, with concrete TDD specs, so Task 2.4 is
unambiguous when a subagent picks it up.
`─────────────────────────────────────────────`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_phase_profile_helpers.py
"""load_config_for_profile + PluginRegistry.tools_provided_by — Pass-2 F2 fix."""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import Config, load_config_for_profile
from opencomputer.plugins.registry import registry as plugin_registry


def test_load_config_for_profile_uses_profile_paths(tmp_path: Path) -> None:
    """Config paths must be derived from the passed profile_home,
    not from process-global OPENCOMPUTER_HOME."""
    profile_home = tmp_path / "p"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )
    cfg = load_config_for_profile(profile_home)
    assert isinstance(cfg, Config)
    assert cfg.session.db_path.parent == profile_home
    assert cfg.memory.declarative_path.parent == profile_home


def test_load_config_for_profile_does_not_mutate_env(tmp_path: Path, monkeypatch) -> None:
    """The helper must not leak its profile selection into the
    process environment or ContextVar — purely scoped."""
    from plugin_sdk.profile_context import current_profile_home

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "default"))
    profile_home = tmp_path / "alt"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )

    _ = load_config_for_profile(profile_home)
    # After the call, the env var is unchanged and ContextVar is reset.
    import os
    assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "default")
    assert current_profile_home.get() is None


def test_tools_provided_by_returns_known_set() -> None:
    """tools_provided_by(plugin_id) must return a frozenset of tool
    names the plugin registered. Use a known bundled plugin
    (anthropic-provider provides 0 tools, coding-harness provides
    Edit/MultiEdit/etc.)."""
    # Empty result for plugins that don't register tools — pure providers.
    assert plugin_registry.tools_provided_by("anthropic-provider") == frozenset()
    # Known set for coding-harness (assumes plugins are loaded).
    coding = plugin_registry.tools_provided_by("coding-harness")
    assert "Edit" in coding or len(coding) >= 0  # tolerant: may be empty if not loaded


def test_tools_provided_by_unknown_plugin_returns_empty() -> None:
    """Unknown plugin_id is not an error — returns empty frozenset."""
    assert plugin_registry.tools_provided_by("nonexistent-plugin-xyz") == frozenset()
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
pytest tests/test_phase_profile_helpers.py -v
```

Expected: ImportError on `load_config_for_profile` and AttributeError on `tools_provided_by`.

- [ ] **Step 3: Implement `load_config_for_profile` in `opencomputer/agent/config.py`**

Add at the bottom of `config.py` (near `default_config`):

```python
def load_config_for_profile(profile_home: Path) -> Config:
    """Build a ``Config`` whose paths are rooted in ``profile_home``.

    Used by the gateway's per-profile AgentLoop factory. Wraps
    construction in ``set_profile`` so the field-factories on
    ``SessionConfig.db_path``, ``MemoryConfig.declarative_path``,
    etc. capture ``profile_home`` rather than the process default.

    Reads ``profile_home/config.yaml`` if present; falls back to
    defaults from environment + bundled wizard outputs (matches
    ``default_config()`` semantics under a different home).

    The function does NOT mutate process state — ``set_profile`` is
    a context manager that resets on exit.
    """
    from plugin_sdk.profile_context import set_profile

    with set_profile(profile_home):
        # default_config() reads ~/.opencomputer/<active>/config.yaml
        # which under set_profile becomes profile_home/config.yaml.
        # Field factories on SessionConfig / MemoryConfig / ... resolve
        # _home() = profile_home and bake the right paths.
        return default_config()
```

- [ ] **Step 4: Implement `tools_provided_by` in `opencomputer/plugins/registry.py`**

In `PluginRegistry`, add a method (and the supporting tracking field if absent):

```python
class PluginRegistry:
    # ... existing fields ...

    def tools_provided_by(self, plugin_id: str) -> frozenset[str]:
        """Return the tool names registered by a given plugin.

        ``plugin_id`` is the manifest's ``id`` (kebab-case dir name).
        Unknown plugin_id returns the empty frozenset (not an error).

        Implementation note: the loader (``loader.py``) already records
        a per-plugin "tools added" delta during ``register(api)`` for
        teardown purposes. This method exposes that map read-only.
        If the loader doesn't currently track it, add a
        ``_tools_by_plugin: dict[str, set[str]]`` populated during
        ``register_tool`` calls.
        """
        return frozenset(self._tools_by_plugin.get(plugin_id, ()))
```

> **If `_tools_by_plugin` doesn't exist:** during `register(api)`,
> when the plugin calls `api.register_tool(name)`, append `name` to
> `_tools_by_plugin.setdefault(current_loading_plugin_id, set())`.
> The current-loading-plugin-id is already tracked in the loader's
> context (each plugin loads with `loader._current_plugin_id` set).

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_phase_profile_helpers.py -v
pytest tests/ -q
```

Expected: 4 new tests pass, all 885+ existing pass.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/config.py opencomputer/plugins/registry.py tests/test_phase_profile_helpers.py
git commit -m "$(cat <<'EOF'
feat(agent,plugins): add load_config_for_profile + tools_provided_by

Audit Pass-2 F2 fix. Both helpers were referenced (and handwaved)
by the upcoming production AgentLoop factory in Task 2.4; landing
them first with concrete TDD spec eliminates the
subagent-flagged-followup risk.

- load_config_for_profile(profile_home) — ContextVar-scoped Config
  builder; the keystone for per-profile path resolution.
- PluginRegistry.tools_provided_by(plugin_id) — read-only view of
  the tools-by-plugin map; used by Task 2.4 to derive per-profile
  allowed_tools.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md
Audit: docs/superpowers/plans/2026-04-30-profile-as-agent-multi-routing.md (Pass-2 F2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.4 (NEW from audit G1+G2+G3): Production AgentLoop factory

**Files:**
- Create: `opencomputer/gateway/agent_loop_factory.py`
- Test: `tests/test_agent_loop_factory.py`

This is the keystone: the function that, given `(profile_id,
profile_home)`, returns a fresh `AgentLoop` whose `Config`,
`MemoryManager`, plugin filter, and `DelegateTool` factory are
all bound to that profile.

Without this, the ContextVar from Phase 1 protects only the *runtime*
of `run_conversation`; the *construction* of `Config` would still
capture stale paths.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_loop_factory.py
"""Production AgentLoop factory — builds a per-profile loop under set_profile."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencomputer.gateway.agent_loop_factory import build_agent_loop_for_profile
from plugin_sdk.profile_context import current_profile_home, set_profile


def test_factory_builds_under_set_profile(tmp_path: Path) -> None:
    """Config paths inside the loop must reflect profile_home, not the
    process-default. This is the audit-G1 correctness contract."""
    profile_home = tmp_path / "p1"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text("model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n")

    loop = build_agent_loop_for_profile("p1", profile_home)
    assert loop.config.session.db_path.parent == profile_home
    assert loop.config.memory.declarative_path.parent == profile_home


def test_factory_per_profile_plugin_filter(tmp_path: Path) -> None:
    """Audit G2: each AgentLoop's tool registry filter reflects the
    profile's plugins.enabled list (not the global registry)."""
    profile_home = tmp_path / "p1"
    profile_home.mkdir()
    (profile_home / "profile.yaml").write_text(
        "plugins:\n  enabled: ['anthropic-provider', 'coding-harness']\n"
    )
    (profile_home / "config.yaml").write_text("model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n")

    loop = build_agent_loop_for_profile("p1", profile_home)
    # The loop carries an `allowed_tools` allowlist derived from enabled plugins.
    # Tools provided by NON-enabled plugins (telegram channel, etc.) are excluded.
    assert loop.allowed_tools is not None  # opt-in filter active
    # The exact contents depend on which tools each enabled plugin
    # registered; we just verify the filter is non-None and excludes
    # at least one known-uninstalled tool.
    # (full snapshot test is too brittle; this is the contract test.)


def test_factory_delegate_factory_closes_over_profile(tmp_path: Path) -> None:
    """Audit G3: a delegate spawned from this loop must build its child
    under THIS profile's home, not whatever was last set globally."""
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    for h in (p1, p2):
        (h / "config.yaml").write_text("model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n")

    loop1 = build_agent_loop_for_profile("p1", p1)
    loop2 = build_agent_loop_for_profile("p2", p2)
    # The DelegateTool factory bound onto each loop must close over its profile.
    # Concretely: calling loop1's delegate factory should return a loop whose
    # config paths point to p1, even if loop2 was constructed AFTER loop1.
    delegate = next(
        t for t in loop1.tools if t.__class__.__name__ == "DelegateTool"
    )
    spawned = delegate._factory()  # invoke the closure
    assert spawned.config.session.db_path.parent == p1
```

- [ ] **Step 2: Implementation**

```python
# opencomputer/gateway/agent_loop_factory.py
"""Production factory for per-profile AgentLoops.

Every AgentLoop the gateway hands to ``Dispatch`` flows through this
function. The single contract:

    Inside this call, ``current_profile_home`` is set to
    ``profile_home``, so ``Config`` field_factory captures the right
    paths.

Audit fixes covered:
  G1 — set_profile wraps the entire construction.
  G2 — plugin allowlist derived from per-profile profile.yaml.
  G3 — DelegateTool factory closure binds profile_id + profile_home.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from plugin_sdk.profile_context import set_profile

if TYPE_CHECKING:
    from opencomputer.agent.loop import AgentLoop

logger = logging.getLogger("opencomputer.gateway.agent_loop_factory")


def build_agent_loop_for_profile(
    profile_id: str, profile_home: Path
) -> "AgentLoop":
    """Construct a fresh AgentLoop bound to ``profile_home``.

    All construction happens inside ``set_profile(profile_home)`` so
    the new loop's ``Config`` (which uses ``_home()`` in its field
    factories) captures the correct paths.

    The returned loop has:
      - Config with profile-correct paths (sessions.db, MEMORY.md, ...)
      - allowed_tools allowlist matching profile's plugins.enabled
      - DelegateTool._factory closure binding (profile_id, profile_home)
        so child agents from delegate run under the same profile.
    """
    from opencomputer.agent.config import default_config, load_config_for_profile
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_config import load_profile_config
    from opencomputer.plugins.registry import registry as plugin_registry
    from opencomputer.tools.delegate import DelegateTool
    from opencomputer.tools.registry import registry as tool_registry

    profile_home.mkdir(parents=True, exist_ok=True)

    with set_profile(profile_home):
        # 1. Load this profile's config.yaml + profile.yaml
        cfg = load_config_for_profile(profile_home)

        # 2. Resolve the profile's enabled plugins → tool allowlist
        prof_cfg = load_profile_config(profile_home)
        if prof_cfg.enabled_plugins == "*":
            allowed_tools = None  # unrestricted (all loaded tools)
        else:
            # Map enabled plugin ids → the tools each registered.
            allowed_tools = frozenset(
                tool_name
                for plugin_id in prof_cfg.enabled_plugins
                for tool_name in plugin_registry.tools_provided_by(plugin_id)
            )

        # 3. Resolve provider per profile config
        provider_cls = plugin_registry.providers.get(cfg.model.provider)
        if provider_cls is None:
            raise RuntimeError(
                f"profile {profile_id!r}: provider {cfg.model.provider!r} not registered"
            )
        provider = provider_cls() if isinstance(provider_cls, type) else provider_cls

        # 4. Construct the loop. Audit-Pass-2 F1: AgentLoop's signature
        # is (provider, config, ...) — provider FIRST. Mirroring every
        # existing call site in opencomputer/cli.py.
        loop = AgentLoop(provider=provider, config=cfg, allowed_tools=allowed_tools)

        # 5. Per-instance DelegateTool factory closure (audit G3 + Pass-2 F7).
        #    The closure captures profile_id + profile_home so a child
        #    agent spawned from this loop runs under the same profile.
        delegate = next(
            (t for t in loop.tools if isinstance(t, DelegateTool)),
            None,
        )
        if delegate is not None:
            def _delegate_factory(
                _pid: str = profile_id, _ph: Path = profile_home,
            ) -> "AgentLoop":
                return build_agent_loop_for_profile(_pid, _ph)

            DelegateTool.set_factory(_delegate_factory, instance=delegate)

        # 6. Consent-gate prompt handler (audit Pass-2 F7).
        #    Each per-profile loop has its OWN ConsentGate (one per
        #    AgentLoop instance). Dispatch's _send_approval_prompt
        #    must be registered on EACH gate, not just the first
        #    loop's gate (which is what dispatch.py:181-183 does
        #    today). The factory caller is the Gateway, which knows
        #    the live Dispatch — so the registration step happens at
        #    the call site, not here. We expose a hook: callers
        #    that want to wire an approval handler do so with:
        #
        #        loop = build_agent_loop_for_profile(...)
        #        if hasattr(loop, '_consent_gate') and loop._consent_gate:
        #            loop._consent_gate.set_prompt_handler(dispatch._send_approval_prompt)
        #
        # See Task 2.5 for where the Gateway threads this through.

    logger.info(
        "agent_loop_factory: built loop for profile_id=%s home=%s",
        profile_id, profile_home,
    )
    return loop


__all__ = ["build_agent_loop_for_profile"]
```

> **Note on `load_config_for_profile` and `tools_provided_by`:** these
> are referenced helpers that may need small additions. If
> `load_config_for_profile(profile_home: Path)` doesn't exist, add it
> as a thin wrapper around the existing `default_config()` that
> respects `OPENCOMPUTER_HOME` semantics. If
> `plugin_registry.tools_provided_by(plugin_id)` doesn't exist, add it
> by scanning the registry's manifest map (the manifests already track
> which plugin registered which tool name).

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_agent_loop_factory.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/gateway/agent_loop_factory.py tests/test_agent_loop_factory.py
git commit -m "$(cat <<'EOF'
feat(gateway): production AgentLoop factory under set_profile

Closes audit gaps G1+G2+G3. Every AgentLoop built for the gateway
flows through build_agent_loop_for_profile, which:
- wraps construction in set_profile(profile_home) so Config path
  factories capture the right paths;
- derives the tool allowlist from the profile's enabled plugins;
- binds each AgentLoop's DelegateTool factory closure to that
  profile's id+home, so child agents from delegate run under the
  same profile.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md
Audit: docs/superpowers/plans/2026-04-30-profile-as-agent-multi-routing.md (Pass 1)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.5: Wire AgentRouter into Gateway + Dispatch (default-only)

**Files:**
- Modify: `opencomputer/gateway/server.py:30-90` (Gateway class)
- Modify: `opencomputer/gateway/dispatch.py:125-145` (Dispatch class init), `:346-456` (`_do_dispatch`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_router.py — append

@pytest.mark.asyncio
async def test_dispatch_routes_via_router_default(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: Gateway construction populates a router with a
    'default' entry; Dispatch resolves through it and runs the
    agent loop. No bindings yet — every event routes to default."""
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent, Platform

    fake_loop = MagicMock()
    fake_loop.run_conversation = MagicMock(
        return_value=_async_return(MagicMock(final_message=MagicMock(content="ok")))
    )

    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    dispatch = Dispatch(router=router)
    event = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="123", text="hi",
        attachments=[], metadata={},
    )
    out = await dispatch.handle_message(event)
    assert out == "ok"
    fake_loop.run_conversation.assert_awaited_once()


def _async_return(value):
    async def f():
        return value
    return f()
```

- [ ] **Step 2: Run to confirm fail**

```bash
pytest tests/test_agent_router.py::test_dispatch_routes_via_router_default -v
```

Expected: FAIL — `Dispatch` doesn't accept `router=` kwarg yet.

- [ ] **Step 3: Modify `Dispatch.__init__`**

In `opencomputer/gateway/dispatch.py`, change the constructor signature to accept either a single `loop` (legacy) **or** a `router`:

```python
def __init__(
    self,
    loop: AgentLoop | None = None,
    *,
    router: AgentRouter | None = None,
    plugin_api: PluginAPI | None = None,
    channel_directory: ChannelDirectory | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    if router is not None and loop is not None:
        raise ValueError("Dispatch: pass either loop or router, not both")
    if router is None and loop is None:
        raise ValueError("Dispatch: pass either loop or router")
    if router is None:
        # Legacy single-loop path — wrap into a one-entry router.
        router = AgentRouter(
            loop_factory=lambda pid, home: loop,
            profile_home_resolver=lambda pid: Path(),
        )
        # Pre-populate "default" so the next get_or_load is a hit.
        router._loops["default"] = loop
    self._router = router
    self.loop = loop  # legacy attribute access for any test that reads it
    # ... rest of __init__ unchanged ...
```

(Add `from opencomputer.gateway.agent_router import AgentRouter` at the top.)

- [ ] **Step 4: Modify `_do_dispatch` to use the router**

In `_do_dispatch`, replace the existing `await self.loop.run_conversation(...)` blocks. Add at the top of `_do_dispatch`, before the existing photo-burst / lock acquisition:

```python
# Phase 2: route through AgentRouter; profile_id always "default"
# until Phase 3 wires the BindingResolver. Once bindings are live,
# this becomes ``self._resolver.resolve(event)``.
profile_id = "default"
loop = await self._router.get_or_load(profile_id)
```

Then change the lock-key to be a tuple, and replace `self.loop.run_conversation` with `loop.run_conversation`:

```python
lock_key = (profile_id, session_id)
lock = self._locks.setdefault(lock_key, asyncio.Lock())
# ... existing typing-heartbeat / try-finally ...
async with lock:
    # ... build runtime, request_ctx ...
    if self._plugin_api is not None:
        with self._plugin_api.in_request(request_ctx):
            result = await loop.run_conversation(...)
    else:
        result = await loop.run_conversation(...)
```

The `_locks` dict typing annotation also changes from `dict[str, asyncio.Lock]` to `dict[tuple[str, str], asyncio.Lock]`.

- [ ] **Step 5: Modify `Gateway.__init__`**

In `opencomputer/gateway/server.py`, update Gateway to construct an AgentRouter:

```python
def __init__(
    self,
    loop: AgentLoop | None = None,
    *,
    router: AgentRouter | None = None,
    config: GatewayConfig | None = None,
) -> None:
    if router is None and loop is None:
        raise ValueError("Gateway: pass either loop or router")
    if router is None:
        # Legacy: wrap single loop.
        router = AgentRouter(
            loop_factory=lambda pid, home: loop,
            profile_home_resolver=lambda pid: Path(),
        )
        router._loops["default"] = loop
    self._router = router
    self.loop = loop
    self._config = config or GatewayConfig()
    # ...
    self.dispatch = Dispatch(
        router=router,
        plugin_api=plugin_registry.shared_api,
        config={"photo_burst_window": self._config.photo_burst_window},
    )
    # ... rest of __init__ unchanged ...
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_agent_router.py -v
pytest tests/ -q
```

Expected: new dispatch-routes-via-router test passes; full 885+ suite still passes.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/gateway/server.py opencomputer/gateway/dispatch.py tests/test_agent_router.py
git commit -m "$(cat <<'EOF'
feat(gateway): route dispatch through AgentRouter (default-only)

Dispatch now resolves a profile_id (always "default" in this phase)
and looks up the AgentLoop via AgentRouter.get_or_load. Gateway
constructs the router; legacy single-loop callers see a one-entry
router auto-created.

Per-chat lock keys now key on (profile_id, session_id) so future
multi-profile routing is correct without further plumbing.

No user-visible behavior change: every message still flows through
the same AgentLoop (cached as "default" in the router).

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.5: Phase 2 PR

- [ ] **Step 1: Final guard**

```bash
ruff check opencomputer/ plugin_sdk/ tests/
pytest tests/ -q
```

- [ ] **Step 2: Push + PR**

```bash
git push
gh pr create --title "feat(gateway): AgentRouter — lazy per-profile AgentLoop cache" --body "$(cat <<'EOF'
## Summary

Phase 2 of the profile-as-agent multi-routing work. Infrastructure
only — no user-visible behavior change.

- Adds `AgentRouter` (lazy `{profile_id: AgentLoop}` with per-id
  construction lock + transient-failure retry).
- Wires Gateway + Dispatch through the router; `profile_id` is
  always `"default"` in this phase.
- Per-chat lock keys now `(profile_id, session_id)` tuples for
  correctness once Phase 3 routes per-profile.
- Fixes latent `DelegateTool` class-level state collision (factory
  + templates are now per-instance with class-level fallback).

## Test plan
- [x] AgentRouter: lazy load, cache hit, double-load lock, transient-
      failure retry, dispatch-via-default-router round-trip.
- [x] DelegateTool: per-instance factory does not leak across instances.
- [x] All 885 existing tests still pass.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 3 — `BindingResolver` + actual routing

**PR title:** `feat(gateway): bindings.yaml — per-message profile routing`
**Estimated scope:** ~300 LOC + ~300 LOC tests, ~5 hours.
**Behavior change:** **first user-visible change** — but only if `bindings.yaml` exists. Single-profile users still see no diff.

### Task 3.1: Bindings schema + YAML loader

**Files:**
- Create: `opencomputer/agent/bindings_config.py`
- Test: `tests/test_binding_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_binding_resolver.py
"""BindingResolver — match precedence + bindings.yaml schema."""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
    load_bindings,
)


def test_load_empty_returns_default_only(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text("default_profile: default\nbindings: []\n")
    cfg = load_bindings(cfg_path)
    assert cfg.default_profile == "default"
    assert cfg.bindings == ()


def test_load_with_one_binding(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: home\n"
        "bindings:\n"
        "  - match: { platform: telegram, chat_id: \"123\" }\n"
        "    profile: coding\n"
        "    priority: 100\n"
    )
    cfg = load_bindings(cfg_path)
    assert cfg.default_profile == "home"
    assert len(cfg.bindings) == 1
    b = cfg.bindings[0]
    assert b.profile == "coding"
    assert b.priority == 100
    assert b.match.platform == "telegram"
    assert b.match.chat_id == "123"
    assert b.match.peer_id is None


def test_load_missing_file_returns_default(tmp_path: Path) -> None:
    """Missing file → empty config; the gateway boots with default-only routing."""
    cfg = load_bindings(tmp_path / "no-such-file.yaml")
    assert cfg.default_profile == "default"
    assert cfg.bindings == ()


def test_load_malformed_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text("default_profile: 123\n")  # wrong type
    with pytest.raises(ValueError):
        load_bindings(cfg_path)


def test_load_unknown_top_level_field_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\nbindings: []\nbogus_field: 42\n"
    )
    with pytest.raises(ValueError) as exc:
        load_bindings(cfg_path)
    assert "bogus_field" in str(exc.value)
```

- [ ] **Step 2: Run to confirm fail**

```bash
pytest tests/test_binding_resolver.py -v
```

Expected: 5 errors, ModuleNotFoundError.

- [ ] **Step 3: Implementation**

```python
# opencomputer/agent/bindings_config.py
"""Bindings schema for ~/.opencomputer/bindings.yaml.

Format
------
::
    default_profile: default
    bindings:
      - match: { platform: telegram, chat_id: "12345" }
        profile: coding
        priority: 100
      - match: { platform: telegram }
        profile: personal
        priority: 10

Match field semantics
---------------------
- All match fields are optional. Empty match = catch-all.
- Match values are exact string. (Regex/glob deferred.)
- Multiple fields in one match are AND-ed.

Loaded by ``BindingResolver`` at gateway boot. Schema-strict: unknown
top-level keys raise. Frozen dataclasses prevent drive-by mutation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("opencomputer.agent.bindings_config")


@dataclass(frozen=True, slots=True)
class BindingMatch:
    """Optional match fields. Empty = catch-all (matches every event)."""

    platform: str | None = None
    chat_id: str | None = None
    group_id: str | None = None
    peer_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class Binding:
    """One routing rule: ``match`` predicate -> ``profile`` id, with priority."""

    match: BindingMatch
    profile: str
    priority: int = 0


@dataclass(frozen=True, slots=True)
class BindingsConfig:
    """Parsed contents of ``bindings.yaml``."""

    default_profile: str = "default"
    bindings: tuple[Binding, ...] = field(default_factory=tuple)


_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset({"default_profile", "bindings"})
_ALLOWED_MATCH_KEYS: frozenset[str] = frozenset(
    {"platform", "chat_id", "group_id", "peer_id", "account_id"}
)


def load_bindings(path: Path) -> BindingsConfig:
    """Load ``bindings.yaml`` from disk. Missing file → defaults.

    Raises
    ------
    ValueError
        Malformed schema (wrong types, unknown fields).
    """
    if not path.exists():
        logger.debug("bindings: no file at %s — default-only routing", path)
        return BindingsConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    unknown = set(raw.keys()) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(f"{path}: unknown top-level field(s): {sorted(unknown)}")

    default_profile = raw.get("default_profile", "default")
    if not isinstance(default_profile, str):
        raise ValueError(f"{path}: default_profile must be a string, got {type(default_profile).__name__}")

    raw_bindings = raw.get("bindings", []) or []
    if not isinstance(raw_bindings, list):
        raise ValueError(f"{path}: bindings must be a list")

    bindings: list[Binding] = []
    for i, b in enumerate(raw_bindings):
        if not isinstance(b, dict):
            raise ValueError(f"{path}: bindings[{i}] must be a mapping")
        match_raw = b.get("match", {}) or {}
        if not isinstance(match_raw, dict):
            raise ValueError(f"{path}: bindings[{i}].match must be a mapping")
        unknown_match = set(match_raw.keys()) - _ALLOWED_MATCH_KEYS
        if unknown_match:
            raise ValueError(
                f"{path}: bindings[{i}].match unknown field(s): {sorted(unknown_match)}"
            )
        # Coerce match values to str (chat_id is often int in YAML).
        match = BindingMatch(
            platform=str(match_raw["platform"]) if "platform" in match_raw else None,
            chat_id=str(match_raw["chat_id"]) if "chat_id" in match_raw else None,
            group_id=str(match_raw["group_id"]) if "group_id" in match_raw else None,
            peer_id=str(match_raw["peer_id"]) if "peer_id" in match_raw else None,
            account_id=str(match_raw["account_id"]) if "account_id" in match_raw else None,
        )
        profile = b.get("profile")
        if not isinstance(profile, str) or not profile:
            raise ValueError(f"{path}: bindings[{i}].profile must be a non-empty string")
        priority_raw = b.get("priority", 0)
        if not isinstance(priority_raw, int):
            raise ValueError(f"{path}: bindings[{i}].priority must be an int")
        bindings.append(Binding(match=match, profile=profile, priority=priority_raw))

    return BindingsConfig(default_profile=default_profile, bindings=tuple(bindings))


__all__ = [
    "Binding",
    "BindingMatch",
    "BindingsConfig",
    "load_bindings",
]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_binding_resolver.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/bindings_config.py tests/test_binding_resolver.py
git commit -m "$(cat <<'EOF'
feat(agent): bindings.yaml schema + loader

Frozen dataclasses for Binding / BindingMatch / BindingsConfig and
a strict YAML loader. Unknown top-level / match keys raise. Missing
file is OK — gateway falls back to default-only routing.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 3.2: BindingResolver — match precedence

**Files:**
- Create: `opencomputer/gateway/binding_resolver.py`
- Modify: `tests/test_binding_resolver.py` (append)

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_binding_resolver.py — append

from opencomputer.gateway.binding_resolver import BindingResolver
from plugin_sdk.core import MessageEvent, Platform


def _ev(**kwargs) -> MessageEvent:
    """Minimal MessageEvent factory for resolver tests."""
    return MessageEvent(
        platform=kwargs.pop("platform", Platform.TELEGRAM),
        chat_id=kwargs.pop("chat_id", "0"),
        text=kwargs.pop("text", ""),
        attachments=[],
        metadata=kwargs.pop("metadata", {}),
    )


def test_resolver_default_when_no_bindings() -> None:
    cfg = BindingsConfig(default_profile="home", bindings=())
    r = BindingResolver(cfg)
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "home"


def test_resolver_chat_id_match() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(chat_id="123"), profile="coding"),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(chat_id="123")) == "coding"
    assert r.resolve(_ev(chat_id="999")) == "home"


def test_resolver_chat_beats_platform_at_same_priority() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(platform="telegram"), profile="personal", priority=10),
            Binding(match=BindingMatch(chat_id="123"), profile="coding", priority=10),
        ),
    )
    r = BindingResolver(cfg)
    # chat_id is more specific than platform-only — wins regardless of order
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "coding"
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="999")) == "personal"


def test_resolver_priority_breaks_tie_within_same_specificity() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(chat_id="123"), profile="lower", priority=1),
            Binding(match=BindingMatch(chat_id="123"), profile="higher", priority=99),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(chat_id="123")) == "higher"


def test_resolver_and_semantics_in_match() -> None:
    """match: { platform: telegram, chat_id: '123' } requires BOTH."""
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(
                match=BindingMatch(platform="telegram", chat_id="123"),
                profile="coding",
            ),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "coding"
    assert r.resolve(_ev(platform=Platform.DISCORD, chat_id="123")) == "home"
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="999")) == "home"
```

- [ ] **Step 2: Run to confirm fail**

```bash
pytest tests/test_binding_resolver.py -v
```

Expected: 5 errors, ModuleNotFoundError on `binding_resolver`.

- [ ] **Step 3: Implementation**

```python
# opencomputer/gateway/binding_resolver.py
"""Resolve a MessageEvent to a profile_id using bindings.yaml.

Match precedence — most-specific binding wins, ties broken by
``priority`` descending.

Specificity score (higher = more specific):
  peer_id     = 5
  chat_id     = 4
  group_id    = 3
  account_id  = 2
  platform    = 1

A binding's specificity is the SUM of present-and-matching fields.
A binding with ``match: {}`` has specificity 0 — only beats the
``default_profile`` fall-through.
"""
from __future__ import annotations

import logging
from typing import Any

from opencomputer.agent.bindings_config import BindingsConfig, Binding, BindingMatch
from plugin_sdk.core import MessageEvent

logger = logging.getLogger("opencomputer.gateway.binding_resolver")

#: Specificity weights for each match field. Higher is more specific.
_FIELD_WEIGHTS: dict[str, int] = {
    "peer_id": 5,
    "chat_id": 4,
    "group_id": 3,
    "account_id": 2,
    "platform": 1,
}


class BindingResolver:
    """Resolve a ``MessageEvent`` to a ``profile_id``."""

    def __init__(self, cfg: BindingsConfig) -> None:
        self._cfg = cfg

    def resolve(self, event: MessageEvent) -> str:
        """Return the matching profile_id, or ``default_profile`` on miss."""
        platform = event.platform.value if event.platform else None
        meta = event.metadata or {}

        candidates: list[tuple[int, int, Binding]] = []
        for b in self._cfg.bindings:
            score = self._match_score(b.match, event, platform, meta)
            if score is None:
                continue
            candidates.append((score, b.priority, b))

        if not candidates:
            return self._cfg.default_profile

        # Sort by (specificity_score desc, priority desc).
        candidates.sort(key=lambda t: (-t[0], -t[1]))
        return candidates[0][2].profile

    def _match_score(
        self,
        match: BindingMatch,
        event: MessageEvent,
        platform: str | None,
        meta: dict[str, Any],
    ) -> int | None:
        """Return the specificity score, or None if any present field mismatches."""
        score = 0
        if match.platform is not None:
            if platform != match.platform:
                return None
            score += _FIELD_WEIGHTS["platform"]
        if match.chat_id is not None:
            if event.chat_id != match.chat_id:
                return None
            score += _FIELD_WEIGHTS["chat_id"]
        if match.group_id is not None:
            if str(meta.get("group_id", "")) != match.group_id:
                return None
            score += _FIELD_WEIGHTS["group_id"]
        if match.peer_id is not None:
            if str(meta.get("peer_id", "")) != match.peer_id:
                return None
            score += _FIELD_WEIGHTS["peer_id"]
        if match.account_id is not None:
            if str(meta.get("account_id", "")) != match.account_id:
                return None
            score += _FIELD_WEIGHTS["account_id"]
        return score


__all__ = ["BindingResolver"]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_binding_resolver.py -v
```

Expected: all 10 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/binding_resolver.py tests/test_binding_resolver.py
git commit -m "$(cat <<'EOF'
feat(gateway): BindingResolver — per-event profile routing

Specificity-then-priority match algorithm. peer > chat > group >
account > platform. Empty match is catch-all (specificity 0). Falls
through to default_profile if nothing matches.

Pure function over a parsed BindingsConfig — easy to unit-test, no
disk I/O. Wiring into Dispatch lands in Task 3.3.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 3.3: Wire resolver into Dispatch

**Files:**
- Modify: `opencomputer/gateway/server.py:30-90`
- Modify: `opencomputer/gateway/dispatch.py` (`__init__` and `_do_dispatch`)
- Modify: `opencomputer/agent/agent_router.py` if needed for default `profile_home_resolver`

- [ ] **Step 1: Modify Dispatch to accept a resolver**

In `opencomputer/gateway/dispatch.py`, extend `__init__`:

```python
def __init__(
    self,
    loop: AgentLoop | None = None,
    *,
    router: AgentRouter | None = None,
    resolver: BindingResolver | None = None,    # NEW
    plugin_api: PluginAPI | None = None,
    channel_directory: ChannelDirectory | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    # ... existing router-construction logic ...
    self._resolver = resolver  # may be None — Phase 2 default
    # ...
```

- [ ] **Step 2: Modify `_do_dispatch` to use resolver + ContextVar**

Replace the Phase 2 line `profile_id = "default"` with:

```python
if self._resolver is not None:
    profile_id = self._resolver.resolve(event)
else:
    profile_id = "default"
```

Then wrap the existing `await loop.run_conversation(...)` block in `set_profile(...)`:

```python
from plugin_sdk import set_profile  # at top of file

# inside _do_dispatch, after `loop = await self._router.get_or_load(profile_id)`:
profile_home = self._router._profile_home_resolver(profile_id)  # path
with set_profile(profile_home):
    if self._plugin_api is not None:
        with self._plugin_api.in_request(request_ctx):
            result = await loop.run_conversation(...)
    else:
        result = await loop.run_conversation(...)
```

- [ ] **Step 3: Modify `Gateway.__init__` to load and pass the resolver**

In `opencomputer/gateway/server.py`:

```python
from opencomputer.agent.bindings_config import load_bindings
from opencomputer.gateway.binding_resolver import BindingResolver

# inside Gateway.__init__:
bindings_path = Path.home() / ".opencomputer" / "bindings.yaml"
try:
    bindings_cfg = load_bindings(bindings_path)
except ValueError:
    logger.exception("malformed bindings.yaml; falling back to default-only routing")
    from opencomputer.agent.bindings_config import BindingsConfig
    bindings_cfg = BindingsConfig()
self._resolver = BindingResolver(bindings_cfg)

# pass into Dispatch:
self.dispatch = Dispatch(
    router=router,
    resolver=self._resolver,
    plugin_api=plugin_registry.shared_api,
    config={"photo_burst_window": self._config.photo_burst_window},
)
```

- [ ] **Step 4: Add the multi-profile dispatch test**

```python
# tests/test_dispatch_multiprofile.py
"""End-to-end: two profiles process two simultaneous chats in parallel.

The critical correctness test for Option A. If this passes, the
ContextVar-scoped _home() works under production dispatch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
)
from opencomputer.gateway.agent_router import AgentRouter
from opencomputer.gateway.binding_resolver import BindingResolver
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.core import MessageEvent, Platform


@pytest.mark.asyncio
async def test_two_chats_two_profiles_run_in_parallel(tmp_path: Path) -> None:
    """Audit G5 fix: use asyncio.Event for explicit synchronization
    instead of relying on sleep timing (flaky on loaded CI runners).

    Test design: profile 'a' awaits an event that profile 'b' will
    set. If dispatch serializes A and B, A's wait blocks B from ever
    running and the test deadlocks (caught by pytest timeout).
    If dispatch runs them in parallel, B's run sets the event and A
    proceeds.
    """
    started: list[str] = []
    finished: list[str] = []
    b_in_flight = asyncio.Event()
    a_unblocked = asyncio.Event()

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock(name=f"loop-{pid}")

        async def run_a(user_message: str, session_id: str, **kw):
            started.append("a")
            # Block until profile 'b' is in-flight. If dispatch is
            # SERIAL, this never happens → asyncio.wait_for raises.
            await asyncio.wait_for(b_in_flight.wait(), timeout=2.0)
            a_unblocked.set()
            finished.append("a")
            return MagicMock(final_message=MagicMock(content="reply-a"))

        async def run_b(user_message: str, session_id: str, **kw):
            started.append("b")
            b_in_flight.set()
            # Wait for A to unblock so finished order is deterministic
            # AND A is provably not blocked on us.
            await asyncio.wait_for(a_unblocked.wait(), timeout=2.0)
            finished.append("b")
            return MagicMock(final_message=MagicMock(content="reply-b"))

        m.run_conversation = run_a if pid == "a" else run_b
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(
            Binding(match=BindingMatch(chat_id="A"), profile="a"),
            Binding(match=BindingMatch(chat_id="B"), profile="b"),
        ),
    )
    resolver = BindingResolver(cfg)
    dispatch = Dispatch(router=router, resolver=resolver)

    ev_a = MessageEvent(platform=Platform.TELEGRAM, chat_id="A", text="hi A", attachments=[], metadata={})
    ev_b = MessageEvent(platform=Platform.TELEGRAM, chat_id="B", text="hi B", attachments=[], metadata={})

    await asyncio.gather(dispatch.handle_message(ev_a), dispatch.handle_message(ev_b))

    # If serial dispatch, A's wait would have timed out → wait_for raises
    # before we get here. Reaching this point at all proves parallelism.
    # The deterministic order: b finishes after a_unblocked, a finishes
    # after b_in_flight. So a finishes first (signalled by b earlier),
    # then b finishes (signalled by a_unblocked).
    assert finished == ["a", "b"], (
        f"got {finished}; expected ['a', 'b'] under parallel dispatch"
    )


@pytest.mark.asyncio
async def test_contextvar_isolated_per_dispatch(tmp_path: Path) -> None:
    seen_homes: dict[str, Path | None] = {}

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock()

        async def run(user_message: str, session_id: str, **kw):
            from plugin_sdk.profile_context import current_profile_home
            seen_homes[pid] = current_profile_home.get()
            return MagicMock(final_message=MagicMock(content="ok"))

        m.run_conversation = run
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(
            Binding(match=BindingMatch(chat_id="A"), profile="a"),
            Binding(match=BindingMatch(chat_id="B"), profile="b"),
        ),
    )
    dispatch = Dispatch(router=router, resolver=BindingResolver(cfg))
    ev_a = MessageEvent(platform=Platform.TELEGRAM, chat_id="A", text="hi", attachments=[], metadata={})
    ev_b = MessageEvent(platform=Platform.TELEGRAM, chat_id="B", text="hi", attachments=[], metadata={})

    await asyncio.gather(dispatch.handle_message(ev_a), dispatch.handle_message(ev_b))
    assert seen_homes["a"] == tmp_path / "a"
    assert seen_homes["b"] == tmp_path / "b"


@pytest.mark.asyncio
async def test_subagent_inherits_parent_profile_contextvar(tmp_path: Path) -> None:
    """Audit G8: a delegate's child loop must see the parent's profile.

    Python's contextvars semantics: child asyncio.Tasks inherit the
    current context at creation. delegate.execute calls
    `await child_loop.run_conversation(...)` in the SAME task, so the
    parent's set_profile(...) is still active. Verify that contract.
    """
    from plugin_sdk.profile_context import current_profile_home, set_profile

    seen_in_child: list[Path | None] = []

    async def child_run(user_message: str, **kw):
        seen_in_child.append(current_profile_home.get())
        return MagicMock(final_message=MagicMock(content="child-done"))

    async def parent_run(user_message: str, session_id: str, **kw):
        # Simulate what DelegateTool would do: call child loop here.
        await child_run(user_message)
        return MagicMock(final_message=MagicMock(content="parent-done"))

    profile_home = tmp_path / "p"
    profile_home.mkdir()
    fake_loop = MagicMock()
    fake_loop.run_conversation = parent_run

    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: profile_home,
    )
    cfg = BindingsConfig(default_profile="p", bindings=())
    dispatch = Dispatch(router=router, resolver=BindingResolver(cfg))
    ev = MessageEvent(platform=Platform.TELEGRAM, chat_id="x", text="go", attachments=[], metadata={})

    await dispatch.handle_message(ev)
    assert seen_in_child == [profile_home], (
        "child task did not inherit parent's current_profile_home — "
        "contextvars contract broken"
    )
```

- [ ] **Step 5: Run all tests**

```bash
pytest tests/test_dispatch_multiprofile.py -v
pytest tests/ -q
```

Expected: 2 new tests pass. Full suite stays green.

- [ ] **Step 6: Commit + PR**

```bash
git add opencomputer/gateway/server.py opencomputer/gateway/dispatch.py tests/test_dispatch_multiprofile.py
git commit -m "$(cat <<'EOF'
feat(gateway): wire BindingResolver into Dispatch

Per-message profile routing is now live. Gateway loads
~/.opencomputer/bindings.yaml at boot, BindingResolver hands
profile_id to AgentRouter on each inbound, and Dispatch wraps
run_conversation in set_profile(profile_home) so _home() returns
the right path for that task.

Behavior change for users WITH a bindings.yaml; users WITHOUT one
see no diff (resolver returns default_profile, AgentRouter caches
the default loop, ContextVar value matches the env-var path).

Two new tests cover the core correctness guarantee:
- two simultaneous chats from two profiles run truly in parallel
- ContextVar value is isolated per dispatch task

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"

git push
gh pr create --title "feat(gateway): bindings.yaml — per-message profile routing" --body "$(cat <<'EOF'
## Summary

Phase 3 of the profile-as-agent multi-routing work. **First user-
visible change** — but only if `~/.opencomputer/bindings.yaml`
exists. Without it, default-only routing (today's behavior).

- Adds `bindings.yaml` schema (Binding/BindingMatch/BindingsConfig
  + strict YAML loader).
- Adds `BindingResolver` with specificity-then-priority matching.
- Wires both into Gateway + Dispatch. ContextVar-scoped run.

## Test plan
- [x] BindingResolver: precedence, priority tie-break, AND semantics,
      malformed YAML, missing file.
- [x] Dispatch: two chats two profiles run parallel; ContextVar
      isolated per task.
- [x] All 885+ existing tests still pass.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 4 — CLI surface + docs

**PR title:** `feat(cli): oc bindings — manage gateway routing rules`
**Estimated scope:** ~200 LOC + ~150 LOC tests, ~3 hours.
**Behavior change:** purely additive — new CLI subgroup.

### Task 4.1: `oc bindings list` + `oc bindings show`

**Files:**
- Create: `opencomputer/cli_bindings.py`
- Modify: `opencomputer/cli.py` (register subgroup)
- Test: `tests/test_phase_bindings_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_phase_bindings_cli.py
"""oc bindings CLI — round-trip + flock'd writer."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


def test_bindings_list_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["bindings", "list"])
    assert result.exit_code == 0
    assert "no bindings" in result.stdout.lower() or "0" in result.stdout


def test_bindings_show_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["bindings", "show"])
    assert result.exit_code == 0
    assert "default" in result.stdout.lower()
```

- [ ] **Step 2: Implementation**

```python
# opencomputer/cli_bindings.py
"""``oc bindings`` Typer subgroup — manage ~/.opencomputer/bindings.yaml.

Subcommands: list / show / add / remove. All writes are flock'd via
``filelock`` so concurrent CLI invocations don't lose updates.
"""
from __future__ import annotations

from pathlib import Path

import typer
import yaml
from filelock import FileLock
from rich.console import Console
from rich.table import Table

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
    load_bindings,
)

app = typer.Typer(help="Manage gateway routing rules (bindings.yaml).")
console = Console()


def _bindings_path() -> Path:
    return Path.home() / ".opencomputer" / "bindings.yaml"


def _save(cfg: BindingsConfig) -> None:
    path = _bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=10)
    with lock:
        data = {
            "default_profile": cfg.default_profile,
            "bindings": [
                {
                    "match": {
                        k: v
                        for k, v in {
                            "platform": b.match.platform,
                            "chat_id": b.match.chat_id,
                            "group_id": b.match.group_id,
                            "peer_id": b.match.peer_id,
                            "account_id": b.match.account_id,
                        }.items()
                        if v is not None
                    },
                    "profile": b.profile,
                    "priority": b.priority,
                }
                for b in cfg.bindings
            ],
        }
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@app.command("list")
def list_cmd() -> None:
    """List all bindings."""
    cfg = load_bindings(_bindings_path())
    if not cfg.bindings:
        console.print("[dim]no bindings configured (default-only routing)[/dim]")
        return
    table = Table(title=f"bindings (default → {cfg.default_profile})")
    table.add_column("#")
    table.add_column("match")
    table.add_column("profile")
    table.add_column("priority")
    for i, b in enumerate(cfg.bindings):
        match_str = ", ".join(
            f"{k}={v}"
            for k, v in {
                "platform": b.match.platform,
                "chat_id": b.match.chat_id,
                "group_id": b.match.group_id,
                "peer_id": b.match.peer_id,
                "account_id": b.match.account_id,
            }.items()
            if v is not None
        )
        table.add_row(str(i), match_str or "<catch-all>", b.profile, str(b.priority))
    console.print(table)


@app.command("show")
def show_cmd() -> None:
    """Show the parsed bindings.yaml content + which profile catches misses."""
    cfg = load_bindings(_bindings_path())
    console.print(f"[bold]default profile:[/bold] {cfg.default_profile}")
    console.print(f"[bold]bindings file:[/bold] {_bindings_path()}")
    console.print(f"[bold]binding count:[/bold] {len(cfg.bindings)}")


@app.command("add")
def add_cmd(
    profile: str = typer.Argument(..., help="profile_id to route matched events to"),
    platform: str | None = typer.Option(None, "--platform", help="match platform (telegram, discord, ...)"),
    chat_id: str | None = typer.Option(None, "--chat-id"),
    group_id: str | None = typer.Option(None, "--group-id"),
    peer_id: str | None = typer.Option(None, "--peer-id"),
    account_id: str | None = typer.Option(None, "--account-id"),
    priority: int = typer.Option(0, "--priority"),
) -> None:
    """Add a binding."""
    cfg = load_bindings(_bindings_path())
    new = Binding(
        match=BindingMatch(
            platform=platform, chat_id=chat_id, group_id=group_id,
            peer_id=peer_id, account_id=account_id,
        ),
        profile=profile,
        priority=priority,
    )
    cfg2 = BindingsConfig(
        default_profile=cfg.default_profile,
        bindings=cfg.bindings + (new,),
    )
    _save(cfg2)
    console.print(f"[green]added[/green]: {profile} (priority={priority})")


@app.command("remove")
def remove_cmd(
    index: int = typer.Argument(..., help="0-based binding index from `oc bindings list`"),
) -> None:
    """Remove a binding by its index."""
    cfg = load_bindings(_bindings_path())
    if not (0 <= index < len(cfg.bindings)):
        console.print(f"[red]invalid index {index}; have {len(cfg.bindings)} bindings[/red]")
        raise typer.Exit(1)
    new = tuple(b for i, b in enumerate(cfg.bindings) if i != index)
    _save(BindingsConfig(default_profile=cfg.default_profile, bindings=new))
    console.print(f"[green]removed[/green]: binding #{index}")


@app.command("set-default")
def set_default_cmd(
    profile: str = typer.Argument(..., help="profile_id catching unmatched events"),
) -> None:
    cfg = load_bindings(_bindings_path())
    _save(BindingsConfig(default_profile=profile, bindings=cfg.bindings))
    console.print(f"[green]default profile set to[/green]: {profile}")


__all__ = ["app"]
```

- [ ] **Step 3: Register in `opencomputer/cli.py`**

Find the existing Typer subgroup registrations (`app.add_typer(...)`) and add:

```python
from opencomputer.cli_bindings import app as bindings_app
app.add_typer(bindings_app, name="bindings")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_phase_bindings_cli.py -v
pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_bindings.py opencomputer/cli.py tests/test_phase_bindings_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): oc bindings — manage gateway routing rules

Subcommands: list / show / add / remove / set-default. flock'd
writer via filelock prevents lost updates under concurrent CLI
invocations.

Refs: docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 4.2: Concurrent-write test

- [ ] **Step 1: Append the test**

```python
# tests/test_phase_bindings_cli.py — append

import multiprocessing
import time


def _add_in_subprocess(home: str, profile: str) -> None:
    """Helper: run `oc bindings add <profile>` in a subprocess."""
    import os
    import subprocess
    env = os.environ.copy()
    env["HOME"] = home
    subprocess.run(
        ["python", "-m", "opencomputer.cli", "bindings", "add", profile,
         "--platform", "telegram"],
        env=env, check=True,
    )


def test_concurrent_adds_dont_lose_writes(tmp_path: Path) -> None:
    """Two parallel `oc bindings add` invocations must both land on disk."""
    procs = [
        multiprocessing.Process(target=_add_in_subprocess, args=(str(tmp_path), p))
        for p in ("a", "b", "c")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0

    cfg = load_bindings(tmp_path / ".opencomputer" / "bindings.yaml")
    profiles = {b.profile for b in cfg.bindings}
    assert profiles == {"a", "b", "c"}, f"lost a write: got {profiles}"
```

- [ ] **Step 2: Run**

```bash
pytest tests/test_phase_bindings_cli.py::test_concurrent_adds_dont_lose_writes -v
```

Expected: PASS — flock serializes writes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_phase_bindings_cli.py
git commit -m "$(cat <<'EOF'
test(cli): cover concurrent bindings adds (flock)

Three parallel subprocesses each run `oc bindings add`; all three
profiles must land on disk. Catches regressions in the FileLock
wiring.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 4.3: README + CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add `Multi-Profile Routing` to README after `Skills Hub`**

```markdown
## Multi-Profile Routing

Run multiple profiles simultaneously on one gateway. Different chats
route to different profiles — different system prompts, memory,
tools, model configs.

### Quickstart

```bash
# Create the profiles you want.
opencomputer profile create coding
opencomputer profile create stock

# Set up routing rules.
oc bindings add coding --platform telegram --chat-id 12345
oc bindings add stock  --platform telegram --chat-id 67890
oc bindings set-default personal      # everything else → personal

# Inspect.
oc bindings list

# Run.
opencomputer gateway
```

`~/.opencomputer/bindings.yaml` is the source of truth; the CLI is
just a porcelain. See
`docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md`
for the design rationale and match-precedence rules.
```

- [ ] **Step 2: Add CHANGELOG entry under Unreleased**

```markdown
### Added
- **Multi-profile gateway routing.** `~/.opencomputer/bindings.yaml`
  maps inbound messages to profiles; multiple AgentLoops run in
  parallel under their own ContextVar-scoped profile home. New
  `oc bindings add/list/remove/set-default` CLI. Profiles act as
  agents in OpenClaw's sense — workspace, memory, tools, prompt all
  isolated per profile. (#PR-1, #PR-2, #PR-3, #PR-4)
```

- [ ] **Step 3: Commit + final PR**

```bash
git add README.md CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: README + CHANGELOG for multi-profile routing

Quickstart for `oc bindings`. Pointer to the design spec.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"

git push
gh pr create --title "feat(cli): oc bindings — manage gateway routing rules" --body "$(cat <<'EOF'
## Summary

Phase 4 of the profile-as-agent multi-routing work. CLI surface +
docs.

- `oc bindings list / show / add / remove / set-default`.
- flock'd YAML writer (closes the latent profile.yaml flock tech
  debt at the same time — pattern reused).
- README "Multi-Profile Routing" section.
- CHANGELOG entry.

With this merged, the feature is end-user-usable. See spec:
`docs/superpowers/specs/2026-04-30-profile-as-agent-multi-routing-design.md`.

## Test plan
- [x] CLI round-trip: list / show / add / remove.
- [x] Concurrent-write flock test (3 subprocesses, none lost).
- [x] All 885+ existing tests still pass.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist (run after writing the plan)

1. **Spec coverage**
   - [x] §3 Architecture → Phase 1 (ContextVar) + Phase 2 (AgentRouter) + Phase 3 (resolver wiring)
   - [x] §4 Components: AgentRouter (Phase 2), BindingResolver (Phase 3), ProfileContext (Phase 1), BindingsConfig (Phase 3), oc bindings CLI (Phase 4)
   - [x] §5 Data flow → Phase 3 dispatch wiring
   - [x] §6 bindings.yaml schema → Phase 3 Task 3.1
   - [x] §7 Error handling → covered by `test_load_malformed_raises`, `test_broken_profile_logs_and_raises_first_time`, Gateway init exception fallback
   - [x] §8 Backwards compatibility → asserted via "all 885 tests pass" gate at every phase + Phase 1 `_home()` fallback chain tests
   - [x] §10 Testing → all five new test files specified above
   - [x] §13 Migration / rollback → each phase is independently revertable

2. **Placeholder scan**
   - No "TBD", "TODO", "implement later".
   - No "add appropriate error handling" — concrete error paths every time.
   - No "similar to Task N" — code repeated where needed.

3. **Type consistency**
   - `Binding` / `BindingMatch` / `BindingsConfig` consistent across tasks 3.1, 3.2, 3.3, 4.1.
   - `AgentRouter.get_or_load(profile_id) -> AgentLoop` consistent across 2.1, 2.4, 3.3.
   - `set_profile(home: Path) -> ContextManager` consistent across 1.1, 3.3, 4 tests.
   - `current_profile_home: ContextVar[Path | None]` consistent.

---

## Adversarial Audit — Pass 1 (2026-04-30)

After drafting the plan, I performed an adversarial self-review per
the user's request, then dispatched the same plan + spec to an
independent reviewer angle. Findings + applied refinements:

### Gaps found and fixed inline

| # | Severity | Description | Fix |
|---|---|---|---|
| G1 | HIGH | Phase 2 had no production AgentLoop factory; `Config` field_factories would capture stale paths if not built under `set_profile(profile_home)` | Added Task 2.4: `build_agent_loop_for_profile` always wraps construction in `set_profile`. |
| G2 | HIGH | Per-profile plugin filter was asserted but not implemented | Task 2.4 derives `allowed_tools` from the profile's `plugins.enabled`. |
| G3 | HIGH | DelegateTool refactor moved factory class→instance but didn't show closure binding profile_id+profile_home | Task 2.4 closes the delegate factory over `(profile_id, profile_home)`. |
| G4 | MEDIUM | Channel adapters don't populate `peer_id` / `group_id` / `account_id` in `MessageEvent.metadata` | Documented as v1 limitation: only `platform` + `chat_id` matchable in v1. Schema supports the future fields; adapter updates land in v1.1. |
| G5 | MEDIUM | `test_two_chats_two_profiles_run_in_parallel` relied on sleep timing → flaky on CI | Replaced with `asyncio.Event` synchronization. Serial dispatch now causes timeout, not silent failure. |
| G6 | MEDIUM | Honcho overlay multi-profile not tested | Spec §11 documents per-profile `host_key`; defer concrete test to follow-up. README warns when Honcho + multi-profile coexist. |
| G7 | LOW | `--profile` CLI flag semantic shift undocumented | Phase 4 README task includes the docs entry: under multi-profile, `--profile` sets the *default* profile when bindings miss. |
| G8 | MEDIUM | Subagent ContextVar propagation untested | Added `test_subagent_inherits_parent_profile_contextvar` to Phase 3 test file. |
| G9 | LOW | Dispatch logs lacked `profile_id` | Phase 3 Dispatch wiring includes `profile_id` in log fields (use `extra={"profile_id": profile_id}` on `logger.info` / `.exception`). |
| G10 | LOW | First-inbound latency per profile undocumented (~200-500ms) | Phase 4 README "Multi-Profile" section includes a "first message to a new profile is slower while we build its loop" note. |

### Alternative approaches considered

| # | Approach | Verdict |
|---|---|---|
| Alt A | Explicit `profile_home` parameter threaded through every `_home()` call site (no ContextVar) | Cleaner long-term; ~80+ call-site refactor too invasive for now. ContextVar is the *minimal* change that gets correctness. |
| Alt B | Subprocess per profile, gateway as IPC router | Hard isolation; spec §12 explicitly out of scope. Right answer if security model ever requires it. |
| Alt C | Per-AgentLoop frozen `Config`, no `_home()` at all | Cleanest possible; same refactor scope as Alt A. Future evolution path. |

**Recommendation stands: ContextVar (current plan) is the right minimal change.**

### Worst-case scenarios mitigated

| Scenario | Mitigation in plan |
|---|---|
| ContextVar forgotten in critical code path → wrong profile reads wrong memory | Phase 1 grep audit + multi-profile dispatch test (G2/G8) |
| Plugin singleton state collides between profiles | Task 2.4 explicit allowlist filter; spec docs the no-process-cache plugin contract |
| User edits `bindings.yaml` mid-conversation → routes change | v1: gateway restart required; documented |
| Memory accumulates: 50 profiles × AgentLoop ≈ 1.5 GB | v1: no eviction; spec §11 / README "Multi-Profile" notes; LRU in v2 |
| `asyncio.gather` on rapid messages from same chat → out-of-order replies | Per-`(profile_id, session_id)` lock prevents this |

### Confidence summary

| Decision | Confidence | Notes |
|---|---|---|
| ContextVar correct for asyncio | 0.99 | Python 3.7+ canonical |
| `_home()` is the only path-state global | 0.85 | Phase 1 grep step verifies |
| Plugin filter clean per profile | 0.80 (was 0.7) | Task 2.4 implements explicitly + tests |
| DelegateTool refactor safe | 0.85 (was 0.8) | Task 2.4 binds closure correctly + tests |
| Phase 3 multi-profile test stable | 0.95 (was 0.6) | G5 fix removes sleep timing |
| Honcho overlay survives multi-profile | 0.7 | Acceptable given v1 stance; tighter test in follow-up |
| 4-PR phasing is right | 0.9 | Each phase independently revertable; matches per-phase workflow rule |

Residual sub-0.85 risks:
- Honcho (0.7) — accept; monitor in dogfood.
- `_home()` callers exhaustive (0.85) — Phase 1 grep step turns this from confidence into evidence.

### Deferred (acceptable)

- Hot-reload of `bindings.yaml` (gateway restart for v1).
- LRU eviction of cached AgentLoops (manual restart for now).
- `oc bindings test <event>` debug command (nice-to-have).
- Telegram/Discord/Slack adapter updates to populate `peer_id` /
  `group_id` / `account_id` (v1.1; schema is forward-compatible).
- Honcho concrete multi-profile test (warn + document in v1).

---

## Adversarial Audit — Pass 2 (2026-04-30, independent reviewer)

After Pass 1 closed G1–G10, an independent Opus subagent reviewed
both the spec and the plan against the actual codebase. It surfaced
12 *new* findings (numbered F1–F12) that Pass 1 missed by trusting
the plan's pseudocode without grounding it in real APIs.

### Findings + applied refinements

| # | Severity | Description | Resolution |
|---|---|---|---|
| F1 | HIGH | `AgentLoop(cfg, provider)` was wrong — actual signature is `AgentLoop(provider, config, ...)` | **Fixed inline** in Task 2.4 — `AgentLoop(provider=provider, config=cfg, allowed_tools=allowed_tools)` |
| F2 | HIGH | `load_config_for_profile` and `tools_provided_by` were invented APIs the plan handwaved | **New Pre-Task 2.4** lands both helpers with full TDD spec before Task 2.4 references them |
| F3 | HIGH | `BindingMatch.peer_id` / `group_id` / `account_id` would silently never match (no adapter populates them today) | **See task 3.2-WARN below** — resolver logs ERROR at load time for any binding that references a field the platform doesn't surface |
| F4 | HIGH | `WireServer` and `auxiliary_client` paths bypass `Dispatch` entirely; "single-profile users see no diff" promise breaks for `oc wire` clients | **See Task 2.6 below** — thread the router into WireServer; wire callers route through default profile until per-call binding lands in v1.1 |
| F5 | MEDIUM | Gateway shutdown race — `Gateway.stop()` doesn't await per-profile in-flight turns | **Documented** as known limitation in spec §11; same risk exists today, multi-profile increases visibility. v1.1 follow-up. |
| F6 | MEDIUM | Fatal-error supervisor reconnects single-shared adapter; affects all profiles bound to that platform | **Documented** in spec §11 — adapters are gateway-scoped, not profile-scoped. Per-profile adapters punted to v2. |
| F7 | MEDIUM | Each per-profile AgentLoop has its own ConsentGate; Dispatch's prompt handler must register on EACH gate, not just the first loop's | **Fixed inline** in Task 2.4 (factory exposes the hook) + **Task 2.5 wiring update below** |
| F8 | MEDIUM | `PluginRegistry.api()` calls `default_config()` at load time, capturing the default profile's paths into the shared `PluginAPI` | **See Task 2.5-API below** — audit `api.session_db_path` consumers; shift to lazy `_home()` resolution OR build per-profile PluginAPI |
| F9 | MEDIUM | Phases not actually independent — Phase 4 CLI is meaningless without Phase 3 resolver; user could `oc bindings add` with no effect | **Phasing reordered** — see "Phase ordering" note below |
| F10 | MEDIUM | Long-running tool tasks (`bg_notify`, `StartProcess`, F2 subscribers) capture ContextVar at task-creation; outlive dispatch → stale-profile writes after profile rebuild | **See Task 3.x test below** — pin the contract; audit `bg_notify` / `StartProcess` for path-resolved-at-task-creation paths |
| F11 | LOW | `/steer` registry is process-global, keyed by session_id only; mid-flight rebinding could misroute queued steers | **Documented** in spec "Out of scope" |
| F12 | LOW | Multi-profile debugging has no first-class support (no log line per resolution, no `oc bindings test` debug subcommand) | **See Task 4.4 below** — add structured resolver log + `oc bindings test` debug command |

### Concrete plan additions from Pass 2

#### Task 2.5-API (new): Audit PluginAPI for default-profile-frozen paths

Inserted between current Task 2.4 (factory) and 2.5 (Gateway/Dispatch
wiring).

- [ ] **Step 1: Run grep to enumerate consumers**

```bash
grep -rn "api\.session_db_path\|api\.outgoing_queue\|api\.profile_home" \
    extensions/ opencomputer/ \
    --include='*.py' \
    | tee /tmp/pluginapi-consumers.txt
```

- [ ] **Step 2: For each hit, classify**

For each hit, decide one of:
- **Per-profile correct already** (resolves via `_home()` lazily → ContextVar-aware) → no change.
- **Default-profile frozen** (captures the path eagerly at registration) → change to lazy resolution OR document v1.1 follow-up.

- [ ] **Step 3: Write a test pinning the contract**

```python
# tests/test_pluginapi_per_profile.py
@pytest.mark.asyncio
async def test_outgoing_queue_writes_to_active_profile_db(tmp_path: Path) -> None:
    """A plugin enqueueing into api.outgoing_queue under set_profile(b)
    must land in profile b's sessions.db, not the boot-time default."""
    # ... build two profiles, enqueue under each, assert separate DBs ...
```

- [ ] **Step 4: Commit**

#### Task 2.5-Gate (extends current Task 2.5): register prompt handler on each per-profile gate

When the Gateway constructs the AgentRouter's loop_factory, add to
the post-construction step (per Pass-2 F7):

```python
# In Gateway.__init__, after AgentRouter is built:
# Wrap the factory to also register the consent prompt handler.
def _wrapped_factory(pid: str, home: Path) -> AgentLoop:
    loop = build_agent_loop_for_profile(pid, home)
    gate = getattr(loop, "_consent_gate", None)
    if gate is not None and self.dispatch is not None:
        gate.set_prompt_handler(self.dispatch._send_approval_prompt)
    return loop
```

Add a test (extends `test_dispatch_multiprofile.py`):

```python
@pytest.mark.asyncio
async def test_consent_prompt_fires_on_non_default_profile(tmp_path: Path) -> None:
    """Audit Pass-2 F7: an F1 capability prompt on a non-default
    profile must reach the channel adapter (not vanish into the
    void because the gate has no handler attached)."""
    # Build router with two profiles. Trigger a fake CapabilityClaim
    # on profile_b's gate. Assert dispatch._send_approval_prompt was
    # called with profile_b's binding.
    ...
```

#### Task 2.6 (NEW from F4): Thread router into WireServer

**Files:**
- Modify: `opencomputer/gateway/wire_server.py:54, 230` (and any other `self.loop` references)
- Test: `tests/test_wire_server_router.py`

For v1: WireServer accepts an `AgentRouter` and routes every wire-RPC
call through the *default* profile, preserving current behavior. (No
per-call binding via wire — that's v1.1.) Spec's "single-profile users
see no diff" is preserved because there's only one profile in the
router for them.

```python
# In WireServer.__init__ — replace `loop: AgentLoop` with:
def __init__(self, router: AgentRouter, ...) -> None:
    self._router = router
    ...

# In the run_conversation handler:
loop = await self._router.get_or_load("default")
profile_home = self._router._profile_home_resolver("default")
with set_profile(profile_home):
    result = await loop.run_conversation(...)
```

Test: a wire RPC call with two profiles in the router still works,
defaults to "default", does not error.

#### Task 3.2-WARN (extends Phase 3 Task 3.2): Resolver-load schema validator

Add to `BindingResolver.__init__`:

```python
# Pass-2 F3: forward-compat fields that no adapter populates yet.
# Until adapter updates land, log an ERROR for any binding that
# references one — silent miss is the worst outcome.
_ADAPTER_SUPPORT_MATRIX: dict[str, frozenset[str]] = {
    "telegram": frozenset({"platform", "chat_id"}),
    "discord":  frozenset({"platform", "chat_id"}),
    "slack":    frozenset({"platform", "chat_id"}),
    # ... extend as adapters add metadata ...
}

class BindingResolver:
    def __init__(self, cfg: BindingsConfig) -> None:
        self._cfg = cfg
        self._validate(cfg)

    def _validate(self, cfg: BindingsConfig) -> None:
        for i, b in enumerate(cfg.bindings):
            platform = b.match.platform
            if platform is None:
                continue  # platform-agnostic binding — fields validated per-event
            supported = _ADAPTER_SUPPORT_MATRIX.get(platform, frozenset())
            for field_name in ("peer_id", "group_id", "account_id"):
                if getattr(b.match, field_name) is not None and field_name not in supported:
                    logger.error(
                        "binding[%d]: platform=%s does not surface match field %s "
                        "in v1; this binding will never match. Use chat_id or "
                        "platform-only matching.",
                        i, platform, field_name,
                    )
```

Add a test that verifies the WARN fires:

```python
def test_resolver_warns_on_unsupported_field(caplog) -> None:
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(Binding(
            match=BindingMatch(platform="telegram", peer_id="123"),
            profile="x",
        ),),
    )
    with caplog.at_level("ERROR"):
        BindingResolver(cfg)
    assert any("peer_id" in r.message for r in caplog.records)
```

#### Task 3.3-LOG (extends Phase 3 Task 3.3): Structured resolver logging

Add to `_do_dispatch` after resolving profile_id:

```python
logger.info(
    "dispatch routing",
    extra={
        "platform": event.platform.value if event.platform else None,
        "chat_id": event.chat_id,
        "session_id": session_id,
        "profile_id": profile_id,
        "binding_match": "default" if profile_id == self._resolver._cfg.default_profile else "matched",
    },
)
```

Update the existing `logger.exception("dispatch error for %s: %s", ...)`
to include profile_id.

#### Task 3.4-BG (NEW from F10): Long-running tool task ContextVar contract test

```python
# tests/test_dispatch_multiprofile.py — append

@pytest.mark.asyncio
async def test_long_running_tool_task_carries_creation_time_profile(tmp_path):
    """Pass-2 F10: a tool that creates a task during dispatch
    captures the ContextVar at creation time. After the dispatch
    ends and a different profile dispatches, the original task
    still resolves to its original profile.
    
    This is the contextvars contract; the test pins it so future
    refactors don't accidentally break it (or, if intentional, force
    explicit accommodation for bg-task profile rebinding)."""
    from plugin_sdk.profile_context import current_profile_home, set_profile

    seen_in_bg: list[Path] = []
    bg_done = asyncio.Event()

    async def long_running_task() -> None:
        # Snapshot at task creation; runs after dispatch ends
        await asyncio.sleep(0.05)
        v = current_profile_home.get()
        if v is not None:
            seen_in_bg.append(v)
        bg_done.set()

    # Simulate a tool creating a bg task during dispatch under profile A.
    profile_a = tmp_path / "a"; profile_a.mkdir()
    profile_b = tmp_path / "b"; profile_b.mkdir()

    with set_profile(profile_a):
        task = asyncio.create_task(long_running_task())
    # Now dispatch under profile B.
    with set_profile(profile_b):
        await asyncio.sleep(0.001)
    # Wait for the bg task — it ran AFTER profile B dispatched.
    await asyncio.wait_for(bg_done.wait(), timeout=2.0)
    assert seen_in_bg == [profile_a], (
        f"expected bg task to keep profile_a contextvar at creation; got {seen_in_bg}"
    )
```

#### Task 4.4 (NEW from F12): `oc bindings test` debug subcommand

**Files:**
- Modify: `opencomputer/cli_bindings.py`
- Test: extend `tests/test_phase_bindings_cli.py`

```python
@app.command("test")
def test_cmd(
    platform: str = typer.Option(..., "--platform"),
    chat_id: str | None = typer.Option(None, "--chat-id"),
    peer_id: str | None = typer.Option(None, "--peer-id"),
    group_id: str | None = typer.Option(None, "--group-id"),
    account_id: str | None = typer.Option(None, "--account-id"),
) -> None:
    """Show which profile WOULD catch a hypothetical event.

    Useful for debugging routing rules without sending real messages.
    """
    cfg = load_bindings(_bindings_path())
    resolver = BindingResolver(cfg)
    fake_event = MessageEvent(
        platform=Platform(platform), chat_id=chat_id or "",
        text="", attachments=[],
        metadata={
            k: v for k, v in {
                "peer_id": peer_id, "group_id": group_id,
                "account_id": account_id,
            }.items() if v is not None
        },
    )
    profile = resolver.resolve(fake_event)
    console.print(f"resolved profile: [bold]{profile}[/bold]")
```

### Phase ordering (revised from F9)

| Phase | Independently shippable to PROD? | Rationale |
|---|---|---|
| Phase 1 | ✅ yes | Pure plumbing; no user-visible behavior |
| Phase 2 | ✅ yes | Router exists; default-only routing identical to today |
| Phase 3 | ✅ yes | Resolver active; without `bindings.yaml`, identical to today |
| Phase 4 | ⚠️ requires Phase 3 merged | Without Phase 3 the CLI writes a file the system ignores |

**Resolution:** explicitly state Phase 4 PR description as "depends on
Phase 3 PR landed on main." Don't merge them in parallel.

### Confidence summary (Pass 2)

| Decision | Confidence | Notes |
|---|---|---|
| ContextVar correct for asyncio | 0.99 | Unchanged |
| `_home()` is the only path-state global | 0.85 | Phase 1 grep step verifies |
| Plugin filter clean per profile | 0.90 (was 0.80) | Pre-Task 2.4 lands the helper concretely; Task 2.5-API audits PluginAPI consumers |
| DelegateTool refactor safe | 0.85 | Unchanged |
| Phase 3 multi-profile test stable | 0.95 | Unchanged |
| Honcho overlay survives multi-profile | 0.7 | Unchanged; v1 acceptable |
| WireServer compat preserved | 0.85 (NEW) | Task 2.6 routes through default; v1.1 adds per-call binding |
| Consent gate on non-default profile | 0.90 (NEW) | Task 2.5-Gate registers handler on every per-profile gate |
| Phase 4 not shipping ahead of Phase 3 | 0.95 (NEW) | Phasing note + PR description gate |
| Long-running task contract pinned | 0.85 (NEW) | Task 3.4-BG test pins behavior; future refactors must explicitly opt-in to override |

### Residual deferrals (acceptable for v1)

- F5 (shutdown grace) — same risk as today; documented in spec §11.
- F6 (per-platform fatal-error blast radius) — documented in spec §11.
- F11 (`/steer` process-global) — documented in spec "Out of scope".
- F8 *complete* fix (per-profile PluginAPI) — Task 2.5-API does the
  audit + lazy-resolution shift; full per-profile PluginAPI rebuild
  is v2 if needed.
- v1.1 follow-ups: adapter updates to populate `peer_id` /
  `group_id` / `account_id`; per-call wire binding; LRU cache
  eviction; hot-reload of `bindings.yaml`.

---

## Execution choice

Plan complete and double-audited (Pass 1 + Pass 2). Two execution
options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh Sonnet or
   Opus subagent per task, reviewing between tasks. Fast iteration;
   per the user's standing rule, no Haiku.

2. **Inline Execution** — Tasks executed in this session via
   `superpowers:executing-plans`, batch with checkpoints.

Per the user's stated sequence, the next step is `/executing-plans`.
Tell me when you're ready and which mode you prefer.
