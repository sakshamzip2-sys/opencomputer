# Always-On Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-platform always-on daemon support — macOS launchd backend + Windows Task Scheduler backend + unified `oc service install/uninstall/status/start/stop/logs/doctor` CLI + `--install-daemon` convenience flags. Linux systemd backend is preserved via shims.

**Architecture:** Typed `ServiceBackend` Protocol (`opencomputer/service/base.py`) + module-polymorphic backends (`_linux_systemd.py`, `_macos_launchd.py`, `_windows_schtasks.py`) + factory dispatch on `sys.platform` (`factory.py`). Each backend exports module-level functions matching the Protocol — no class hierarchy. CLI calls `factory.get_backend()` once, never branches on `sys.platform`. Backward-compat shims in `service/__init__.py` preserve all 14+ existing import sites; daily profile-analyze cron in `service/launchd.py` is untouched.

**Tech Stack:** Python 3.12+, Typer (CLI), pytest + monkeypatch + unittest.mock, subprocess (systemctl/launchctl/schtasks shells), Jinja2 already in repo (templates use `.format()` instead — consistent with existing template style).

**Spec:** `docs/superpowers/specs/2026-05-03-always-on-daemon-design.md`

---

## File Structure

### New files

| File | Purpose |
|---|---|
| `opencomputer/service/base.py` | `ServiceBackend` Protocol + `InstallResult`/`StatusResult`/`UninstallResult` dataclasses + `ServiceUnsupportedError` |
| `opencomputer/service/factory.py` | `get_backend()` — sys.platform dispatch |
| `opencomputer/service/_common.py` | `resolve_executable()`, `log_paths()`, `workdir()`, `tail_lines()` |
| `opencomputer/service/_linux_systemd.py` | systemd-user gateway backend (Protocol-conforming module) |
| `opencomputer/service/_macos_launchd.py` | macOS launchd gateway backend |
| `opencomputer/service/_windows_schtasks.py` | Windows Task Scheduler backend |
| `opencomputer/service/templates/com.opencomputer.gateway.plist` | macOS launchd plist template |
| `opencomputer/service/templates/opencomputer-task.xml` | Windows Task Scheduler XML template |
| `opencomputer/cli_setup/section_handlers/service_install.py` | platform-agnostic wizard section (replaces `launchd_service.py` content) |
| `tests/test_service_base.py` | Protocol + dataclasses tests |
| `tests/test_service_common.py` | `_common.py` helpers tests |
| `tests/test_service_factory.py` | factory dispatch tests |
| `tests/test_service_linux_backend.py` | `_linux_systemd` backend tests |
| `tests/test_service_macos_backend.py` | `_macos_launchd` backend tests |
| `tests/test_service_windows_backend.py` | `_windows_schtasks` backend tests (Windows-gated) |
| `tests/test_service_alias_deprecation.py` | DeprecationWarning on legacy wizard import |
| `tests/test_cli_service_commands.py` | new CLI subcommands |
| `tests/test_cli_install_daemon_flag.py` | `--install-daemon` flags |
| `tests/test_cli_setup_section_service_install.py` | new wizard section |
| `tests/test_doctor_service_section.py` | doctor.py service health |
| `docs/runbooks/always-on-daemon.md` | user runbook |

### Modified files

| File | What changes |
|---|---|
| `opencomputer/service/__init__.py` | shims delegate to `_linux_systemd`; profile-analyze functions stay |
| `opencomputer/cli_setup/section_handlers/launchd_service.py` | becomes 5-line alias module → `service_install` with DeprecationWarning |
| `opencomputer/cli_setup/sections.py` | swap `run_launchd_service_section` → `run_service_install_section` |
| `opencomputer/cli.py` | add `service start/stop/logs/doctor` subcommands; `--install-daemon` on `setup` and `gateway` |
| `opencomputer/doctor.py` | new `_check_service()` section |
| `tests/test_cli_setup_section_launchd_service.py` | renamed → `test_cli_setup_section_service_install.py`; calls renamed function |
| `.github/workflows/test.yml` | add `os: [ubuntu-latest, macos-latest, windows-latest]` matrix |
| `README.md` | one-liner under Quick Start linking to runbook |
| `CLAUDE.md` | new row in §4; new gotcha in §7 |

---

## Tasks

### Task 1: Protocol + result dataclasses (`service/base.py`)

**Files:**
- Create: `opencomputer/service/base.py`
- Test: `tests/test_service_base.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_service_base.py
"""Protocol + result dataclasses for the cross-platform service backend."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_install_result_is_frozen_dataclass() -> None:
    from opencomputer.service.base import InstallResult

    r = InstallResult(
        backend="systemd",
        config_path=Path("/tmp/x.service"),
        enabled=True,
        started=True,
        notes=["hint"],
    )
    assert r.backend == "systemd"
    assert r.config_path == Path("/tmp/x.service")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.backend = "launchd"  # type: ignore[misc]


def test_status_result_fields() -> None:
    from opencomputer.service.base import StatusResult

    s = StatusResult(
        backend="launchd",
        file_present=True,
        enabled=True,
        running=False,
        pid=None,
        uptime_seconds=None,
        last_log_lines=[],
    )
    assert s.backend == "launchd"
    assert s.pid is None


def test_uninstall_result_fields() -> None:
    from opencomputer.service.base import UninstallResult

    u = UninstallResult(
        backend="schtasks",
        file_removed=True,
        config_path=Path("/tmp/x.xml"),
        notes=[],
    )
    assert u.file_removed is True


def test_service_unsupported_error_is_runtime_error() -> None:
    from opencomputer.service.base import ServiceUnsupportedError

    assert issubclass(ServiceUnsupportedError, RuntimeError)
    err = ServiceUnsupportedError("no backend for platform foo")
    assert "no backend" in str(err)


def test_protocol_has_required_attrs() -> None:
    """ServiceBackend Protocol declares the expected methods + NAME class var."""
    from opencomputer.service.base import ServiceBackend

    # Protocol attribute names accessible via __annotations__ or method lookup
    expected_methods = {"supported", "install", "uninstall", "status", "start", "stop", "follow_logs"}
    actual = set(dir(ServiceBackend)) & expected_methods
    assert actual == expected_methods, f"missing methods: {expected_methods - actual}"
```

- [ ] **Step 2: Run tests; expect FAIL**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
.venv/bin/pytest tests/test_service_base.py -v
```

Expected: ImportError / ModuleNotFoundError for `opencomputer.service.base`.

- [ ] **Step 3: Implement `service/base.py`**

```python
# opencomputer/service/base.py
"""Cross-platform service backend Protocol + result dataclasses.

This is the contract every per-platform backend module must satisfy.
The factory in ``service/factory.py`` returns a module conforming to
this Protocol (modules-as-objects polymorphism; no inheritance).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol


class ServiceUnsupportedError(RuntimeError):
    """Raised when the current platform has no service backend."""


@dataclass(frozen=True)
class InstallResult:
    backend: str
    config_path: Path
    enabled: bool
    started: bool
    notes: list[str]


@dataclass(frozen=True)
class StatusResult:
    backend: str
    file_present: bool
    enabled: bool
    running: bool
    pid: int | None
    uptime_seconds: float | None
    last_log_lines: list[str]


@dataclass(frozen=True)
class UninstallResult:
    backend: str
    file_removed: bool
    config_path: Path | None
    notes: list[str]


class ServiceBackend(Protocol):
    """Module-level Protocol every backend conforms to."""

    NAME: ClassVar[str]

    def supported(self) -> bool: ...

    def install(
        self,
        *,
        profile: str,
        extra_args: str,
        restart: bool = True,
    ) -> InstallResult: ...

    def uninstall(self) -> UninstallResult: ...

    def status(self) -> StatusResult: ...

    def start(self) -> bool: ...

    def stop(self) -> bool: ...

    def follow_logs(
        self, *, lines: int = 100, follow: bool = False
    ) -> Iterator[str]: ...


__all__ = [
    "InstallResult",
    "ServiceBackend",
    "ServiceUnsupportedError",
    "StatusResult",
    "UninstallResult",
]
```

- [ ] **Step 4: Run tests; expect PASS**

```bash
.venv/bin/pytest tests/test_service_base.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/base.py tests/test_service_base.py
git commit -m "feat(service): Protocol + result dataclasses for cross-platform backend"
```

---

### Task 2: `_common.py` — `resolve_executable()`

**Files:**
- Create: `opencomputer/service/_common.py`
- Test: `tests/test_service_common.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_service_common.py
"""Common helpers shared across all service backends."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_resolve_executable_finds_oc_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service._common import resolve_executable

    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/oc" if name == "oc" else None)
    assert resolve_executable() == "/usr/local/bin/oc"


def test_resolve_executable_falls_back_to_opencomputer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service._common import resolve_executable

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/opencomputer" if name == "opencomputer" else None

    monkeypatch.setattr("shutil.which", fake_which)
    assert resolve_executable() == "/usr/local/bin/opencomputer"


def test_resolve_executable_searches_homebrew_when_path_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from opencomputer.service import _common

    monkeypatch.setattr("shutil.which", lambda name: None)
    homebrew = tmp_path / "opt" / "homebrew" / "bin"
    homebrew.mkdir(parents=True)
    fake_oc = homebrew / "oc"
    fake_oc.write_text("#!/bin/sh\n")
    fake_oc.chmod(0o755)

    monkeypatch.setattr(_common, "_FALLBACK_PATHS", [fake_oc])
    assert resolve_executable_via(_common) == str(fake_oc)


def resolve_executable_via(_common):  # helper to keep the call site importable
    return _common.resolve_executable()


def test_resolve_executable_raises_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _common

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_common, "_FALLBACK_PATHS", [])

    with pytest.raises(RuntimeError, match="could not find oc executable"):
        _common.resolve_executable()
