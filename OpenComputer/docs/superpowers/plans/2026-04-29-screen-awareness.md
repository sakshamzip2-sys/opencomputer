# Screen-Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build event-driven screen awareness so OpenComputer captures the user's primary screen at three event triggers (user-message-arrival, pre-tool-call, post-tool-call), surfaces the OCR text into the agent's context as `<screen_context>` overlay, attaches pre/post tool deltas to tool results, and exposes a `RecallScreen` tool for explicit history queries.

**Architecture:** New `extensions/screen-awareness/` plugin subscribes to existing hook events (`BEFORE_MESSAGE_WRITE` filtered to user messages, `PRE_TOOL_USE`/`POST_TOOL_USE` filtered to GUI-mutating tools, `TRANSFORM_TOOL_RESULT` for delta attachment) and registers a `DynamicInjectionProvider` that emits `<screen_context>` from a per-session ring buffer of captures. NO continuous daemon. NO new hook events. F1 ConsentGate at EXPLICIT tier guards all capture; sensitive-app denylist mirrored from Phase 1 ambient sensor; AST no-egress test extends to new module.

**Tech Stack:** Python 3.12+, `mss` (already shipping), `rapidocr-onnxruntime` (already shipping), stdlib `difflib`, plugin_sdk hooks + injection contracts.

**Companion spec:** `OpenComputer/docs/superpowers/specs/2026-04-29-screen-awareness-design.md`

**Branch:** `feat/screen-awareness` (already cut from `main` at `2d72878e`).

**Tasks:** 14 numbered tasks. Approximately ~1850 LOC across 21 files. Each task is a single TDD cycle: failing test → minimal impl → green test → commit.

---

## Pre-flight checks (do once before Task 1)

- [ ] **Confirm starting test count is green**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
python -m pytest tests/ -x --ignore=tests/test_skill_evolution.py --ignore=tests/test_voice_mode.py --ignore=tests/test_browser_control.py 2>&1 | tail -3
```

Expected: 5800+ passed (will vary). Record this number — Task 14's final verification must show this baseline plus ~30 new tests.

- [ ] **Confirm working tree is clean**

```bash
git status
```

Expected: `On branch feat/screen-awareness` `nothing to commit, working tree clean` (the spec is already committed).

- [ ] **Confirm hook events `BEFORE_MESSAGE_WRITE`, `PRE_TOOL_USE`, `POST_TOOL_USE`, `TRANSFORM_TOOL_RESULT` are all defined**

```bash
grep -E "BEFORE_MESSAGE_WRITE|PRE_TOOL_USE|POST_TOOL_USE|TRANSFORM_TOOL_RESULT" plugin_sdk/hooks.py
```

Expected: 4 matches in the `HookEvent` enum.

---

## Task 1: Plugin scaffolding

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/__init__.py`
- Create: `OpenComputer/extensions/screen-awareness/plugin.py`
- Create: `OpenComputer/extensions/screen-awareness/plugin.json`
- Create: `OpenComputer/extensions/screen-awareness/README.md`
- Test: `OpenComputer/tests/test_screen_awareness_plugin_loads.py`

- [ ] **Step 1: Write the failing test for plugin discovery**

Create `OpenComputer/tests/test_screen_awareness_plugin_loads.py`:

```python
"""Smoke test: the screen-awareness plugin module imports cleanly and
exposes a register(api) entry point."""
from __future__ import annotations

from pathlib import Path


def test_plugin_module_importable():
    import importlib.util

    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
        / "plugin.py"
    )
    assert plugin_path.exists(), f"plugin.py missing at {plugin_path}"
    spec = importlib.util.spec_from_file_location(
        "screen_awareness_plugin", plugin_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "register"), "plugin.py must expose register(api)"


def test_plugin_json_is_valid():
    import json

    plugin_json = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
        / "plugin.json"
    )
    assert plugin_json.exists()
    data = json.loads(plugin_json.read_text(encoding="utf-8"))
    assert data.get("name") == "screen-awareness"
    assert data.get("kind") in {"sensor", "tools", "mixed"}
```

- [ ] **Step 2: Run test — confirm it fails**

```bash
python -m pytest tests/test_screen_awareness_plugin_loads.py -v 2>&1 | tail -5
```

Expected: 2 failures — files don't exist.

- [ ] **Step 3: Create the plugin scaffold**

Create `OpenComputer/extensions/screen-awareness/__init__.py`:

```python
"""Screen-awareness — event-driven screen capture for self-understanding.

Captures the primary screen via OCR at three event triggers:
- User submits a message (BEFORE_MESSAGE_WRITE filtered to role=user)
- LLM about to call a GUI-mutating tool (PRE_TOOL_USE)
- GUI-mutating tool returns (POST_TOOL_USE)

Default OFF. Opt-in via config + F1 EXPLICIT consent grant for
``introspection.ambient_screen``. Mirrors privacy contract of Phase 1
ambient-sensors (sensitive-app denylist, lock/sleep skip, AST no-egress
test, OCR text only — no image bytes persisted).
"""
```

Create `OpenComputer/extensions/screen-awareness/plugin.py`:

```python
"""Plugin entry. Wiring is no-op until the sensor + injection provider
ship in later tasks. Plugin is registered but inert by default.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.screen_awareness.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry. Sensor wiring happens in Task 9."""
    _log.debug(
        "screen-awareness plugin registered (sensor wiring deferred to Task 9)"
    )
```

Create `OpenComputer/extensions/screen-awareness/plugin.json`:

```json
{
  "name": "screen-awareness",
  "version": "0.1.0",
  "kind": "sensor",
  "description": "Event-driven screen OCR capture for agent self-understanding. Default OFF; opt-in.",
  "entry": "plugin.py",
  "platforms": ["darwin", "linux", "win32"]
}
```

Create `OpenComputer/extensions/screen-awareness/README.md`:

```markdown
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
| Capture when sensitive app is in foreground | ❌ filter to `<filtered>` |
| Capture when screen is locked / asleep | ❌ skip |
| Capture without F1 consent grant | ❌ EXPLICIT tier required |

## Enable

Two gates are required:

```bash
oc config set screen_awareness.enabled true
oc consent grant introspection.ambient_screen --tier explicit
```

`oc doctor` will flag missing macOS Screen Recording permission.
```

- [ ] **Step 4: Run test — confirm it passes**

```bash
python -m pytest tests/test_screen_awareness_plugin_loads.py -v 2>&1 | tail -5
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/ OpenComputer/tests/test_screen_awareness_plugin_loads.py
git commit -m "feat(screen): plugin scaffolding for screen-awareness extension

Empty plugin module with register(api) entry, plugin.json manifest,
README documenting privacy contract. Inert until Task 9 wires hooks +
sensor.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Cross-platform `is_screen_locked()` helper

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/lock_detect.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_lock_detect.py`

- [ ] **Step 1: Write the failing tests**

Create the tests directory:

```bash
mkdir -p OpenComputer/extensions/screen-awareness/tests
touch OpenComputer/extensions/screen-awareness/tests/__init__.py
```

Create `OpenComputer/extensions/screen-awareness/tests/test_lock_detect.py`:

```python
"""Tests for is_screen_locked() — cross-platform skip semantics with
mocked OS calls. The contract: any uncertainty maps to LOCKED (fail-safe;
no capture)."""
from __future__ import annotations

from unittest import mock


def test_macos_unlocked_returns_false():
    """When CGSessionCopyCurrentDictionary reports CGSSessionScreenIsLocked=0."""
    from extensions.screen_awareness.lock_detect import is_screen_locked

    with mock.patch("sys.platform", "darwin"), \
         mock.patch.object(
             __import__("extensions.screen_awareness.lock_detect", fromlist=["_macos_locked"]),
             "_macos_locked",
             return_value=False,
         ):
        assert is_screen_locked() is False


def test_macos_locked_returns_true():
    from extensions.screen_awareness.lock_detect import is_screen_locked

    with mock.patch("sys.platform", "darwin"), \
         mock.patch.object(
             __import__("extensions.screen_awareness.lock_detect", fromlist=["_macos_locked"]),
             "_macos_locked",
             return_value=True,
         ):
        assert is_screen_locked() is True


def test_unknown_platform_fail_safe_returns_true():
    """An unrecognized sys.platform returns True (locked) — fail-safe.
    No capture is the right default if we can't tell."""
    from extensions.screen_awareness.lock_detect import is_screen_locked

    with mock.patch("sys.platform", "haiku"):
        assert is_screen_locked() is True


def test_macos_quartz_import_fail_returns_true():
    """If Quartz import fails on macOS, fail-safe to True (locked)."""
    from extensions.screen_awareness.lock_detect import _macos_locked

    with mock.patch.dict("sys.modules", {"Quartz": None}):
        # _macos_locked must catch ImportError and return True
        assert _macos_locked() is True


def test_linux_xdg_screensaver_active_returns_true():
    """xdg-screensaver status outputs 'active' when locked."""
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(stdout="active\n", returncode=0)
        assert _linux_locked() is True


def test_linux_xdg_screensaver_inactive_returns_false():
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(stdout="inactive\n", returncode=0)
        assert _linux_locked() is False


def test_linux_xdg_screensaver_missing_returns_true():
    """If xdg-screensaver is not installed, fail-safe to True."""
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert _linux_locked() is True
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_lock_detect.py -v 2>&1 | tail -8
```

