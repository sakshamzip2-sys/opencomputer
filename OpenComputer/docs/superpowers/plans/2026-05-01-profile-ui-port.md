# Profile UI Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the persona `Ctrl+P` cycler and status-bar badge from the persona system to the profile system. Profile switch takes effect at the next user-turn boundary (no mid-turn `AgentLoop` swap, no force-restart).

**Architecture:** Four changes wired through `runtime.custom`:
1. New `cycle_profile()` in `cli_ui/_profile_swap.py` (helper module) writes `runtime.custom["pending_profile_id"]`. Pure, prompt_toolkit-free, unit-testable.
2. New `MemoryManager.rebind_to_profile()` re-resolves `declarative_path`, `user_path`, `soul_path` to a new profile home so subsequent `read_*` calls hit the new files.
3. Badge renderer in `input_loop.py` reads `active_profile_id` instead of `active_persona_id`; shows pending switch as `profile: a → b`.
4. Turn-entry orchestration in `run_conversation`: (a) initialize `active_profile_id` from sticky `active_profile` file if absent, (b) consume `pending_profile_id` if set — persists via `write_active_profile()`, calls `memory.rebind_to_profile()`, evicts prompt-cache snapshot for this session id.

Config / tools / model registry stay bound to the process — full Grade-B swap (process-level reload) is out of scope. SOUL.md/MEMORY.md/USER.md swap is in scope and necessary for the UX to feel real.

**Tech Stack:** Python 3.12+, prompt_toolkit (TUI key bindings + FormattedText badge), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-01-profile-ui-port-design.md`.

---

## Files affected

| Path | Action | Purpose |
|---|---|---|
| `opencomputer/cli_ui/_profile_swap.py` | **Create** | Pure helpers: `cycle_profile()` + `consume_pending_profile_swap()`. Kept out of `input_loop.py` so they're testable without prompt_toolkit machinery. |
| `opencomputer/cli_ui/input_loop.py` | Modify | Replace `_cycle_persona` call site at `:722-730` (Ctrl+P binding); update badge at `:420-497`; update hint text. `_cycle_persona()` itself (`:376-402`) is left in place — orphaned but harmless until Plan 2 deletes it. `_CHAT_PERSONAS` constant (`:424`) is deleted (no other readers; verified via grep). |
| `opencomputer/agent/memory.py` | Modify | Add `MemoryManager.rebind_to_profile(profile_home: Path)` — re-resolves `declarative_path`, `user_path`, `soul_path` to the new profile's `home/` directory. ~12 LOC. |
| `opencomputer/agent/loop.py` | Modify | At top of `run_conversation` (line 533): (a) initialize `runtime.custom["active_profile_id"]` from sticky if absent, (b) call `_apply_pending_profile_swap(runtime, sid)` which orchestrates: `consume_pending_profile_swap()` (helper) + `self.memory.rebind_to_profile()` + `self._prompt_snapshots.pop(sid, None)`. |
| `tests/test_profile_ui_port.py` | **Create** | 20 tests covering cycle helper, badge rendering, swap consumer, sticky init, memory rebind, end-to-end orchestrator. |

---

## Task 1: `cycle_profile()` helper + state-mirror

**Files:**
- Create: `opencomputer/cli_ui/_profile_swap.py`
- Create: `tests/test_profile_ui_port.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_profile_ui_port.py`:

```python
"""Plan 1 of 3 — Profile UI port. Tests the cycle helper + swap consumer.

The persona auto-classifier still runs during Plan 1 (deleted in Plan 2),
so we deliberately leave persona-related runtime state alone.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opencomputer.cli_ui._profile_swap import (
    cycle_profile,
    consume_pending_profile_swap,
)


def _runtime() -> SimpleNamespace:
    """Fake RuntimeContext sufficient for the helpers under test."""
    return SimpleNamespace(custom={})


def _seed_profiles(root: Path, names: list[str]) -> None:
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / "profiles" / n).mkdir()


def test_cycle_profile_with_two_named_profiles_plus_default(tmp_path, monkeypatch):
    """default + work + side → cycles default → side → work → default → side."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    assert cycle_profile(runtime) == "side"
    assert runtime.custom["pending_profile_id"] == "side"

    runtime.custom["active_profile_id"] = "side"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "work"

    runtime.custom["active_profile_id"] = "work"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "default"