```

- [ ] **Step 2: Run; expect FAIL** (`ImportError`)

```bash
.venv/bin/pytest tests/test_service_common.py -v
```

- [ ] **Step 3: Implement `_common.resolve_executable`**

```python
# opencomputer/service/_common.py
"""Helpers shared across service backends.

resolve_executable: locate the ``oc`` shim by trying ``shutil.which`` first,
then a known list of fallbacks (Homebrew, pipx, pyenv, sys.executable's dir).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_FALLBACK_PATHS: list[Path] = [
    Path("/opt/homebrew/bin/oc"),
    Path("/opt/homebrew/bin/opencomputer"),
    Path.home() / ".local" / "bin" / "oc",
    Path.home() / ".local" / "bin" / "opencomputer",
    Path.home() / ".pyenv" / "shims" / "oc",
    Path.home() / ".pyenv" / "shims" / "opencomputer",
    Path(sys.executable).parent / "oc",
    Path(sys.executable).parent / "opencomputer",
]


def resolve_executable() -> str:
    """Locate the oc/opencomputer executable. Returns absolute path string.

    Search order: ``shutil.which("oc")`` → ``shutil.which("opencomputer")``
    → ``_FALLBACK_PATHS`` (Homebrew, ~/.local/bin, pyenv shims, sys.executable dir)
    → ``OC_EXECUTABLE`` env var as last-resort override.
    """
    override = os.environ.get("OC_EXECUTABLE")
    if override and Path(override).exists():
        return override
    for name in ("oc", "opencomputer"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _FALLBACK_PATHS:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "could not find oc executable. Tried: $PATH, "
        "/opt/homebrew/bin, ~/.local/bin, ~/.pyenv/shims, "
        f"{Path(sys.executable).parent}. "
        "Set OC_EXECUTABLE env var to override.",
    )
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_service_common.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_common.py tests/test_service_common.py
git commit -m "feat(service): _common.resolve_executable with fallbacks"
```

---

### Task 3: `_common.py` — `log_paths`, `workdir`, `tail_lines`

**Files:**
- Modify: `opencomputer/service/_common.py`
- Modify: `tests/test_service_common.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_service_common.py`:

```python
def test_log_paths_returns_stdout_and_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import log_paths

    out, err = log_paths("default")
    assert out == tmp_path / ".opencomputer" / "default" / "logs" / "gateway.stdout.log"
    assert err == tmp_path / ".opencomputer" / "default" / "logs" / "gateway.stderr.log"
    assert out.parent.exists()  # logs dir created


def test_workdir_creates_profile_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import workdir

    wd = workdir("myprofile")
    assert wd == tmp_path / ".opencomputer" / "myprofile"
    assert wd.exists()


def test_workdir_rejects_path_traversal_in_profile_name() -> None:
    from opencomputer.service._common import workdir

    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("../../etc")
    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("foo/bar")
    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("")


def test_workdir_accepts_dashes_dots_underscores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import workdir

    for ok in ("p1", "my-profile", "my_profile", "my.profile", "p"):
        wd = workdir(ok)
        assert wd.exists()


def test_tail_lines_returns_last_n(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    f = tmp_path / "log"
    f.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    out = tail_lines(f, 5)
    assert out == ["line 15", "line 16", "line 17", "line 18", "line 19"]


def test_tail_lines_handles_missing_file(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    out = tail_lines(tmp_path / "does-not-exist", 5)
    assert out == []


def test_tail_lines_handles_short_file(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    f = tmp_path / "log"
    f.write_text("only\ntwo lines\n")
    out = tail_lines(f, 5)
    assert out == ["only", "two lines"]
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_common.py -v
```

- [ ] **Step 3: Implement helpers**

Append to `opencomputer/service/_common.py`:

```python
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _validate_profile(profile: str) -> str:
    """Reject profile names with path-traversal or shell-metachar potential."""
    if not _PROFILE_NAME_RE.match(profile):
        raise ValueError(
            f"invalid profile name {profile!r}: must match {_PROFILE_NAME_RE.pattern}",
        )
    return profile


def workdir(profile: str) -> Path:
    """Return the per-profile workdir, creating it if absent.

    Defaults to ``~/.opencomputer/<profile>``. Profile name is validated
    against a strict allowlist to prevent path-traversal abuse.
    """
    _validate_profile(profile)
    base = Path(os.environ.get("HOME", str(Path.home())))
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    wd = base / ".opencomputer" / profile
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def log_paths(profile: str) -> tuple[Path, Path]:
    """Return (stdout_log, stderr_log) paths for the gateway service.

    Creates the parent ``logs/`` dir if absent. Used by all three backends
    so the log location is consistent across platforms (the OS service
    manager redirects ``stdout``/``stderr`` to these files on macOS/Windows;
    Linux uses journald and ignores them).
    """
    base = workdir(profile) / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "gateway.stdout.log", base / "gateway.stderr.log")


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path``. Empty list if file missing.

    Reads the whole file (small log files are common). For very large
    logs, callers should slice journalctl/launchctl output before this.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else []
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_service_common.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_common.py tests/test_service_common.py
git commit -m "feat(service): _common.log_paths/workdir/tail_lines helpers"
```

---

### Task 4: Factory dispatch (`service/factory.py`)

**Files:**
- Create: `opencomputer/service/factory.py`
- Test: `tests/test_service_factory.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_service_factory.py
"""factory.get_backend dispatches on sys.platform."""
from __future__ import annotations

import pytest


def test_factory_returns_linux_backend_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "systemd"


def test_factory_returns_macos_backend_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "launchd"


def test_factory_returns_windows_backend_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "schtasks"


def test_factory_raises_on_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd14")
    from opencomputer.service.base import ServiceUnsupportedError
    from opencomputer.service.factory import get_backend

    with pytest.raises(ServiceUnsupportedError, match="freebsd14"):
        get_backend()


def test_factory_returned_backend_has_protocol_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    for name in ("supported", "install", "uninstall", "status", "start", "stop", "follow_logs"):
        assert callable(getattr(backend, name)), f"backend missing {name}"
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_factory.py -v
```

- [ ] **Step 3: Implement `factory.py`**

Note: this implementation imports the backend modules lazily — they don't all exist yet. Tasks 5/9/11/13 add them.

```python
# opencomputer/service/factory.py
"""Pick the right service backend module for the current platform."""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .base import ServiceUnsupportedError

if TYPE_CHECKING:
    from .base import ServiceBackend


def get_backend() -> ServiceBackend:
    """Return the module-level ServiceBackend conforming to the current platform.

    Lazy imports keep test bootstrap fast and avoid loading unused
    backends (e.g., systemd module on macOS).
    """
    if sys.platform == "darwin":
        from . import _macos_launchd as backend
    elif sys.platform.startswith("linux"):
        from . import _linux_systemd as backend
    elif sys.platform.startswith("win"):
        from . import _windows_schtasks as backend
    else:
        raise ServiceUnsupportedError(
            f"no service backend for sys.platform={sys.platform!r}",
        )
    return backend  # type: ignore[return-value]


__all__ = ["get_backend"]
```

- [ ] **Step 4: Run partial tests** (expect 1 pass + 3 fail because backends don't exist yet)

```bash
.venv/bin/pytest tests/test_service_factory.py::test_factory_raises_on_unsupported_platform -v
```

Expected: PASS. Remaining 4 tests fail until tasks 5/9/11 land — that's intentional. We'll re-run the full file at end of Task 11.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/factory.py tests/test_service_factory.py
git commit -m "feat(service): factory.get_backend dispatch on sys.platform"
```

---

### Task 5: Linux backend — render + install + uninstall

**Files:**
- Create: `opencomputer/service/_linux_systemd.py`
- Test: `tests/test_service_linux_backend.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_service_linux_backend.py
"""Linux systemd-user backend (gateway, not the daily profile-analyze cron)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    True, reason="enable on Linux only — gated below per-test"
)


def _is_linuxish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")


def test_install_writes_unit_with_restart_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _linux_systemd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    _is_linuxish(monkeypatch)
    monkeypatch.setattr(
        _linux_systemd, "_resolve_executable",
        lambda: "/usr/local/bin/oc",
    )

    with patch.object(_linux_systemd, "_systemctl") as sysctl:
        sysctl.return_value = (0, "active", "")
        result = _linux_systemd.install(profile="default", extra_args="gateway")

    expected = fake_home / ".config" / "systemd" / "user" / "opencomputer.service"
    assert result.config_path == expected
    assert result.backend == "systemd"
    body = expected.read_text()
    assert "Restart=always" in body
    assert "ExecStart=/usr/local/bin/oc --headless --profile default gateway" in body


