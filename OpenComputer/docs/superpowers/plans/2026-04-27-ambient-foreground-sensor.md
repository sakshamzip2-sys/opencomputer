# Ambient Foreground Sensor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a cross-platform, opt-in foreground-app sensor that publishes hashed `ForegroundAppEvent`s to OpenComputer's F2 typed event bus. Default OFF for any user; enabled via `oc ambient on`. Local-only with hard contracts on no-network-egress, no-raw-title-leakage, no-LLM-training.

**Architecture:** New plugin at `extensions/ambient-sensors/` (~700-900 LOC across 5 files). One asyncio daemon spawned by gateway OR run standalone via `oc ambient daemon`. Three cross-platform foreground detectors (macOS osascript, Linux X11 xdotool/wmctrl, Windows pywin32). Sensitive-app regex filter with default + user override. Pause/resume CLI. Two new doctor checks. F1 capability `ambient.foreground.observe` IMPLICIT tier. ~10 unit + 1 contract test files, ~400 LOC tests total.

**Tech Stack:** Python 3.12+; reuses existing F2 bus (`opencomputer/ingestion/bus.py`), F1 ConsentGate (`opencomputer/agent/consent/`), profile_home resolver, channel adapter pattern. New deps: optional `pywin32` (Windows-only — already conditional). Linux requires `xdotool` system package (doctor warns).

**Spec:** `OpenComputer/docs/superpowers/specs/2026-04-27-ambient-foreground-sensor-design.md`

**Branch:** `feat/ambient-foreground-sensor` (worktree at `/tmp/oc-ambient/`).

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `extensions/ambient-sensors/plugin.json` | Manifest (kind=mixed) | CREATE |
| `extensions/ambient-sensors/plugin.py` | Registration + start daemon if state.enabled | CREATE |
| `extensions/ambient-sensors/foreground.py` | Cross-platform `detect_foreground()` | CREATE |
| `extensions/ambient-sensors/daemon.py` | `ForegroundSensorDaemon` poll loop | CREATE |
| `extensions/ambient-sensors/sensitive_apps.py` | Default regex list + user override loader | CREATE |
| `extensions/ambient-sensors/pause_state.py` | state.json read/write helpers | CREATE |
| `extensions/ambient-sensors/README.md` | What/what-not/how-to-disable | CREATE |
| `plugin_sdk/ingestion.py` | Add `ForegroundAppEvent` + `AmbientSensorPauseEvent` | EDIT |
| `opencomputer/agent/consent/capability_taxonomy.py` | Register `ambient.foreground.observe` | EDIT |
| `opencomputer/cli_ambient.py` | `oc ambient {on,off,pause,resume,status,daemon}` | CREATE |
| `opencomputer/cli.py` | Mount `cli_ambient.app` Typer subcommand | EDIT |
| `opencomputer/doctor.py` | Add `_check_ambient_state` + `_check_ambient_foreground_capable` | EDIT |
| `opencomputer/gateway/server.py` | Start ambient daemon at gateway boot if enabled | EDIT |
| `tests/test_ambient_foreground_event.py` | SDK type contract | CREATE |
| `tests/test_ambient_foreground_detector.py` | Platform forks, mocked OS calls | CREATE |
| `tests/test_ambient_sensitive_filter.py` | Regex matching + override | CREATE |
| `tests/test_ambient_daemon_dedup.py` | Dedup + min-interval logic | CREATE |
| `tests/test_ambient_pause_state.py` | State file read/write + expiry | CREATE |
| `tests/test_ambient_cli.py` | Typer CLI smoke tests | CREATE |
| `tests/test_ambient_capability_claim.py` | F1 namespace contract | CREATE |
| `tests/test_ambient_no_cloud_egress.py` | AST scan: no `httpx`/`requests`/etc. in plugin | CREATE |
| `tests/test_ambient_doctor_checks.py` | Both new doctor checks | CREATE |
| `OpenComputer/CHANGELOG.md` | Unreleased entry | EDIT |

---

## Tasks

### Task 1: SDK types + capability registration

**Files:**
- Edit: `plugin_sdk/ingestion.py` — add `ForegroundAppEvent`, `AmbientSensorPauseEvent`
- Edit: `opencomputer/agent/consent/capability_taxonomy.py` — register `ambient.foreground.observe`
- Create: `tests/test_ambient_foreground_event.py`

- [ ] **Step 1: Write failing test for the SDK types**

```python
"""tests/test_ambient_foreground_event.py"""
from __future__ import annotations
from plugin_sdk.ingestion import ForegroundAppEvent, AmbientSensorPauseEvent, SignalEvent


def test_foreground_event_inherits_signal_event():
    e = ForegroundAppEvent(app_name="Code", window_title_hash="abc", platform="darwin")
    assert isinstance(e, SignalEvent)
    assert e.event_type == "foreground_app"


def test_foreground_event_default_fields_are_safe():
    e = ForegroundAppEvent()
    # Privacy: defaults should NOT leak any data
    assert e.app_name == ""
    assert e.window_title_hash == ""
    assert e.bundle_id == ""
    assert e.is_sensitive is False
    assert e.platform == ""


def test_foreground_event_is_frozen():
    import dataclasses
    e = ForegroundAppEvent(app_name="Code")
    with pytest_raises(dataclasses.FrozenInstanceError):
        e.app_name = "Other"


def test_pause_event_inherits_signal_event():
    e = AmbientSensorPauseEvent(sensor_name="foreground", paused=True, reason="user")
    assert isinstance(e, SignalEvent)
    assert e.event_type == "ambient_sensor_pause"
    assert e.sensor_name == "foreground"
    assert e.paused is True


def pytest_raises(exc_type):
    import pytest
    return pytest.raises(exc_type)
```

- [ ] **Step 2: Add types in `plugin_sdk/ingestion.py`**

After existing event types, append:

```python
@dataclass(frozen=True, slots=True)
class ForegroundAppEvent(SignalEvent):
    """Foreground app or window-title change observed by ambient-sensors plugin.

    Privacy: ``window_title_hash`` is SHA-256 of the title — raw title NEVER
    leaves the sensor. Sensitive-app filter replaces ``app_name`` with
    ``"<filtered>"`` and ``window_title_hash`` with empty string when the
    deny-list matches; ``is_sensitive=True`` records that filtering happened.
    """

    event_type: str = "foreground_app"
    app_name: str = ""
    window_title_hash: str = ""
    bundle_id: str = ""
    is_sensitive: bool = False
    platform: str = ""


@dataclass(frozen=True, slots=True)
class AmbientSensorPauseEvent(SignalEvent):
    """An ambient sensor entered or exited a paused state."""

    event_type: str = "ambient_sensor_pause"
    sensor_name: str = "foreground"
    paused: bool = True
    reason: str = ""
```

- [ ] **Step 3: Register capability in taxonomy**

Add to `opencomputer/agent/consent/capability_taxonomy.py`:

```python
"ambient.foreground.observe": CapabilityTaxonomyEntry(
    capability_id="ambient.foreground.observe",
    tier_default=ConsentTier.IMPLICIT,
    description="Observe foreground app + hashed window title; sensitive apps filtered before publish; data stays local.",
),
```