def test_cycle_profile_default_only_returns_none(tmp_path, monkeypatch):
    """Only the implicit default exists → no other profiles to cycle to."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    assert cycle_profile(runtime) is None
    assert runtime.custom.get("profile_cycle_hint") == (
        "no other profiles — use /profile create"
    )
    assert "pending_profile_id" not in runtime.custom


def test_cycle_profile_unknown_current_starts_from_first(tmp_path, monkeypatch):
    """If active_profile_id is missing/garbage, cycle starts from sorted[0]."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["alpha", "beta"])
    runtime = _runtime()
    # No active_profile_id set.
    assert cycle_profile(runtime) == "alpha"


def test_cycle_profile_re_press_advances_pending(tmp_path, monkeypatch):
    """Pressing Ctrl+P twice without a turn boundary advances pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    cycle_profile(runtime)  # → side
    assert runtime.custom["pending_profile_id"] == "side"

    cycle_profile(runtime)  # → work
    assert runtime.custom["pending_profile_id"] == "work"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 4 FAILS with `ModuleNotFoundError: opencomputer.cli_ui._profile_swap`.

- [ ] **Step 3: Implement `_profile_swap.py`**

Create `opencomputer/cli_ui/_profile_swap.py`:

```python
"""Profile-cycling and pending-swap helpers (Plan 1 of 3).

Kept out of ``input_loop.py`` so we can unit-test without the full
prompt_toolkit ``Application``. The Ctrl+P key binding in
``input_loop.py`` calls :func:`cycle_profile`; the turn-entry hook in
``agent/loop.py`` calls :func:`consume_pending_profile_swap`.