def test_uninstall_removes_unit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _linux_systemd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    _is_linuxish(monkeypatch)
    monkeypatch.setattr(
        _linux_systemd, "_resolve_executable",
        lambda: "/usr/local/bin/oc",
    )

    with patch.object(_linux_systemd, "_systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        result_install = _linux_systemd.install(profile="default", extra_args="gateway")
        assert result_install.config_path.exists()
        result_uninstall = _linux_systemd.uninstall()
        assert not result_install.config_path.exists()
        assert result_uninstall.file_removed is True


def test_supported_returns_true_only_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setattr("sys.platform", "linux")
    assert _linux_systemd.supported() is True
    monkeypatch.setattr("sys.platform", "darwin")
    assert _linux_systemd.supported() is False
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_linux_backend.py -v
```

- [ ] **Step 3: Implement `_linux_systemd.py` (install + uninstall + supported)**

```python
# opencomputer/service/_linux_systemd.py
"""systemd-user service backend for the always-on gateway daemon.

Conforms to ``opencomputer.service.base.ServiceBackend`` Protocol via
module-level functions. The factory in ``service/factory.py`` returns
this module on Linux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from . import _common
from .base import InstallResult, StatusResult, UninstallResult

NAME: ClassVar[str] = "systemd"
_UNIT_FILENAME = "opencomputer.service"
_TEMPLATE = (Path(__file__).parent / "templates" / _UNIT_FILENAME).read_text()


def supported() -> bool:
    return sys.platform.startswith("linux")


def _user_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / _UNIT_FILENAME


def _systemctl(*args: str) -> tuple[int, str, str]:
    if shutil.which("systemctl") is None:
        return (0, "", "(systemctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _render_unit(executable: str, workdir: Path, profile: str, extra_args: str) -> str:
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        extra_args=extra_args,
    )


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    body = _render_unit(executable, wd, profile, extra_args)
    path = _user_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    rc_dr, _, _ = _systemctl("daemon-reload")
    notes: list[str] = []
    enabled = False
    started = False
    if restart:
        rc_en, _, _ = _systemctl("enable", "--now", _UNIT_FILENAME)
        enabled = rc_en == 0
        if enabled:
            rc_act, out, _ = _systemctl("is-active", _UNIT_FILENAME)
            started = rc_act == 0 and out.strip() == "active"
    if not _is_lingering():
        notes.append(
            "On a headless Linux server, run `sudo loginctl enable-linger $USER` "
            "so the service keeps running across SSH disconnects.",
        )
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started, notes=notes,
    )


def uninstall() -> UninstallResult:
    path = _user_unit_path()
    if not path.exists():
        return UninstallResult(
            backend=NAME, file_removed=False, config_path=None, notes=[],
        )
    _systemctl("stop", _UNIT_FILENAME)
    _systemctl("disable", _UNIT_FILENAME)
    path.unlink()
    _systemctl("daemon-reload")
    return UninstallResult(
        backend=NAME, file_removed=True, config_path=path, notes=[],
    )


def _is_lingering() -> bool:
    if shutil.which("loginctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
            capture_output=True, text=True, timeout=5,
        )
        return "Linger=yes" in proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


# status / start / stop / follow_logs added in next tasks
def status() -> StatusResult:
    raise NotImplementedError("see Task 6")


def start() -> bool:
    raise NotImplementedError("see Task 7")


def stop() -> bool:
    raise NotImplementedError("see Task 7")


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    raise NotImplementedError("see Task 7")


__all__ = [
    "NAME",
    "follow_logs",
    "install",
    "start",
    "status",
    "stop",
    "supported",
    "uninstall",
]
```

- [ ] **Step 4: Run; expect PASS for the 3 tests in this task**

```bash
.venv/bin/pytest tests/test_service_linux_backend.py::test_install_writes_unit_with_restart_always tests/test_service_linux_backend.py::test_uninstall_removes_unit_file tests/test_service_linux_backend.py::test_supported_returns_true_only_on_linux -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_linux_systemd.py tests/test_service_linux_backend.py
git commit -m "feat(service): _linux_systemd install/uninstall/supported"
```

---

### Task 6: Linux backend — `status()`

**Files:**
- Modify: `opencomputer/service/_linux_systemd.py`
- Modify: `tests/test_service_linux_backend.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_service_linux_backend.py`:

```python
def test_status_reports_running_active_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    unit_path = _linux_systemd._user_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("(unit body)")

    def fake_systemctl(*args):
        if args == ("is-enabled", "opencomputer.service"):
            return (0, "enabled\n", "")
        if args == ("is-active", "opencomputer.service"):
            return (0, "active\n", "")
        if args[0] == "show":
            # MainPID + ActiveEnterTimestampMonotonic
            return (0, "MainPID=12345\nActiveEnterTimestampMonotonic=42000000\n", "")
        if args[0] == "is-active":
            return (0, "active\n", "")
        return (0, "", "")

    monkeypatch.setattr(_linux_systemd, "_systemctl", fake_systemctl)
    monkeypatch.setattr(_linux_systemd, "_journalctl_tail", lambda n: ["log line A", "log line B"])

    s = _linux_systemd.status()
    assert s.backend == "systemd"
    assert s.file_present is True
    assert s.enabled is True
    assert s.running is True
    assert s.pid == 12345
    assert s.last_log_lines == ["log line A", "log line B"]


def test_status_reports_missing_file_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(_linux_systemd, "_systemctl", lambda *a: (3, "", ""))
    monkeypatch.setattr(_linux_systemd, "_journalctl_tail", lambda n: [])

    s = _linux_systemd.status()
    assert s.file_present is False
    assert s.enabled is False
    assert s.running is False
    assert s.pid is None
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_linux_backend.py -v -k status
```

- [ ] **Step 3: Implement `status()` and `_journalctl_tail()`**

In `_linux_systemd.py`, replace the `status()` stub:

```python
def _journalctl_tail(n: int) -> list[str]:
    if shutil.which("journalctl") is None:
        return []
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "-u", _UNIT_FILENAME, "-n", str(n), "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        return [ln for ln in proc.stdout.splitlines() if ln.strip()][-n:]
    except (subprocess.TimeoutExpired, OSError):
        return []


def status() -> StatusResult:
    path = _user_unit_path()
    file_present = path.exists()
    rc_en, out_en, _ = _systemctl("is-enabled", _UNIT_FILENAME)
    enabled = rc_en == 0 and out_en.strip() == "enabled"
    rc_ac, out_ac, _ = _systemctl("is-active", _UNIT_FILENAME)
    running = rc_ac == 0 and out_ac.strip() == "active"
    pid: int | None = None
    uptime: float | None = None
    if running:
        rc_sh, out_sh, _ = _systemctl("show", _UNIT_FILENAME, "-p", "MainPID,ActiveEnterTimestampMonotonic")
        if rc_sh == 0:
            for line in out_sh.splitlines():
                if line.startswith("MainPID="):
                    try:
                        pid = int(line.split("=", 1)[1])
                        if pid == 0:
                            pid = None
                    except ValueError:
                        pid = None
                # uptime parsed from monotonic timestamp; conversion to wall-clock
                # is approximate — we leave it None unless precisely needed.
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=pid,
        uptime_seconds=uptime,
        last_log_lines=_journalctl_tail(5),
    )
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_service_linux_backend.py -v -k status
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_linux_systemd.py tests/test_service_linux_backend.py
git commit -m "feat(service): _linux_systemd.status with PID + journalctl tail"
```

---

### Task 7: Linux backend — `start/stop/follow_logs`

**Files:**
- Modify: `opencomputer/service/_linux_systemd.py`
- Modify: `tests/test_service_linux_backend.py`

- [ ] **Step 1: Add failing tests**

```python
def test_start_invokes_systemctl_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    calls = []
    monkeypatch.setattr(
        _linux_systemd, "_systemctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _linux_systemd.start() is True
    assert ("start", "opencomputer.service") in calls


def test_stop_invokes_systemctl_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    calls = []
    monkeypatch.setattr(
        _linux_systemd, "_systemctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _linux_systemd.stop() is True
    assert ("stop", "opencomputer.service") in calls


def test_follow_logs_returns_journalctl_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setattr(_linux_systemd, "_journalctl_tail", lambda n: ["a", "b", "c"])
    out = list(_linux_systemd.follow_logs(lines=3, follow=False))
    assert out == ["a", "b", "c"]
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Implement `start/stop/follow_logs`**

Replace the three stubs in `_linux_systemd.py`:

```python
def start() -> bool:
    rc, _, _ = _systemctl("start", _UNIT_FILENAME)
    return rc == 0


def stop() -> bool:
    rc, _, _ = _systemctl("stop", _UNIT_FILENAME)
    return rc == 0


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    if not follow:
        yield from _journalctl_tail(lines)
        return
    if shutil.which("journalctl") is None:
        return
    try:
        proc = subprocess.Popen(
            ["journalctl", "--user", "-u", _UNIT_FILENAME, "-f", "-n", str(lines)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return
    try:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            yield line.rstrip()
    finally:
        proc.terminate()
```

- [ ] **Step 4: Run; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_linux_systemd.py tests/test_service_linux_backend.py
git commit -m "feat(service): _linux_systemd start/stop/follow_logs"
```

---

### Task 8: `service/__init__.py` shims

**Files:**
- Modify: `opencomputer/service/__init__.py`

- [ ] **Step 1: Run existing test to confirm shape**

```bash
.venv/bin/pytest tests/test_service_install.py -v
```

Expected: 4 passed. (Captures the legacy public API.)

- [ ] **Step 2: Refactor `__init__.py` to delegate to `_linux_systemd`**

Replace the body of `opencomputer/service/__init__.py` with:

```python
"""Public service-install API.

Gateway-related functions delegate to the per-platform backend modules
(see ``service/_linux_systemd.py``, ``_macos_launchd.py``, ``_windows_schtasks.py``).
The cross-platform entry point is ``service.factory.get_backend()``.

The profile-analyze daily-cron functions stay here unchanged — they are
a different concern (StartCalendarInterval / OnCalendar timer, not an
always-on supervisor).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .base import ServiceUnsupportedError

# ─── Gateway shims (delegate to _linux_systemd) ───────────────────────


def render_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str,
) -> str:
    """Compat shim — pure render, no platform check."""
    from . import _linux_systemd
    return _linux_systemd._render_unit(
        executable=executable, workdir=Path(workdir),
        profile=profile, extra_args=extra_args,
    )


def install_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str,
) -> Path:
    """Compat shim — Linux only.

    The cross-platform replacement is ``service.factory.get_backend().install(...)``.
    """
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}",
        )
    from . import _linux_systemd
    body = _linux_systemd._render_unit(executable, Path(workdir), profile, extra_args)
    path = _linux_systemd._user_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _systemctl("daemon-reload")
    return path


def uninstall_systemd_unit() -> Path | None:
    """Compat shim — Linux only."""
    from . import _linux_systemd
    path = _linux_systemd._user_unit_path()
    if not path.exists():
        return None
    _systemctl("stop", "opencomputer.service")
    _systemctl("disable", "opencomputer.service")
    path.unlink()
    _systemctl("daemon-reload")
    return path