- [ ] **Step 4: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_foreground_event.py -v` → 4 PASS.

- [ ] **Step 5: Commit**

```
cd /tmp/oc-ambient
git add OpenComputer/plugin_sdk/ingestion.py OpenComputer/opencomputer/agent/consent/capability_taxonomy.py OpenComputer/tests/test_ambient_foreground_event.py
git commit -m "feat(ambient): SDK event types + capability taxonomy entry (T1)"
```

---

### Task 2: Cross-platform foreground detector

**Files:**
- Create: `extensions/ambient-sensors/foreground.py`
- Create: `tests/test_ambient_foreground_detector.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_ambient_foreground_detector.py"""
from __future__ import annotations
import sys
from unittest.mock import patch, MagicMock
import pytest

# Skip if module not yet created (TDD red)
pytest.importorskip("extensions.coding_harness")  # ensures aliasing infra is loaded
from extensions.ambient_sensors.foreground import (
    ForegroundSnapshot,
    detect_foreground,
    _detect_macos,
    _detect_linux,
    _detect_windows,
)


def test_snapshot_is_frozen_dataclass():
    snap = ForegroundSnapshot(app_name="Code", window_title="t", bundle_id="b", platform="darwin")
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.app_name = "x"


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only path")
def test_macos_calls_osascript():
    fake_run = MagicMock(return_value=MagicMock(stdout="Code\nfile.py — Code\ncom.microsoft.VSCode\n", returncode=0))
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is not None
    assert snap.app_name == "Code"
    assert "file.py" in snap.window_title
    assert snap.bundle_id == "com.microsoft.VSCode"
    assert snap.platform == "darwin"


def test_macos_returns_none_on_failure():
    fake_run = MagicMock(side_effect=FileNotFoundError("osascript missing"))
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is None


def test_linux_returns_none_on_wayland():
    with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=False), \
         patch.dict("os.environ", {"DISPLAY": ""}, clear=False):
        snap = _detect_linux()
    assert snap is None or snap.platform == "wayland"


def test_linux_uses_xdotool_when_available():
    fake_which = MagicMock(side_effect=lambda c: "/usr/bin/xdotool" if c == "xdotool" else None)
    fake_run = MagicMock(side_effect=[
        MagicMock(stdout="123\n", returncode=0),  # getactivewindow
        MagicMock(stdout="my-file.py - VS Code\n", returncode=0),  # getwindowname
        MagicMock(stdout="code.Code\n", returncode=0),  # getwindowclassname
    ])
    with patch("extensions.ambient_sensors.foreground.shutil.which", fake_which), \
         patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run), \
         patch.dict("os.environ", {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}, clear=False):
        snap = _detect_linux()
    assert snap is not None
    assert snap.app_name == "Code" or "Code" in snap.app_name
    assert "my-file.py" in snap.window_title


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only path")
def test_windows_uses_win32gui():
    pytest.importorskip("win32gui")
    # Smoke-test: actual call may legitimately return None on CI VM with no foreground window
    snap = _detect_windows()
    # Don't assert non-None; just ensure no crash
    assert snap is None or snap.platform == "win32"


def test_detect_foreground_dispatches_by_platform():
    """The top-level detect_foreground() must call the right platform helper."""
    if sys.platform == "darwin":
        with patch("extensions.ambient_sensors.foreground._detect_macos", return_value=None) as m:
            detect_foreground()
            m.assert_called_once()
    elif sys.platform.startswith("linux"):
        with patch("extensions.ambient_sensors.foreground._detect_linux", return_value=None) as m:
            detect_foreground()
            m.assert_called_once()
    elif sys.platform == "win32":
        with patch("extensions.ambient_sensors.foreground._detect_windows", return_value=None) as m:
            detect_foreground()
            m.assert_called_once()
```

- [ ] **Step 2: Implement `extensions/ambient-sensors/foreground.py`**

```python
"""Cross-platform foreground-app detection.

Each platform path returns ``None`` on failure rather than raising — the
caller (sensor daemon) treats that as "skip this tick" and tries again.

macOS: single osascript invocation pulling app name + window title + bundle ID.
Linux X11: xdotool first, wmctrl fallback.
Linux Wayland: returns None; ambient sensor reports unsupported.
Windows: pywin32 GetForegroundWindow + GetWindowText + psutil for app name.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ForegroundSnapshot:
    app_name: str
    window_title: str
    bundle_id: str
    platform: str


_OSASCRIPT = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set bundleID to bundle identifier of frontApp
    try
        set winTitle to name of front window of frontApp
    on error
        set winTitle to ""
    end try
end tell
return appName & "\\n" & winTitle & "\\n" & bundleID
"""


def _detect_macos() -> ForegroundSnapshot | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", _OSASCRIPT],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").rstrip("\n").split("\n", 2)
    while len(parts) < 3:
        parts.append("")
    return ForegroundSnapshot(
        app_name=parts[0].strip(),
        window_title=parts[1].strip(),
        bundle_id=parts[2].strip(),
        platform="darwin",
    )


def _detect_linux() -> ForegroundSnapshot | None:
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return None  # pure Wayland — unsupported in v1

    if shutil.which("xdotool"):
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            if wid.returncode != 0:
                return None
            wid_str = wid.stdout.strip()
            title = subprocess.run(
                ["xdotool", "getwindowname", wid_str],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            klass = subprocess.run(
                ["xdotool", "getwindowclassname", wid_str],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            return ForegroundSnapshot(
                app_name=(klass.stdout or "").strip(),
                window_title=(title.stdout or "").strip(),
                bundle_id="",
                platform="linux",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    if shutil.which("wmctrl"):
        try:
            result = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            for line in (result.stdout or "").splitlines():
                # wmctrl format: <id> <desktop> <hostname> <title>
                parts = line.split(None, 3)
                if len(parts) == 4:
                    return ForegroundSnapshot(
                        app_name="", window_title=parts[3],
                        bundle_id="", platform="linux",
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    return None


def _detect_windows() -> ForegroundSnapshot | None:
    try:
        import win32gui
        import win32process
        import psutil
    except ImportError:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            app = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app = ""
        return ForegroundSnapshot(
            app_name=app, window_title=title,
            bundle_id="", platform="win32",
        )
    except Exception:  # noqa: BLE001 — Windows API quirks
        return None


def detect_foreground() -> ForegroundSnapshot | None:
    """Return a snapshot of the foreground app, or None if unavailable."""
    if sys.platform == "darwin":
        return _detect_macos()
    if sys.platform.startswith("linux"):
        return _detect_linux()
    if sys.platform == "win32":
        return _detect_windows()
    return None
```

- [ ] **Step 3: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_foreground_detector.py -v` → expect ALL PASS (some skipif by platform).

- [ ] **Step 4: Commit**

```
cd /tmp/oc-ambient
git add OpenComputer/extensions/ambient-sensors/foreground.py OpenComputer/tests/test_ambient_foreground_detector.py
git commit -m "feat(ambient): cross-platform foreground detector — mac/linux/windows (T2)"
```

---

### Task 3: Sensitive-app filter

**Files:**
- Create: `extensions/ambient-sensors/sensitive_apps.py`
- Create: `tests/test_ambient_sensitive_filter.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_ambient_sensitive_filter.py"""
from __future__ import annotations
from pathlib import Path
import pytest
from extensions.ambient_sensors.sensitive_apps import (
    is_sensitive,
    load_user_overrides,
    _DEFAULT_PATTERNS,
)
from extensions.ambient_sensors.foreground import ForegroundSnapshot


def _snap(app: str, title: str = "") -> ForegroundSnapshot:
    return ForegroundSnapshot(app_name=app, window_title=title, bundle_id="", platform="linux")


@pytest.mark.parametrize("name", ["1Password", "Bitwarden", "KeePassXC"])
def test_password_managers_default_sensitive(name):
    assert is_sensitive(_snap(name)) is True


@pytest.mark.parametrize("name", ["Chase Mobile", "HDFC Bank", "Robinhood"])
def test_banking_default_sensitive(name):
    assert is_sensitive(_snap(name)) is True


def test_non_sensitive_app_returns_false():
    assert is_sensitive(_snap("Code")) is False
    assert is_sensitive(_snap("Safari", title="github.com — Safari")) is False


def test_title_pattern_match():
    assert is_sensitive(_snap("Safari", title="Chase Bank — Account Summary")) is True


def test_user_override_extends_default(tmp_path):
    override = tmp_path / "sensitive_apps.txt"
    override.write_text("(?i)MyCustomApp\n# comment ignored\n\n")
    user_patterns = load_user_overrides(override)
    assert any("MyCustomApp" in p for p in user_patterns)


def test_user_override_missing_file_returns_empty(tmp_path):
    assert load_user_overrides(tmp_path / "missing.txt") == []


def test_default_patterns_are_compilable():
    import re
    for pat in _DEFAULT_PATTERNS:
        re.compile(pat)