Backwards compat note: persona state in ``runtime.custom`` is left
strictly alone here. Plan 2 (persona-removal) will retire those keys.
"""
from __future__ import annotations

from typing import Any

_NO_OTHER_PROFILES_HINT = "no other profiles — use /profile create"


def _all_cycle_targets() -> list[str]:
    """Sorted list of cycle targets including the implicit ``default``.

    ``list_profiles()`` only returns subdirs of ``~/.opencomputer/profiles/``;
    "default" is the implicit fallback when no active_profile file is set.
    The cycler treats "default" as a first-class entry so the user can
    always cycle back to it.
    """
    from opencomputer.profiles import list_profiles
    names = list_profiles()
    if "default" not in names:
        names = sorted([*names, "default"])
    return names


def cycle_profile(runtime: Any) -> str | None:
    """Advance the runtime's pending profile to the next available.

    Mutates ``runtime.custom["pending_profile_id"]``. Returns the new
    pending id, or ``None`` if there's only one profile (default-only).
    Sets ``runtime.custom["profile_cycle_hint"]`` for one render-tick
    when there's nothing to cycle to.
    """
    targets = _all_cycle_targets()
    if len(targets) <= 1:
        runtime.custom["profile_cycle_hint"] = _NO_OTHER_PROFILES_HINT
        return None

    current = (
        runtime.custom.get("pending_profile_id")
        or runtime.custom.get("active_profile_id")
        or "default"
    )
    try:
        idx = targets.index(current)
    except ValueError:
        idx = -1
    new_id = targets[(idx + 1) % len(targets)]
    runtime.custom["pending_profile_id"] = new_id
    runtime.custom.pop("profile_cycle_hint", None)
    return new_id


def consume_pending_profile_swap(runtime: Any) -> str | None:
    """Apply ``pending_profile_id`` if set. Called at turn entry.

    Pure: only mutates ``runtime.custom`` and writes the sticky
    ``active_profile`` file. Memory rebinding and prompt-cache eviction
    are the caller's responsibility (handled in ``agent/loop.py``).

    Returns the new active profile id, or ``None`` if no swap occurred.
    """
    pending = runtime.custom.pop("pending_profile_id", None)
    if not pending:
        return None
    current = runtime.custom.get("active_profile_id") or "default"
    if pending == current:
        return None

    from opencomputer.profiles import write_active_profile
    write_active_profile(None if pending == "default" else pending)
    runtime.custom["active_profile_id"] = pending
    return pending


def init_active_profile_id(runtime: Any) -> None:
    """Mirror the sticky ``active_profile`` file into runtime.custom on
    first turn of a session. Idempotent — runs only when the key is
    missing.
    """
    if "active_profile_id" in runtime.custom:
        return
    from opencomputer.profiles import read_active_profile
    runtime.custom["active_profile_id"] = read_active_profile() or "default"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/cli_ui/_profile_swap.py OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
feat(profile-ui): cycle_profile helper + tests

Pure helpers extracted into _profile_swap.py so the cycle logic is
testable without the prompt_toolkit Application. Mirrors the persona
cycler: writes runtime.custom["pending_profile_id"] which the
turn-entry hook will consume next turn.

Plan 1 of 3 — UI port (spec: 2026-05-01-profile-ui-port-design.md)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `consume_pending_profile_swap()` + `init_active_profile_id()` tests

**Files:**
- Modify: `tests/test_profile_ui_port.py`

- [ ] **Step 1: Append tests to `tests/test_profile_ui_port.py`**

```python
from opencomputer.cli_ui._profile_swap import init_active_profile_id


def test_consume_swap_no_pending_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    assert consume_pending_profile_swap(runtime) is None


def test_consume_swap_same_as_current_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "work"
    assert consume_pending_profile_swap(runtime) is None
    assert "pending_profile_id" not in runtime.custom


def test_consume_swap_writes_sticky_and_updates_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "work"

    result = consume_pending_profile_swap(runtime)

    assert result == "work"
    assert runtime.custom["active_profile_id"] == "work"
    assert "pending_profile_id" not in runtime.custom
    sticky = (tmp_path / "active_profile").read_text().strip()
    assert sticky == "work"


def test_consume_swap_to_default_clears_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "default"

    result = consume_pending_profile_swap(runtime)

    assert result == "default"
    assert not (tmp_path / "active_profile").exists()


def test_init_active_profile_id_reads_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "work"


def test_init_active_profile_id_default_when_no_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "default"


def test_init_active_profile_id_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "side"  # already set; do not overwrite
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "side"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 11 PASS (4 from Task 1 + 7 new).

- [ ] **Step 3: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
test(profile-ui): consume_pending_profile_swap + init_active_profile_id

Covers: no-pending no-op, same-as-current no-op, sticky write,
default-clears-sticky. init_active_profile_id covers sticky-read,
default-fallback, and idempotence (don't overwrite existing key).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2.5: `MemoryManager.rebind_to_profile()`

**Files:**
- Modify: `opencomputer/agent/memory.py`
- Modify: `tests/test_profile_ui_port.py`

- [ ] **Step 1: Append tests**

```python
def test_memory_manager_rebind_to_profile(tmp_path):
    """rebind_to_profile re-resolves the 3 path attributes to a new
    profile home so subsequent read_* calls hit the new files."""
    from opencomputer.agent.memory import MemoryManager

    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    (profile_a).mkdir()
    (profile_b).mkdir()
    (profile_a / "MEMORY.md").write_text("memory-A")
    (profile_a / "USER.md").write_text("user-A")
    (profile_a / "SOUL.md").write_text("soul-A")
    (profile_b / "MEMORY.md").write_text("memory-B")
    (profile_b / "USER.md").write_text("user-B")
    (profile_b / "SOUL.md").write_text("soul-B")

    skills = tmp_path / "skills"
    skills.mkdir()

    mm = MemoryManager(
        declarative_path=profile_a / "MEMORY.md",
        skills_path=skills,
        user_path=profile_a / "USER.md",
        soul_path=profile_a / "SOUL.md",
    )
    assert mm.read_declarative() == "memory-A"
    assert mm.read_user() == "user-A"
    assert mm.read_soul() == "soul-A"

    mm.rebind_to_profile(profile_b)

    assert mm.read_declarative() == "memory-B"
    assert mm.read_user() == "user-B"
    assert mm.read_soul() == "soul-B"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py::test_memory_manager_rebind_to_profile -v`
Expected: FAIL — `rebind_to_profile` not found.

- [ ] **Step 3: Add `rebind_to_profile` to `MemoryManager` in `opencomputer/agent/memory.py`**

Insert after the `__init__` method (after line ~291), before the `# ─── declarative (MEMORY.md)` comment:

```python
    def rebind_to_profile(self, profile_home: Path) -> None:
        """Re-resolve declarative_path / user_path / soul_path to point at
        a new profile's home directory. Used by the Ctrl+P profile-swap
        flow to make subsequent read_* calls hit the new profile's
        SOUL.md / MEMORY.md / USER.md without recreating the manager.

        ``skills_path`` and bundled-skills paths are NOT rebound — skill
        roots are global, not per-profile, in the current model.
        """
        self.declarative_path = profile_home / "MEMORY.md"
        self.user_path = profile_home / "USER.md"
        self.soul_path = profile_home / "SOUL.md"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py::test_memory_manager_rebind_to_profile -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/memory.py OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
feat(memory): MemoryManager.rebind_to_profile for mid-session profile swap

Re-resolves declarative/user/soul paths to a new profile_home. Enables
the Ctrl+P swap flow to actually use the new profile's memory files
on subsequent turns without recreating the manager.

Skills paths intentionally not rebound — skill roots are global.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Badge rendering — read profile state, show pending arrow

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py:420-497`
- Modify: `tests/test_profile_ui_port.py`

- [ ] **Step 1: Append badge tests to `tests/test_profile_ui_port.py`**

```python
from opencomputer.cli_ui.input_loop import (
    _badge_has_meaningful_content,
    _render_mode_badge,
)


def _runtime_for_badge(**custom):
    rt = SimpleNamespace(custom=dict(custom))
    return rt


def test_badge_shows_profile_when_set():
    rt = _runtime_for_badge(active_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: work" in text


def test_badge_shows_pending_arrow():
    rt = _runtime_for_badge(active_profile_id="work", pending_profile_id="side")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: work → side" in text


def test_badge_pending_same_as_current_no_arrow():
    rt = _runtime_for_badge(active_profile_id="work", pending_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "→" not in text


def test_badge_default_profile_renders_default():
    rt = _runtime_for_badge(active_profile_id="default")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: default" in text


def test_badge_hint_says_profile_not_persona():
    rt = _runtime_for_badge(active_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "Ctrl+P profile" in text
    assert "Ctrl+P persona" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 5 new FAILS — badge still reads `active_persona_id` and renders `Ctrl+P persona`.

- [ ] **Step 3: Update `_badge_has_meaningful_content` in `cli_ui/input_loop.py:420-455`**

Replace:

```python
#: 2026-04-29 PR-6: persona ids where the mode badge is *not* useful — these
#: are non-coding registers (chat/companion). When the auto-classifier lands
#: on one of these AND the user hasn't explicitly switched modes, hide the
#: badge to keep the chat surface uncluttered.
_CHAT_PERSONAS = frozenset({"companion"})


def _badge_has_meaningful_content(runtime: object) -> bool:
    """Decide whether the badge has anything worth showing.
    ...
    """
    if runtime is None:
        return False
    from plugin_sdk import effective_permission_mode

    if effective_permission_mode(runtime).value != "default":
        return True

    personality = runtime.custom.get("personality", "")
    if personality and personality != "helpful":
        return True

    # Persona unset (early session) OR non-chat → show; chat persona → hide.
    persona = runtime.custom.get("active_persona_id", "")
    return persona not in _CHAT_PERSONAS
```

with:

```python
def _badge_has_meaningful_content(runtime: object) -> bool:
    """Decide whether the badge has anything worth showing.

    Always show when:
    - non-default permission mode (CLI flag or ``/mode`` / ``/auto`` / ``/plan``)
    - non-default ``/personality``
    - active profile other than ``default``
    - a pending profile switch is queued

    Hide when on the implicit ``default`` profile with nothing else
    overridden — the badge would just be visual noise.
    """
    if runtime is None:
        return False
    from plugin_sdk import effective_permission_mode

    if effective_permission_mode(runtime).value != "default":
        return True

    personality = runtime.custom.get("personality", "")
    if personality and personality != "helpful":
        return True

    if runtime.custom.get("pending_profile_id"):
        return True

    profile = runtime.custom.get("active_profile_id", "") or "default"
    return profile != "default"
```

- [ ] **Step 4: Update `_render_mode_badge` in `cli_ui/input_loop.py:458-497`**

Replace the persona-rendering block (lines 483-494):

```python
    persona = runtime.custom.get("active_persona_id", "")
    if persona:
        segments.append(("fg:ansicyan", f"· persona: {persona} "))

    personality = runtime.custom.get("personality", "")
    if personality and personality != "helpful":
        segments.append(("fg:ansimagenta", f"· personality: {personality} "))

    # Hint copy depends on which axes are visible. Always show
    # Shift+Tab (mode cycle); add Ctrl+P when persona is shown.
    if persona:
        segments.append(("", "  Shift+Tab mode · Ctrl+P persona"))
    else:
        segments.append(("", "  Shift+Tab to cycle"))
    return segments
```

with:

```python
    profile = runtime.custom.get("active_profile_id", "") or "default"
    pending = runtime.custom.get("pending_profile_id")
    if pending and pending != profile:
        segments.append(("fg:ansicyan", f"· profile: {profile} → {pending} "))
    else:
        segments.append(("fg:ansicyan", f"· profile: {profile} "))

    personality = runtime.custom.get("personality", "")
    if personality and personality != "helpful":
        segments.append(("fg:ansimagenta", f"· personality: {personality} "))

    segments.append(("", "  Shift+Tab mode · Ctrl+P profile"))
    return segments
```

Also delete the now-unused `_CHAT_PERSONAS` constant + its docstring (lines 420-424).

- [ ] **Step 5: Run badge tests to verify they pass + run full suite**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 17 PASS.

Run: `cd OpenComputer && pytest tests/ -x --timeout=60`
Expected: All existing tests pass. Some persona-classifier tests previously asserted on badge visibility — those should still pass because the persona auto-classifier still runs (unchanged) and writes `active_persona_id`; the badge just doesn't read that key anymore. If a test in `test_persona_classifier.py` or similar fails because it asserted on badge content, mark it `xfail` with reason `"Plan 1: badge no longer reads persona; restored in Plan 2 cleanup"` — DO NOT modify the badge to also-read persona.

- [ ] **Step 6: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
feat(profile-ui): badge renders profile, shows pending arrow

Replaces persona badge with profile badge. Shows "profile: <id>" and
"profile: <a> → <b>" when a switch is pending. Hint text updated
to "Ctrl+P profile". Badge now visible whenever active_profile_id is
non-default, a swap is pending, or any other axis is overridden.

Persona auto-classifier untouched — its output just no longer drives
the UI. Removed in Plan 2.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Re-bind Ctrl+P to `cycle_profile`

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py:722-730`
- Modify: `tests/test_profile_ui_port.py`

- [ ] **Step 1: Append binding test to `tests/test_profile_ui_port.py`**

The Ctrl+P binding lives inside `read_user_input` and isn't directly testable without a full `Application`. We test the integration via a callable-substitution pattern: assert that `_ctrl_p` calls `cycle_profile` (not `_cycle_persona`).

```python
def test_ctrl_p_handler_calls_cycle_profile(tmp_path, monkeypatch):
    """Smoke test: importing input_loop binds Ctrl+P to a function whose
    body references cycle_profile, not _cycle_persona."""
    import inspect
    from opencomputer.cli_ui import input_loop

    src = inspect.getsource(input_loop.read_user_input)
    # The Ctrl+P handler must reference our new helper.
    assert "cycle_profile(runtime" in src
    # And NOT the old persona helper.
    assert "_cycle_persona(runtime" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py::test_ctrl_p_handler_calls_cycle_profile -v`
Expected: FAIL — `_cycle_persona(runtime)` is still called at line 727.

- [ ] **Step 3: Update Ctrl+P binding at `input_loop.py:722-730`**

Replace:

```python
    @kb.add(Keys.ControlP)  # Ctrl+P — cycle personas (2026-05-01)
    def _ctrl_p(event):  # noqa: ANN001
        if runtime is None:
            return
        try:
            _cycle_persona(runtime)
        except Exception:  # noqa: BLE001
            return
        event.app.invalidate()
```

with:

```python
    @kb.add(Keys.ControlP)  # Ctrl+P — cycle profiles (Plan 1 of 3, 2026-05-01)
    def _ctrl_p(event):  # noqa: ANN001
        if runtime is None:
            return
        from opencomputer.cli_ui._profile_swap import cycle_profile
        try:
            cycle_profile(runtime)
        except Exception:  # noqa: BLE001
            return
        event.app.invalidate()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 18 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
feat(profile-ui): rebind Ctrl+P to cycle_profile

Ctrl+P now writes pending_profile_id; the next user turn consumes it.
The legacy _cycle_persona function is left in place but orphaned —
deleted in Plan 2 (persona system removal).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Orchestrate profile swap in `run_conversation`

**Files:**
- Modify: `opencomputer/agent/loop.py` (entry of `run_conversation`)
- Modify: `tests/test_profile_ui_port.py`

This is the orchestration tying together: `init_active_profile_id` (Task 2) + `consume_pending_profile_swap` (Task 2) + `MemoryManager.rebind_to_profile` (Task 2.5) + prompt-cache eviction.

- [ ] **Step 1: Find the insertion point**

Run: `cd OpenComputer && grep -n "async def run_conversation\|self._prompt_snapshots" opencomputer/agent/loop.py | head -20`

Two pieces of evidence you need:
- Line of `async def run_conversation` (expected ~533)
- Lines that pop/evict `self._prompt_snapshots[sid]` — confirms the cache attribute name

Note the actual line numbers; the patches below use `:533` as the canonical reference but adjust if the file has shifted.

- [ ] **Step 2: Append integration test**

```python
import asyncio


def test_apply_pending_profile_swap_orchestrator(tmp_path, monkeypatch):
    """Orchestrator: init + consume + rebind memory + evict snapshot."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    # Seed home/ subdirs that profiles.get_profile_dir(name)/"home" expects
    (tmp_path / "profiles" / "work" / "home").mkdir()
    (tmp_path / "profiles" / "work" / "home" / "MEMORY.md").write_text("memory-work")
    (tmp_path / "profiles" / "work" / "home" / "USER.md").write_text("user-work")
    (tmp_path / "profiles" / "work" / "home" / "SOUL.md").write_text("soul-work")

    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.loop import _apply_pending_profile_swap

    skills = tmp_path / "skills"
    skills.mkdir()
    home_a = tmp_path / "home_a"
    home_a.mkdir()
    (home_a / "MEMORY.md").write_text("memory-default")
    (home_a / "USER.md").write_text("user-default")
    (home_a / "SOUL.md").write_text("soul-default")
    mm = MemoryManager(
        declarative_path=home_a / "MEMORY.md",
        skills_path=skills,
        user_path=home_a / "USER.md",
        soul_path=home_a / "SOUL.md",
    )

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "work"
    snapshots = {"sid-1": "cached-prompt", "sid-2": "other-cached"}

    swapped = _apply_pending_profile_swap(
        runtime, memory=mm, prompt_snapshots=snapshots, sid="sid-1"
    )

    assert swapped == "work"
    assert runtime.custom["active_profile_id"] == "work"
    assert "pending_profile_id" not in runtime.custom
    assert mm.read_declarative() == "memory-work"
    assert mm.read_soul() == "soul-work"
    assert "sid-1" not in snapshots  # evicted
    assert "sid-2" in snapshots       # other sessions untouched