def is_active() -> bool:
    """Compat shim — Linux only."""
    rc, out, _ = _systemctl("is-active", "opencomputer.service")
    return rc == 0 and out.strip() == "active"


def _systemctl(*args: str) -> tuple[int, str, str]:
    """Internal compat — wraps the systemd backend's _systemctl."""
    if shutil.which("systemctl") is None:
        return (0, "", "(systemctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


# ─── Profile-analyze daily timer (unchanged from prior versions) ──────

_PA_TIMER_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.timer"
).read_text()
_PA_SERVICE_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.service"
).read_text()
_PA_UNIT_NAME = "opencomputer-profile-analyze"


def _pa_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def install_profile_analyze_timer(*, executable: str) -> tuple[Path, Path]:
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}",
        )
    log_path = str(Path.home() / ".opencomputer" / "profile-analyze.log")
    target_dir = _pa_unit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    timer_path = target_dir / f"{_PA_UNIT_NAME}.timer"
    service_path = target_dir / f"{_PA_UNIT_NAME}.service"
    timer_path.write_text(_PA_TIMER_TEMPLATE)
    service_path.write_text(
        _PA_SERVICE_TEMPLATE.format(executable=executable, log_path=log_path),
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{_PA_UNIT_NAME}.timer")
    return (timer_path, service_path)


def uninstall_profile_analyze_timer() -> tuple[Path | None, Path | None]:
    target_dir = _pa_unit_dir()
    timer_path = target_dir / f"{_PA_UNIT_NAME}.timer"
    service_path = target_dir / f"{_PA_UNIT_NAME}.service"
    timer_existed = timer_path.exists()
    service_existed = service_path.exists()
    _systemctl("stop", f"{_PA_UNIT_NAME}.timer")
    _systemctl("disable", f"{_PA_UNIT_NAME}.timer")
    if timer_existed:
        timer_path.unlink()
    if service_existed:
        service_path.unlink()
    _systemctl("daemon-reload")
    return (
        timer_path if timer_existed else None,
        service_path if service_existed else None,
    )


def is_profile_analyze_timer_active() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    rc, out, _ = _systemctl("is-active", f"{_PA_UNIT_NAME}.timer")
    return rc == 0 and out.strip() == "active"


__all__ = [
    "ServiceUnsupportedError",
    "install_profile_analyze_timer",
    "install_systemd_unit",
    "is_active",
    "is_profile_analyze_timer_active",
    "render_systemd_unit",
    "uninstall_profile_analyze_timer",
    "uninstall_systemd_unit",
]
```

- [ ] **Step 3: Run all existing service tests**

```bash
.venv/bin/pytest tests/test_service_install.py tests/test_service_linux_backend.py -v
```

Expected: all green (4 + 8 = 12 tests).

- [ ] **Step 4: Commit**

```bash
git add opencomputer/service/__init__.py
git commit -m "refactor(service): __init__.py shims delegate to _linux_systemd"
```

---

### Task 9: macOS launchd — template + install/uninstall

**Files:**
- Create: `opencomputer/service/templates/com.opencomputer.gateway.plist`
- Create: `opencomputer/service/_macos_launchd.py`
- Test: `tests/test_service_macos_backend.py`

- [ ] **Step 1: Write the plist template**

`opencomputer/service/templates/com.opencomputer.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>--headless</string>
        <string>--profile</string>
        <string>{profile}</string>
        <string>gateway</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
```

**Note:** the argv ends at `gateway` (no `run` subcommand). `oc gateway` is itself the long-running command — see `opencomputer/cli.py` near the `gateway()` function. Mismatched here would 400 with "Got unexpected extra argument 'run'."

- [ ] **Step 2: Write failing tests**

```python
# tests/test_service_macos_backend.py
"""macOS launchd gateway backend (NOT the daily profile-analyze cron)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_render_plist_substitutes_fields(tmp_path: Path) -> None:
    from opencomputer.service import _macos_launchd

    body = _macos_launchd._render_plist(
        executable="/opt/homebrew/bin/oc",
        workdir=tmp_path,
        profile="default",
        stdout_log=tmp_path / "stdout.log",
        stderr_log=tmp_path / "stderr.log",
    )
    assert "<string>/opt/homebrew/bin/oc</string>" in body
    assert "<string>--profile</string>" in body
    assert "<string>default</string>" in body
    assert "<key>KeepAlive</key>" in body
    assert "<true/>" in body
    # Plist is well-formed XML
    import xml.etree.ElementTree as ET
    ET.fromstring(body)  # would raise ParseError if malformed


def test_install_writes_plist_and_calls_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    calls: list[tuple[str, ...]] = []
    def fake_launchctl(*a):
        calls.append(a)
        return (0, "", "")
    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)

    result = _macos_launchd.install(profile="default", extra_args="")
    expected = fake_home / "Library" / "LaunchAgents" / "com.opencomputer.gateway.plist"
    assert result.config_path == expected
    assert expected.exists()
    body = expected.read_text()
    assert "com.opencomputer.gateway" in body
    # bootstrap was called
    bootstrap_calls = [c for c in calls if c[:1] == ("bootstrap",)]
    assert bootstrap_calls
    assert bootstrap_calls[0][1] == "gui/501"


def test_uninstall_removes_plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    monkeypatch.setattr(_macos_launchd, "_launchctl", lambda *a: (0, "", ""))

    install_result = _macos_launchd.install(profile="default", extra_args="")
    assert install_result.config_path.exists()

    uninstall_result = _macos_launchd.uninstall()
    assert uninstall_result.file_removed is True
    assert not install_result.config_path.exists()


def test_supported_returns_true_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr("sys.platform", "darwin")
    assert _macos_launchd.supported() is True
    monkeypatch.setattr("sys.platform", "linux")
    assert _macos_launchd.supported() is False
```

- [ ] **Step 3: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_macos_backend.py -v
```

- [ ] **Step 4: Implement `_macos_launchd.py`**

```python
# opencomputer/service/_macos_launchd.py
"""macOS launchd backend for the always-on gateway daemon.

Uses the modern ``launchctl bootstrap gui/<uid>`` API (not the
deprecated ``launchctl load``). Distinct from ``service/launchd.py``
which is the daily profile-analyze cron — different concern, kept
side-by-side.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from . import _common
from .base import InstallResult, StatusResult, UninstallResult

NAME: ClassVar[str] = "launchd"
_LABEL = "com.opencomputer.gateway"
_PLIST_FILENAME = f"{_LABEL}.plist"
_TEMPLATE = (Path(__file__).parent / "templates" / _PLIST_FILENAME).read_text()


def supported() -> bool:
    return sys.platform == "darwin"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _launch_agents_dir() / _PLIST_FILENAME


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _uid() -> int:
    return os.getuid()


def _launchctl(*args: str) -> tuple[int, str, str]:
    if shutil.which("launchctl") is None:
        return (0, "", "(launchctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["launchctl", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _render_plist(
    *,
    executable: str,
    workdir: Path,
    profile: str,
    stdout_log: Path,
    stderr_log: Path,
) -> str:
    return _TEMPLATE.format(
        label=_LABEL,
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    out_log, err_log = _common.log_paths(profile)
    body = _render_plist(
        executable=executable, workdir=wd, profile=profile,
        stdout_log=out_log, stderr_log=err_log,
    )
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # If a previous version is loaded, bootout first so bootstrap doesn't fail.
    if path.exists():
        _launchctl("bootout", f"gui/{_uid()}/{_LABEL}")
    path.write_text(body)
    started = False
    enabled = False
    if restart:
        rc, _, _ = _launchctl("bootstrap", f"gui/{_uid()}", str(path))
        enabled = rc == 0
        started = enabled
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started,
        notes=[],
    )


def uninstall() -> UninstallResult:
    path = _plist_path()
    if not path.exists():
        return UninstallResult(
            backend=NAME, file_removed=False, config_path=None, notes=[],
        )
    _launchctl("bootout", f"gui/{_uid()}/{_LABEL}")
    path.unlink()
    return UninstallResult(
        backend=NAME, file_removed=True, config_path=path, notes=[],
    )


def status() -> StatusResult:
    raise NotImplementedError("see Task 10")


def start() -> bool:
    raise NotImplementedError("see Task 10")


def stop() -> bool:
    raise NotImplementedError("see Task 10")


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    raise NotImplementedError("see Task 10")


__all__ = [
    "NAME",
    "follow_logs",
    "install",
    "start",
    "status",
    "stop",
    "supported",
    "uninstall",
]
```

- [ ] **Step 5: Run; expect PASS for the 4 tests in this task**

```bash
.venv/bin/pytest tests/test_service_macos_backend.py -v -k "render_plist or install or uninstall or supported"
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/service/_macos_launchd.py opencomputer/service/templates/com.opencomputer.gateway.plist tests/test_service_macos_backend.py
git commit -m "feat(service): macOS launchd backend (gateway plist + install/uninstall)"
```

---

### Task 10: macOS backend — `status/start/stop/follow_logs`

**Files:**
- Modify: `opencomputer/service/_macos_launchd.py`
- Modify: `tests/test_service_macos_backend.py`

- [ ] **Step 1: Add failing tests**

```python
def test_status_parses_launchctl_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    # Create a fake plist file so file_present is True.
    fake_plist = tmp_path / "com.opencomputer.gateway.plist"
    fake_plist.write_text("(stub)")
    monkeypatch.setattr(_macos_launchd, "_plist_path", lambda: fake_plist)

    sample_print = """\
gui/501/com.opencomputer.gateway = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/com.opencomputer.gateway.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 91234
}"""

    def fake_launchctl(*args):
        if args[0] == "print":
            return (0, sample_print, "")
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr("opencomputer.service._common.tail_lines", lambda p, n: ["log a", "log b"])

    s = _macos_launchd.status()
    assert s.backend == "launchd"
    assert s.file_present is True
    assert s.running is True
    assert s.pid == 91234


def test_start_kickstart(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    calls = []
    monkeypatch.setattr(_macos_launchd, "_launchctl", lambda *a: (calls.append(a) or (0, "", "")))

    assert _macos_launchd.start() is True
    assert any(c[0] == "kickstart" for c in calls)


def test_stop_bootout(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    calls = []
    monkeypatch.setattr(_macos_launchd, "_launchctl", lambda *a: (calls.append(a) or (0, "", "")))

    # NOTE: stop() should NOT remove the plist — only stop the running instance.
    # On launchd, that's `kill` of the bootstrapped service.
    assert _macos_launchd.stop() is True
    assert any(c[0] == "kill" or c[0] == "bootout" for c in calls)


def test_follow_logs_tails_stdout_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setenv("HOME", str(tmp_path))
    log_dir = tmp_path / ".opencomputer" / "default" / "logs"
    log_dir.mkdir(parents=True)
    out_log = log_dir / "gateway.stdout.log"
    out_log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")

    out = list(_macos_launchd.follow_logs(lines=3, follow=False))
    assert out[-3:] == ["line 7", "line 8", "line 9"]
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Implement status/start/stop/follow_logs**

Replace the four stubs in `_macos_launchd.py`:

```python
def status() -> StatusResult:
    path = _plist_path()
    file_present = path.exists()
    rc, out, _ = _launchctl("print", f"gui/{_uid()}/{_LABEL}")
    enabled = rc == 0
    running = False
    pid: int | None = None
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("state ="):
                running = "running" in line
            elif line.startswith("pid ="):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pid = None
    out_log, _ = _common.log_paths("default")  # log dir is profile-keyed; default is best-effort fallback
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=pid,
        uptime_seconds=None,
        last_log_lines=_common.tail_lines(out_log, 5),
    )


def start() -> bool:
    rc, _, _ = _launchctl("kickstart", "-k", f"gui/{_uid()}/{_LABEL}")
    return rc == 0


def stop() -> bool:
    # SIGTERM the running instance (does not unload the plist).
    rc, _, _ = _launchctl("kill", "SIGTERM", f"gui/{_uid()}/{_LABEL}")
    return rc == 0


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    out_log, _ = _common.log_paths("default")
    if not follow:
        yield from _common.tail_lines(out_log, lines)
        return
    # naive follow: poll every 1s for new lines
    import time
    pos = out_log.stat().st_size if out_log.exists() else 0
    yield from _common.tail_lines(out_log, lines)
    while True:
        if out_log.exists() and out_log.stat().st_size > pos:
            with out_log.open() as f:
                f.seek(pos)
                for line in f:
                    yield line.rstrip()
                pos = f.tell()
        time.sleep(1)
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_service_macos_backend.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_macos_launchd.py tests/test_service_macos_backend.py
git commit -m "feat(service): _macos_launchd status/start/stop/follow_logs"
```

---

### Task 11: Windows backend — template + install/uninstall

**Files:**
- Create: `opencomputer/service/templates/opencomputer-task.xml`
- Create: `opencomputer/service/_windows_schtasks.py`
- Test: `tests/test_service_windows_backend.py`

- [ ] **Step 1: Write the Task Scheduler XML template**

`opencomputer/service/templates/opencomputer-task.xml`:

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenComputer always-on gateway daemon</Description>
    <URI>\OpenComputerGateway</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>9999</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{executable}</Command>
      <Arguments>--headless --profile {profile} gateway</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
  <!-- Note: argv ends at "gateway" (no "run") — oc gateway is itself the long-running command. -->
  
</Task>
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_service_windows_backend.py
"""Windows Task Scheduler backend (gateway always-on)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_render_task_xml_substitutes_fields(tmp_path: Path) -> None:
    from opencomputer.service import _windows_schtasks

    body = _windows_schtasks._render_task(
        executable=r"C:\Python313\Scripts\oc.exe",
        workdir=tmp_path,
        profile="default",
    )
    assert r"<Command>C:\Python313\Scripts\oc.exe</Command>" in body
    assert "--profile default" in body
    assert "<RestartOnFailure>" in body
    # well-formed XML
    import xml.etree.ElementTree as ET
    ET.fromstring(body)


def test_install_invokes_schtasks_create(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(_windows_schtasks, "_resolve_executable", lambda: r"C:\bin\oc.exe")

    calls = []
    def fake_schtasks(*a):
        calls.append(a)
        return (0, "SUCCESS", "")
    monkeypatch.setattr(_windows_schtasks, "_schtasks", fake_schtasks)

    result = _windows_schtasks.install(profile="default", extra_args="")
    assert result.backend == "schtasks"
    assert any(a[0] == "/create" for a in calls)


def test_uninstall_invokes_schtasks_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")

    # Fake the rendered xml file existing
    (tmp_path / ".opencomputer").mkdir()
    xml_path = tmp_path / ".opencomputer" / "opencomputer-task.xml"
    xml_path.write_text("<Task/>")

    monkeypatch.setattr(_windows_schtasks, "_xml_path", lambda: xml_path)
    calls = []
    monkeypatch.setattr(_windows_schtasks, "_schtasks", lambda *a: (calls.append(a) or (0, "", "")))

    result = _windows_schtasks.uninstall()
    assert result.file_removed is True
    assert any(a[0] == "/delete" for a in calls)


def test_supported_returns_true_only_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setattr("sys.platform", "win32")
    assert _windows_schtasks.supported() is True
    monkeypatch.setattr("sys.platform", "linux")
    assert _windows_schtasks.supported() is False
```

- [ ] **Step 3: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_service_windows_backend.py -v
```

- [ ] **Step 4: Implement `_windows_schtasks.py`**

```python
# opencomputer/service/_windows_schtasks.py
"""Windows Task Scheduler backend for the always-on gateway daemon.

User scope (no admin elevation). Triggered on login, restart-on-failure
configured in the task XML. Logs go to ``%USERPROFILE%\.opencomputer\<profile>\logs``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from . import _common
from .base import InstallResult, StatusResult, UninstallResult

NAME: ClassVar[str] = "schtasks"
_TASK_NAME = "OpenComputerGateway"
_TEMPLATE = (Path(__file__).parent / "templates" / "opencomputer-task.xml").read_text()


def supported() -> bool:
    return sys.platform.startswith("win")


def _user_dir() -> Path:
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / ".opencomputer"


def _xml_path() -> Path:
    return _user_dir() / "opencomputer-task.xml"


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _schtasks(*args: str) -> tuple[int, str, str]:
    if shutil.which("schtasks") is None:
        return (0, "", "(schtasks not found — skipping)")
    try:
        proc = subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _render_task(*, executable: str, workdir: Path, profile: str) -> str:
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
    )


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    body = _render_task(executable=executable, workdir=wd, profile=profile)
    path = _xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-16")
    rc, _, err = _schtasks("/create", "/xml", str(path), "/tn", _TASK_NAME, "/f")
    enabled = rc == 0
    started = False
    if restart and enabled:
        rc_run, _, _ = _schtasks("/run", "/tn", _TASK_NAME)
        started = rc_run == 0
    notes: list[str] = []
    if not enabled:
        notes.append(f"schtasks /create returned {rc}: {err.strip()}")
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started, notes=notes,
    )


def uninstall() -> UninstallResult:
    _schtasks("/delete", "/tn", _TASK_NAME, "/f")
    path = _xml_path()
    if path.exists():
        path.unlink()
        return UninstallResult(
            backend=NAME, file_removed=True, config_path=path, notes=[],
        )
    return UninstallResult(
        backend=NAME, file_removed=False, config_path=None, notes=[],
    )


def status() -> StatusResult:
    raise NotImplementedError("see Task 12")


def start() -> bool:
    raise NotImplementedError("see Task 12")


def stop() -> bool:
    raise NotImplementedError("see Task 12")


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    raise NotImplementedError("see Task 12")


__all__ = [
    "NAME",
    "follow_logs",
    "install",
    "start",
    "status",
    "stop",
    "supported",
    "uninstall",
]
```

- [ ] **Step 5: Run; expect PASS for the 4 tests in this task**

```bash
.venv/bin/pytest tests/test_service_windows_backend.py -v -k "render_task or install_invokes or uninstall_invokes or supported"
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/service/_windows_schtasks.py opencomputer/service/templates/opencomputer-task.xml tests/test_service_windows_backend.py
git commit -m "feat(service): Windows Task Scheduler backend (install/uninstall)"
```

---

### Task 12: Windows backend — `status/start/stop/follow_logs`

**Files:**
- Modify: `opencomputer/service/_windows_schtasks.py`
- Modify: `tests/test_service_windows_backend.py`

- [ ] **Step 1: Add failing tests**

```python
def test_status_parses_schtasks_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    sample = """\
HostName:                             DESKTOP-AB1
TaskName:                             \\OpenComputerGateway
Status:                               Running
"""
    fake_xml = tmp_path / "opencomputer-task.xml"
    fake_xml.write_text("<Task/>")
    monkeypatch.setattr(_windows_schtasks, "_xml_path", lambda: fake_xml)
    monkeypatch.setattr(_windows_schtasks, "_schtasks", lambda *a: (0, sample, ""))

    s = _windows_schtasks.status()
    assert s.backend == "schtasks"
    assert s.file_present is True
    assert s.running is True


def test_start_invokes_schtasks_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _windows_schtasks
    calls = []
    monkeypatch.setattr(_windows_schtasks, "_schtasks", lambda *a: (calls.append(a) or (0, "", "")))
    assert _windows_schtasks.start() is True
    assert any("/run" in a for a in calls)


def test_stop_invokes_schtasks_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _windows_schtasks
    calls = []
    monkeypatch.setattr(_windows_schtasks, "_schtasks", lambda *a: (calls.append(a) or (0, "", "")))
    assert _windows_schtasks.stop() is True
    assert any("/end" in a for a in calls)
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Implement status/start/stop/follow_logs**

Replace stubs in `_windows_schtasks.py`:

```python
def status() -> StatusResult:
    path = _xml_path()
    file_present = path.exists()
    rc, out, _ = _schtasks("/query", "/tn", _TASK_NAME, "/v", "/fo", "list")
    enabled = rc == 0
    running = False
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("Status:") and "Running" in line:
                running = True
                break
    out_log, _ = _common.log_paths("default")
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=None,
        uptime_seconds=None,
        last_log_lines=_common.tail_lines(out_log, 5),
    )


def start() -> bool:
    rc, _, _ = _schtasks("/run", "/tn", _TASK_NAME)
    return rc == 0


def stop() -> bool:
    rc, _, _ = _schtasks("/end", "/tn", _TASK_NAME)
    return rc == 0


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    out_log, _ = _common.log_paths("default")
    if not follow:
        yield from _common.tail_lines(out_log, lines)
        return
    import time
    pos = out_log.stat().st_size if out_log.exists() else 0
    yield from _common.tail_lines(out_log, lines)
    while True:
        if out_log.exists() and out_log.stat().st_size > pos:
            with out_log.open() as f:
                f.seek(pos)
                for line in f:
                    yield line.rstrip()
                pos = f.tell()
        time.sleep(1)
```

- [ ] **Step 4: Run; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_windows_schtasks.py tests/test_service_windows_backend.py
git commit -m "feat(service): _windows_schtasks status/start/stop/follow_logs"
```

---

### Task 13: CLI — `service start/stop/logs/doctor`

**Files:**
- Modify: `opencomputer/cli.py` (around the existing `service_app` block, ~line 2451)
- Test: `tests/test_cli_service_commands.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli_service_commands.py
"""New oc service subcommands: start, stop, logs, doctor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_service_start_invokes_factory_backend_start(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.start.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "start"])
    assert result.exit_code == 0
    fake_backend.start.assert_called_once()


def test_service_stop_invokes_factory_backend_stop(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.stop.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "stop"])
    assert result.exit_code == 0
    fake_backend.stop.assert_called_once()


def test_service_logs_returns_recent_lines(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.follow_logs.return_value = iter(["line a", "line b"])
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "logs", "-n", "2"])
    assert result.exit_code == 0
    assert "line a" in result.stdout
    assert "line b" in result.stdout


def test_service_doctor_reports_health_checks(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_status = MagicMock(
        backend="systemd", file_present=True, enabled=True, running=True,
        pid=12345, uptime_seconds=None,
        last_log_lines=["ok"],
    )
    fake_backend.status.return_value = fake_status
    fake_backend.NAME = "systemd"
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "doctor"])
    assert result.exit_code == 0
    assert "config_file_present" in result.stdout
    assert "service_running" in result.stdout
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_cli_service_commands.py -v
```

- [ ] **Step 3: Add subcommands to `cli.py`**

Find the existing `service_app = typer.Typer(...)` block (around line 2451). Add after the existing `_service_status` command:

```python
# ─── new: start / stop / logs / doctor ────────────────────────────────


@service_app.command("start")
def _service_start() -> None:
    """OS-level start (no install). Idempotent."""
    from opencomputer.service.factory import get_backend
    backend = get_backend()
    ok = backend.start()
    typer.echo("started" if ok else "start failed")
    raise typer.Exit(0 if ok else 1)


@service_app.command("stop")
def _service_stop() -> None:
    """OS-level stop (does not uninstall the service)."""
    from opencomputer.service.factory import get_backend
    backend = get_backend()
    ok = backend.stop()
    typer.echo("stopped" if ok else "stop failed")
    raise typer.Exit(0 if ok else 1)


@service_app.command("logs")
def _service_logs(
    n: int = typer.Option(100, "-n", "--lines", help="Number of recent lines."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new lines."),
) -> None:
    """Tail the gateway logs (journald on Linux, file tail on macOS/Windows)."""
    from opencomputer.service.factory import get_backend
    backend = get_backend()
    for line in backend.follow_logs(lines=n, follow=follow):
        typer.echo(line)


@service_app.command("doctor")
def _service_doctor() -> None:
    """Diagnostic health check for the service."""
    from opencomputer.service.factory import get_backend
    backend = get_backend()
    status = backend.status()
    checks: list[tuple[str, str, str]] = []  # (name, level, detail)
    checks.append(
        ("executable_resolvable", "OK", _executable_or_warn()),
    )
    checks.append(
        ("config_file_present", "OK" if status.file_present else "FAIL",
         "yes" if status.file_present else "missing — run `oc service install`"),
    )
    checks.append(
        ("service_enabled", "OK" if status.enabled else "WARN",
         "yes" if status.enabled else "not enabled"),
    )
    checks.append(
        ("service_running", "OK" if status.running else "WARN",
         f"pid={status.pid}" if status.running else "not running"),
    )
    crash_terms = ("Traceback", "panic", "FATAL")
    has_crash = any(t in line for line in status.last_log_lines for t in crash_terms)
    checks.append(
        ("recent_crashes", "WARN" if has_crash else "OK",
         "found in last 5 lines" if has_crash else "none in last 5 lines"),
    )
    for name, level, detail in checks:
        typer.echo(f"  [{level}] {name}: {detail}")


def _executable_or_warn() -> str:
    try:
        from opencomputer.service._common import resolve_executable
        return resolve_executable()
    except RuntimeError as exc:
        return f"WARN: {exc}"
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_cli_service_commands.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py tests/test_cli_service_commands.py
git commit -m "feat(cli): oc service start/stop/logs/doctor subcommands"
```

---

### Task 13b: Quick verification — WizardCtx + setup() shape

Before Tasks 14–16, verify two upstream assumptions:

**Verified upstream (2026-05-03):**
- `WizardCtx` (in `opencomputer/cli_setup/sections.py`) is a `@dataclass` with: `config: dict`, `config_path: Path`, `is_first_run: bool`, `quick_mode: bool = False`, `extra: dict`. Test setup uses `WizardCtx(config={}, config_path=tmp_path / "config.yaml", is_first_run=True)`.
- `setup()` Typer command exists at `opencomputer/cli.py:2132` with `--new` and `--non-interactive` flags. We extend it additively with `--install-daemon` and `--daemon-profile`.
- The legacy wizard entry point is `opencomputer.setup_wizard.run_setup`; the new section-driven wizard is `opencomputer.cli_setup.wizard.run_setup`. The `--install-daemon` flag should fire AFTER whichever wizard ran.

---

### Task 14: CLI — `--install-daemon` flag on `oc gateway`

**Files:**
- Modify: `opencomputer/cli.py` (the `gateway()` function around line 2016)
- Test: `tests/test_cli_install_daemon_flag.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_cli_install_daemon_flag.py
"""--install-daemon convenience flags on oc setup and oc gateway."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_gateway_install_daemon_calls_install_and_exits(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.install.return_value = MagicMock(
        backend="systemd", config_path="/tmp/x.service",
        enabled=True, started=True, notes=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["gateway", "--install-daemon"])
    assert result.exit_code == 0
    fake_backend.install.assert_called_once()
    # Should NOT have actually launched the gateway loop
    # (verified by absence of typing the gateway server up message in output)
    assert "Gateway connecting" not in result.stdout