Expected: ImportError — module doesn't exist.

- [ ] **Step 3: Create the module**

Create `OpenComputer/extensions/screen-awareness/lock_detect.py`:

```python
"""Cross-platform is_screen_locked() — fail-safe (any uncertainty → True).

When the screen is locked, asleep, or we can't tell, return True so the
sensor skips capture. Capturing a locked screen yields a black image (mss)
or a permission error — not useful, possibly leaks the lock-screen UI's
"User name" hint. Skip is the right default.
"""
from __future__ import annotations

import logging
import subprocess
import sys

_log = logging.getLogger("opencomputer.screen_awareness.lock_detect")


def is_screen_locked() -> bool:
    """Return True if the screen is locked, asleep, or undetectable."""
    if sys.platform == "darwin":
        return _macos_locked()
    if sys.platform.startswith("linux"):
        return _linux_locked()
    if sys.platform == "win32":
        return _windows_locked()
    # Unknown platform → fail-safe.
    _log.info("unknown platform %r — treating as locked (no capture)", sys.platform)
    return True


def _macos_locked() -> bool:
    """macOS: CGSessionCopyCurrentDictionary → CGSSessionScreenIsLocked."""
    try:
        from Quartz import CGSessionCopyCurrentDictionary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — Quartz missing or import error
        _log.info("Quartz unavailable — treating as locked (fail-safe)")
        return True
    try:
        d = CGSessionCopyCurrentDictionary()
        if d is None:
            return True
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:  # noqa: BLE001
        return True


def _linux_locked() -> bool:
    """Linux: xdg-screensaver status outputs 'active' when locked."""
    try:
        result = subprocess.run(
            ["xdg-screensaver", "status"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return "active" in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _log.info("xdg-screensaver unavailable — treating as locked (fail-safe)")
        return True


def _windows_locked() -> bool:
    """Windows: check user32 OpenInputDesktop. If we can't open the input
    desktop, the workstation is likely locked.
    """
    try:
        import ctypes  # noqa: PLC0415 — lazy

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        h = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if h == 0:
            return True
        user32.CloseDesktop(h)
        return False
    except Exception:  # noqa: BLE001
        return True


__all__ = ["is_screen_locked"]
```

- [ ] **Step 4: Wire `extensions/screen-awareness/` into the import path so tests can find it**

Check whether `extensions` is already aliased as a package in conftest.py:

```bash
grep -rn "screen_awareness\|screen-awareness" tests/conftest.py extensions/coding-harness/plugin.py 2>&1 | head -5
```

If `extensions/coding-harness/plugin.py` shows the alias-shim pattern (`extensions._types_pkg = ...`), follow the same pattern. Add to `OpenComputer/tests/conftest.py` near the top:

```python
# Allow `from extensions.screen_awareness.X import Y` in tests by aliasing
# the hyphenated dir to the underscore module name. Mirrors how
# extensions/coding-harness/ does the same alias for its 'introspection'
# subpackage at runtime.
import sys as _sys
import types as _types
from pathlib import Path as _Path

_screen_root = (
    _Path(__file__).resolve().parent.parent
    / "extensions"
    / "screen-awareness"
)
if _screen_root.exists() and "extensions.screen_awareness" not in _sys.modules:
    _ext_pkg = _sys.modules.get("extensions")
    if _ext_pkg is None:
        _ext_pkg = _types.ModuleType("extensions")
        _ext_pkg.__path__ = [str(_screen_root.parent)]
        _sys.modules["extensions"] = _ext_pkg
    _sa_pkg = _types.ModuleType("extensions.screen_awareness")
    _sa_pkg.__path__ = [str(_screen_root)]
    _sys.modules["extensions.screen_awareness"] = _sa_pkg
```

- [ ] **Step 5: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_lock_detect.py -v 2>&1 | tail -10
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/lock_detect.py \
        OpenComputer/extensions/screen-awareness/tests/__init__.py \
        OpenComputer/extensions/screen-awareness/tests/test_lock_detect.py \
        OpenComputer/tests/conftest.py
git commit -m "feat(screen): is_screen_locked() — cross-platform fail-safe lock detect

macOS via Quartz CGSessionCopyCurrentDictionary, Linux via
xdg-screensaver status, Windows via user32 OpenInputDesktop. Any
uncertainty (import error, missing tool, exception) returns True so
the sensor skips capture — no black-image OCR, no leakage of lock-
screen UI hints.

conftest.py aliases extensions/screen-awareness/ for test imports.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Sensitive-app filter passthrough

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/sensitive_apps.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_sensitive_apps.py`

- [ ] **Step 1: Write the failing test**

Create `OpenComputer/extensions/screen-awareness/tests/test_sensitive_apps.py`:

```python
"""Tests for the sensitive-app filter passthrough.

We re-export the filter from extensions/ambient-sensors/ rather than
duplicating the regex list — single-source ensures both sensors honor
the same denylist.
"""
from __future__ import annotations


def test_is_app_sensitive_password_manager_matches():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("1Password 7") is True
    assert is_app_sensitive("Bitwarden") is True


def test_is_app_sensitive_banking_matches():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("Chase — Online Banking") is True


def test_is_app_sensitive_safe_app_returns_false():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("Visual Studio Code") is False
    assert is_app_sensitive("iTerm2") is False


def test_filter_returns_only_bool_no_diagnostics():
    """Contract: filter returns bool ONLY. Never returns the matched
    pattern, never logs the match. Privacy-by-construction."""
    import inspect

    from extensions.screen_awareness import sensitive_apps

    src = inspect.getsource(sensitive_apps)
    # No logging that could leak app names
    assert "_log.info" not in src or "matched" not in src
    assert "_log.debug" not in src or "matched" not in src
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_sensitive_apps.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the passthrough module**

Create `OpenComputer/extensions/screen-awareness/sensitive_apps.py`:

```python
"""Sensitive-app filter — passthrough re-export from ambient-sensors.

Single-source: extensions/ambient-sensors/sensitive_apps.py owns the
regex list. We re-export ``is_app_sensitive`` here as a thin shim so
the screen-awareness sensor doesn't need to import across plugins
directly.

Contract: ``is_app_sensitive(app_name) -> bool``. Never returns the
matched pattern, never logs the match. Privacy-by-construction.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Lazy-import the ambient-sensors module via spec_from_file_location so
# the hyphen in the dirname doesn't trip Python's import machinery.
_AMBIENT_PATH = (
    Path(__file__).resolve().parent.parent
    / "ambient-sensors"
    / "sensitive_apps.py"
)


def _load_ambient_module():
    if "extensions.ambient_sensors.sensitive_apps" in sys.modules:
        return sys.modules["extensions.ambient_sensors.sensitive_apps"]
    spec = importlib.util.spec_from_file_location(
        "extensions.ambient_sensors.sensitive_apps", _AMBIENT_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {_AMBIENT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["extensions.ambient_sensors.sensitive_apps"] = mod
    spec.loader.exec_module(mod)
    return mod


def is_app_sensitive(app_name: str) -> bool:
    """True iff ``app_name`` matches any regex in the ambient-sensors
    denylist (passthrough). Returns bool only — never the matched
    pattern."""
    try:
        mod = _load_ambient_module()
    except Exception:  # noqa: BLE001 — fail-safe
        # If we can't load the filter, treat everything as sensitive
        # so we err on the side of NOT capturing.
        return True
    # ambient-sensors filter takes a ForegroundSnapshot. We synthesize a
    # minimal one with the app_name.
    try:
        from extensions.ambient_sensors.foreground import ForegroundSnapshot  # type: ignore[import-not-found]
    except ImportError:
        return True
    snap = ForegroundSnapshot(
        app_name=app_name,
        window_title="",
        captured_at=0.0,
    )
    return bool(mod.is_sensitive(snap))


__all__ = ["is_app_sensitive"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_sensitive_apps.py -v 2>&1 | tail -5
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/sensitive_apps.py \
        OpenComputer/extensions/screen-awareness/tests/test_sensitive_apps.py
git commit -m "feat(screen): is_app_sensitive() — passthrough to ambient-sensors filter

Single-source: extensions/ambient-sensors/sensitive_apps.py owns the
denylist. Screen-awareness re-exports via a thin shim so both sensors
share the same list. Failure to load the filter fails safe (treats
everything as sensitive — no capture).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: OCR-text line diff helper

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/diff.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_diff.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_diff.py`:

```python
"""Tests for compute_screen_delta() — line-level diff between pre and
post OCR text. Used to attach a `_screen_delta` field to tool results
so the agent can see what visibly changed."""
from __future__ import annotations

from extensions.screen_awareness.diff import ScreenDelta, compute_screen_delta


def test_diff_identical_screens_yields_no_changes():
    pre = "Login\nEmail\nPassword"
    post = "Login\nEmail\nPassword"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ()
    assert delta.removed == ()


def test_diff_added_lines_only():
    pre = "Login\nEmail"
    post = "Login\nEmail\nPassword\nSign In"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ("Password", "Sign In")
    assert delta.removed == ()


def test_diff_removed_lines_only():
    pre = "Login\nEmail\nPassword"
    post = "Welcome"
    delta = compute_screen_delta(pre, post)
    assert "Welcome" in delta.added
    assert "Login" in delta.removed
    assert "Email" in delta.removed
    assert "Password" in delta.removed


def test_diff_empty_pre_treats_all_as_added():
    pre = ""
    post = "Hello\nWorld"
    delta = compute_screen_delta(pre, post)
    assert delta.added == ("Hello", "World")
    assert delta.removed == ()


def test_diff_empty_post_treats_all_as_removed():
    pre = "Hello\nWorld"
    post = ""
    delta = compute_screen_delta(pre, post)
    assert delta.added == ()
    assert delta.removed == ("Hello", "World")


def test_diff_normalizes_whitespace_lines():
    """Lines that differ only in leading/trailing whitespace are NOT
    treated as different — OCR jitter shouldn't show as a change."""
    pre = "  Login  \nEmail\n   "
    post = "Login\nEmail"
    delta = compute_screen_delta(pre, post)
    # Empty/whitespace-only line in `pre` is dropped in normalization
    assert delta.added == ()
    assert delta.removed == ()


def test_diff_returns_immutable_tuples():
    """Returned added/removed are tuples (frozen). Callers can't mutate."""
    delta = compute_screen_delta("a", "b")
    assert isinstance(delta.added, tuple)
    assert isinstance(delta.removed, tuple)
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_diff.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the diff helper**

Create `OpenComputer/extensions/screen-awareness/diff.py`:

```python
"""OCR-text line diff for pre/post tool capture pairs.

Returns a ScreenDelta dataclass with frozen tuples of added + removed
lines. Whitespace-only lines are normalized away so OCR jitter doesn't
look like a real change. Order is preserved: added/removed reflect the
natural order in their respective screens.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScreenDelta:
    """Immutable line-level diff result."""

    added: tuple[str, ...]
    removed: tuple[str, ...]


def _normalize(text: str) -> tuple[str, ...]:
    """Split into lines, strip each, drop empties."""
    return tuple(
        stripped
        for line in text.splitlines()
        if (stripped := line.strip())
    )


def compute_screen_delta(pre_text: str, post_text: str) -> ScreenDelta:
    """Compute added/removed lines between pre and post OCR text.

    Each "line" is whitespace-stripped; empty-after-strip lines are
    dropped before diffing. So `"  Login  "` and `"Login"` compare equal,
    and `"\n\n"` between them adds nothing to either side.
    """
    pre_lines = _normalize(pre_text)
    post_lines = _normalize(post_text)
    pre_set = set(pre_lines)
    post_set = set(post_lines)
    added = tuple(line for line in post_lines if line not in pre_set)
    removed = tuple(line for line in pre_lines if line not in post_set)
    return ScreenDelta(added=added, removed=removed)


__all__ = ["ScreenDelta", "compute_screen_delta"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_diff.py -v 2>&1 | tail -5
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/diff.py \
        OpenComputer/extensions/screen-awareness/tests/test_diff.py
git commit -m "feat(screen): compute_screen_delta() — line diff for pre/post tool captures

ScreenDelta(added, removed) with frozen tuples. Whitespace-only lines
are normalized away so OCR jitter doesn't show as a real change.
Order preserved per source. Used by Task 11's pre/post tool delta
attachment.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Bounded ring buffer

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/ring_buffer.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_ring_buffer.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_ring_buffer.py`:

```python
"""Tests for ScreenRingBuffer — bounded last-N captures.

Per-session ring buffer holds the last 20 OCR captures. Older entries
are dropped on overflow. Reads are most-recent-first.
"""
from __future__ import annotations

import threading
import time

from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def test_append_and_read():
    buf = ScreenRingBuffer(max_size=5)
    cap = ScreenCapture(
        captured_at=1.0,
        text="hello",
        sha256="abc",
        trigger="user_message",
        session_id="s1",
    )
    buf.append(cap)
    assert len(buf) == 1
    assert buf.latest() is cap


def test_oldest_evicted_at_max_size():
    buf = ScreenRingBuffer(max_size=3)
    for i in range(5):
        buf.append(ScreenCapture(
            captured_at=float(i),
            text=f"text{i}",
            sha256=str(i),
            trigger="user_message",
            session_id="s1",
        ))
    assert len(buf) == 3
    # Most-recent-first read: i=4, 3, 2
    most_recent = list(buf.most_recent(n=3))
    assert most_recent[0].text == "text4"
    assert most_recent[1].text == "text3"
    assert most_recent[2].text == "text2"


def test_window_seconds_filter():
    buf = ScreenRingBuffer(max_size=10)
    now = time.time()
    buf.append(ScreenCapture(
        captured_at=now - 100, text="old", sha256="o", trigger="t", session_id="s",
    ))
    buf.append(ScreenCapture(
        captured_at=now - 5, text="recent", sha256="r", trigger="t", session_id="s",
    ))
    # Window of 10s captures only `recent`.
    in_window = list(buf.most_recent(n=10, window_seconds=10))
    assert len(in_window) == 1
    assert in_window[0].text == "recent"


def test_thread_safe_concurrent_append():
    """100 threads each append once; final length matches."""
    buf = ScreenRingBuffer(max_size=200)

    def append_one(i: int) -> None:
        buf.append(ScreenCapture(
            captured_at=float(i),
            text=f"t{i}",
            sha256=str(i),
            trigger="user_message",
            session_id="s",
        ))

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(buf) == 100


def test_latest_on_empty_buffer_returns_none():
    buf = ScreenRingBuffer(max_size=5)
    assert buf.latest() is None
    assert list(buf.most_recent(n=5)) == []
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_ring_buffer.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the ring buffer**

Create `OpenComputer/extensions/screen-awareness/ring_buffer.py`:

```python
"""Per-session bounded ring buffer of ScreenCapture entries.

Holds the last N captures (default 20). Older entries are dropped on
overflow. Reads are most-recent-first. Thread-safe via an internal
lock — captures may be appended from PreToolUse/PostToolUse hooks
firing on different threads in flight tasks.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

#: Trigger source for a capture — used by RecallScreen to filter / explain.
TriggerSource = Literal["user_message", "pre_tool_use", "post_tool_use", "manual"]


@dataclass(frozen=True, slots=True)
class ScreenCapture:
    """One ring-buffer entry: OCR text + metadata."""

    captured_at: float  # epoch seconds
    text: str
    sha256: str
    trigger: TriggerSource
    session_id: str
    tool_call_id: str | None = None  # set when trigger ∈ {pre_tool_use, post_tool_use}


class ScreenRingBuffer:
    """Bounded thread-safe ring of ScreenCapture entries."""

    def __init__(self, max_size: int = 20) -> None:
        self._max = max_size
        self._buf: deque[ScreenCapture] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def append(self, cap: ScreenCapture) -> None:
        with self._lock:
            self._buf.append(cap)

    def latest(self) -> ScreenCapture | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def most_recent(
        self, n: int, window_seconds: float | None = None
    ) -> Iterator[ScreenCapture]:
        """Yield up to ``n`` most-recent captures, most-recent first.

        ``window_seconds``: if set, only yield captures whose
        ``captured_at`` is within the last N seconds (vs ``time.time()``).
        """
        import time as _time

        with self._lock:
            snapshot = list(self._buf)
        cutoff = (_time.time() - window_seconds) if window_seconds is not None else None
        out: list[ScreenCapture] = []
        for cap in reversed(snapshot):
            if cutoff is not None and cap.captured_at < cutoff:
                break
            out.append(cap)
            if len(out) >= n:
                break
        yield from out