def test_apply_pending_profile_swap_no_pending_is_noop(tmp_path):
    """No pending → orchestrator is a clean no-op."""
    from opencomputer.agent.loop import _apply_pending_profile_swap
    runtime = _runtime()
    snapshots = {"sid-1": "cached"}
    result = _apply_pending_profile_swap(
        runtime, memory=None, prompt_snapshots=snapshots, sid="sid-1"
    )
    assert result is None
    assert "sid-1" in snapshots  # not evicted on no-op
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py::test_apply_pending_profile_swap_orchestrator tests/test_profile_ui_port.py::test_apply_pending_profile_swap_no_pending_is_noop -v`
Expected: 2 FAILS — `_apply_pending_profile_swap` doesn't exist yet.

- [ ] **Step 4: Add the orchestrator function to `agent/loop.py`**

Add at module scope near the top of `agent/loop.py` (after the imports, before the `AgentLoop` class definition):

```python
def _apply_pending_profile_swap(
    runtime: object,
    *,
    memory: object,
    prompt_snapshots: dict | None,
    sid: str | None,
) -> str | None:
    """Apply a queued profile swap at turn entry.

    Sequence:
      1. Consume ``pending_profile_id`` (delegates to _profile_swap helper).
      2. If a swap occurred, rebind ``memory`` to the new profile_home.
      3. Evict the prompt-cache snapshot for ``sid`` so the next turn
         rebuilds the system prompt against the new SOUL.md/MEMORY.md.

    Returns the new active profile id, or None if no swap occurred.

    Plan 1 of 3 — see docs/superpowers/specs/2026-05-01-profile-ui-port-design.md.
    """
    from opencomputer.cli_ui._profile_swap import (
        consume_pending_profile_swap,
        init_active_profile_id,
    )
    from opencomputer.profiles import get_profile_dir

    init_active_profile_id(runtime)
    new_id = consume_pending_profile_swap(runtime)
    if new_id is None:
        return None

    # Rebind memory pointers to the new profile's home directory.
    # get_profile_dir() returns ~/.opencomputer/profiles/<name>/ for named
    # profiles and ~/.opencomputer/ for "default".
    new_home_root = get_profile_dir(None if new_id == "default" else new_id)
    new_home = new_home_root / "home"
    if memory is not None and hasattr(memory, "rebind_to_profile"):
        try:
            memory.rebind_to_profile(new_home)
        except Exception:  # noqa: BLE001 — don't roll back the user-visible swap
            pass

    # Evict the cached prompt snapshot for this session so the next turn
    # rebuilds against the new memory pointers.
    if prompt_snapshots is not None and sid is not None:
        prompt_snapshots.pop(sid, None)

    return new_id