```

- [ ] **Step 2: Run; expect FAIL**

```bash
.venv/bin/pytest tests/test_cli_install_daemon_flag.py -v
```

- [ ] **Step 3: Add `--install-daemon` to `gateway()` in `cli.py`**

Find the `gateway()` function around line 2016. Add a new typer.Option right after the function signature opens, and an early-return branch:

```python
@app.command()
def gateway(
    install_daemon: bool = typer.Option(
        False, "--install-daemon",
        help="Install OpenComputer as an always-on system service and exit "
             "(does not run the gateway in the foreground).",
    ),
    daemon_profile: str = typer.Option(
        "default", "--daemon-profile",
        help="Profile to install the daemon for (only with --install-daemon).",
    ),
) -> None:
    """Run the gateway daemon — connects all configured channel adapters."""
    if install_daemon:
        from opencomputer.service.factory import get_backend
        backend = get_backend()
        result = backend.install(profile=daemon_profile, extra_args="gateway")
        typer.echo(f"Installed {result.backend} service at {result.config_path}")
        for note in result.notes:
            typer.echo(f"note: {note}")
        raise typer.Exit(0)
    # ... existing body of gateway() unchanged below
```

- [ ] **Step 4: Run; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py tests/test_cli_install_daemon_flag.py
git commit -m "feat(cli): oc gateway --install-daemon convenience flag"
```