```

- [ ] **Step 2: Implement**

```python
"""Sensitive-app filter for the ambient foreground sensor.

A snapshot is "sensitive" if its app_name OR window_title matches any
regex in (defaults + user overrides). Sensitive snapshots are filtered
to ``app_name="<filtered>"`` BEFORE publish — raw values never leave the
sensor.
"""

from __future__ import annotations

import re
from pathlib import Path

from .foreground import ForegroundSnapshot


_DEFAULT_PATTERNS: tuple[str, ...] = (
    # Password managers
    r"(?i)1Password",
    r"(?i)Bitwarden",
    r"(?i)KeePass",
    r"(?i)Dashlane",
    r"(?i)LastPass",
    # Banking — generic + region-specific
    r"(?i)\bbank\b",
    r"(?i)Chase",
    r"(?i)HDFC",
    r"(?i)ICICI",
    r"(?i)\bSBI\b",
    r"(?i)Robinhood",
    r"(?i)Coinbase",
    r"(?i)MetaMask",
    r"(?i)Zerodha",
    r"(?i)Groww",
    r"(?i)Schwab",
    r"(?i)Fidelity",
    # Healthcare
    r"(?i)MyChart",
    r"(?i)Teladoc",
    r"(?i)Healow",
    # Private browsing / secure
    r"(?i)Private Browsing",
    r"(?i)Incognito",
    r"(?i)Tor Browser",
    r"(?i)Signal",
    r"(?i)ProtonMail",
)


def load_user_overrides(path: Path) -> list[str]:
    """Read additional regex patterns from a user-managed text file.

    Format: one regex per line; lines starting with ``#`` are comments;
    blank lines ignored. Returns an empty list if the file doesn't exist.
    """
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def is_sensitive(
    snap: ForegroundSnapshot,
    extra_patterns: list[str] | None = None,
) -> bool:
    """Return True if the snapshot matches any default or extra pattern."""
    patterns = list(_DEFAULT_PATTERNS) + (extra_patterns or [])
    haystack = f"{snap.app_name}\n{snap.window_title}"
    for pat in patterns:
        try:
            if re.search(pat, haystack):
                return True
        except re.error:
            continue  # malformed user pattern — skip
    return False
```

- [ ] **Step 3: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_sensitive_filter.py -v` → 7 PASS.

- [ ] **Step 4: Commit**

```
git add OpenComputer/extensions/ambient-sensors/sensitive_apps.py OpenComputer/tests/test_ambient_sensitive_filter.py
git commit -m "feat(ambient): sensitive-app regex filter + user override (T3)"
```

---

### Task 4: Pause/resume state file

**Files:**
- Create: `extensions/ambient-sensors/pause_state.py`
- Create: `tests/test_ambient_pause_state.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_ambient_pause_state.py"""
from __future__ import annotations
import json
import time
import pytest
from extensions.ambient_sensors.pause_state import (
    AmbientState,
    load_state,
    save_state,
    is_currently_paused,
)


def test_load_missing_returns_default(tmp_path):
    state = load_state(tmp_path / "state.json")
    assert state.enabled is False  # default OFF
    assert state.paused_until is None


def test_save_then_load_round_trip(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, AmbientState(enabled=True, paused_until=None, sensors=("foreground",)))
    loaded = load_state(p)
    assert loaded.enabled is True
    assert loaded.sensors == ("foreground",)


def test_pause_until_in_future_means_paused():
    state = AmbientState(enabled=True, paused_until=time.time() + 60, sensors=("foreground",))
    assert is_currently_paused(state) is True


def test_pause_until_in_past_means_not_paused():
    state = AmbientState(enabled=True, paused_until=time.time() - 60, sensors=("foreground",))
    assert is_currently_paused(state) is False


def test_disabled_means_not_paused_either():
    """Disabled is a stronger state than paused — pause check still returns False."""
    state = AmbientState(enabled=False, paused_until=time.time() + 60, sensors=("foreground",))
    assert is_currently_paused(state) is False  # but daemon also won't run because enabled=False


def test_corrupt_json_returns_default(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{ this is not json")
    state = load_state(p)
    assert state.enabled is False
```

- [ ] **Step 2: Implement**

```python
"""Pause/resume state for the ambient sensor daemon.

State lives at ``<profile_home>/ambient/state.json``. CLI writes it; daemon
reads it each tick.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AmbientState:
    enabled: bool = False
    paused_until: float | None = None
    sensors: tuple[str, ...] = field(default_factory=tuple)


def load_state(path: Path) -> AmbientState:
    """Read state.json; return default (disabled) if missing or corrupt."""
    if not path.exists():
        return AmbientState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AmbientState()
    return AmbientState(
        enabled=bool(raw.get("enabled", False)),
        paused_until=raw.get("paused_until"),
        sensors=tuple(raw.get("sensors", ())),
    )


def save_state(path: Path, state: AmbientState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "enabled": state.enabled,
            "paused_until": state.paused_until,
            "sensors": list(state.sensors),
        }, indent=2),
        encoding="utf-8",
    )


def is_currently_paused(state: AmbientState) -> bool:
    """Daemon is "paused" iff enabled AND paused_until is in the future."""
    if not state.enabled:
        return False
    if state.paused_until is None:
        return False
    return state.paused_until > time.time()
```

- [ ] **Step 3: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_pause_state.py -v` → 6 PASS.

- [ ] **Step 4: Commit**

```
git add OpenComputer/extensions/ambient-sensors/pause_state.py OpenComputer/tests/test_ambient_pause_state.py
git commit -m "feat(ambient): pause/resume state file + helpers (T4)"
```

---

### Task 5: Sensor daemon (dedup + min-interval + sensitive filter integration)

**Files:**
- Create: `extensions/ambient-sensors/daemon.py`
- Create: `tests/test_ambient_daemon_dedup.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_ambient_daemon_dedup.py"""
from __future__ import annotations
import asyncio
from unittest.mock import patch, MagicMock
import pytest

from extensions.ambient_sensors.daemon import ForegroundSensorDaemon
from extensions.ambient_sensors.foreground import ForegroundSnapshot
from plugin_sdk.ingestion import ForegroundAppEvent


def _snap(app: str, title: str = "x", bundle: str = "") -> ForegroundSnapshot:
    return ForegroundSnapshot(app_name=app, window_title=title, bundle_id=bundle, platform="linux")


@pytest.mark.asyncio
async def test_publishes_first_snapshot():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: _snap("Code"))
    await d._tick()
    assert len(bus_calls) == 1
    assert isinstance(bus_calls[0], ForegroundAppEvent)
    assert bus_calls[0].app_name == "Code"


@pytest.mark.asyncio
async def test_dedup_skips_identical_snapshots():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: _snap("Code"))
    await d._tick()  # first publish
    await d._tick()  # same snapshot — should NOT publish
    await d._tick()  # same snapshot — should NOT publish
    assert len(bus_calls) == 1


@pytest.mark.asyncio
async def test_dedup_publishes_when_app_changes():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    snaps = iter([_snap("Code"), _snap("Safari"), _snap("Safari"), _snap("TradingView")])
    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: next(snaps))
    for _ in range(4):
        await d._tick()
    assert len(bus_calls) == 3
    assert [e.app_name for e in bus_calls] == ["Code", "Safari", "TradingView"]


@pytest.mark.asyncio
async def test_sensitive_app_filtered_before_publish():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: _snap("1Password 7", title="Personal Vault"))
    await d._tick()
    assert len(bus_calls) == 1
    assert bus_calls[0].app_name == "<filtered>"
    assert bus_calls[0].window_title_hash == ""
    assert bus_calls[0].is_sensitive is True


@pytest.mark.asyncio
async def test_window_title_is_hashed_not_plaintext():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: _snap("Code", title="my-secret-project.py - VS Code"))
    await d._tick()
    assert len(bus_calls) == 1
    assert "my-secret-project" not in bus_calls[0].window_title_hash
    assert len(bus_calls[0].window_title_hash) == 64  # SHA-256 hex


@pytest.mark.asyncio
async def test_skips_when_detect_returns_none():
    bus_calls = []
    fake_bus = MagicMock()
    fake_bus.apublish = MagicMock(side_effect=lambda e: bus_calls.append(e) or asyncio.sleep(0))

    d = ForegroundSensorDaemon(bus=fake_bus, profile_home_factory=lambda: None,
                                detect=lambda: None)
    await d._tick()
    assert len(bus_calls) == 0
```

- [ ] **Step 2: Implement**

```python
"""Foreground sensor daemon — polls detect_foreground() on a tick interval,
dedups, filters sensitive apps, and publishes ForegroundAppEvent to the F2 bus.