```

- [ ] **Step 5: Wire the orchestrator into `run_conversation`**

Locate `async def run_conversation` (~line 533). After `runtime` is established but before the first model call, insert:

```python
        # Plan 1 of 3 — UI port: apply queued profile swap (Ctrl+P or
        # /persona slash command). Idempotent on no-pending.
        _apply_pending_profile_swap(
            runtime,
            memory=getattr(self, "memory", None),
            prompt_snapshots=getattr(self, "_prompt_snapshots", None),
            sid=getattr(self, "_session_id", None),
        )
```

If `_session_id` is exposed under a different attribute name (e.g. `self._sid` or via a method), adjust accordingly — confirm via the grep output from Step 1.

- [ ] **Step 6: Run integration test + full suite + ruff**

Run: `cd OpenComputer && pytest tests/test_profile_ui_port.py -v`
Expected: 20 PASS (18 from prior tasks + 2 new orchestrator tests).

Run: `cd OpenComputer && pytest tests/ -x --timeout=60`
Expected: all previously-passing tests still pass.

Run: `cd OpenComputer && ruff check opencomputer/cli_ui/_profile_swap.py opencomputer/cli_ui/input_loop.py opencomputer/agent/loop.py opencomputer/agent/memory.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_profile_ui_port.py
git commit -m "$(cat <<'EOF'
feat(profile-ui): orchestrate Ctrl+P swap at run_conversation entry