---

### Task 15: CLI — `--install-daemon` flag on `oc setup`

**Files:**
- Modify: `opencomputer/cli.py` (the `setup()` function — find via `grep -n "def setup\|@app.command.*setup" opencomputer/cli.py`)
- Modify: `tests/test_cli_install_daemon_flag.py`

- [ ] **Step 1: Add failing test**

```python
def test_setup_install_daemon_skips_prompt_and_installs(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.install.return_value = MagicMock(
        backend="launchd", config_path="/tmp/x.plist",
        enabled=True, started=True, notes=[],
    )
    # Legacy wizard is the default for `oc setup` (without --new)
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend), \
         patch("opencomputer.setup_wizard.run_setup") as legacy_wiz, \
         patch("opencomputer.cli_setup.wizard.run_setup") as new_wiz:
        legacy_wiz.return_value = None
        new_wiz.return_value = None
        result = runner.invoke(app, ["setup", "--install-daemon"])
    # at least one wizard variant ran AND service was installed afterwards
    assert legacy_wiz.called or new_wiz.called
    fake_backend.install.assert_called_once()
    assert result.exit_code == 0
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Extend the existing `setup()` command in `cli.py:2132`**

Add `install_daemon` and `daemon_profile` options to the existing signature (do NOT replace the existing `--new` and `--non-interactive` options). At the END of the function body (after the wizard call returns), append the install branch:

```python
# add to the existing options block:
install_daemon: bool = typer.Option(
    False, "--install-daemon",
    help="After completing the wizard, install OpenComputer as an always-on system service.",
),
daemon_profile: str = typer.Option(
    "default", "--daemon-profile",
    help="Profile to install the daemon for (only with --install-daemon).",
),