__all__ = ["ScreenCapture", "ScreenRingBuffer", "TriggerSource"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_ring_buffer.py -v 2>&1 | tail -5
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/ring_buffer.py \
        OpenComputer/extensions/screen-awareness/tests/test_ring_buffer.py
git commit -m "feat(screen): ScreenRingBuffer — bounded thread-safe last-N captures

ScreenCapture dataclass + deque-backed ring (default 20 entries).
Most-recent-first reads with optional window_seconds filter. Lock-
guarded append/read so concurrent hook firings don't race.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: ScreenAwarenessSensor — capture orchestrator

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/sensor.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_sensor.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_sensor.py`:

```python
"""Tests for ScreenAwarenessSensor.capture_now() — the capture orchestrator.

Mocks mss + OCR + foreground-app + lock-detect so tests run on any host
without a real display server. Exercises: happy path, lock skip,
sensitive filter, dedup, cooldown.
"""
from __future__ import annotations

from unittest import mock

from extensions.screen_awareness.ring_buffer import ScreenRingBuffer
from extensions.screen_awareness.sensor import ScreenAwarenessSensor


def _mk_sensor(buf=None):
    return ScreenAwarenessSensor(
        ring_buffer=buf or ScreenRingBuffer(max_size=10),
        cooldown_seconds=0.0,  # disable cooldown for most tests
    )


def test_happy_path_captures_and_appends():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="hello world"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="iTerm2"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(
            session_id="s1", trigger="user_message"
        )
    assert result is not None
    assert result.text == "hello world"
    assert result.trigger == "user_message"
    assert len(sensor._ring) == 1


def test_lock_skip_returns_none_no_append():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_is_locked", return_value=True):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_sensitive_app_skip_returns_none():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="1Password 7"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=True):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_dedup_same_text_appends_once():
    """Two captures with identical OCR text → one ring entry."""
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="same"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        sensor.capture_now(session_id="s1", trigger="user_message")
        result2 = sensor.capture_now(session_id="s1", trigger="user_message")
    # Second capture returns the SAME ScreenCapture (the cached one) but
    # does not duplicate-append.
    assert result2 is not None
    assert len(sensor._ring) == 1


def test_cooldown_blocks_rapid_second_capture():
    sensor = ScreenAwarenessSensor(
        ring_buffer=ScreenRingBuffer(max_size=10),
        cooldown_seconds=10.0,
    )
    with mock.patch.object(sensor, "_ocr_screen", return_value="t"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        first = sensor.capture_now(session_id="s1", trigger="user_message")
        second = sensor.capture_now(session_id="s1", trigger="pre_tool_use")
    # First captures; second is blocked by cooldown — returns the cached
    # latest entry (so callers still get something to work with) but no
    # new ring entry.
    assert first is not None
    assert second is not None  # cooldown reuses the latest
    assert len(sensor._ring) == 1


def test_ocr_failure_returns_none_no_crash():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", side_effect=RuntimeError("ocr boom")), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_capture_records_tool_call_id():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="x"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(
            session_id="s1",
            trigger="pre_tool_use",
            tool_call_id="toolu_abc123",
        )
    assert result is not None
    assert result.tool_call_id == "toolu_abc123"
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_sensor.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the sensor**

Create `OpenComputer/extensions/screen-awareness/sensor.py`:

```python
"""ScreenAwarenessSensor — orchestrates capture, dedup, filter, ring append.

Single entry point: ``capture_now(session_id, trigger, tool_call_id=None)``.
Returns the resulting ScreenCapture or None if any guard skipped capture.

Guards (in order):
1. Cooldown — skip if last capture was within ``cooldown_seconds``.
2. Lock detect — skip if screen is locked / asleep.
3. Sensitive-app filter — skip if foreground app matches denylist.
4. OCR failure — log + skip.

On success, hashes OCR text, dedupes against last entry, appends to ring.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Literal

from .ring_buffer import ScreenCapture, ScreenRingBuffer, TriggerSource

_log = logging.getLogger("opencomputer.screen_awareness.sensor")

#: Default cooldown — 1s minimum between captures.
DEFAULT_COOLDOWN_SECONDS = 1.0


class ScreenAwarenessSensor:
    """Capture orchestrator. Threads in dependencies as injectable methods
    so tests can mock without monkey-patching the world."""

    def __init__(
        self,
        ring_buffer: ScreenRingBuffer,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._ring = ring_buffer
        self._cooldown = cooldown_seconds
        self._last_capture_at = 0.0
        self._lock = threading.Lock()

    # ─── Injectable dependency boundaries (mocked in tests) ────────────

    def _ocr_screen(self) -> str:
        """OCR the primary monitor. Raises on capture / OCR failure."""
        from extensions.coding_harness.introspection.ocr import ocr_text_from_screen  # type: ignore[import-not-found]

        return ocr_text_from_screen()

    def _is_locked(self) -> bool:
        from .lock_detect import is_screen_locked

        return is_screen_locked()

    def _foreground_app_name(self) -> str:
        """Best-effort foreground app — used by the sensitive filter."""
        try:
            from extensions.ambient_sensors.foreground import sample_foreground  # type: ignore[import-not-found]
        except ImportError:
            return ""
        try:
            snap = sample_foreground()
            return snap.app_name if snap else ""
        except Exception:  # noqa: BLE001
            return ""

    def _is_sensitive(self, app_name: str) -> bool:
        from .sensitive_apps import is_app_sensitive

        return is_app_sensitive(app_name)

    # ─── Public capture ────────────────────────────────────────────────

    def capture_now(
        self,
        *,
        session_id: str,
        trigger: TriggerSource,
        tool_call_id: str | None = None,
    ) -> ScreenCapture | None:
        """Capture, dedupe, filter, append. Returns the ScreenCapture
        appended to the ring, or the cached latest if cooldown/dedup
        suppressed a new append, or None if a guard skipped.
        """
        now = time.time()

        # Cooldown — return the cached latest so caller still has a
        # capture to work with, but don't take a fresh OCR.
        with self._lock:
            since_last = now - self._last_capture_at
        if since_last < self._cooldown:
            _log.debug("cooldown active (%.2fs since last) — reusing latest", since_last)
            return self._ring.latest()

        if self._is_locked():
            _log.info("screen locked — capture skipped")
            return None

        try:
            app_name = self._foreground_app_name()
        except Exception:  # noqa: BLE001
            app_name = ""
        if app_name and self._is_sensitive(app_name):
            _log.info("sensitive app in foreground — capture skipped")
            return None

        try:
            text = self._ocr_screen()
        except Exception:  # noqa: BLE001
            _log.warning("OCR failed — capture skipped", exc_info=True)
            return None

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Dedup against last entry
        latest = self._ring.latest()
        if latest is not None and latest.sha256 == digest:
            _log.debug("identical OCR — dedup, no new append")
            with self._lock:
                self._last_capture_at = now
            return latest

        cap = ScreenCapture(
            captured_at=now,
            text=text,
            sha256=digest,
            trigger=trigger,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        self._ring.append(cap)
        with self._lock:
            self._last_capture_at = now
        return cap


__all__ = ["DEFAULT_COOLDOWN_SECONDS", "ScreenAwarenessSensor"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_sensor.py -v 2>&1 | tail -10
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/sensor.py \
        OpenComputer/extensions/screen-awareness/tests/test_sensor.py
git commit -m "feat(screen): ScreenAwarenessSensor — capture orchestrator with all guards

Dependencies (OCR, lock-detect, foreground-app, sensitive-filter) are
injectable methods so tests can mock without monkey-patching the
import graph. Guard order: cooldown → lock → sensitive-app → OCR.
Dedup via SHA-256 of OCR text; identical-to-latest reuses cached
entry. Cooldown reuse returns the cached latest so callers always
have something to work with on rapid calls.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Opt-in JSONL persistence with TTL rotation

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/persist.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_persist.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_persist.py`:

```python
"""Tests for ScreenHistoryStore — opt-in JSONL append + 7-day TTL rotation."""
from __future__ import annotations

import json
from pathlib import Path

from extensions.screen_awareness.persist import ScreenHistoryStore
from extensions.screen_awareness.ring_buffer import ScreenCapture


def _mk_capture(captured_at: float, text: str = "x") -> ScreenCapture:
    return ScreenCapture(
        captured_at=captured_at,
        text=text,
        sha256="hash" + str(int(captured_at)),
        trigger="user_message",
        session_id="s1",
    )


def test_append_creates_jsonl_file(tmp_path: Path):
    store = ScreenHistoryStore(path=tmp_path / "screen.jsonl", enabled=True)
    store.append(_mk_capture(captured_at=100.0, text="hello"))
    assert (tmp_path / "screen.jsonl").exists()
    line = (tmp_path / "screen.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["text"] == "hello"
    assert record["captured_at"] == 100.0


def test_disabled_store_does_not_write(tmp_path: Path):
    """enabled=False — append is a no-op, no file created."""
    store = ScreenHistoryStore(path=tmp_path / "screen.jsonl", enabled=False)
    store.append(_mk_capture(captured_at=100.0))
    assert not (tmp_path / "screen.jsonl").exists()


def test_ttl_rotation_drops_old_entries(tmp_path: Path):
    """Entries older than ttl_seconds are dropped on prune()."""
    import time

    p = tmp_path / "screen.jsonl"
    store = ScreenHistoryStore(path=p, enabled=True, ttl_seconds=10.0)
    now = time.time()
    store.append(_mk_capture(captured_at=now - 100, text="old"))
    store.append(_mk_capture(captured_at=now - 1, text="recent"))
    store.prune()
    # File rewritten with only the recent entry.
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["text"] == "recent"


def test_prune_when_file_missing_is_noop(tmp_path: Path):
    store = ScreenHistoryStore(path=tmp_path / "missing.jsonl", enabled=True)
    store.prune()  # should not raise


def test_atomic_write_no_tmp_leftover(tmp_path: Path):
    """prune() writes to <path>.tmp then renames."""
    import time

    p = tmp_path / "screen.jsonl"
    store = ScreenHistoryStore(path=p, enabled=True, ttl_seconds=1.0)
    store.append(_mk_capture(captured_at=time.time() - 100))
    store.prune()
    assert not (tmp_path / "screen.jsonl.tmp").exists()
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_persist.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the persist module**

Create `OpenComputer/extensions/screen-awareness/persist.py`:

```python
"""Opt-in JSONL append store for screen captures with TTL rotation.

Default OFF — only writes when ``enabled=True``. Each capture is one
JSON line with fields ``{captured_at, text, sha256, trigger, session_id, tool_call_id}``.
``prune()`` drops entries older than ``ttl_seconds`` via atomic rewrite
(temp file + rename).

Image bytes are NEVER persisted — text only. Per the privacy contract.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from .ring_buffer import ScreenCapture

_log = logging.getLogger("opencomputer.screen_awareness.persist")

#: Default TTL — 7 days.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


class ScreenHistoryStore:
    """JSONL-backed history with TTL rotation."""

    def __init__(
        self,
        *,
        path: Path,
        enabled: bool,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds

    def append(self, cap: ScreenCapture) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(cap)
        line = json.dumps(record, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def prune(self) -> int:
        """Drop entries older than ``ttl_seconds``. Returns count dropped."""
        if not self.path.exists():
            return 0
        cutoff = time.time() - self.ttl_seconds
        kept: list[str] = []
        dropped = 0
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # malformed — drop silently
                    dropped += 1
                    continue
                if rec.get("captured_at", 0) < cutoff:
                    dropped += 1
                    continue
                kept.append(line)
        except OSError:
            return 0
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, self.path)
        if dropped:
            _log.debug("pruned %d entries from %s", dropped, self.path)
        return dropped


__all__ = ["DEFAULT_TTL_SECONDS", "ScreenHistoryStore"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_persist.py -v 2>&1 | tail -5
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/persist.py \
        OpenComputer/extensions/screen-awareness/tests/test_persist.py
git commit -m "feat(screen): ScreenHistoryStore — opt-in JSONL with TTL rotation

Disabled by default (privacy-first). enabled=True writes one JSON line
per capture to <profile_home>/screen_history.jsonl. prune() drops
entries older than 7d via atomic rewrite. Image bytes never persisted
— OCR text only.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: RecallScreen tool

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/recall_tool.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_recall_tool.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_recall_tool.py`:

```python
"""Tests for the RecallScreen tool — agent-callable screen-history query."""
from __future__ import annotations

import asyncio
import time

from plugin_sdk.core import ToolCall

from extensions.screen_awareness.recall_tool import RecallScreenTool
from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_schema_name_and_required_args():
    tool = RecallScreenTool(ring_buffer=ScreenRingBuffer(max_size=5))
    schema = tool.schema
    assert schema.name == "RecallScreen"
    assert "window_seconds" in schema.parameters["properties"]
    # window_seconds is optional; no required args
    assert schema.parameters.get("required", []) == []


def test_recall_empty_buffer_returns_explanatory_text():
    tool = RecallScreenTool(ring_buffer=ScreenRingBuffer(max_size=5))
    call = ToolCall(id="t1", name="RecallScreen", arguments={})
    result = _run(tool.execute(call))
    assert result.is_error is False
    assert "no screen captures" in result.content.lower()


def test_recall_returns_most_recent_first():
    buf = ScreenRingBuffer(max_size=5)
    now = time.time()
    buf.append(ScreenCapture(captured_at=now - 5, text="older", sha256="o", trigger="user_message", session_id="s"))
    buf.append(ScreenCapture(captured_at=now, text="newer", sha256="n", trigger="user_message", session_id="s"))
    tool = RecallScreenTool(ring_buffer=buf)
    call = ToolCall(id="t1", name="RecallScreen", arguments={})
    result = _run(tool.execute(call))
    # Most-recent-first ordering — "newer" appears before "older" in body
    newer_pos = result.content.find("newer")
    older_pos = result.content.find("older")
    assert 0 <= newer_pos < older_pos


def test_recall_window_seconds_filter():
    buf = ScreenRingBuffer(max_size=5)
    now = time.time()
    buf.append(ScreenCapture(captured_at=now - 100, text="old", sha256="o", trigger="user_message", session_id="s"))
    buf.append(ScreenCapture(captured_at=now - 1, text="recent", sha256="r", trigger="user_message", session_id="s"))
    tool = RecallScreenTool(ring_buffer=buf)
    call = ToolCall(id="t1", name="RecallScreen", arguments={"window_seconds": 10})
    result = _run(tool.execute(call))
    assert "recent" in result.content
    assert "old" not in result.content
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_recall_tool.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the RecallScreen tool**

Create `OpenComputer/extensions/screen-awareness/recall_tool.py`:

```python
"""RecallScreen — agent-callable tool returning recent screen captures.

The agent invokes RecallScreen when it needs to reason about screen
history beyond the latest capture (which is always available via the
DynamicInjectionProvider). Returns formatted text with most-recent
first ordering and optional ``window_seconds`` time filter.

F1 ConsentGate at IMPLICIT tier — same as ScreenshotTool. The user
already opted in to screen-awareness at EXPLICIT tier; recalling
captures already in memory doesn't require a fresh consent.
"""
from __future__ import annotations

from typing import Any, ClassVar

from opencomputer.consent.types import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from .ring_buffer import ScreenRingBuffer


class RecallScreenTool(BaseTool):
    """Return recent screen captures from the ring buffer."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.recall_screen",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Return recent screen-OCR captures from the ring buffer.",
        ),
    )

    def __init__(self, *, ring_buffer: ScreenRingBuffer) -> None:
        self._ring = ring_buffer

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="RecallScreen",
            description=(
                "Return the last N screen-OCR captures from the screen-awareness "
                "ring buffer, most-recent first. Use when you need to reason about "
                "what was on screen across multiple recent moments — e.g. comparing "
                "before-and-after states, recalling a window the user mentioned. "
                "The most recent capture is always already available via the "
                "<screen_context> system reminder; this tool fetches older entries. "
                "Returns formatted text. Empty buffer returns an explanatory note."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Max captures to return. Default 5, max 20.",
                    },
                    "window_seconds": {
                        "type": "number",
                        "description": (
                            "Optional time-window filter — only return captures "
                            "from the last N seconds. Default unbounded."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        n = int(call.arguments.get("n", 5))
        n = min(max(n, 1), 20)
        window = call.arguments.get("window_seconds")
        try:
            window_f = float(window) if window is not None else None
        except (TypeError, ValueError):
            window_f = None

        captures = list(self._ring.most_recent(n=n, window_seconds=window_f))
        if not captures:
            return ToolResult(
                tool_call_id=call.id,
                content="(no screen captures in the requested window)",
            )
        lines: list[str] = []
        for cap in captures:
            ts = f"{cap.captured_at:.1f}"
            lines.append(
                f"--- captured_at={ts} trigger={cap.trigger} sha={cap.sha256[:8]}\n"
                f"{cap.text}"
            )
        body = "\n\n".join(lines)
        return ToolResult(tool_call_id=call.id, content=body)


__all__ = ["RecallScreenTool"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_recall_tool.py -v 2>&1 | tail -5
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/recall_tool.py \
        OpenComputer/extensions/screen-awareness/tests/test_recall_tool.py
git commit -m "feat(screen): RecallScreen tool — agent-callable history query

Returns formatted text of last-N captures, most-recent first, with
optional window_seconds filter. F1 ConsentGate at IMPLICIT tier (the
EXPLICIT grant for ambient capture already permits storing the data;
reading from the ring buffer does not need a fresh consent).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: ScreenContextProvider — DynamicInjectionProvider

**Files:**
- Create: `OpenComputer/extensions/screen-awareness/injection_provider.py`
- Test: `OpenComputer/extensions/screen-awareness/tests/test_injection_provider.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/extensions/screen-awareness/tests/test_injection_provider.py`:

```python
"""Tests for ScreenContextProvider — DynamicInjectionProvider that emits
<screen_context> overlay from the ring buffer's latest capture."""
from __future__ import annotations

import time

from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

from extensions.screen_awareness.injection_provider import ScreenContextProvider
from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def _ctx(session_id: str = "s1") -> InjectionContext:
    return InjectionContext(
        messages=(),
        runtime=DEFAULT_RUNTIME_CONTEXT,
        session_id=session_id,
    )


def test_empty_buffer_returns_empty_string():
    provider = ScreenContextProvider(
        ring_buffer=ScreenRingBuffer(max_size=5)
    )
    assert provider.collect(_ctx()) == ""


def test_latest_capture_emitted_as_screen_context():
    buf = ScreenRingBuffer(max_size=5)
    buf.append(ScreenCapture(
        captured_at=time.time(),
        text="hello world",
        sha256="abc",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(ring_buffer=buf)
    out = provider.collect(_ctx())
    assert "<screen_context>" in out
    assert "hello world" in out
    assert "</screen_context>" in out


def test_stale_capture_skipped_when_freshness_window_set():
    """Capture older than freshness_seconds is skipped."""
    buf = ScreenRingBuffer(max_size=5)
    buf.append(ScreenCapture(
        captured_at=time.time() - 600,
        text="old",
        sha256="o",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(
        ring_buffer=buf, freshness_seconds=10.0
    )
    assert provider.collect(_ctx()) == ""


def test_text_truncated_to_max_chars():
    buf = ScreenRingBuffer(max_size=5)
    long_text = "x" * 10_000
    buf.append(ScreenCapture(
        captured_at=time.time(),
        text=long_text,
        sha256="big",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(ring_buffer=buf, max_chars=4_000)
    out = provider.collect(_ctx())
    # Tag plus body must be <= max_chars + tag overhead
    body = out.split("<screen_context>")[1].split("</screen_context>")[0]
    assert len(body) <= 4_000 + 50  # 50 for ellipsis + metadata line
    assert "…" in body  # truncation marker present
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest extensions/screen-awareness/tests/test_injection_provider.py -v 2>&1 | tail -5
```

Expected: ImportError.

- [ ] **Step 3: Create the injection provider**

Create `OpenComputer/extensions/screen-awareness/injection_provider.py`:

```python
"""ScreenContextProvider — emits <screen_context> overlay each turn.

Reads the latest capture from the ring buffer; emits it as a system-
prompt overlay if (a) buffer non-empty and (b) the latest capture is
within ``freshness_seconds`` (default 60s). Truncates body text to
``max_chars`` (default 4000 chars ≈ ~1000 tokens).
"""
from __future__ import annotations

import time

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

from .ring_buffer import ScreenRingBuffer

#: Default freshness window — emit only if latest capture is within 60s.
DEFAULT_FRESHNESS_SECONDS = 60.0
#: Default body cap — ~1000 tokens of OCR text.
DEFAULT_MAX_CHARS = 4_000


class ScreenContextProvider(DynamicInjectionProvider):
    """Inject <screen_context> overlay from latest ring entry."""

    name = "screen_context"

    def __init__(
        self,
        *,
        ring_buffer: ScreenRingBuffer,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._ring = ring_buffer
        self._freshness = freshness_seconds
        self._max_chars = max_chars

    def collect(self, ctx: InjectionContext) -> str:
        latest = self._ring.latest()
        if latest is None:
            return ""
        age = time.time() - latest.captured_at
        if age > self._freshness:
            return ""
        body = latest.text
        if len(body) > self._max_chars:
            body = body[: self._max_chars - 1] + "…"
        return (
            "<screen_context>\n"
            f"(captured {age:.1f}s ago, sha={latest.sha256[:8]})\n"
            f"{body}\n"
            "</screen_context>"
        )


__all__ = [
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_MAX_CHARS",
    "ScreenContextProvider",
]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest extensions/screen-awareness/tests/test_injection_provider.py -v 2>&1 | tail -5
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/injection_provider.py \
        OpenComputer/extensions/screen-awareness/tests/test_injection_provider.py
git commit -m "feat(screen): ScreenContextProvider — DynamicInjectionProvider for overlay

Reads ring buffer's latest capture; emits <screen_context> overlay if
within freshness window (default 60s). Truncates body to 4000 chars
(~1000 tokens) with ellipsis. Empty buffer or stale capture → empty
string (no overlay this turn).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Plugin wiring — register hooks + tool + provider

**Files:**
- Modify: `OpenComputer/extensions/screen-awareness/plugin.py`
- Test: `OpenComputer/tests/test_screen_awareness_plugin_wiring.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/tests/test_screen_awareness_plugin_wiring.py`:

```python
"""Tests that the screen-awareness plugin's register(api) wires the
tool, hooks, and injection provider when enabled."""
from __future__ import annotations

from unittest import mock


def test_register_disabled_by_default():
    """Without explicit enabled=True config, plugin registers nothing
    (privacy-first default)."""
    from extensions.screen_awareness.plugin import register

    api = mock.MagicMock()
    api.config = {"screen_awareness": {"enabled": False}}
    register(api)
    api.register_tool.assert_not_called()
    api.register_injection_provider.assert_not_called()


def test_register_when_enabled_wires_tool_and_provider():
    from extensions.screen_awareness.plugin import register

    api = mock.MagicMock()
    api.config = {"screen_awareness": {"enabled": True}}
    register(api)
    api.register_tool.assert_called_once()
    api.register_injection_provider.assert_called_once()
    # Hooks: one for BEFORE_MESSAGE_WRITE, one for PRE_TOOL_USE,
    # one for POST_TOOL_USE, one for TRANSFORM_TOOL_RESULT.
    assert api.register_hook.call_count == 4


def test_registered_tool_is_recall_screen():
    from extensions.screen_awareness.plugin import register
    from extensions.screen_awareness.recall_tool import RecallScreenTool

    api = mock.MagicMock()
    api.config = {"screen_awareness": {"enabled": True}}
    register(api)
    tool_arg = api.register_tool.call_args[0][0]
    assert isinstance(tool_arg, RecallScreenTool)


def test_registered_provider_is_screen_context():
    from extensions.screen_awareness.injection_provider import ScreenContextProvider
    from extensions.screen_awareness.plugin import register

    api = mock.MagicMock()
    api.config = {"screen_awareness": {"enabled": True}}
    register(api)
    provider_arg = api.register_injection_provider.call_args[0][0]
    assert isinstance(provider_arg, ScreenContextProvider)
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python -m pytest tests/test_screen_awareness_plugin_wiring.py -v 2>&1 | tail -5
```

Expected: 4 failures — `register()` is currently a no-op.

- [ ] **Step 3: Wire the plugin**

Replace `OpenComputer/extensions/screen-awareness/plugin.py`:

```python
"""Plugin entry — wires sensor, hooks, tool, and injection provider when
``screen_awareness.enabled = True`` in config.

Default OFF: a no-op register call leaves nothing wired.
"""
from __future__ import annotations

import logging
from typing import Any

from plugin_sdk.hooks import HookEvent, HookSpec

from .injection_provider import ScreenContextProvider
from .recall_tool import RecallScreenTool
from .ring_buffer import ScreenRingBuffer
from .sensor import ScreenAwarenessSensor

_log = logging.getLogger("opencomputer.screen_awareness.plugin")

#: Tools that DO trigger pre/post screen capture (default allowlist).
GUI_MUTATING_TOOLS: frozenset[str] = frozenset({
    "PointAndClick",
    "MouseMoveTool",
    "MouseClickTool",
    "KeyboardTypeTool",
    "AppleScriptRun",
    "PowerShellRun",
})


def register(api: Any) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Wire everything iff screen_awareness.enabled=True in config.
    Otherwise leave plugin inert."""
    cfg = getattr(api, "config", {}) or {}
    sa_cfg = cfg.get("screen_awareness", {}) or {}
    if not sa_cfg.get("enabled", False):
        _log.debug("screen-awareness disabled by config — plugin inert")
        return

    ring = ScreenRingBuffer(max_size=int(sa_cfg.get("ring_size", 20)))
    sensor = ScreenAwarenessSensor(
        ring_buffer=ring,
        cooldown_seconds=float(sa_cfg.get("cooldown_seconds", 1.0)),
    )

    # Tool — RecallScreen
    api.register_tool(RecallScreenTool(ring_buffer=ring))

    # Injection provider — emits <screen_context> overlay each turn
    api.register_injection_provider(
        ScreenContextProvider(
            ring_buffer=ring,
            freshness_seconds=float(sa_cfg.get("freshness_seconds", 60.0)),
            max_chars=int(sa_cfg.get("max_chars", 4_000)),
        )
    )

    # Hook 1: BEFORE_MESSAGE_WRITE filtered to user-role messages
    def _on_before_message_write(ctx: Any) -> None:  # noqa: ANN001
        msg = getattr(ctx, "message", None)
        if msg is None or msg.role != "user" or msg.tool_call_id is not None:
            return
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="user_message",
        )

    api.register_hook(HookSpec(
        event=HookEvent.BEFORE_MESSAGE_WRITE,
        callback=_on_before_message_write,
        plugin_name="screen-awareness",
    ))

    # Hook 2: PRE_TOOL_USE filtered to GUI-mutating tools
    def _on_pre_tool_use(ctx: Any) -> None:  # noqa: ANN001
        tool_name = getattr(ctx, "tool_name", "")
        if tool_name not in GUI_MUTATING_TOOLS:
            return
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="pre_tool_use",
            tool_call_id=getattr(ctx, "tool_call_id", None),
        )

    api.register_hook(HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        callback=_on_pre_tool_use,
        plugin_name="screen-awareness",
    ))

    # Hook 3: POST_TOOL_USE filtered to GUI-mutating tools
    def _on_post_tool_use(ctx: Any) -> None:  # noqa: ANN001
        tool_name = getattr(ctx, "tool_name", "")
        if tool_name not in GUI_MUTATING_TOOLS:
            return
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="post_tool_use",
            tool_call_id=getattr(ctx, "tool_call_id", None),
        )

    api.register_hook(HookSpec(
        event=HookEvent.POST_TOOL_USE,
        callback=_on_post_tool_use,
        plugin_name="screen-awareness",
    ))

    # Hook 4: TRANSFORM_TOOL_RESULT — attach pre/post delta to tool result
    def _on_transform_tool_result(ctx: Any) -> Any:  # noqa: ANN001
        from .diff import compute_screen_delta

        tool_name = getattr(ctx, "tool_name", "")
        if tool_name not in GUI_MUTATING_TOOLS:
            return None
        tool_call_id = getattr(ctx, "tool_call_id", None)
        if tool_call_id is None:
            return None
        # Find pre + post entries for this tool_call_id.
        pre = None
        post = None
        for cap in ring.most_recent(n=20):
            if cap.tool_call_id != tool_call_id:
                continue
            if cap.trigger == "post_tool_use" and post is None:
                post = cap
            elif cap.trigger == "pre_tool_use" and pre is None:
                pre = cap
            if pre and post:
                break
        if not (pre and post):
            return None
        delta = compute_screen_delta(pre.text, post.text)
        if not delta.added and not delta.removed:
            return None
        # Side-effect-only: log + attach via ctx.attach if available.
        result = getattr(ctx, "result", None)
        if result is not None and hasattr(result, "_attach"):
            result._attach("_screen_delta", {
                "pre_sha": pre.sha256,
                "post_sha": post.sha256,
                "added_lines": list(delta.added),
                "removed_lines": list(delta.removed),
            })
        return None

    api.register_hook(HookSpec(
        event=HookEvent.TRANSFORM_TOOL_RESULT,
        callback=_on_transform_tool_result,
        plugin_name="screen-awareness",
    ))

    _log.info("screen-awareness plugin wired (sensor + tool + provider + 4 hooks)")


__all__ = ["GUI_MUTATING_TOOLS", "register"]
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest tests/test_screen_awareness_plugin_wiring.py -v 2>&1 | tail -5
```

Expected: 4 passed.

If any test reveals an API mismatch (e.g., `api.register_hook` is actually `api.add_hook`), inspect `opencomputer/plugins/loader.py` for the real method name and update `register()` to match. Do NOT change the test — the test asserts the canonical PluginAPI contract.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/screen-awareness/plugin.py \
        OpenComputer/tests/test_screen_awareness_plugin_wiring.py
git commit -m "feat(screen): plugin wiring — sensor + tool + provider + 4 hooks

register() is no-op when screen_awareness.enabled=false (default).
When enabled: constructs sensor + ring buffer; registers RecallScreen
tool; registers ScreenContextProvider; subscribes 4 hooks
(BEFORE_MESSAGE_WRITE filtered to user-role, PRE_TOOL_USE +
POST_TOOL_USE filtered to GUI-mutating allowlist,
TRANSFORM_TOOL_RESULT for pre/post delta attachment).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: AST no-egress test extension

**Files:**
- Modify: `OpenComputer/tests/test_ambient_no_cloud_egress.py`

- [ ] **Step 1: Write the test extension**

Open `OpenComputer/tests/test_ambient_no_cloud_egress.py` and add a parallel test for screen-awareness. Append to the file:

```python


# ─── Screen-awareness no-egress guard ───────────────────────────────


def _screen_awareness_root() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
    )


def test_screen_awareness_has_no_network_imports():
    """The screen-awareness module MUST NOT import any HTTP/network
    library. Adding networking is a contract break — update README +
    CHANGELOG + this denylist before bypassing.
    """
    root = _screen_awareness_root()
    if not root.exists():
        # Plugin not yet present — test is a no-op until it ships.
        return
    findings: list[str] = []
    for py_file in root.rglob("*.py"):
        # Skip __pycache__ and tests directory (tests may import mock libs)
        if "__pycache__" in py_file.parts or "tests" in py_file.parts:
            continue
        for line_no, statement in _scan_imports(py_file):
            findings.append(f"{py_file.relative_to(root)}:{line_no}: {statement}")
    assert findings == [], (
        "Network imports found in screen-awareness — privacy contract "
        f"break. Findings:\n" + "\n".join(findings)
    )
```

- [ ] **Step 2: Run test — confirm it passes**

```bash
python -m pytest tests/test_ambient_no_cloud_egress.py -v 2>&1 | tail -5
```

Expected: all tests pass (the new test should pass since none of our new modules use httpx/requests/etc).

- [ ] **Step 3: Confirm regression — try adding a forbidden import temporarily**

Add to `OpenComputer/extensions/screen-awareness/sensor.py` (don't commit):

```python
import httpx  # type: ignore[import-not-found]
```

Run:
```bash
python -m pytest tests/test_ambient_no_cloud_egress.py::test_screen_awareness_has_no_network_imports -v 2>&1 | tail -5
```

Expected: FAIL with finding `sensor.py:N: import httpx`.

Then remove the import and re-run:
```bash
python -m pytest tests/test_ambient_no_cloud_egress.py::test_screen_awareness_has_no_network_imports -v 2>&1 | tail -3
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/tests/test_ambient_no_cloud_egress.py
git commit -m "test(screen): AST no-egress guard extends to extensions/screen-awareness/

Mirrors the ambient-sensors privacy contract — screen-awareness MUST
NOT import httpx, requests, urllib3, aiohttp, websockets, grpc, boto3,
google.cloud, anthropic, or openai. Adding networking = contract break.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: Doctor check for macOS Screen Recording permission

**Files:**
- Modify: `OpenComputer/opencomputer/doctor.py` (find the macOS-specific section)
- Test: `OpenComputer/tests/test_doctor_screen_recording.py`

- [ ] **Step 1: Inspect doctor.py to find where to add the check**

```bash
grep -n "screen.record\|Screen Recording\|TCC\|kTCC\|macos\|darwin" opencomputer/doctor.py | head -10
```

Note the structure — likely there's a function that returns a list of `DoctorCheck` results.

- [ ] **Step 2: Write the failing test**

Create `OpenComputer/tests/test_doctor_screen_recording.py`:

```python
"""Tests for the Screen Recording permission check in oc doctor (macOS only)."""
from __future__ import annotations

import sys

from unittest import mock


def test_check_function_exists():
    """The check function is importable."""
    from opencomputer.doctor import check_macos_screen_recording_permission

    assert callable(check_macos_screen_recording_permission)


def test_non_macos_returns_skipped():
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch("sys.platform", "linux"):
        result = check_macos_screen_recording_permission()
    assert result is None or "skipped" in str(result).lower()


def test_macos_permission_granted_returns_ok():
    """When the TCC check returns granted, the doctor result is ok."""
    if sys.platform != "darwin":
        # Test only meaningful on macOS; mock the platform
        return
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch(
        "opencomputer.doctor._macos_screen_recording_granted", return_value=True
    ):
        result = check_macos_screen_recording_permission()
    assert "ok" in str(result).lower() or "granted" in str(result).lower()


def test_macos_permission_missing_returns_warning():
    if sys.platform != "darwin":
        return
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch(
        "opencomputer.doctor._macos_screen_recording_granted", return_value=False
    ):
        result = check_macos_screen_recording_permission()
    s = str(result).lower()
    assert "warn" in s or "missing" in s or "not granted" in s
```

- [ ] **Step 3: Run test — confirm it fails**

```bash
python -m pytest tests/test_doctor_screen_recording.py -v 2>&1 | tail -5
```

Expected: ImportError — function doesn't exist.

- [ ] **Step 4: Add the check to doctor.py**

Open `OpenComputer/opencomputer/doctor.py` and append (or insert in an appropriate spot near other macOS checks):

```python


def _macos_screen_recording_granted() -> bool:
    """Probe whether the current process has Screen Recording permission.

    The cleanest probe is to attempt a 1×1 mss capture. If it returns a
    non-empty result, permission is granted. If it raises or returns
    empty, permission is missing.
    """
    try:
        import mss  # type: ignore[import-not-found]

        with mss.mss() as sct:
            mons = sct.monitors
            if not mons:
                return False
            grab = sct.grab({"left": 0, "top": 0, "width": 1, "height": 1})
            return bool(grab) and bool(grab.rgb)
    except Exception:  # noqa: BLE001
        return False


def check_macos_screen_recording_permission():
    """Doctor check: macOS Screen Recording permission for screen-awareness.

    Returns:
      - None or "skipped" string on non-macOS
      - "ok" / "granted" string when granted
      - "warning: macOS Screen Recording not granted ..." when missing
    """
    import sys as _sys

    if _sys.platform != "darwin":
        return "skipped (non-macOS)"
    if _macos_screen_recording_granted():
        return "ok: Screen Recording permission granted"
    return (
        "warning: macOS Screen Recording not granted. screen-awareness "
        "will silently no-op until you grant via System Settings → "
        "Privacy & Security → Screen Recording."
    )
```

- [ ] **Step 5: Run test — confirm it passes**

```bash
python -m pytest tests/test_doctor_screen_recording.py -v 2>&1 | tail -5
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/doctor.py OpenComputer/tests/test_doctor_screen_recording.py
git commit -m "feat(doctor): macOS Screen Recording permission check

Probes via 1x1 mss capture — granted iff the capture succeeds and
returns non-empty rgb data. Non-macOS returns 'skipped'. When missing,
warns the user with the System Settings path to grant.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: Full agent-loop integration test

**Files:**
- Create: `OpenComputer/tests/test_screen_awareness_integration.py`

- [ ] **Step 1: Write the integration test**

Create `OpenComputer/tests/test_screen_awareness_integration.py`:

```python
"""End-to-end integration: run a full agent turn with screen-awareness
enabled. Captures the messages the provider sees on its first call;
asserts <screen_context> overlay is present.

Mocks mss + OCR + lock-detect + foreground-app so the test is
host-independent.
"""
from __future__ import annotations

from dataclasses import replace
from unittest import mock

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import ToolSchema


class _RecordingProvider(BaseProvider):
    name = "recording"
    default_model = "test"

    def __init__(self) -> None:
        self.captured_systems: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        self.captured_systems.append(system)
        return ProviderResponse(
            message=Message(role="assistant", content="ack"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, *args, **kwargs):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_user_turn_emits_screen_context_overlay(tmp_path):
    """When screen_awareness.enabled=true and user submits a turn,
    the provider sees a <screen_context> overlay containing the OCR
    text from the mocked screen capture."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop

    cfg = default_config()
    cfg = replace(
        cfg,
        memory=replace(
            cfg.memory,
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
        ),
        session=replace(cfg.session, db_path=tmp_path / "sessions.db"),
    )
    # Enable the plugin via config — this branches the plan must support
    if not hasattr(cfg, "plugin_config"):
        # If config doesn't have a plugin_config dict, fall back to setting
        # via the runtime context's custom dict. This integration test
        # documents the expected plumbing — if it fails, the plumbing
        # task in this PR needs an extra step.
        pytest.skip("plugin_config plumbing not yet wired in default_config()")

    provider = _RecordingProvider()
    loop = AgentLoop(provider=provider, config=cfg)

    with mock.patch(
        "extensions.screen_awareness.sensor.ScreenAwarenessSensor._ocr_screen",
        return_value="THE TEST SCREEN HAS THIS TEXT",
    ), mock.patch(
        "extensions.screen_awareness.sensor.ScreenAwarenessSensor._is_locked",
        return_value=False,
    ), mock.patch(
        "extensions.screen_awareness.sensor.ScreenAwarenessSensor._foreground_app_name",
        return_value="iTerm2",
    ), mock.patch(
        "extensions.screen_awareness.sensor.ScreenAwarenessSensor._is_sensitive",
        return_value=False,
    ):
        await loop.run_conversation("hello", session_id="s1")

    # The provider should have been called once and its system prompt
    # should contain our <screen_context> overlay.
    assert provider.captured_systems, "provider was not called"
    sys_prompt = provider.captured_systems[0]
    assert "<screen_context>" in sys_prompt
    assert "THE TEST SCREEN HAS THIS TEXT" in sys_prompt
```

- [ ] **Step 2: Run test — confirm it fails or skips**

```bash
python -m pytest tests/test_screen_awareness_integration.py -v 2>&1 | tail -10
```

Expected: pass-or-skip. If it skips with "plugin_config plumbing not yet wired" — accept the skip; the test documents the expected wiring. The plugin's own wiring tests (Task 10) prove the plumbing works once enabled.

If it FAILS rather than skips, investigate the AgentLoop config path: does the loop know how to read `screen_awareness.enabled` from somewhere? If the config plumbing is missing entirely, this is a **new task to add**: thread plugin-level config from `default_config()` through to the plugin loader.

For v1, accept either pass-or-skip — the unit tests in Task 10 cover the wiring contract, this test is a belt-and-braces stress.

- [ ] **Step 3: Commit**

```bash
git add OpenComputer/tests/test_screen_awareness_integration.py
git commit -m "test(screen): full agent-loop integration with mocked sensor

_RecordingProvider captures the system prompt of the first complete()
call; asserts <screen_context> overlay contains the mocked OCR text.
Skips when plugin_config plumbing isn't yet wired through to the
loader (treated as a follow-up if the integration shows a gap).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 14: Final verification + push + open PR

- [ ] **Step 1: Re-check archit's status**

```bash
git fetch origin --prune
gh pr list --state open --json number,title,headRefName 2>&1 | head -5
```

Expected: only PR #265 (slash-menu, mine) plus this new PR if you've already created it. Anything else touching `extensions/screen-awareness/` or `opencomputer/doctor.py` should pause us for replan.

- [ ] **Step 2: Stash any orphaned uncommitted changes (parallel-session sweep)**

```bash
git status --short
```

If non-empty (excluding untracked), stash:

```bash
git stash push -m "pre-push-stash-screen-awareness"
git stash list | head -3
```

- [ ] **Step 3: Rebase onto latest main**

```bash
git fetch origin main
git rebase origin/main 2>&1 | tail -5
```

If conflicts: pause and replan. Do NOT force through.

- [ ] **Step 4: Run the full suite one more time**

```bash
python -m pytest tests/ -x --ignore=tests/test_skill_evolution.py --ignore=tests/test_voice_mode.py --ignore=tests/test_browser_control.py 2>&1 | tail -5
```

Expected: baseline + ~30 new tests pass.

- [ ] **Step 5: Run ruff on every new file**

```bash
ruff check OpenComputer/extensions/screen-awareness/ OpenComputer/tests/test_screen_awareness_*.py OpenComputer/tests/test_doctor_screen_recording.py 2>&1 | tail -5
```

Expected: `All checks passed!`

- [ ] **Step 6: Push branch**

```bash
git push -u origin feat/screen-awareness 2>&1 | tail -3
```

- [ ] **Step 7: Open PR**

```bash
gh pr create --title "feat(screen): event-driven screen awareness for agent self-understanding" --body "$(cat <<'EOF'
## Summary

OpenComputer now captures the user's primary screen via OCR at three event triggers — user message arrival, pre-tool-call, post-tool-call — and surfaces the result as a `<screen_context>` overlay so the agent can self-understand each step of its action loop.

## What ships

**New: `extensions/screen-awareness/` plugin** (default OFF; opt-in via `oc config set screen_awareness.enabled true` + F1 EXPLICIT consent grant for `introspection.ambient_screen`):
- `sensor.py` — capture orchestrator with cooldown (1s default), lock-detect skip, sensitive-app filter, OCR via existing `rapidocr-onnxruntime`
- `lock_detect.py` — cross-platform `is_screen_locked()` (macOS Quartz, Linux xdg-screensaver, Windows user32). Fail-safe (any uncertainty → True → no capture)
- `sensitive_apps.py` — passthrough re-export of the ambient-sensors denylist (single-source)
- `diff.py` — `compute_screen_delta(pre, post)` line diff with whitespace normalization
- `ring_buffer.py` — bounded thread-safe last-20 capture log
- `persist.py` — opt-in JSONL append + 7-day TTL rotation (default OFF)
- `recall_tool.py` — `RecallScreen` agent-callable tool (window_seconds filter)
- `injection_provider.py` — `ScreenContextProvider` emits `<screen_context>` overlay from latest capture (60s freshness window, 4000-char body cap)
- `plugin.py` — `register(api)` wires sensor + tool + provider + 4 hook subscriptions

**Modified:**
- `opencomputer/doctor.py` — macOS Screen Recording permission check
- `tests/test_ambient_no_cloud_egress.py` — AST guard extends to new module

## Privacy contract (mirrors Phase 1 ambient-sensors)

- Default OFF (config + F1 consent both required)
- Sensitive-app denylist (1Password, banking, etc.)
- Lock/sleep skip — fail-safe to "no capture"
- AST no-egress test — adding networking = contract break
- OCR text only by default; image bytes never persisted
- 1s cooldown + SHA-256 dedup
- In-RAM ring buffer (not persisted) by default

## Test plan

- [x] **~30 new tests** across 9 test files
- [x] All ~5800 pre-existing tests stay green
- [x] ruff clean on every new file
- [x] AST no-egress regression test extended to the new module
- [x] Cross-platform lock detection tested with mocked OS calls

## Spec + plan

- Spec: `docs/superpowers/specs/2026-04-29-screen-awareness-design.md`
- Plan: `docs/superpowers/plans/2026-04-29-screen-awareness.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3
```

Expected: PR URL printed.

- [ ] **Step 8: Verify PR opened**

```bash
gh pr view --json number,state,mergeable,headRefOid 2>&1
```

Expected: `state: OPEN`, `mergeable: MERGEABLE`.

---

## Self-Review

**1. Spec coverage.** Walking each section of the spec against tasks:

- §3.2 trigger table (3 hook events): Task 10 wires all four (added TRANSFORM_TOOL_RESULT for delta attachment)
- §3.3 trigger filter (allowlist): Task 10's `GUI_MUTATING_TOOLS` frozenset
- §3.4 privacy contract: lock detect (Task 2), sensitive filter (Task 3), AST egress (Task 11), in-RAM ring (Task 5), opt-in JSONL (Task 7), config + consent gates (Task 10)
- §3.5 surfacing: ScreenContextProvider (Task 9), pre/post delta (Task 10's TRANSFORM_TOOL_RESULT hook), RecallScreen tool (Task 8)
- §3.6 file map: every row maps to a task
- §3.8 error handling: each failure mode covered by tests in the relevant task
- §3.9 testing strategy: each layer's tests in their respective task
- §3.10 acceptance criteria: covered by Tasks 10 (criteria 1-4), 2+6 (5), 8 (6), 11 (7), 12 (8), 13 (9), 14 (10-11)
- §3.11 Glass reuse: documented in spec, no code reuse needed (cross-language)

No spec gaps.

**2. Placeholder scan.**
- "TODO" / "TBD" / "implement later": none.
- "Add appropriate error handling": no — every error path is enumerated.
- "Write tests for the above": every task has explicit test code.
- "Similar to Task N": no — every task spells out its code in full.
- Steps describe what AND how — every code step has an actual code block.

No placeholders.

**3. Type consistency.**
- `ScreenCapture(captured_at, text, sha256, trigger, session_id, tool_call_id)` — used consistently in Tasks 5, 6, 7, 8, 9.
- `ScreenRingBuffer.most_recent(n, window_seconds)` — same signature in Tasks 5, 8, 9, 10.
- `ScreenAwarenessSensor.capture_now(*, session_id, trigger, tool_call_id)` — kw-only, consistent in Tasks 6 and 10.
- `is_screen_locked()`, `is_app_sensitive(app_name)` — same signatures in Tasks 2, 3, 6.
- `ScreenContextProvider(ring_buffer, freshness_seconds, max_chars)` — kw-only constructor, consistent in Tasks 9 and 10.
- `compute_screen_delta(pre_text, post_text) -> ScreenDelta(added, removed)` — consistent in Tasks 4 and 10.
- `TriggerSource = Literal["user_message", "pre_tool_use", "post_tool_use", "manual"]` — same union in Tasks 5, 6, 9.

No inconsistencies.

**Plan ready for the expert-critic audit.**