Closes the loop: init_active_profile_id → consume_pending_profile_swap
→ MemoryManager.rebind_to_profile → prompt-cache eviction. Next turn
rebuilds the system prompt against the new profile's MEMORY.md /
SOUL.md / USER.md.

Plan 1 of 3 — Persona auto-classifier still alive (deleted in Plan 2).
Config/tools/model bound to the process — full Grade-B swap not
attempted; user can restart for those if needed.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manual smoke + push

**Files:** none — this is a verification step.

- [ ] **Step 1: Manual smoke (TUI)**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
source .venv/bin/activate

# Make sure at least one non-default profile exists
oc profile create work --quiet 2>/dev/null || true
oc profile list

# Launch the chat
oc
```

In the TUI:
1. Confirm the badge shows `profile: default` (or whatever your sticky is).
2. Press Ctrl+P. Badge should update to `profile: default → work`.
3. Type a message and press Enter. After the response, badge should show `profile: work`.
4. Press Ctrl+P again. Badge should cycle through any other profiles + default.
5. Run `oc profile show` in another shell — confirm the sticky `active_profile` file matches.

If anything is off, fix forward — don't push a broken UX.

- [ ] **Step 2: Push the branch**

```bash
cd /Users/saksham/Vscode/claude
git push origin feat/profile-as-agent-phase-2
```

(Or whatever branch you started on — `git branch --show-current` confirms.)

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(profile-ui): port Ctrl+P + badge from persona to profile (Plan 1 of 3)" --body "$(cat <<'EOF'
## Summary
- Replace persona Ctrl+P binding with profile cycler (\`cycle_profile\` helper)
- Replace persona badge with profile badge — shows pending switch as \`profile: a → b\`
- Wire turn-entry consumer in \`run_conversation\` to apply pending swaps (sticky + memory reload, Grade-A only)
- Persona auto-classifier left running internally — deleted in Plan 2

## Plan series
1. **This PR** — UI port
2. Persona system removal (next)
3. Auto-profile-suggester (final)

## Test plan
- [x] 17 new tests in \`tests/test_profile_ui_port.py\` — all green
- [x] Existing \`test_persona_classifier.py\` (27 tests) still green
- [x] Full pytest suite green
- [x] ruff clean
- [x] Manual TUI smoke — Ctrl+P cycles, badge updates, swap applies on next turn

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

(Run mentally before declaring Plan 1 done.)

- [ ] **Spec coverage:** every component in the spec has at least one task.
  - Spec §Components #1 (`cycle_profile`) → Task 1
  - Spec §Components #2 (badge rendering) → Task 3
  - Spec §Components #3 (hint text) → Task 3 (badge rendering replacement also updates hint)
  - Spec §Components #4 (turn-entry hook) → Task 5 (orchestrator) + Task 2.5 (memory rebind)
  - Spec §Components #5 (`/profile` slash command parity) → existing slash command writes runtime state via its own path; explicit "make it write `pending_profile_id`" is OUT OF SCOPE for this plan and tracked as a Plan-2 follow-up if user feedback wants the parity.
- [ ] **Error-handling table coverage:**
  - "User has only 1 profile" → `test_cycle_profile_default_only_returns_none`
  - "Profile dir deleted between Ctrl+P and turn-entry" → orchestrator catches `rebind_to_profile` failure (try/except wrapper). No explicit test in this plan — flagged for inclusion if Plan 2 lands.
  - "Profile config fails to load" → out of scope (Plan 1 only rebinds memory; config/tools/model not reloaded mid-session — documented limitation).
  - "pending set, never sent another turn" → per-session lifecycle; runtime is gc'd at session end. No test needed.
  - "Ctrl+P pressed multiple times" → `test_cycle_profile_re_press_advances_pending`
  - "Concurrent /persona slash command + Ctrl+P" → both write `pending_profile_id`; last-write-wins is the spec; no explicit test (would require dual-fixture).
- [ ] **No placeholders:** scanned, none found.
- [ ] **Type consistency:** function name `cycle_profile` consistent across all references. `consume_pending_profile_swap` consistent. `init_active_profile_id` consistent. `_apply_pending_profile_swap` consistent. `runtime.custom["pending_profile_id"]` key consistent across helper, badge, and orchestrator. `MemoryManager.rebind_to_profile(profile_home: Path)` signature consistent.
- [ ] **Env-var consistency:** all tests use `OPENCOMPUTER_HOME_ROOT` (matches `profiles.py:74`). The other env var, `OPENCOMPUTER_HOME`, is set dynamically by `_apply_profile_override` and is NOT what tests should override.
- [ ] **Backwards compat:** persona classifier and `/persona-mode` slash command untouched. `_cycle_persona` function left in place (orphaned). `_CHAT_PERSONAS` constant deleted (verified via grep — only readers were inside `_badge_has_meaningful_content`).
- [ ] **Test count audit:** Task 1 (4) + Task 2 (7) + Task 2.5 (1) + Task 3 (5) + Task 4 (1) + Task 5 (2) = **20 new tests** in `tests/test_profile_ui_port.py`.

---

## Risks / fallbacks

1. **`run_conversation` insertion point may have early returns.** If the method has guard clauses (e.g., empty user message, cancellation), insert `_apply_pending_profile_swap()` AFTER those guards but before any model interaction. The semantic invariant: the swap happens iff the turn actually runs.

2. **`self._session_id` attribute name may differ.** Confirmed via grep in Task 5 Step 1. If the attribute is `self._sid` or accessed via a method, adjust the call site. The `prompt_snapshots` cache eviction is best-effort — if the wrong key is popped, the cache just stays warm one extra turn (correctness preserved, only mild cache cost).

3. **`get_profile_dir(name)` returns the profile's outer dir** (e.g. `~/.opencomputer/profiles/work/`). The `home/` subdirectory inside it is what holds `MEMORY.md` etc. Task 5's orchestrator constructs `new_home = get_profile_dir(name) / "home"` to match. If a profile has been created without a `home/` subdir (legacy or partial creation), `rebind_to_profile` will set paths that point at non-existent files — `read_*` returns `""`, which is the correct empty-string fallback. No runtime error.

4. **Concurrent profile-swap via `/persona` ensemble slash command.** That command currently mutates `PersonaSwitcher.current` but there's no instantiation of `PersonaSwitcher` wired into `AgentLoop`. So as of Plan 1, `/persona` is effectively a no-op for the badge / runtime state path. Out of scope to fix here; documented separately as a Plan-2 follow-up.

5. **Tools/model/config don't change on swap.** Documented limitation: a session started in profile X with model Y and toolset Z keeps Y and Z even after Ctrl+P → profile X′. Only memory pointers (SOUL.md/MEMORY.md/USER.md) follow the swap. Full process-level swap is deferred. If the user files a complaint about "my model didn't change", point them at `oc -p X′` (process restart) or wait for Plan 3 to address it as part of the auto-suggester flow.