# at the end of the function, after the wizard runs:
if install_daemon:
    from opencomputer.service.factory import get_backend
    backend = get_backend()
    result = backend.install(profile=daemon_profile, extra_args="gateway")
    typer.echo(f"\nInstalled {result.backend} service at {result.config_path}")
    for note in result.notes:
        typer.echo(f"  note: {note}")
```

- [ ] **Step 4: Run; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py tests/test_cli_install_daemon_flag.py
git commit -m "feat(cli): oc setup --install-daemon convenience flag"
```

---

### Task 16: Wizard — rename `launchd_service` → `service_install` (platform-agnostic)

**Files:**
- Create: `opencomputer/cli_setup/section_handlers/service_install.py`
- Modify: `opencomputer/cli_setup/sections.py`
- Test: `tests/test_cli_setup_section_service_install.py` (renamed from `test_cli_setup_section_launchd_service.py`)

- [ ] **Step 1: Read the existing test to understand expected shape**

```bash
cat tests/test_cli_setup_section_launchd_service.py
```

- [ ] **Step 2: Create new test file (renamed copy adapted)**

`tests/test_cli_setup_section_service_install.py`:

```python
"""service_install wizard section — platform-agnostic, calls factory.get_backend()."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_service_install_section_calls_factory_install_when_user_picks_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.cli_setup.section_handlers import service_install
    from opencomputer.cli_setup.sections import SectionResult, WizardCtx

    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.supported.return_value = True
    fake_backend.install.return_value = MagicMock(
        backend="launchd", config_path="/tmp/x.plist", enabled=True, started=True, notes=[],
    )
    monkeypatch.setattr("opencomputer.cli_setup.section_handlers.service_install.radiolist",
                        lambda *a, **k: 0)
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        ctx = WizardCtx(config={}, config_path=tmp_path / "config.yaml", is_first_run=True)
        result = service_install.run_service_install_section(ctx)
    assert result == SectionResult.CONFIGURED
    fake_backend.install.assert_called_once()


def test_service_install_section_skip_returns_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.cli_setup.section_handlers import service_install
    from opencomputer.cli_setup.sections import SectionResult, WizardCtx

    monkeypatch.setattr("opencomputer.cli_setup.section_handlers.service_install.radiolist",
                        lambda *a, **k: 1)
    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.supported.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        ctx = WizardCtx(config={}, config_path=tmp_path / "config.yaml", is_first_run=True)
        result = service_install.run_service_install_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH
    fake_backend.install.assert_not_called()
```

- [ ] **Step 3: Run; expect FAIL**

- [ ] **Step 4: Implement `service_install.py`**

```python
# opencomputer/cli_setup/section_handlers/service_install.py
"""Platform-agnostic service-install wizard section.

Replaces the macOS-only launchd_service.py. Calls
``opencomputer.service.factory.get_backend()`` so the wizard works
identically on Linux, macOS, and Windows.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def run_service_install_section(ctx: WizardCtx) -> SectionResult:
    from opencomputer.service.base import ServiceUnsupportedError
    from opencomputer.service.factory import get_backend

    try:
        backend = get_backend()
    except ServiceUnsupportedError as exc:
        print(f"  ({exc} — service install skipped)")
        return SectionResult.SKIPPED_FRESH

    if not backend.supported():
        print(f"  ({backend.NAME} backend reports unsupported — skipped)")
        return SectionResult.SKIPPED_FRESH

    choices = [
        Choice("Install gateway as a system service", "install"),
        Choice("Skip — run gateway manually with `oc gateway`", "skip"),
    ]
    idx = radiolist(
        f"Install the gateway as a {backend.NAME} service? "
        "(runs in background, starts on login)",
        choices, default=0,
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    profile = ctx.config.get("active_profile", "default")
    result = backend.install(profile=profile, extra_args="gateway")
    print(f"  ✓ Installed {result.backend} service at {result.config_path}")
    for note in result.notes:
        print(f"    note: {note}")

    ctx.config.setdefault("gateway", {})
    ctx.config["gateway"]["service_installed"] = True
    ctx.config["gateway"]["service_backend"] = result.backend

    return SectionResult.CONFIGURED
```

- [ ] **Step 5: Update `sections.py` to call the renamed function**

```bash
grep -n "launchd_service\|run_launchd_service_section" opencomputer/cli_setup/sections.py
```

Replace each match in `sections.py`:
- Import: `from .section_handlers.launchd_service import run_launchd_service_section` → `from .section_handlers.service_install import run_service_install_section`
- Call site: `run_launchd_service_section(ctx)` → `run_service_install_section(ctx)`

- [ ] **Step 6: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_cli_setup_section_service_install.py -v
```

- [ ] **Step 7: Delete the renamed-but-redundant old test file**

```bash
rm tests/test_cli_setup_section_launchd_service.py
```

- [ ] **Step 8: Commit**

```bash
git add opencomputer/cli_setup/section_handlers/service_install.py opencomputer/cli_setup/sections.py tests/test_cli_setup_section_service_install.py
git rm tests/test_cli_setup_section_launchd_service.py
git commit -m "refactor(setup): platform-agnostic service_install wizard section"
```

---

### Task 17: Wizard — `launchd_service.py` becomes deprecation alias

**Files:**
- Modify: `opencomputer/cli_setup/section_handlers/launchd_service.py`
- Create: `tests/test_service_alias_deprecation.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_service_alias_deprecation.py
"""Importing the legacy launchd_service module emits DeprecationWarning."""
from __future__ import annotations

import importlib
import warnings


def test_legacy_launchd_service_emits_deprecation_warning() -> None:
    # Force a fresh import
    import sys
    sys.modules.pop("opencomputer.cli_setup.section_handlers.launchd_service", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module(
            "opencomputer.cli_setup.section_handlers.launchd_service",
        )
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        f"expected DeprecationWarning, got categories: "
        f"{[w.category.__name__ for w in caught]}"
    )


def test_legacy_run_launchd_service_section_still_callable() -> None:
    """Old function name continues to work as alias."""
    from opencomputer.cli_setup.section_handlers.launchd_service import (
        run_launchd_service_section,
    )
    from opencomputer.cli_setup.section_handlers.service_install import (
        run_service_install_section,
    )
    assert run_launchd_service_section is run_service_install_section
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Replace `launchd_service.py` with the alias module**

```python
# opencomputer/cli_setup/section_handlers/launchd_service.py
"""Legacy alias — use service_install instead.

This module re-exports the new platform-agnostic section function
under its old name to preserve backward compatibility for one release.
Removed in the next major version.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "opencomputer.cli_setup.section_handlers.launchd_service is deprecated; "
    "use opencomputer.cli_setup.section_handlers.service_install instead.",
    DeprecationWarning,
    stacklevel=2,
)

from .service_install import run_service_install_section as run_launchd_service_section

__all__ = ["run_launchd_service_section"]
```

- [ ] **Step 4: Run; expect PASS**

```bash
.venv/bin/pytest tests/test_service_alias_deprecation.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_setup/section_handlers/launchd_service.py tests/test_service_alias_deprecation.py
git commit -m "refactor(setup): launchd_service module → deprecation alias"
```

---

### Task 18: `doctor.py` — service health section

**Files:**
- Modify: `opencomputer/doctor.py`
- Test: `tests/test_doctor_service_section.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_doctor_service_section.py
"""oc doctor includes a service health section that wraps factory.get_backend()."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


def test_doctor_reports_service_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.NAME = "systemd"
    fake_backend.status.return_value = MagicMock(
        backend="systemd", file_present=True, enabled=True, running=True,
        pid=12345, uptime_seconds=None, last_log_lines=[],
    )
    runner = CliRunner()
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "service" in result.stdout.lower()
    assert "running" in result.stdout.lower()


def test_doctor_handles_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.cli import app
    from opencomputer.service.base import ServiceUnsupportedError

    runner = CliRunner()
    with patch(
        "opencomputer.service.factory.get_backend",
        side_effect=ServiceUnsupportedError("no backend for platform foo"),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "n/a" in result.stdout.lower() or "not supported" in result.stdout.lower()
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Find the doctor entry-point and add `_check_service`**

Find the doctor command in `cli.py`:

```bash
grep -n "@app.command.*doctor\|def doctor" opencomputer/cli.py
```

If `doctor` is in `opencomputer/doctor.py`, look there. Add a new check function:

```python
# opencomputer/doctor.py — append (or modify the existing entry point to call this)

def _check_service() -> tuple[str, str, str]:
    """Return (name, level, detail) for the service health row of `oc doctor`."""
    try:
        from opencomputer.service.base import ServiceUnsupportedError
        from opencomputer.service.factory import get_backend
    except ImportError as exc:
        return ("service", "FAIL", f"import error: {exc}")

    try:
        backend = get_backend()
    except ServiceUnsupportedError as exc:
        return ("service", "N/A", f"not supported on this platform ({exc})")

    s = backend.status()
    if s.running:
        return ("service", "OK", f"{backend.NAME} running (pid={s.pid})")
    if s.enabled:
        return ("service", "WARN", f"{backend.NAME} enabled but not running")
    if s.file_present:
        return ("service", "WARN", f"{backend.NAME} file present but not enabled")
    return ("service", "N/A", f"{backend.NAME} not installed")
```

Then ensure the existing `doctor` command renders this row (if doctor uses a list of (name, level, detail) tuples, just append; otherwise wire it into the existing render loop).

- [ ] **Step 4: Run; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/doctor.py tests/test_doctor_service_section.py
git commit -m "feat(doctor): service health row in oc doctor"
```