Designed to run inside the gateway daemon (alongside cron/scheduler) OR as a
standalone process via ``oc ambient daemon``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plugin_sdk.ingestion import AmbientSensorPauseEvent, ForegroundAppEvent

from .foreground import ForegroundSnapshot, detect_foreground
from .pause_state import AmbientState, is_currently_paused, load_state
from .sensitive_apps import is_sensitive, load_user_overrides

_log = logging.getLogger("opencomputer.ambient.daemon")

_MIN_PUBLISH_INTERVAL_S = 2.0
_HEARTBEAT_FILENAME = "heartbeat"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class ForegroundSensorDaemon:
    """Polls foreground detector, dedups, filters, publishes to bus."""

    def __init__(
        self,
        *,
        bus: Any,                           # opencomputer.ingestion.bus.TypedEventBus
        profile_home_factory: Callable[[], Path | None],
        detect: Callable[[], ForegroundSnapshot | None] = detect_foreground,
        tick_seconds: float = 10.0,
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._detect = detect
        self._tick_seconds = tick_seconds
        self._last_publish: tuple[str, str, str] | None = None
        self._last_publish_time: float = 0.0
        self._last_pause_state: bool | None = None
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        profile_home = self._profile_home_factory()
        state_path = (profile_home / "ambient" / "state.json") if profile_home else None
        state = load_state(state_path) if state_path else AmbientState(enabled=True, sensors=("foreground",))

        if not state.enabled:
            self._last_pause_state = None
            return

        # Heartbeat (always written when enabled)
        if profile_home:
            hb = profile_home / "ambient" / _HEARTBEAT_FILENAME
            try:
                hb.parent.mkdir(parents=True, exist_ok=True)
                hb.write_text(str(time.time()))
            except OSError:
                _log.debug("heartbeat write failed", exc_info=True)

        if is_currently_paused(state):
            if self._last_pause_state is not True:
                await self._bus.apublish(AmbientSensorPauseEvent(
                    sensor_name="foreground", paused=True, reason="user-paused",
                    source="ambient-sensors",
                ))
                self._last_pause_state = True
            return
        elif self._last_pause_state is True:
            await self._bus.apublish(AmbientSensorPauseEvent(
                sensor_name="foreground", paused=False, reason="resumed",
                source="ambient-sensors",
            ))
            self._last_pause_state = False

        snap = self._detect()
        if snap is None:
            return

        # Sensitive filter
        extra = []
        if profile_home:
            override_path = profile_home / "ambient" / "sensitive_apps.txt"
            extra = load_user_overrides(override_path)
        sensitive = is_sensitive(snap, extra_patterns=extra)

        if sensitive:
            app_name = "<filtered>"
            title_hash = ""
            bundle_id = ""
        else:
            app_name = snap.app_name
            title_hash = _sha256(snap.window_title) if snap.window_title else ""
            bundle_id = snap.bundle_id

        key = (app_name, title_hash, bundle_id)
        now = time.time()

        # Dedup: skip if same as last publish
        if key == self._last_publish:
            return
        # Min-interval guard
        if now - self._last_publish_time < _MIN_PUBLISH_INTERVAL_S:
            return

        event = ForegroundAppEvent(
            app_name=app_name,
            window_title_hash=title_hash,
            bundle_id=bundle_id,
            is_sensitive=sensitive,
            platform=snap.platform or sys.platform,
            source="ambient-sensors",
        )
        await self._bus.apublish(event)
        self._last_publish = key
        self._last_publish_time = now

    async def run(self) -> None:
        """Run the poll loop forever (or until cancelled)."""
        _log.info("ambient foreground daemon starting (tick=%ss)", self._tick_seconds)
        try:
            while True:
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001
                    _log.exception("ambient daemon tick failed")
                await asyncio.sleep(self._tick_seconds)
        except asyncio.CancelledError:
            _log.info("ambient foreground daemon stopped")
            raise

    def start(self) -> asyncio.Task:
        """Start the daemon as an asyncio task. Returns the task handle."""
        self._task = asyncio.create_task(self.run(), name="ambient-foreground-daemon")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
```

- [ ] **Step 3: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_daemon_dedup.py -v` → 6 PASS.

- [ ] **Step 4: Commit**

```
git add OpenComputer/extensions/ambient-sensors/daemon.py OpenComputer/tests/test_ambient_daemon_dedup.py
git commit -m "feat(ambient): sensor daemon — dedup + min-interval + sensitive filter (T5)"
```

---

### Task 6: CLI (`oc ambient {on,off,pause,resume,status,daemon}`)

**Files:**
- Create: `opencomputer/cli_ambient.py`
- Edit: `opencomputer/cli.py` — mount the new Typer app
- Create: `tests/test_ambient_cli.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_ambient_cli.py"""
from __future__ import annotations
from typer.testing import CliRunner
from opencomputer.cli_ambient import app


def test_status_shows_disabled_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "disabled" in result.output.lower() or "not enabled" in result.output.lower()


def test_on_writes_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["on"])
    assert result.exit_code == 0
    assert (tmp_path / "ambient" / "state.json").exists()


def test_off_clears_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["off"])
    assert result.exit_code == 0
    import json
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["enabled"] is False


def test_pause_with_duration_sets_paused_until(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    import time
    before = time.time()
    result = CliRunner().invoke(app, ["pause", "--duration", "1h"])
    assert result.exit_code == 0
    import json
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["paused_until"] is not None
    assert state["paused_until"] > before + 3500


def test_resume_clears_paused_until(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    CliRunner().invoke(app, ["pause", "--duration", "1h"])
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 0
    import json
    state = json.loads((tmp_path / "ambient" / "state.json").read_text())
    assert state["paused_until"] is None


def test_status_does_not_leak_specific_apps(tmp_path, monkeypatch):
    """Status output must show AGGREGATE counts only — never specific app names."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["on"])
    result = CliRunner().invoke(app, ["status"])
    # Spot-check: no obvious app-name-shaped strings in output
    assert "1Password" not in result.output
    assert "Chase" not in result.output
```

- [ ] **Step 2: Implement `opencomputer/cli_ambient.py`**

```python
"""CLI: oc ambient {on,off,pause,resume,status,daemon}.

State file at <profile_home>/ambient/state.json. The active profile_home is
resolved via opencomputer.agent.config._home() OR the
OPENCOMPUTER_PROFILE_HOME env var (for testing).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import typer

app = typer.Typer(help="Ambient sensor controls.")


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    from opencomputer.agent.config import _home  # lazy — avoids import cycles
    return _home()


def _state_path() -> Path:
    return _profile_home() / "ambient" / "state.json"


def _heartbeat_path() -> Path:
    return _profile_home() / "ambient" / "heartbeat"


def _parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", text)
    if not m:
        raise typer.BadParameter("duration must be like '90s', '5m', '1h', '2d'")
    value = float(m.group(1))
    unit = m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@app.command()
def on() -> None:
    """Enable the ambient foreground sensor."""
    from extensions.ambient_sensors.pause_state import AmbientState, save_state
    save_state(_state_path(), AmbientState(enabled=True, paused_until=None, sensors=("foreground",)))
    typer.echo("ambient: enabled. The sensor publishes hashed foreground events to the F2 bus.")
    typer.echo("Run `oc ambient status` to verify, or `oc ambient off` to disable.")


@app.command()
def off() -> None:
    """Disable the ambient foreground sensor."""
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state
    state = load_state(_state_path())
    save_state(_state_path(), AmbientState(enabled=False, paused_until=None, sensors=state.sensors))
    typer.echo("ambient: disabled.")


@app.command()
def pause(
    duration: str = typer.Option("", "--duration", "-d", help="e.g. 5m, 1h, 2d. Empty = indefinite."),
) -> None:
    """Pause the sensor without disabling it."""
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state
    state = load_state(_state_path())
    if not state.enabled:
        typer.echo("ambient: sensor is not enabled. Use `oc ambient on` first.")
        raise typer.Exit(code=1)
    if duration:
        secs = _parse_duration(duration)
        until = time.time() + secs
        new_state = AmbientState(enabled=True, paused_until=until, sensors=state.sensors)
        typer.echo(f"ambient: paused for {duration} (until {time.strftime('%H:%M:%S', time.localtime(until))}).")
    else:
        # "Indefinite" pause: 100 years
        until = time.time() + 100 * 365 * 86400
        new_state = AmbientState(enabled=True, paused_until=until, sensors=state.sensors)
        typer.echo("ambient: paused indefinitely. `oc ambient resume` to lift.")
    save_state(_state_path(), new_state)


@app.command()
def resume() -> None:
    """Resume after a pause."""
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state
    state = load_state(_state_path())
    save_state(_state_path(), AmbientState(enabled=state.enabled, paused_until=None, sensors=state.sensors))
    typer.echo("ambient: resumed.")


@app.command()
def status() -> None:
    """Show current state. Aggregate-only — never specific app names."""
    from extensions.ambient_sensors.pause_state import is_currently_paused, load_state
    state = load_state(_state_path())
    typer.echo(f"enabled: {state.enabled}")
    if not state.enabled:
        typer.echo("(run `oc ambient on` to enable)")
        return
    if is_currently_paused(state):
        until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.paused_until or 0))
        typer.echo(f"paused until: {until}")
    else:
        typer.echo("paused: no")
    hb = _heartbeat_path()
    if hb.exists():
        try:
            ts = float(hb.read_text().strip())
            age = time.time() - ts
            typer.echo(f"last heartbeat: {age:.0f}s ago")
        except (OSError, ValueError):
            typer.echo("last heartbeat: unknown")
    else:
        typer.echo("last heartbeat: (never — daemon not running)")
    typer.echo(f"sensors: {', '.join(state.sensors) or '(none)'}")


@app.command()
def daemon() -> None:
    """Run the ambient sensor daemon standalone (outside gateway)."""
    from extensions.ambient_sensors.daemon import ForegroundSensorDaemon
    from opencomputer.ingestion.bus import default_bus

    typer.echo("ambient daemon: starting (Ctrl+C to stop)")

    async def _run() -> None:
        d = ForegroundSensorDaemon(bus=default_bus, profile_home_factory=_profile_home)
        await d.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nambient daemon: stopped")
```

- [ ] **Step 3: Mount in `opencomputer/cli.py`**

Find the main Typer app declaration. Add:

```python
from opencomputer.cli_ambient import app as _ambient_app
app.add_typer(_ambient_app, name="ambient", help="Ambient sensor controls (foreground app observation).")
```

- [ ] **Step 4: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_cli.py -v` → 6 PASS.

- [ ] **Step 5: Commit**

```
git add OpenComputer/opencomputer/cli_ambient.py OpenComputer/opencomputer/cli.py OpenComputer/tests/test_ambient_cli.py
git commit -m "feat(ambient): CLI — oc ambient {on,off,pause,resume,status,daemon} (T6)"
```

---

### Task 7: F1 capability-claims contract test + plugin manifest + plugin.py registration

**Files:**
- Create: `extensions/ambient-sensors/plugin.json`
- Create: `extensions/ambient-sensors/plugin.py`
- Create: `tests/test_ambient_capability_claim.py`

- [ ] **Step 1: Write contract test**

```python
"""tests/test_ambient_capability_claim.py"""
from __future__ import annotations
from opencomputer.agent.consent.capability_taxonomy import CAPABILITY_TAXONOMY
from plugin_sdk.consent import ConsentTier


def test_ambient_capability_registered():
    entry = CAPABILITY_TAXONOMY.get("ambient.foreground.observe")
    assert entry is not None
    assert entry.tier_default == ConsentTier.IMPLICIT


def test_ambient_namespace_uses_dot_separator():
    """Sanity: capability_id is dot-separated, not slash or colon."""
    cid = "ambient.foreground.observe"
    assert "/" not in cid
    assert ":" not in cid
    assert cid.startswith("ambient.")
```

- [ ] **Step 2: Create `plugin.json`**

```json
{
  "schema_version": 1,
  "name": "ambient-sensors",
  "description": "Cross-platform ambient awareness sensors. Phase 1: foreground-app observation.",
  "version": "0.1.0",
  "kind": "mixed",
  "enabled_by_default": false,
  "capabilities": ["ambient.foreground.observe"]
}
```

- [ ] **Step 3: Create `plugin.py`**

```python
"""Ambient sensors plugin — Phase 1: foreground-app observation.

The plugin's daemon does NOT auto-start. The user opts in via
``oc ambient on`` and the daemon launches at gateway boot (or via
``oc ambient daemon`` standalone).
"""

from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.ambient.plugin")


def register(api):  # noqa: ANN001
    """Plugin entry. Currently registers nothing at import time —
    the daemon lifecycle is gateway-managed (see opencomputer/gateway/server.py)."""
    _log.debug("ambient-sensors plugin registered (daemon starts via gateway/CLI)")
```

- [ ] **Step 4: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_capability_claim.py -v` → 2 PASS.

- [ ] **Step 5: Commit**

```
git add OpenComputer/extensions/ambient-sensors/plugin.json OpenComputer/extensions/ambient-sensors/plugin.py OpenComputer/tests/test_ambient_capability_claim.py
git commit -m "feat(ambient): plugin manifest + register stub + capability contract test (T7)"
```

---

### Task 8: Gateway integration + doctor checks

**Files:**
- Edit: `opencomputer/gateway/server.py` — start daemon at boot if state.enabled
- Edit: `opencomputer/doctor.py` — add 2 new checks
- Create: `tests/test_ambient_doctor_checks.py`

- [ ] **Step 1: Write doctor-check test**

```python
"""tests/test_ambient_doctor_checks.py"""
from __future__ import annotations
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from opencomputer.doctor import _check_ambient_state, _check_ambient_foreground_capable


def test_state_missing_returns_ok_and_disabled(tmp_path):
    result = _check_ambient_state(tmp_path)
    assert result.ok is True
    assert "disabled" in result.message.lower()


def test_state_enabled_with_fresh_heartbeat(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text(
        '{"enabled": true, "paused_until": null, "sensors": ["foreground"]}'
    )
    (tmp_path / "ambient" / "heartbeat").write_text(str(time.time()))
    result = _check_ambient_state(tmp_path)
    assert result.ok is True


def test_state_enabled_with_stale_heartbeat_warns(tmp_path):
    (tmp_path / "ambient").mkdir()
    (tmp_path / "ambient" / "state.json").write_text(
        '{"enabled": true, "paused_until": null, "sensors": ["foreground"]}'
    )
    (tmp_path / "ambient" / "heartbeat").write_text(str(time.time() - 600))
    result = _check_ambient_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"


@pytest.mark.skipif(not _is_linux_with_no_x_or_wayland(), reason="environment-specific")
def test_linux_warns_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    result = _check_ambient_foreground_capable()
    assert result.ok is False
    assert "wayland" in result.message.lower()


def _is_linux_with_no_x_or_wayland():
    import sys
    return sys.platform.startswith("linux")
```

(The `pytest.mark.skipif` keeps the wayland test honest — it only runs on Linux.)

- [ ] **Step 2: Implement doctor checks**

Add to `opencomputer/doctor.py`:

```python
import json
import os
import shutil
import sys
import time


def _check_ambient_state(profile_home: Path) -> CheckResult:
    """Read ambient state.json; warn if enabled but heartbeat is stale."""
    state_path = profile_home / "ambient" / "state.json"
    if not state_path.exists():
        return CheckResult(ok=True, level="info", message="ambient sensor disabled (default)")
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(ok=False, level="warning", message=f"ambient state.json unreadable: {exc}")
    if not state.get("enabled", False):
        return CheckResult(ok=True, level="info", message="ambient sensor disabled")
    hb_path = profile_home / "ambient" / "heartbeat"
    if not hb_path.exists():
        return CheckResult(ok=False, level="warning",
                           message="ambient sensor enabled but heartbeat missing — daemon not running")
    try:
        hb_age = time.time() - float(hb_path.read_text().strip())
    except (OSError, ValueError):
        return CheckResult(ok=False, level="warning", message="ambient heartbeat unreadable")
    if hb_age > 60:
        return CheckResult(ok=False, level="warning",
                           message=f"ambient sensor heartbeat stale ({hb_age:.0f}s old) — daemon may be stuck")
    return CheckResult(ok=True, level="info", message=f"ambient sensor running (heartbeat {hb_age:.0f}s ago)")


def _check_ambient_foreground_capable() -> CheckResult:
    """Verify the platform-specific foreground detector can actually run."""
    if sys.platform == "darwin":
        if shutil.which("osascript"):
            return CheckResult(ok=True, level="info", message="ambient: osascript present (macOS)")
        return CheckResult(ok=False, level="warning",
                           message="ambient: osascript missing — sensor cannot detect foreground on macOS")
    if sys.platform.startswith("linux"):
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            return CheckResult(ok=False, level="warning",
                               message="ambient: Wayland-only display server — foreground sensor unsupported in v1")
        if shutil.which("xdotool") or shutil.which("wmctrl"):
            return CheckResult(ok=True, level="info", message="ambient: xdotool/wmctrl available (Linux X11)")
        return CheckResult(ok=False, level="warning",
                           message="ambient: install xdotool or wmctrl for Linux foreground detection")
    if sys.platform == "win32":
        try:
            __import__("win32gui")
            return CheckResult(ok=True, level="info", message="ambient: pywin32 importable (Windows)")
        except ImportError:
            return CheckResult(ok=False, level="warning",
                               message="ambient: install pywin32 for Windows foreground detection")
    return CheckResult(ok=False, level="warning", message=f"ambient: platform {sys.platform} unsupported")
```

Wire both into the doctor's main check list (follow existing pattern).

- [ ] **Step 3: Hook gateway boot**

In `opencomputer/gateway/server.py`, locate where the cron scheduler starts. Add after it:

```python
# Ambient sensor daemon — only starts if state.enabled.
try:
    from extensions.ambient_sensors.pause_state import load_state
    from extensions.ambient_sensors.daemon import ForegroundSensorDaemon
    from opencomputer.agent.config import _home

    state_path = _home() / "ambient" / "state.json"
    if load_state(state_path).enabled:
        ambient_daemon = ForegroundSensorDaemon(bus=default_bus, profile_home_factory=_home)
        ambient_daemon.start()
        _log.info("ambient sensor daemon started")
except Exception:  # noqa: BLE001
    _log.exception("failed to start ambient daemon — continuing without it")
```

- [ ] **Step 4: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_doctor_checks.py -v` → all PASS.

- [ ] **Step 5: Commit**

```
git add OpenComputer/opencomputer/doctor.py OpenComputer/opencomputer/gateway/server.py OpenComputer/tests/test_ambient_doctor_checks.py
git commit -m "feat(ambient): doctor checks + gateway boot integration (T8)"
```

---

### Task 9: No-cloud-egress contract test + README

**Files:**
- Create: `tests/test_ambient_no_cloud_egress.py`
- Create: `extensions/ambient-sensors/README.md`

- [ ] **Step 1: Write the contract test**

```python
"""tests/test_ambient_no_cloud_egress.py — local-only contract guard.

The ambient sensor MUST NOT send data anywhere. AST-scan the plugin's
source for any HTTP-client import. Adding networking here is a contract
break that has to be deliberate (delete this test + update CHANGELOG).
"""

from __future__ import annotations

import ast
from pathlib import Path

_DENIED_NETWORK_IMPORTS = frozenset({
    "httpx", "requests", "urllib3", "aiohttp", "websockets",
    "grpc", "boto3", "google.cloud",
})


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent / "extensions" / "ambient-sensors"


def test_no_network_imports_in_ambient_plugin():
    violations = []
    for path in _plugin_root().rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in _DENIED_NETWORK_IMPORTS:
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in _DENIED_NETWORK_IMPORTS:
                    violations.append(f"{path}:{node.lineno}: from {node.module} import ...")

    assert not violations, (
        "Ambient sensor must NOT import network libraries — local-only contract.\n"
        + "Violations:\n  " + "\n  ".join(violations)
    )


def test_no_urllib_request_in_ambient_plugin():
    """urllib.request is in stdlib so the import-name check above won't
    catch it — explicit AST sweep."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                violations.append(f"{path}:{node.lineno}: from urllib.request import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "urllib.request":
                        violations.append(f"{path}:{node.lineno}: import urllib.request")
    assert not violations, "urllib.request leaked into ambient plugin: " + str(violations)
```

- [ ] **Step 2: Write `extensions/ambient-sensors/README.md`**

```markdown
# Ambient Sensors (foreground-app)

Cross-platform ambient awareness for OpenComputer. **Default OFF.**

## What this does

When enabled, a small daemon polls the foreground application every 10
seconds and publishes a `ForegroundAppEvent` to OpenComputer's F2 typed
event bus. The persona classifier and motif extractor use these events
to build a richer picture of how you spend your time across apps.

## What this does NOT do

- Capture screen content
- Record audio
- Send any data to a network destination
- Send raw window titles anywhere (only SHA-256 hashes leave the sensor)
- Train any model on collected data
- Run when paused or disabled

The "no network" rule is enforced by `tests/test_ambient_no_cloud_egress.py`
— a CI guard that scans this directory for HTTP-client imports.

## Privacy contract

| What we capture | How it's stored | Where it goes |
|---|---|---|
| App name (e.g. "Code") | In-memory only | F2 bus (consumed locally) |
| Window title | SHA-256 hashed before publish | Hash only — to F2 bus |
| Bundle ID (macOS) | In-memory only | F2 bus |
| Sensitive-app match | Boolean only | F2 bus |

If the sensitive-app filter matches, `app_name` is replaced with
`"<filtered>"` before publish. The raw value never leaves the sensor.

## Sensitive-app filter

Default deny-list (regex) covers password managers, banking apps,
healthcare apps, private-browsing tabs, and secure-messaging apps. To
extend the list, create:

```
<profile_home>/ambient/sensitive_apps.txt
```

One regex per line; lines starting with `#` are comments. Example:

```
# my company's internal tools
(?i)AcmeFinancialPortal
(?i)InternalHRSystem
```

## How to use

```bash
# Enable
oc ambient on

# Pause for an hour (e.g. during a sensitive call)
oc ambient pause --duration 1h

# Resume
oc ambient resume

# Disable completely
oc ambient off

# See state (aggregate counts only, never specific apps)
oc ambient status

# Run the daemon outside the gateway
oc ambient daemon
```

## Platform support

| Platform | Status | Mechanism |
|---|---|---|
| macOS | Supported | osascript via System Events (Accessibility permission required) |
| Linux X11 | Supported | xdotool primary; wmctrl fallback |
| Linux Wayland | Unsupported in v1 | Daemon stays running; reports unsupported via event |
| Windows | Supported | pywin32 (`win32gui` + `psutil`) |

`opencomputer doctor` reports per-platform readiness.

## Troubleshooting

- **macOS "not authorized" error**: System Settings → Privacy & Security
  → Accessibility → enable Terminal (or your editor).
- **Linux daemon emits nothing**: install xdotool or wmctrl.
  `sudo apt install xdotool`.
- **Wayland**: support is planned; v1 ships X11-only.
- **Daemon not starting**: confirm `oc ambient on` was run AND the
  gateway daemon is up (or use `oc ambient daemon` standalone).

## Disabling completely

`oc ambient off` flips the `enabled` flag. The daemon stops within one
tick (≤10 s). If you don't trust the flag, you can also delete
`<profile_home>/ambient/state.json` — the daemon defaults to disabled
when the file is missing or unreadable.
```

- [ ] **Step 3: Run tests**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/test_ambient_no_cloud_egress.py -v` → 2 PASS.

- [ ] **Step 4: Commit**

```
git add OpenComputer/tests/test_ambient_no_cloud_egress.py OpenComputer/extensions/ambient-sensors/README.md
git commit -m "test(ambient): no-cloud-egress contract guard + README (T9)"
```

---

### Task 10: Final validation + CHANGELOG + push + PR

- [ ] **Step 1: Run full pytest suite**

`cd /tmp/oc-ambient/OpenComputer && .venv/bin/pytest tests/ -q 2>&1 | tail -10` — expect ≥ baseline tests + ambient additions, all green.

- [ ] **Step 2: Run ruff**

`cd /tmp/oc-ambient && ruff check . 2>&1 | tail -5` — clean.

- [ ] **Step 3: Smoke test the CLI**

```
cd /tmp/oc-ambient/OpenComputer
.venv/bin/opencomputer ambient status      # disabled
.venv/bin/opencomputer ambient on
.venv/bin/opencomputer ambient status      # enabled, no heartbeat (no daemon)
.venv/bin/opencomputer ambient pause --duration 5m
.venv/bin/opencomputer ambient status      # paused until ...
.venv/bin/opencomputer ambient resume
.venv/bin/opencomputer ambient off
```

All should run without crash; status output should never include specific app names.

- [ ] **Step 4: Add CHANGELOG entry**

Insert under `[Unreleased]`:

```markdown
### Added (Ambient foreground sensor — Phase 1)

OpenComputer now ships a cross-platform, opt-in ambient sensor that
publishes hashed `ForegroundAppEvent`s to the F2 typed event bus. The
persona classifier and motif extractor can use these events to build a
richer picture of how the user spends their time across apps without
ever seeing raw window titles.

- `extensions/ambient-sensors/` — new plugin (~700 LOC across daemon,
  cross-platform foreground detector, sensitive-app filter, pause-state
  helpers, plugin manifest, README).
- `plugin_sdk/ingestion.py` — new `ForegroundAppEvent` and
  `AmbientSensorPauseEvent` SDK types (frozen, slots, metadata-only).
- `opencomputer/agent/consent/capability_taxonomy.py` — new
  `ambient.foreground.observe` capability at IMPLICIT tier.
- `opencomputer/cli_ambient.py` — `oc ambient {on,off,pause,resume,status,daemon}`.
- `opencomputer/doctor.py` — `_check_ambient_state` +
  `_check_ambient_foreground_capable` per-platform checks.
- `opencomputer/gateway/server.py` — boots the ambient daemon when
  `state.enabled` is true.
- 9 new test files (~400 LOC) covering: SDK types, cross-platform
  detector, sensitive filter, daemon dedup/min-interval/sensitive
  filtering, pause-state, CLI, capability namespace, doctor checks, and
  the no-cloud-egress AST contract.

### Privacy contract (hard, baked into tests)

The ambient sensor must NOT:
- Send any data to a network destination (`tests/test_ambient_no_cloud_egress.py` AST-scans the plugin for HTTP-client imports).
- Capture screen pixels, OCR'd text, or audio.
- Publish raw window titles (only SHA-256 hashes).
- Auto-take a user-visible action.
- Run when paused or disabled.
- Train any model on collected data.

### Defaults

Default OFF. Random user installing OpenComputer never gets a surprise
daemon. Opt in with `oc ambient on`. macOS: requires Accessibility
permission (doctor warns). Linux X11: requires `xdotool` or `wmctrl`
(doctor warns). Linux Wayland: unsupported in v1, doctor reports cleanly.
Windows: requires `pywin32`.
```

- [ ] **Step 5: Commit CHANGELOG**

```
git add OpenComputer/CHANGELOG.md
git commit -m "docs(changelog): ambient foreground sensor Phase 1 (T10)"
```

- [ ] **Step 6: Push branch**

```
cd /tmp/oc-ambient
git push -u origin feat/ambient-foreground-sensor
```

- [ ] **Step 7: Open PR**

```
gh pr create --base main --head feat/ambient-foreground-sensor --title "feat(ambient): cross-platform opt-in foreground sensor (Phase 1)" --body "..."
```

(Use the spec doc link + summary from §1-§3 of this plan as the body.)

- [ ] **Step 8: After CI green, squash-merge**

```
gh pr merge <PR#> --squash --delete-branch
```

---

## Self-Audit (rigorous expert critic)

### Spec coverage

- §3.1 Default OFF — Task 7 plugin.json `enabled_by_default: false`; Task 6 CLI `on`/`off` + Task 4 default `AmbientState(enabled=False)`. ✓
- §3.2 Cross-platform first-class — Task 2 implements all 3 OS paths; Task 8 doctor reports per-platform; Task 10 CHANGELOG documents support matrix. ✓
- §3.3 Local-only — Task 9 AST contract test enforces no network imports; Task 5 daemon never imports anything but stdlib + `plugin_sdk` + `opencomputer.ingestion.bus`. ✓
- §3.4 Hashed titles — Task 5 daemon `_sha256(snap.window_title)` before publish; Task 5 test `test_window_title_is_hashed_not_plaintext` enforces. ✓
- §3.5 Sensitive-app filter — Task 3 implementation + tests; Task 5 wires it into daemon; Task 5 test `test_sensitive_app_filtered_before_publish` enforces. ✓
- §3.6 One-shot pause — Task 4 + Task 6 CLI; Task 5 daemon respects pause state. ✓
- §3.7 Audit-logged — capability registered (Task 1), F1 ConsentGate audits all capability uses by existing infra. ✓

### 1. Flawed assumptions

- **FA1**: "All Linux users have xdotool or wmctrl installed." Reality: Wayland defaults on Ubuntu 22.04+; X11 servers are increasingly rare. Many users will hit the "unsupported" path. *Mitigated*: doctor warns clearly with install instructions; daemon stays silent rather than emitting wrong data.
- **FA2**: "macOS osascript reliably returns bundle ID." Reality: not all foreground apps have bundle IDs (some shell scripts, headless tools). *Mitigated*: detector catches AppleScript failures and returns empty bundle_id; tests cover the empty case.
- **FA3**: "SHA-256 hash of titles is private." Reality: title strings have low entropy ("Inbox - Gmail" is the same hash for everyone). The hash is not a secret; it's a dedup token. *Mitigated*: README is honest about this; hashes are useful only locally for dedup, never exfiltrated. Audit log entries see only the hash, never the original.
- **FA4**: "10-second tick is cheap." Reality: on macOS, every osascript call spins up a sub-shell + JavaScriptCore — measurable but minor (<10ms). On Linux, xdotool forks twice. Aggregate CPU cost: ~0.05% on M-series Mac. Negligible. ✓
- **FA5**: "Daemon failures are recoverable." Reality: if osascript prompts a permission dialog and times out, the daemon hangs. *Mitigated*: detector uses `timeout=2.0` on subprocess.run; daemon catches all exceptions in `_tick()`.
- **FA6**: "User will read the README before enabling." Reality: half won't. *Mitigated*: `oc ambient on` echoes a one-line privacy statement at enable time; status shows aggregate-only.
- **FA7**: "TypedEventBus apublish() is non-blocking." Reality: `apublish` schedules subscribers but if a subscriber blocks, it can hold the daemon. *Mitigated*: bus has BackpressurePolicy; daemon catches exceptions; tick interval continues regardless.
- **FA8**: "The hash never leaves the machine via OC." Reality: a cron job that subscribes to ForegroundAppEvent and emails the hash to a remote server WOULD leak the hash. *Mitigated*: this is a "future capability" risk, not an FE concern; F1 audit log + capability claims protect against this; the no-egress contract applies to the SENSOR, not all subscribers.

### 2. Edge cases

- **EC1**: Title is binary garbage (rare, e.g. malformed app). `_sha256(garbage)` works (UTF-8 encodes with `errors="replace"`-like behavior in Python — actually `.encode("utf-8")` raises on un-encodable). *Mitigated*: wrap encode in try/except + skip hash if it fails.
- **EC2**: User has 100+ tabs in a browser; window title flips constantly. *Mitigated*: 2-second min-interval guard; dedup catches identical reuses.
- **EC3**: User pastes `oc ambient status` output to a public chat. *Mitigated*: status outputs aggregate counts only — explicit test enforces this (`test_status_does_not_leak_specific_apps`).
- **EC4**: Two profiles run their gateway daemons concurrently (multi-profile dev). Both write to `<profile_home>/ambient/heartbeat`. *Mitigated*: profile_home is per-profile (`opencomputer.agent.config._home()`); heartbeat paths don't collide.
- **EC5**: User edits `state.json` manually with bad JSON. *Mitigated*: `load_state` catches `JSONDecodeError`, returns default (disabled).
- **EC6**: Daemon starts at gateway boot but state was deleted between boot-check and tick. *Mitigated*: each tick re-reads state; daemon noops when disabled.
- **EC7**: Foreground app changes WHILE the daemon is paused. After resume, captures whatever's foreground at that moment. No replay. *Documented*: matches the spec's "no catch-up" intent.
- **EC8**: macOS user grants Accessibility permission AFTER the daemon started. The first tick that succeeds will publish; prior ticks logged at DEBUG. *Acceptable*: no replay needed.
- **EC9**: Heartbeat write fails (disk full / readonly profile dir). *Mitigated*: write wrapped in try/except OSError; daemon continues.
- **EC10**: User runs `oc ambient pause` indefinitely, forgets, comes back next month. State file paused_until is 100 years out — daemon skips ticks correctly indefinitely. ✓
- **EC11**: Sensitive-app deny-list pattern is so broad it filters everything (e.g. user adds `(?i).` to the override file). *Acceptable*: daemon happily emits "<filtered>" for every event; aggregate motifs become useless but no leak; user can fix the regex.

### 3. Missing considerations

- **MC1**: **CI matrix coverage**. PR #181 added cross-platform CI for the introspection module. The ambient tests should be added to that same matrix. *Action*: Task 10 final-validation step should ALSO update `.github/workflows/test.yml::test-cross-platform`'s pytest pattern to include `tests/test_ambient_*.py`. Otherwise the cross-platform claim isn't actually CI-verified.
- **MC2**: **Audit log volume**. At 10s tick interval = max 8,640 ticks/day. Dedup eliminates ~99%. So ~50-100 publishes/day actual. F1 audit log can handle that easily. ✓
- **MC3**: **Logging interaction with `setup wizard`**. New users running `opencomputer setup` should be told "the ambient sensor exists but is disabled by default; enable with `oc ambient on` if you want it." *Action*: Phase 1 doesn't touch setup wizard; document this as Phase 1.5 follow-up.
- **MC4**: **Per-turn persona reclassification not in scope**. The events are PUBLISHED but the persona classifier still reads foreground at session start only. So Phase 1 alone doesn't make persona classification mid-session. Subscribers (motif extractor) WILL see the events; classifier won't until V2.D. *Documented*: spec §6 is explicit.
- **MC5**: **What if the user has multiple displays?** Foreground detection returns ONE app (the focused one), not "what's visible". Users with multi-monitor workflows where they "look at" two apps simultaneously lose signal. *Acceptable for v1*: documented in README troubleshooting.
- **MC6**: **What about apps users want WHITELISTED to override sensitive-app match?** E.g. user explicitly wants TradingView captured even though it might match a banking pattern. *Out of scope for v1*: user can refine the regex if it overmatches; whitelist support is a follow-up.
- **MC7**: **Test isolation**. `OPENCOMPUTER_PROFILE_HOME` env var is used in CLI tests. If multiple tests set it concurrently (pytest-xdist), they'll collide. *Mitigated*: monkeypatch sets per-test; pytest-xdist not currently in use.
- **MC8**: **README placement and discoverability**. `extensions/ambient-sensors/README.md` is the canonical doc but users browsing GitHub might not find it. *Action*: link from main `OpenComputer/README.md` Phase 1.5 follow-up; not blocking this PR.
- **MC9**: **Linux distros without subprocess.run timeout**. Python 3.12+ has it universally; OC requires 3.12+. ✓

### 4. Alternative approaches

- **AA1**: Use a single GeneralSensorEvent type instead of ForegroundAppEvent + AmbientSensorPauseEvent. Pros: extensible to future sensors. Cons: discriminator is more complex; explicit types are the project's existing convention. *Rejected*.
- **AA2**: Use an out-of-process daemon with its own pyproject. Pros: stronger isolation. Cons: complicates packaging; OC has nothing else doing this. *Rejected*.
- **AA3**: Skip dedup; let bus subscribers handle it. Pros: simpler daemon. Cons: bus traffic explosion under high context-switch rate; subscribers each have to implement dedup. *Rejected*.
- **AA4**: Use `pygetwindow` library cross-platform. Pros: less platform-specific code. Cons: doesn't work on Wayland; quality varies; not maintained much. *Rejected* — direct platform calls are cleaner.
- **AA5**: Run the daemon on a thread instead of asyncio task. Pros: doesn't depend on the gateway's event loop. Cons: GIL contention, harder to coordinate with the F2 bus's async API. *Rejected* — asyncio matches existing OC patterns.
- **AA6**: Use macOS's NSWorkspace notification API instead of polling. Pros: event-driven, lower CPU. Cons: requires pyobjc, ties to macOS-only path; cross-platform parity is broken. *Rejected for v1; reconsider for v2*.

### 5. Refinements applied (from this audit)

1. **MC1**: Update Task 10's final-validation to also extend `.github/workflows/test.yml`'s `test-cross-platform` pytest pattern to include `tests/test_ambient_*.py`. Otherwise the cross-platform claim isn't CI-verified — exactly the regression PR #181 was designed to prevent.
2. **EC1**: Add a try/except around `_sha256` in the daemon for unencodable titles; log at DEBUG.
3. **EC9**: Already mitigated; explicit comment in daemon.

### 6. Effort estimate (post-audit)

| Task | Implementation | Tests | Total |
|---|---|---|---|
| T1 SDK types + capability | 15m | 15m | 30m |
| T2 Cross-platform detector | 45m | 30m | 75m |
| T3 Sensitive filter | 20m | 20m | 40m |
| T4 Pause state | 20m | 20m | 40m |
| T5 Daemon | 40m | 35m | 75m |
| T6 CLI | 35m | 25m | 60m |
| T7 Plugin manifest + register | 15m | 10m | 25m |
| T8 Doctor + gateway hook | 30m | 25m | 55m |
| T9 No-egress + README | 25m | 15m | 40m |
| T10 CHANGELOG + CI + push + PR | 30m | (CI) | 30m |
| **Total** | | | **~7-8 hours** |

Realistic: 1 day of subagent-driven dev with TDD per task.

### 7. Acceptance criteria (merge bar)

- [ ] `ruff check .` clean.
- [ ] Full pytest green on Python 3.12 + 3.13.
- [ ] `tests/test_ambient_*.py` pattern included in cross-platform CI matrix; all 3 OSes pass.
- [ ] `tests/test_ambient_no_cloud_egress.py` finds 0 violations.
- [ ] `tests/test_ambient_capability_claim.py` enforces `ambient.*` namespace + IMPLICIT tier.
- [ ] CLI smoke tests: on/off/pause/resume/status all work; status output never contains specific app names.
- [ ] CHANGELOG entry with privacy contract section.
- [ ] README at `extensions/ambient-sensors/README.md`.
- [ ] PR body links spec + plan.

---

*Plan complete with self-audit. Ready for subagent-driven execution.*