---

### Task 19: CI matrix — add macOS + Windows runners

**Files:**
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Read the current matrix**

```bash
cat .github/workflows/test.yml
```

- [ ] **Step 2: Edit the matrix**

Find the `strategy.matrix` block. Change from:

```yaml
strategy:
  matrix:
    python-version: ["3.12", "3.13"]
```

To:

```yaml
strategy:
  fail-fast: false
  matrix:
    os: [ubuntu-latest, macos-latest, windows-latest]
    python-version: ["3.12", "3.13"]
    exclude:
      # Keep the matrix manageable — only test 3.13 on macOS/Windows.
      - os: macos-latest
        python-version: "3.12"
      - os: windows-latest
        python-version: "3.12"
runs-on: ${{ matrix.os }}
```

(If `runs-on` was hard-coded to `ubuntu-latest`, replace with the matrix expression.)

- [ ] **Step 3: Verify locally with `act` if available**

```bash
which act && act -j test --dryrun || echo "(act not installed — skip dryrun)"
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: add macos-latest + windows-latest runners to test matrix"
```

The CI job runs on PR push. Validation happens in CI itself.

---

### Task 20: Runbook — `docs/runbooks/always-on-daemon.md`

**Files:**
- Create: `docs/runbooks/always-on-daemon.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Always-On Daemon Runbook

OpenComputer can run as a persistent OS-level service that the OS keeps alive
across crashes, terminal sessions, and reboots. This is what makes it possible
to power on a laptop after weeks of being off and immediately receive a
"back online" Telegram message — the agent fully reconstitutes from disk-only
state in seconds.

## The mental model

| State of laptop | Daemon status |
|---|---|
| Powered on, awake | running ✅ |
| Sleep | frozen — instantly resumes when lid opens ⏸️ |
| Shutdown | not running, but auto-starts at next boot 🔄 |

The "always running" illusion comes from short gaps and instant reconnects.

## Install per platform

### One command (all platforms)

```bash
oc setup --install-daemon          # first-run wizard + service install
# OR
oc service install                  # if already onboarded
```

### Linux (systemd-user)

Writes `~/.config/systemd/user/opencomputer.service`. Runs:

```
oc --headless --profile default gateway run
```

with `Restart=always` and `RestartSec=5`.

**Headless servers (no GUI session):** enable lingering so the service runs
across SSH disconnects and at boot before any login:

```bash
sudo loginctl enable-linger $USER
```

### macOS (launchd)

Writes `~/Library/LaunchAgents/com.opencomputer.gateway.plist`. Bootstrapped
into the user's GUI domain via `launchctl bootstrap gui/<uid>`. `KeepAlive=true`
+ `RunAtLoad=true` keep the service alive across crashes and login.

### Windows (Task Scheduler)

Registers a user-scope task `OpenComputerGateway` triggered on logon, with
`RestartOnFailure` configured. No admin elevation needed.

## Verify it's running

```bash
oc service status                   # cross-platform
oc service status --json            # machine-readable

# native commands per platform:
systemctl --user status opencomputer.service          # Linux
launchctl print gui/$(id -u)/com.opencomputer.gateway  # macOS
schtasks /query /tn OpenComputerGateway /v             # Windows
```

A healthy install shows: `enabled=True, running=True, pid=<PID>`.

## Diagnostic: `oc service doctor`

Runs 5+ checks:

- `executable_resolvable` — `oc`/`opencomputer` found on PATH or in fallbacks
- `config_file_present` — service config file exists
- `service_enabled` — OS reports it as enabled
- `service_running` — currently running
- `recent_crashes` — last 5 log lines free of Traceback/panic/FATAL

## Tail logs

```bash
oc service logs                     # last 100 lines
oc service logs -n 500              # last 500
oc service logs --follow            # stream new lines (like `tail -f`)
```

Per-platform sources:
- Linux: `journalctl --user -u opencomputer.service -f`
- macOS: tail `~/.opencomputer/<profile>/logs/gateway.{stdout,stderr}.log`
- Windows: tail `%USERPROFILE%\.opencomputer\<profile>\logs\gateway.{stdout,stderr}.log`

## Credentials persistence

Per-profile credentials live in `~/.opencomputer/<profile>/`:

```
~/.opencomputer/<profile>/
├── config.yaml                # profile config
├── credentials/               # OS keyring fallback file storage
└── logs/                      # gateway stdout/stderr (macOS, Windows)
```

These files are what survive shutdown and let the agent reconnect to Telegram /
Discord without re-pairing. Back them up if you care about identity continuity.

## Uninstall

```bash
oc service uninstall                # cross-platform
```

Removes the service config file and the OS-side registration. Does **not** remove
profile data in `~/.opencomputer/`.

## Troubleshooting

| Symptom | Try |
|---|---|
| `oc service status` reports `not enabled` after install | re-run `oc service install` (idempotent) |
| Linux: service stops when SSH disconnects | `sudo loginctl enable-linger $USER` |
| macOS: `launchctl bootstrap` returned 5 | older plist still loaded — `launchctl bootout gui/$(id -u)/com.opencomputer.gateway`, then re-install |
| Windows Defender SmartScreen flags `schtasks /create` | install via `taskschd.msc` GUI: import the rendered XML at `%USERPROFILE%\.opencomputer\opencomputer-task.xml` |
| First boot after install — no Telegram message | check `oc service logs -n 100` for adapter connection errors |

## Alternative deployment: Docker

If you prefer container-native deployment over OS-service supervision, use
`docker run --restart=always opencomputer/gateway:latest` — but be aware that
local laptop use is better served by the daemon flow above (no Docker Desktop
tax on macOS/Windows).
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/always-on-daemon.md
git commit -m "docs: always-on-daemon runbook"
```

---

### Task 21: README + CLAUDE.md updates

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Quick Start one-liner to README**

Find the Quick Start section. Add after the existing `oc setup` line:

```markdown
- `oc setup --install-daemon` — runs the wizard and registers OpenComputer as an always-on system service. See `docs/runbooks/always-on-daemon.md` for details.
```

- [ ] **Step 2: Update CLAUDE.md**

In §4 ("What's been built"), append a new row at the bottom of the phase table:

```markdown
| Always-on daemon (cross-platform) | (this PR) | `oc service install/uninstall/status/start/stop/logs/doctor` works on Linux/macOS/Windows; new launchd + schtasks backends + factory dispatch + `--install-daemon` flags on `setup` and `gateway`; wizard consolidated; runbook added |
```

In §7 ("Non-obvious gotchas"), append a new gotcha:

```markdown
N. **Two launchd modules co-exist with deliberately distinct purposes.**
   - `opencomputer/service/launchd.py` — daily profile-analyze cron (`StartCalendarInterval`)
   - `opencomputer/service/_macos_launchd.py` — always-on gateway plist (`KeepAlive`, `RunAtLoad`)

   The leading underscore marks the new module as backend-internal — called via
   `service.factory.get_backend()` Protocol dispatch, not directly. The two
   modules deliberately do not share code; their lifecycles and triggers differ.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: README + CLAUDE.md updates for cross-platform daemon"
```

---

### Task 22: Full suite + lint + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run full pytest suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass, including the existing 5800+ tests + ~30 new tests.

- [ ] **Step 2: Run ruff**

```bash
.venv/bin/ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: no lint errors. Fix any introduced.

- [ ] **Step 3: Verify import structure**

```bash
.venv/bin/python -c "from opencomputer.service.factory import get_backend; print(get_backend.__module__)"
.venv/bin/python -c "from opencomputer.service import install_systemd_unit, render_systemd_unit, is_active; print('compat shims OK')"
.venv/bin/python -c "from opencomputer.cli_setup.section_handlers.service_install import run_service_install_section; print('new section OK')"
.venv/bin/python -W error::DeprecationWarning -c "from opencomputer.cli_setup.section_handlers.launchd_service import run_launchd_service_section" 2>&1 | head -5
```

Expected: first 3 succeed cleanly; 4th prints DeprecationWarning text (or is converted to error if `-W error::DeprecationWarning`).

- [ ] **Step 4: Verify CLI surface**

```bash
.venv/bin/oc service --help
.venv/bin/oc service install --help
.venv/bin/oc service doctor --help
.venv/bin/oc gateway --help | grep install-daemon
.venv/bin/oc setup --help | grep install-daemon
```

Each should show the new commands/flags.

- [ ] **Step 5: Smoke test on Linux (this machine is macOS — gated)**

Skip on macOS development machine. CI Linux runner exercises this.

- [ ] **Step 6: Stage everything for the PR**

```bash
git status
git log --oneline main..HEAD | head -25
```

Expected: ~22 commits, all on a feature branch. Verify nothing extraneous.

---

## Self-review

After implementation completes, the executor (or reviewer) MUST:

1. **Spec coverage.** Walk through each goal in `docs/superpowers/specs/2026-05-03-always-on-daemon-design.md` §2 (Goals 1–8) and point to a task that implements it:
   - Goal 1 (cross-platform install) → Tasks 4 + 5 + 9 + 11 + 13–15
   - Goal 2 (modern launchctl bootstrap) → Task 9
   - Goal 3 (Windows schtasks user scope) → Tasks 11–12
   - Goal 4 (--install-daemon flags) → Tasks 14–15
   - Goal 5 (wizard platform-agnostic) → Tasks 16–17
   - Goal 6 (rich `oc service status`) → Task 6 + 13 (doctor)
   - Goal 7 (runbook) → Task 20
   - Goal 8 (backward compat) → Task 8 + tests in Task 22

2. **Acceptance criteria** §13 of the spec — manual check on each platform.

3. **Push:**

   ```bash
   git push -u origin feat/always-on-daemon
   gh pr create --title "feat(service): cross-platform always-on daemon (macOS launchd + Windows schtasks + unified CLI)" --body "..."
   ```
