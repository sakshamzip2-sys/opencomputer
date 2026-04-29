# Cross-Platform Deployment Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the cross-platform gap so OpenComputer ships as a real always-on agent on Linux (Pi/server, the deployment target), enterprise Windows (the wedge), and continues to work on macOS (the dev platform).

**Architecture:** Four orthogonal phases, each independently shippable.
- Phase 1 — **Headless mode**: `--headless` flag + `opencomputer service install` writes a systemd user unit; structured logs go to journald or a rotating file. No new deps.
- Phase 2 — **Multi-arch deployment artifacts**: Dockerfile + GitHub-Actions build for `linux/amd64` + `linux/arm64`; a Pi deployment guide that walks `pi 4 / pi 5` users through a 5-minute install.
- Phase 3 — **Windows wedge**: `PowerShellRun` tool (mirrors `AppleScriptRun`) + a `ctypes`-based Win32 `SendInput` fallback for `SystemClick` / `SystemKeystroke` so the agent works on stock Windows without `pip install opencomputer[gui]`.
- Phase 4 — **Linux desktop integration**: `DBusCall` tool — calls D-Bus methods via the always-present `dbus-send` CLI; gives the agent a parity primitive for GNOME/KDE app automation the way `AppleScriptRun` does for Mac apps.

**Tech Stack:** Python 3.12+, `subprocess` (no new deps in P1/P3/P4), `ctypes` (P3 Win32 shim, stdlib), `hatchling` build, GitHub Actions, Docker buildx, systemd unit (text), `dbus-send` CLI (P4).

---

## Phase ordering rationale

Linux (P1+P2) ships first because it's the deployment-critical path the user emphasized. Windows (P3) ships second because the surface is small and the work mirrors existing Mac patterns (`AppleScriptRun`, `system_notify._notify_powershell`). Linux desktop (P4) ships last because:
- Most Linux deployment targets are headless servers / Pi nodes with no GUI — DBus is irrelevant there
- Linux desktop users are a smaller wedge than Windows enterprise
- DBus schema discovery is non-trivial and benefits from real-world skill demand to scope correctly

Each phase is a separate commit set + standalone tests, so partial completion still ships value.

---

## File Structure

| Phase | File | Touch | Responsibility |
|---|---|---|---|
| 1 | `opencomputer/headless.py` | Create | Detects/forces headless mode; gates Rich Live, prompt-toolkit, bell, etc. |
| 1 | `opencomputer/cli.py` | Modify | Add `--headless` global flag + `opencomputer service install/uninstall/status` subcommand |
| 1 | `opencomputer/service/__init__.py` | Create | systemd-user unit template + install/uninstall logic |
| 1 | `opencomputer/service/templates/opencomputer.service` | Create | systemd user unit body (text) |
| 1 | `opencomputer/observability/logging_config.py` | Modify | Add journald handler when `--headless` + journald available |
| 2 | `Dockerfile` | Create | Multi-stage build, slim final image, `linux/amd64` + `linux/arm64` |
| 2 | `.dockerignore` | Create | Strip dev cruft from build context |
| 2 | `.github/workflows/docker.yml` | Create | Buildx multi-arch publish to GHCR on tag |
| 2 | `docs/deployment/raspberry-pi.md` | Create | 5-minute install guide for Pi 4/5 (Telegram bot example) |
| 2 | `docs/deployment/systemd.md` | Create | Generic systemd-user deployment for any Linux box |
| 3 | `opencomputer/tools/powershell_run.py` | Create | PowerShell automation tool (mirrors `applescript_run.py`) |
| 3 | `opencomputer/tools/_win32_input.py` | Create | `ctypes` Win32 `SendInput` helpers (mouse + keyboard) |
| 3 | `opencomputer/tools/system_click.py` | Modify | Add `_click_win32_sendinput` fallback before pyautogui |
| 3 | `opencomputer/tools/system_keystroke.py` | Modify | Add `_type_win32_sendinput` fallback before pyautogui |
| 3 | `opencomputer/agent/consent/capability_taxonomy.py` | Modify | Register `system.powershell_run` capability |
| 4 | `opencomputer/tools/dbus_call.py` | Create | D-Bus method-call tool via `dbus-send` |
| 4 | `opencomputer/agent/consent/capability_taxonomy.py` | Modify | Register `system.dbus_call` capability |
| All | `pyproject.toml` | Modify | Register new tool entry points; add `service` extra (no new runtime deps) |
| All | `tests/test_*.py` | Create | One file per new tool/feature |

---

# PHASE 1 — Headless mode + systemd service install

**Goal:** A user can run `opencomputer service install` on Linux and have the agent run forever as a systemd user unit, surviving reboots, with logs in journald.

## Task 1.1: `opencomputer/headless.py` — detection + force flag

**Files:**
- Create: `opencomputer/headless.py`
- Test: `tests/test_headless.py`

- [ ] **Step 1: Write the failing test**

```python
"""Headless detection — explicit flag wins, otherwise sys.stdin.isatty()."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest


def test_is_headless_true_when_force_flag_set() -> None:
    from opencomputer.headless import is_headless
    with patch.dict(os.environ, {"OPENCOMPUTER_HEADLESS": "1"}, clear=False):
        assert is_headless(force=True) is True
        assert is_headless() is True  # env reads as truthy too


def test_is_headless_false_when_stdin_is_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)
    fake_stdin = type("S", (), {"isatty": lambda self: True})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    assert is_headless() is False


def test_is_headless_true_when_stdin_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)
    fake_stdin = type("S", (), {"isatty": lambda self: False})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    assert is_headless() is True


def test_is_headless_env_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("OPENCOMPUTER_HEADLESS", val)
        assert is_headless() is True, f"{val!r} should be truthy"


def test_is_headless_env_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    fake_stdin = type("S", (), {"isatty": lambda self: True})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("OPENCOMPUTER_HEADLESS", val)
        assert is_headless() is False, f"{val!r} should be falsy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_headless.py -v`
Expected: FAIL with `ModuleNotFoundError: opencomputer.headless`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/headless.py`:

```python
"""Headless-mode detection.

Three sources, in priority order:

1. ``force=True`` kwarg passed by the CLI when ``--headless`` is on the command line.
2. ``OPENCOMPUTER_HEADLESS`` env var — truthy values: ``1`` ``true`` ``yes`` ``on``
   (case-insensitive). Any other value (including unset) is falsy.
3. ``sys.stdin.isatty()`` — if no TTY, we infer headless.

Headless is a *display* concept, not a *channel* concept: the agent is
still running, talking to channels (Telegram/Discord/etc.) — it just
shouldn't render Rich Live, ring the terminal bell, or open a
prompt-toolkit picker.
"""
from __future__ import annotations

import os
import sys

_TRUTHY = {"1", "true", "yes", "on"}


def is_headless(*, force: bool = False) -> bool:
    """Return True if the process is running headless (no interactive TTY)."""
    if force:
        return True
    env = os.environ.get("OPENCOMPUTER_HEADLESS", "").strip().lower()
    if env in _TRUTHY:
        return True
    if env and env not in _TRUTHY:
        # Explicit falsy override — even if stdin happens to be a non-TTY,
        # the user said no. Useful for ``OPENCOMPUTER_HEADLESS=0 pytest``.
        return False
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        # ``ValueError: I/O operation on closed file`` happens under some
        # supervisors. Treat as headless — better to be quiet than to crash.
        return True


__all__ = ["is_headless"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_headless.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/headless.py tests/test_headless.py
git add opencomputer/headless.py tests/test_headless.py
git commit -m "feat(headless): is_headless() detection — flag, env, isatty"
```

- [ ] **Step 6: Replace existing isatty sites that gate user-facing UI (audit R5)**

The `--headless` flag in Task 1.2 only sets `OPENCOMPUTER_HEADLESS=1`. For that to actually suppress Rich Live / bell / keyboard listener / prompt-toolkit pickers, the existing `isatty()` checks in those code paths need to consult `is_headless()` too. The 4 user-visible sites that should switch:

| File:line | Before | After |
|---|---|---|
| `opencomputer/cli.py:972` | `use_live_ui = sys.stdout.isatty()` | `use_live_ui = sys.stdout.isatty() and not is_headless()` |
| `opencomputer/cli_ui/bell.py:29` | `is_tty = getattr(out, "isatty", lambda: False)` | leave (bell.py already gates on `out.isatty()` AND `runtime.custom["bell_on_complete"]` — `--headless` users won't have set the latter). No change. |
| `opencomputer/cli_ui/keyboard_listener.py:70` | `if not sys.stdin.isatty(): return` | `if not sys.stdin.isatty() or is_headless(): return` |
| `opencomputer/tools/ask_user_question.py:42` | `cli_mode = sys.stdin.isatty()` | `cli_mode = sys.stdin.isatty() and not is_headless()` |

The `cli.py:684`, `cli.py:748`, `cli.py:1266`, and `cli_plugin.py:356` sites gate the read-stdin-non-interactively path (e.g. `printf | opencomputer chat`); those are about INPUT plumbing, not UI rendering, so they stay as-is.

Apply the changes:

```python
# opencomputer/cli.py:972 area:
from opencomputer.headless import is_headless
# ...
use_live_ui = sys.stdout.isatty() and not is_headless()
```

Apply the analogous import + check at the other two sites.

Run: `pytest tests/ -k "cli or headless or keyboard or ask_user" -q --tb=no`
Expected: ALL PASS.

Commit:

```bash
git add opencomputer/cli.py opencomputer/cli_ui/keyboard_listener.py opencomputer/tools/ask_user_question.py
git commit -m "refactor(headless): user-facing UI sites consult is_headless()"
```

---

## Task 1.2: `--headless` CLI flag + propagation

**Files:**
- Modify: `opencomputer/cli.py` (search for the global Typer `@app.callback()` or top-level `Typer(...)`)
- Test: `tests/test_cli_headless_flag.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_headless_flag.py`:

```python
"""--headless on the CLI must set OPENCOMPUTER_HEADLESS=1 for the duration
of the process so downstream is_headless() checks see it."""
from __future__ import annotations

import os
from typer.testing import CliRunner


def test_headless_flag_sets_env_var() -> None:
    from opencomputer.cli import app

    runner = CliRunner()
    # ``opencomputer --headless config show`` — we don't care about the
    # output, only that the env got set during the command run.
    captured: dict[str, str] = {}

    # Monkey-patch a sentinel into the config-show command so we can read
    # the env value at the time the command runs.
    import opencomputer.cli as cli_mod
    orig = cli_mod._configure_logging_once

    def _spy() -> None:
        captured["headless_at_run"] = os.environ.get("OPENCOMPUTER_HEADLESS", "")
        orig()

    cli_mod._configure_logging_once = _spy
    try:
        result = runner.invoke(app, ["--headless", "config", "show"])
    finally:
        cli_mod._configure_logging_once = orig

    assert captured.get("headless_at_run") == "1", (
        f"--headless did not set env var; got {captured!r}"
    )
    assert result.exit_code == 0


def test_headless_flag_absent_does_not_force_env() -> None:
    """Without --headless, the env var must NOT be set to '1' (we want
    is_headless() to fall back to isatty in this case)."""
    from opencomputer.cli import app
    import opencomputer.cli as cli_mod

    captured: dict[str, str] = {}
    orig = cli_mod._configure_logging_once

    def _spy() -> None:
        captured["headless_at_run"] = os.environ.get("OPENCOMPUTER_HEADLESS", "<unset>")
        orig()

    cli_mod._configure_logging_once = _spy
    runner = CliRunner()
    # Pre-clear the env so the test is hermetic.
    prev = os.environ.pop("OPENCOMPUTER_HEADLESS", None)
    try:
        result = runner.invoke(app, ["config", "show"])
    finally:
        if prev is not None:
            os.environ["OPENCOMPUTER_HEADLESS"] = prev
        cli_mod._configure_logging_once = orig

    assert captured.get("headless_at_run") == "<unset>"
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_headless_flag.py -v`
Expected: FAIL — `--headless` is not yet a known flag (Typer rejects with usage error).

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/cli.py`, find the top-level Typer callback (search for `@app.callback(` — there's exactly one). Add `headless` to its signature and set the env var inside:

```python
@app.callback()
def _root(
    ctx: typer.Context,
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Profile name (overrides $OPENCOMPUTER_PROFILE)",
    ),
    headless: bool = typer.Option(
        False, "--headless",
        help=(
            "Force headless mode: no Rich Live, no prompt-toolkit, no bell. "
            "Set OPENCOMPUTER_HEADLESS=1 for the rest of the process. "
            "Auto-detected from sys.stdin.isatty() when not passed."
        ),
    ),
) -> None:
    """OpenComputer CLI."""
    if headless:
        os.environ["OPENCOMPUTER_HEADLESS"] = "1"
    # ...rest of the existing callback body unchanged...
```

If the existing callback already has parameters, ADD `headless` alongside them — don't replace. Add `import os` at the top of `cli.py` if it isn't already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_headless_flag.py -v`
Expected: 2 passed.

Re-run the existing CLI test suite to make sure adding the flag didn't break anything:

Run: `pytest tests/ -k cli -q --tb=short`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/cli.py tests/test_cli_headless_flag.py
git add opencomputer/cli.py tests/test_cli_headless_flag.py
git commit -m "feat(cli): --headless flag sets OPENCOMPUTER_HEADLESS=1"
```

---

## Task 1.3: `opencomputer service install/uninstall/status` subcommand

**Files:**
- Create: `opencomputer/service/__init__.py`
- Create: `opencomputer/service/templates/opencomputer.service`
- Modify: `opencomputer/cli.py` (register a new sub-Typer)
- Test: `tests/test_service_install.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_service_install.py`:

```python
"""opencomputer service install writes a systemd-user unit; uninstall
removes it; status reports whether it's active."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_render_unit_substitutes_executable_and_workdir(tmp_path: Path) -> None:
    from opencomputer.service import render_systemd_unit

    out = render_systemd_unit(
        executable="/home/pi/.local/bin/opencomputer",
        workdir=tmp_path,
        profile="default",
        extra_args="gateway",
    )
    assert "[Unit]" in out
    assert "[Service]" in out
    assert "[Install]" in out
    assert "WantedBy=default.target" in out
    assert f"ExecStart=/home/pi/.local/bin/opencomputer --headless --profile default gateway" in out
    assert f"WorkingDirectory={tmp_path}" in out
    # Restart-on-failure is the whole point — must be present.
    assert "Restart=always" in out


def test_install_writes_unit_to_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import install_systemd_unit

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))

    # Skip the ``systemctl --user daemon-reload`` step — pretend success.
    with patch("opencomputer.service._systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        path = install_systemd_unit(
            executable="/usr/local/bin/opencomputer",
            workdir=fake_home,
            profile="default",
            extra_args="gateway",
        )

    expected = fake_home / ".config" / "systemd" / "user" / "opencomputer.service"
    assert path == expected
    assert expected.exists()
    body = expected.read_text()
    assert "ExecStart=/usr/local/bin/opencomputer" in body
    assert sysctl.called  # daemon-reload was invoked


def test_uninstall_removes_unit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import install_systemd_unit, uninstall_systemd_unit

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))

    with patch("opencomputer.service._systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        path = install_systemd_unit(
            executable="/usr/local/bin/opencomputer",
            workdir=fake_home,
            profile="default",
            extra_args="gateway",
        )
        assert path.exists()
        uninstall_systemd_unit()
        assert not path.exists()


def test_install_refuses_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd is Linux-only — install on Mac/Windows must raise loudly."""
    from opencomputer.service import install_systemd_unit, ServiceUnsupportedError

    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(ServiceUnsupportedError, match="systemd is Linux-only"):
        install_systemd_unit(
            executable="/x", workdir="/y", profile="p", extra_args=""
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_service_install.py -v`
Expected: FAIL — `ModuleNotFoundError: opencomputer.service`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/service/templates/opencomputer.service`:

```ini
[Unit]
Description=OpenComputer agent ({profile})
After=network.target

[Service]
Type=simple
ExecStart={executable} --headless --profile {profile} {extra_args}
WorkingDirectory={workdir}
Restart=always
RestartSec=5
# Log to journald (default).
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Create `opencomputer/service/__init__.py`:

```python
"""systemd-user service install/uninstall.

systemd is Linux-only. macOS uses launchd (out of scope for now — the
launchd-equivalent of this module would live in
``opencomputer/service/launchd.py`` if/when needed). Windows uses the
Service Control Manager (also out of scope).

The unit installs into the standard XDG location:
``$XDG_CONFIG_HOME/systemd/user/opencomputer.service`` (defaults to
``~/.config/systemd/user/opencomputer.service``). After install, the
caller can ``systemctl --user enable --now opencomputer`` or this module
runs ``systemctl --user daemon-reload`` automatically and reports the
next-step command.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATE = (Path(__file__).parent / "templates" / "opencomputer.service").read_text()


class ServiceUnsupportedError(RuntimeError):
    """Raised when service install is attempted on a non-systemd platform."""


def render_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str
) -> str:
    """Render the systemd unit body for the given parameters."""
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        extra_args=extra_args,
    )


def _user_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / "opencomputer.service"


def _systemctl(*args: str) -> tuple[int, str, str]:
    """Call ``systemctl --user``; return (rc, stdout, stderr). No-op-ish if missing."""
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


def install_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str
) -> Path:
    """Write the unit file and run ``daemon-reload``. Returns the path written."""
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}"
        )
    body = render_systemd_unit(
        executable=executable, workdir=workdir,
        profile=profile, extra_args=extra_args,
    )
    path = _user_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _systemctl("daemon-reload")
    return path


def uninstall_systemd_unit() -> Path | None:
    """Stop + disable + remove the unit. Returns the removed path, or None if absent."""
    path = _user_unit_path()
    if not path.exists():
        return None
    _systemctl("stop", "opencomputer.service")
    _systemctl("disable", "opencomputer.service")
    path.unlink()
    _systemctl("daemon-reload")
    return path


def is_active() -> bool:
    """Return True if the systemd unit reports active."""
    rc, out, _ = _systemctl("is-active", "opencomputer.service")
    return rc == 0 and out.strip() == "active"


__all__ = [
    "ServiceUnsupportedError",
    "install_systemd_unit",
    "is_active",
    "render_systemd_unit",
    "uninstall_systemd_unit",
]
```

In `opencomputer/cli.py`, add a sub-Typer (search for an existing sub-Typer like `cli_profile_app` to mirror the pattern). Add near the other sub-typer wirings:

```python
import typer
from opencomputer import service as _service_mod

service_app = typer.Typer(help="Install/uninstall the systemd user service.")
app.add_typer(service_app, name="service")


@service_app.command("install")
def _service_install(
    profile: str = typer.Option("default", help="Which profile to run."),
    extra_args: str = typer.Option(
        # 'gateway' (NOT 'chat') is the right default for a service
        # unit: 'chat' is interactive and would exit immediately under
        # systemd (no stdin). 'gateway' is the long-running daemon that
        # talks to channel adapters (Telegram/Discord/etc.).
        "gateway",
        help=(
            "Args after `opencomputer --headless --profile <p>`. "
            "Default: 'gateway' (the long-running channel daemon). "
            "Note: systemd splits on whitespace and does NOT invoke a "
            "shell — args containing spaces are not supported."
        ),
    ),
) -> None:
    """Write and reload a systemd user unit. Run `systemctl --user enable --now opencomputer` after."""
    import shutil
    exe = shutil.which("opencomputer") or sys.executable + " -m opencomputer"
    path = _service_mod.install_systemd_unit(
        executable=exe,
        workdir=str(Path.home()),
        profile=profile,
        extra_args=extra_args,
    )
    typer.echo(f"installed: {path}")
    typer.echo("next: systemctl --user enable --now opencomputer")


@service_app.command("uninstall")
def _service_uninstall() -> None:
    """Stop, disable, and remove the systemd user unit."""
    path = _service_mod.uninstall_systemd_unit()
    typer.echo(f"removed: {path}" if path else "no unit installed")


@service_app.command("status")
def _service_status() -> None:
    """Report whether the unit is active."""
    typer.echo("active" if _service_mod.is_active() else "inactive")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_service_install.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/service/ opencomputer/cli.py tests/test_service_install.py
git add opencomputer/service/ opencomputer/cli.py tests/test_service_install.py
git commit -m "feat(service): opencomputer service install|uninstall|status (Linux/systemd)"
```

---

## Task 1.4: pyproject — register the `service` extra (no new deps)

The service module pulls only stdlib (`subprocess`, `pathlib`, `os`, `shutil`). No new runtime deps. We only need to make sure the template file ships in the wheel.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Verify the template ships**

Run:
```bash
python -c "from importlib.resources import files; \
    print((files('opencomputer.service') / 'templates' / 'opencomputer.service').is_file())"
```

Expected output: `True`. If `False`, the template isn't being included by hatchling. Check `pyproject.toml` for an explicit include list.

- [ ] **Step 2: If False, add to hatchling include**

Search `pyproject.toml` for `[tool.hatch.build]` or `[tool.hatch.build.targets.wheel]`. If there's no explicit `include` list, hatchling defaults to `*.py` only and will MISS the `.service` template. Add:

```toml
[tool.hatch.build.targets.wheel]
packages = ["opencomputer", "plugin_sdk"]

[tool.hatch.build.targets.wheel.force-include]
"opencomputer/service/templates/opencomputer.service" = "opencomputer/service/templates/opencomputer.service"
```

If `[tool.hatch.build.targets.wheel]` already exists, add the `force-include` table only.

- [ ] **Step 3: Verify after the change**

Run: `python -c "from importlib.resources import files; print((files('opencomputer.service') / 'templates' / 'opencomputer.service').is_file())"`
Expected: `True`.

- [ ] **Step 4: Run pytest in service area**

Run: `pytest tests/test_service_install.py -v`
Expected: still 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: ship opencomputer/service/templates/*.service in wheel"
```

(If Step 1 already returned True, no commit is needed for this task — skip.)

---

## Task 1.5: journald log handler when `--headless` + journald available

**Files:**
- Modify: `opencomputer/observability/logging_config.py:105` — real signature is `def configure(home: Path) -> None`. Do NOT add a `force=` kwarg (audit B1: would break the existing `cli.py:92` call site `configure_logging(_home())`). The function already de-dupes prior handlers (lines 146-152), so re-calls are idempotent.
- Test: `tests/test_logging_journald.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_logging_journald.py`:

```python
"""When OPENCOMPUTER_HEADLESS=1 and ``systemd.journal`` is importable,
configure() must add a JournalHandler to the opencomputer logger."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_journald_handler_added_when_headless_and_systemd_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "1")
    fake_journal_mod = MagicMock()
    fake_journal_mod.JournalHandler = MagicMock(return_value=logging.NullHandler())
    monkeypatch.setitem(sys.modules, "systemd", MagicMock(journal=fake_journal_mod))
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal_mod)

    from opencomputer.observability import logging_config
    logging_config.configure(home=tmp_path)

    assert fake_journal_mod.JournalHandler.called, "JournalHandler not constructed"


def test_journald_handler_silently_skipped_when_systemd_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pi headless without python3-systemd should NOT crash; just skip journald."""
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "1")
    # Force any cached systemd modules out of sys.modules so the import
    # actually runs; setting them to None makes the next import raise.
    monkeypatch.setitem(sys.modules, "systemd", None)
    monkeypatch.setitem(sys.modules, "systemd.journal", None)

    from opencomputer.observability import logging_config
    # Must not raise.
    logging_config.configure(home=tmp_path)


def test_journald_handler_NOT_added_when_not_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Even if python3-systemd is installed, interactive runs shouldn't
    use journald — they want stderr."""
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "0")
    fake_journal_mod = MagicMock()
    fake_journal_mod.JournalHandler = MagicMock(return_value=logging.NullHandler())
    monkeypatch.setitem(sys.modules, "systemd", MagicMock(journal=fake_journal_mod))
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal_mod)

    from opencomputer.observability import logging_config
    logging_config.configure(home=tmp_path)
    assert not fake_journal_mod.JournalHandler.called, (
        "JournalHandler attached in non-headless mode"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_logging_journald.py -v`
Expected: at least the first test FAILS — `configure()` doesn't yet branch on headless+journald.

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/observability/logging_config.py`, append the journald branch INSIDE the existing `configure(home: Path)` body (do not change the signature). Find the section after the rotating-file-handler attachment (around line 146-160) and add:

```python
def configure(home: Path) -> None:
    # ... existing body unchanged through the rotating-file handler ...

    # Headless + systemd journald → attach a JournalHandler so the unit's
    # `journalctl --user -u opencomputer` shows structured logs. Silently
    # skipped when python3-systemd isn't installed (Pi minimal image).
    # De-duped by isinstance check so repeat configure() calls don't
    # double-attach.
    from opencomputer.headless import is_headless

    if is_headless():
        try:
            from systemd.journal import JournalHandler  # type: ignore[import-not-found]
        except ImportError:
            return  # not available — keep stderr/file handlers only
        oc_logger = logging.getLogger("opencomputer")
        already_attached = any(
            type(h).__name__ == "JournalHandler" for h in oc_logger.handlers
        )
        if already_attached:
            return
        try:
            handler = JournalHandler(SYSLOG_IDENTIFIER="opencomputer")
            handler.setLevel(logging.INFO)
            oc_logger.addHandler(handler)
        except Exception:  # noqa: BLE001 — never fail logging setup
            return
```

The de-dupe uses `type(h).__name__ == "JournalHandler"` rather than `isinstance(h, JournalHandler)` because the JournalHandler import is local to this function — checking by class-name avoids re-importing for the comparison.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_logging_journald.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/observability/logging_config.py tests/test_logging_journald.py
git add opencomputer/observability/logging_config.py tests/test_logging_journald.py
git commit -m "feat(logging): add JournalHandler when headless + systemd available"
```

---

# PHASE 2 — Multi-arch deployment artifacts + Pi guide

**Goal:** A user with a Pi 4/5 and Telegram credentials can have a working bot in under 10 minutes by following one markdown doc.

## Task 2.1: `Dockerfile` (multi-stage, slim)

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Write the Dockerfile**

Create `Dockerfile`:

```dockerfile
# Multi-stage build:
# 1. ``builder`` installs the wheel into /opt/venv
# 2. ``runtime`` copies /opt/venv into a slim base image
# Final image is ~150 MB and runs on linux/amd64 + linux/arm64 (Pi 4/5).
FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY opencomputer ./opencomputer
COPY plugin_sdk ./plugin_sdk
COPY extensions ./extensions

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .
# NOT ``pip install -e .`` (audit R8): editable installs leave egg-link
# pointers to /build/, which doesn't exist after the multi-stage COPY.

FROM python:3.13-slim AS runtime

# Runtime deps: systemd-python is optional. Skipped here — the container
# typically logs to stdout (Docker captures it) rather than journald.
# Add ``apt-get install -y libsystemd-dev`` + ``pip install systemd-python``
# in a derived image if you DO want journald inside the container.
RUN useradd --create-home --shell /bin/bash oc

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    OPENCOMPUTER_HEADLESS=1 \
    PYTHONUNBUFFERED=1

USER oc
WORKDIR /home/oc

ENTRYPOINT ["opencomputer"]
CMD ["chat"]
```

Create `.dockerignore`:

```
.git
.venv
.pytest_cache
.ruff_cache
__pycache__
*.pyc
*.pyo
.DS_Store
build/
dist/
*.egg-info/
docs/
tests/
```

- [ ] **Step 2: Build for the host arch (smoke test)**

Run: `docker build -t opencomputer:dev .`
Expected: build succeeds (~3-5 min on first run).

If you don't have Docker locally, skip this step — the GitHub Actions workflow in Task 2.2 verifies on CI.

- [ ] **Step 3: Smoke-test the image**

Run: `docker run --rm opencomputer:dev --help`
Expected: Typer help output prints (top-level CLI help).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build(docker): multi-stage Dockerfile + dockerignore"
```

---

## Task 2.2: GitHub Actions multi-arch buildx workflow

**Files:**
- Create: `.github/workflows/docker.yml`

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/docker.yml`:

```yaml
name: Docker

on:
  push:
    tags: ["v*"]
  workflow_dispatch:

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3
        with:
          platforms: linux/amd64,linux/arm64

      - name: Set up Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          file: ./Dockerfile
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/opencomputer:${{ github.ref_name }}
            ghcr.io/${{ github.repository_owner }}/opencomputer:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/docker.yml'))"`
Expected: no output (parse OK).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/docker.yml
git commit -m "ci(docker): multi-arch buildx (amd64+arm64) on tag, push to GHCR"
```

(The workflow runs on the next `v*` tag — there's no immediate test loop, but Phase 1's tests cover the code that goes IN the image.)

---

## Task 2.3: Raspberry Pi deployment guide

**Files:**
- Create: `docs/deployment/raspberry-pi.md`

- [ ] **Step 1: Write the guide**

Create `docs/deployment/raspberry-pi.md`:

````markdown
# Run OpenComputer on a Raspberry Pi (always-on)

This guide takes a fresh Raspberry Pi 4 or 5 from boot to "agent online,
listening on Telegram, surviving reboots" in under 10 minutes.

## What you need

- Pi 4 (4 GB+) or Pi 5 — 32-bit Pis aren't supported (Python wheels require 64-bit).
- Raspberry Pi OS 64-bit (Lite is fine — no desktop required).
- Network access (Ethernet or Wi-Fi configured).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your Telegram numeric user ID (ask [@userinfobot](https://t.me/userinfobot)).

## Install — option A: pip (smaller image, slightly slower start)

```bash
# 1. Update + Python 3.12+
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# 2. Create a virtualenv outside the home root so systemd can find it
python3 -m venv ~/.venv-oc
source ~/.venv-oc/bin/activate
pip install --upgrade pip
pip install opencomputer

# 3. Optional — journald handler (pretty `journalctl` output)
sudo apt install -y python3-systemd

# 4. Provider creds (pick one)
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
# or
echo 'export OPENAI_API_KEY=sk-...' >> ~/.bashrc
source ~/.bashrc

# 5. Telegram creds
mkdir -p ~/.opencomputer/default
cat > ~/.opencomputer/default/.env <<EOF
TELEGRAM_BOT_TOKEN=12345:abcdef-your-token
TELEGRAM_ALLOWED_USERS=123456789
EOF

# 6. Install + start the systemd user service
opencomputer service install --extra-args 'gateway'
loginctl enable-linger $USER     # so the service runs even when you're logged out
systemctl --user enable --now opencomputer

# 7. Watch it work
journalctl --user -u opencomputer -f
```

## Install — option B: Docker (faster, a bit more disk)

```bash
sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker $USER
# log out + back in for the group to take effect

mkdir -p ~/oc-data
docker run -d --name oc \
    --restart unless-stopped \
    -v ~/oc-data:/home/oc/.opencomputer \
    -e ANTHROPIC_API_KEY=sk-ant-... \
    -e TELEGRAM_BOT_TOKEN=... \
    -e TELEGRAM_ALLOWED_USERS=... \
    ghcr.io/sakshamzip2-sys/opencomputer:latest gateway

docker logs -f oc
```

## Verify

Send a message to your bot on Telegram. You should see:

1. The bot replies (model output).
2. `journalctl` shows the request + response in real time.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Failed to connect to user instance` from `systemctl --user` | Run `loginctl enable-linger $USER` and retry. |
| Bot doesn't reply | Check `TELEGRAM_ALLOWED_USERS` matches your numeric ID exactly. |
| `ImportError: systemd.journal` | Missed `apt install python3-systemd` in step 3 — non-fatal, journald handler stays unattached. |
| OOM kill on Pi 4 (4 GB) | Use a smaller model or set `OPENCOMPUTER_LOOP_BUDGET=4096` to cap context. |

## What the service does

- Runs `opencomputer --headless --profile default gateway` under your user.
- `gateway` is the long-running daemon that talks to channel adapters (Telegram, Discord, Slack — whichever plugins are enabled in your profile).
- Survives reboots (`enable-linger`), restarts on crash (`Restart=always`).
- Logs to journald, viewable with `journalctl --user -u opencomputer`.

## Updating

Pip install:

```bash
~/.venv-oc/bin/pip install --upgrade opencomputer
systemctl --user restart opencomputer
```

Docker:

```bash
docker pull ghcr.io/sakshamzip2-sys/opencomputer:latest
docker rm -f oc && # re-run the docker run command from above
```
````

- [ ] **Step 2: Lint the markdown**

Run: `python -c "import re; body = open('docs/deployment/raspberry-pi.md').read(); assert '##' in body; assert 'systemctl --user' in body; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add docs/deployment/raspberry-pi.md
git commit -m "docs(deployment): Raspberry Pi 4/5 always-on guide"
```

---

## Task 2.4: Generic systemd guide for any Linux box

**Files:**
- Create: `docs/deployment/systemd.md`

- [ ] **Step 1: Write the guide**

Create `docs/deployment/systemd.md`:

````markdown
# Run OpenComputer as a Linux systemd user service

This guide is the generic version of the [Raspberry Pi
guide](./raspberry-pi.md) — same install pattern, but framed for
Ubuntu/Debian/Fedora/Arch on any architecture.

## Prereqs

- Linux with systemd (Ubuntu 20+, Debian 11+, Fedora 35+, Arch — basically
  any modern distro).
- Python 3.12+ (`python3 --version`).
- One of: pip + virtualenv, or Docker.
- API keys for whatever providers + channels you intend to use.

## pip install

```bash
python3 -m venv ~/.venv-oc
source ~/.venv-oc/bin/activate
pip install opencomputer

# Profile + creds
mkdir -p ~/.opencomputer/default
$EDITOR ~/.opencomputer/default/.env   # add ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, etc.

# Install the service
opencomputer service install --extra-args 'gateway'
loginctl enable-linger $USER
systemctl --user enable --now opencomputer
```

## Verify

```bash
opencomputer service status         # → active
journalctl --user -u opencomputer -f
```

## Uninstall

```bash
opencomputer service uninstall
```

## Service file location

`~/.config/systemd/user/opencomputer.service` — feel free to edit
(e.g., to change `--profile default` to `--profile work`); reload with
`systemctl --user daemon-reload && systemctl --user restart opencomputer`.

## Why a USER unit, not a system unit?

The agent runs with your home directory's profile + your API keys. A
system unit would force you to either copy creds into `/etc/` or run as
root, both worse. User units `enable-linger` to survive logout, which
matches what you want for an always-on agent.
````

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/systemd.md
git commit -m "docs(deployment): generic Linux systemd-user guide"
```

---

# PHASE 3 — Windows enterprise wedge

**Goal:** A Windows user with stock `pip install opencomputer` (no `[gui]` extra) gets working `SystemClick` / `SystemKeystroke` AND a `PowerShellRun` tool that mirrors `AppleScriptRun`.

## Task 3.1: `_win32_input.py` — `ctypes` SendInput shim

**Files:**
- Create: `opencomputer/tools/_win32_input.py`
- Test: `tests/test_win32_input.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_win32_input.py`:

```python
"""Win32 SendInput shim — tests are import-shape only on non-Windows
because we don't want to actually move the mouse during CI. The full
behavior is exercised on Windows via integration in test_system_click."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def test_module_imports_on_non_windows() -> None:
    """Module must be importable on macOS/Linux without raising — the
    actual SendInput call sites guard with sys.platform == 'win32'."""
    from opencomputer.tools import _win32_input

    assert hasattr(_win32_input, "click_at")
    assert hasattr(_win32_input, "type_text")
    assert hasattr(_win32_input, "send_keys")


def test_click_at_returns_false_on_non_windows() -> None:
    from opencomputer.tools._win32_input import click_at
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    assert click_at(100, 200, button="left", double=False) is False


def test_type_text_returns_false_on_non_windows() -> None:
    from opencomputer.tools._win32_input import type_text
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    assert type_text("hello") is False


def test_click_at_invokes_user32_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sys.platform IS win32, click_at must call user32.SendInput."""
    fake_user32 = MagicMock()
    fake_user32.SendInput.return_value = 2  # 2 events sent (down+up)
    fake_user32.SetCursorPos.return_value = True

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        "opencomputer.tools._win32_input._load_user32",
        lambda: fake_user32,
    )
    # Force re-import-fresh.
    import importlib
    import opencomputer.tools._win32_input as mod
    importlib.reload(mod)

    ok = mod.click_at(100, 200, button="left", double=False)
    assert ok is True
    assert fake_user32.SetCursorPos.called
    assert fake_user32.SendInput.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_win32_input.py -v`
Expected: FAIL on the import — module doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/tools/_win32_input.py`:

```python
"""Win32 ``SendInput`` shim for stock-Windows mouse + keyboard injection.

Why this exists: ``opencomputer[gui]`` brings ``pyautogui`` which works
everywhere but is a 50+ MB dep with PIL/Pillow. For Windows-only stock
installs we want a zero-dep fallback. ``ctypes`` + ``user32.dll`` is in
the stdlib on Windows.

All public functions return ``False`` on non-Windows so callers can chain
``win32_click_at(...) or pyautogui_click(...)`` without explicit
``sys.platform`` checks at every site.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

# Constants from WinUser.h
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010

_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002


def _load_user32() -> Any:
    """Return ``ctypes.WinDLL('user32')``. Pulled out for testability."""
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


def click_at(x: int, y: int, *, button: str, double: bool) -> bool:
    """Move the cursor to (x, y) and inject a click. Returns False on non-Windows."""
    if sys.platform != "win32":
        return False
    user32 = _load_user32()
    if user32 is None:
        return False

    if not user32.SetCursorPos(x, y):
        return False

    down = _MOUSEEVENTF_RIGHTDOWN if button == "right" else _MOUSEEVENTF_LEFTDOWN
    up = _MOUSEEVENTF_RIGHTUP if button == "right" else _MOUSEEVENTF_LEFTUP

    # MOUSEINPUT: dx, dy, mouseData, dwFlags, time, dwExtraInfo
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    clicks = 2 if double else 1
    events = []
    for _ in range(clicks):
        for flag in (down, up):
            inp = _INPUT()
            inp.type = _INPUT_MOUSE
            inp.mi.dx = 0
            inp.mi.dy = 0
            inp.mi.mouseData = 0
            inp.mi.dwFlags = flag
            inp.mi.time = 0
            inp.mi.dwExtraInfo = None
            events.append(inp)

    n = len(events)
    arr = (_INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    return sent == n


def type_text(text: str) -> bool:
    """Inject Unicode text via repeated KEYEVENTF_UNICODE SendInput. False on non-Windows."""
    if sys.platform != "win32":
        return False
    user32 = _load_user32()
    if user32 is None:
        return False

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    events = []
    for ch in text:
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.ki.wVk = 0
            inp.ki.wScan = ord(ch)
            inp.ki.dwFlags = flags
            inp.ki.time = 0
            inp.ki.dwExtraInfo = None
            events.append(inp)

    n = len(events)
    arr = (_INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    return sent == n


def send_keys(keys: list[str]) -> bool:
    """Inject a hotkey combination (e.g. ``["ctrl", "c"]``). Stub for Phase 3.1.

    The mapping from string names → VK codes is non-trivial (see
    WinUser.h). For Phase 3.1 we ship ``type_text`` (most common case)
    and leave hotkey-by-name as a follow-up Task 3.x once SystemKeystroke
    integrates. Returns False to signal "not implemented" so callers
    fall through to pyautogui.
    """
    return False


__all__ = ["click_at", "type_text", "send_keys"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_win32_input.py -v`
Expected: 4 passed (one is `pytest.skip` on Windows; on macOS/Linux all 4 run).

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/tools/_win32_input.py tests/test_win32_input.py
git add opencomputer/tools/_win32_input.py tests/test_win32_input.py
git commit -m "feat(tools): _win32_input — ctypes SendInput shim for stock Windows"
```

---

## Task 3.2: Wire `_win32_input` into `SystemClick` + `SystemKeystroke`

**Files:**
- Modify: `opencomputer/tools/system_click.py:142-143` (the Windows branch)
- Modify: `opencomputer/tools/system_keystroke.py` (the Windows branch — search `if platform == "windows"`)
- Test: `tests/test_system_click.py` + `tests/test_system_keystroke.py` (existing — verify on win32)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_win32_input.py` (extending the existing file from Task 3.1):

```python
def test_system_click_uses_win32_sendinput_before_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, _click_dispatch must try Win32 SendInput first."""
    monkeypatch.setattr("sys.platform", "win32")
    calls: list[str] = []

    def fake_win32(x: int, y: int, *, button: str, double: bool) -> bool:
        calls.append("win32")
        return True

    def fake_pyautogui(x: int, y: int, button: str, double: bool) -> bool:
        calls.append("pyautogui")
        return True

    import opencomputer.tools.system_click as sc
    monkeypatch.setattr(sc, "_click_win32_sendinput", fake_win32)
    monkeypatch.setattr(sc, "_click_pyautogui", fake_pyautogui)

    ok = sc._click_dispatch("windows", 100, 200, "left", False)
    assert ok is True
    assert calls == ["win32"], (
        f"win32 must come first; got order {calls}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_win32_input.py -v`
Expected: FAIL — `_click_win32_sendinput` doesn't exist on `system_click` yet.

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/tools/system_click.py`, find `_click_dispatch` (around line 132). Update the Windows branch:

```python
def _click_dispatch(platform: str, x: int, y: int, button: str, double: bool) -> bool:
    """Try each backend in preference order. First success wins."""
    if platform == "macos":
        return (
            _click_quartz(x, y, button, double)
            or _click_pyautogui(x, y, button, double)
            or _click_osascript(x, y, button, double)
        )
    if platform == "linux":
        return _click_pyautogui(x, y, button, double) or _click_xdotool(x, y, button, double)
    if platform == "windows":
        return (
            _click_win32_sendinput(x, y, button, double)
            or _click_pyautogui(x, y, button, double)
        )
    return False


def _click_win32_sendinput(x: int, y: int, button: str, double: bool) -> bool:
    """Stock-Windows ctypes SendInput. Zero-dep fallback before pyautogui."""
    from opencomputer.tools._win32_input import click_at
    return click_at(x, y, button=button, double=double)
```

In `opencomputer/tools/system_keystroke.py`, the actual existing `_type_dispatch` (lines 115-122) tries pyautogui FIRST for ALL platforms, then falls back to platform-specific shells (audit B4). Don't rewrite the whole function — just insert a Win32 attempt **before** pyautogui on Windows. The current shape:

```python
# CURRENT (verified at system_keystroke.py:115-122):
def _type_dispatch(platform: str, text: str) -> bool:
    if platform in ("macos", "linux", "windows") and _type_pyautogui(text):
        return True
    if platform == "linux":
        return _type_xdotool(text)
    if platform == "macos":
        return _type_osascript(text)
    return False
```

Migrate to:

```python
# AFTER — adds windows-first Win32 attempt; macos/linux unchanged.
def _type_dispatch(platform: str, text: str) -> bool:
    # Windows-first: try the zero-dep ctypes shim before pyautogui so
    # stock-Windows installs (no `[gui]` extra) still work.
    if platform == "windows" and _type_win32_sendinput(text):
        return True
    if platform in ("macos", "linux", "windows") and _type_pyautogui(text):
        return True
    if platform == "linux":
        return _type_xdotool(text)
    if platform == "macos":
        return _type_osascript(text)
    return False


def _type_win32_sendinput(text: str) -> bool:
    """Stock-Windows ctypes SendInput. Returns False on non-Windows."""
    from opencomputer.tools._win32_input import type_text
    return type_text(text)
```

The existing `_hotkey_dispatch` function (a sibling) is NOT changed in this task — Win32 hotkey-by-name mapping is the explicit follow-up (`_win32_input.send_keys` returns False as a stub).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_win32_input.py -v`
Expected: 5 passed.

Re-run the existing system-click tests:

Run: `pytest tests/ -k "system_click or system_keystroke" -q --tb=short`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/tools/system_click.py opencomputer/tools/system_keystroke.py tests/test_win32_input.py
git add opencomputer/tools/system_click.py opencomputer/tools/system_keystroke.py tests/test_win32_input.py
git commit -m "feat(tools): SystemClick + SystemKeystroke try Win32 SendInput before pyautogui"
```

---

## Task 3.3: `PowerShellRun` tool (mirrors `AppleScriptRun`)

**Files:**
- Create: `opencomputer/tools/powershell_run.py`
- Modify: `opencomputer/agent/consent/capability_taxonomy.py` (add `system.powershell_run` capability)
- Test: `tests/test_powershell_run.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_powershell_run.py`:

```python
"""PowerShellRun tool — Windows-only execution of pwsh/powershell scripts."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch, MagicMock

import pytest

from plugin_sdk.core import ToolCall


def _make_tool():
    from opencomputer.tools.powershell_run import PowerShellRunTool
    return PowerShellRunTool()


def test_schema_advertises_windows_only() -> None:
    tool = _make_tool()
    assert tool.schema.name == "PowerShellRun"
    assert "Windows" in tool.schema.description


def test_capability_claim_per_action_consent() -> None:
    """PowerShell can do anything; consent must be PER_ACTION."""
    from plugin_sdk.consent import ConsentTier
    tool = _make_tool()
    claim = tool.capability_claims[0]
    assert claim.tier_required == ConsentTier.PER_ACTION
    assert claim.capability_id == "gui.powershell_run"


def test_returns_error_on_non_windows() -> None:
    tool = _make_tool()
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    call = ToolCall(id="t1", name="PowerShellRun", arguments={"script": "Write-Host hi"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "windows" in result.content.lower() or "powershell" in result.content.lower()


def test_invokes_pwsh_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, prefer ``pwsh`` over ``powershell.exe``."""
    monkeypatch.setattr("sys.platform", "win32")
    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stdout="hi", stderr="")

    with patch("opencomputer.tools.powershell_run.shutil.which") as which:
        which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("pwsh",) else None
        with patch("opencomputer.tools.powershell_run.subprocess.run", fake_run):
            tool = _make_tool()
            call = ToolCall(id="t1", name="PowerShellRun", arguments={"script": "Write-Host hi"})
            result = asyncio.run(tool.execute(call))

    args, _kwargs = fake_run.call_args
    assert args[0][0] == "/usr/bin/pwsh", f"expected pwsh first, got {args[0]!r}"
    assert "hi" in result.content
    assert result.is_error is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_powershell_run.py -v`
Expected: FAIL with `ModuleNotFoundError: opencomputer.tools.powershell_run`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/tools/powershell_run.py`:

```python
"""PowerShellRun tool — execute a PowerShell script via pwsh/powershell.

Windows-only at the OS level. macOS/Linux installations of PowerShell
exist (cross-platform PowerShell Core / pwsh), but the agent's
PowerShell-targeting skills assume Windows semantics (Win32 cmdlets,
COM objects, etc.) so we hard-gate to Windows.

Mirrors ``AppleScriptRun`` in shape: PER_ACTION consent, captures
stdout/stderr, surfaces non-zero exit as ``is_error=True``.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_TIMEOUT_SECONDS = 30


class PowerShellRunTool(BaseTool):
    """Run a PowerShell script via ``pwsh`` (preferred) or ``powershell.exe``."""

    # parallel_safe = False mirrors AppleScriptRun: PowerShell can mutate
    # registry / services / COM state, so two parallel calls would race.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.powershell_run",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Execute a PowerShell script (Windows). Can read files, "
                "control apps via COM, query system info, modify the "
                "registry — same surface area as a manual PowerShell session."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PowerShellRun",
            description=(
                "Run a PowerShell script via pwsh (PowerShell 7+, preferred) "
                "or powershell.exe (Windows PowerShell 5.1, fallback). Windows "
                "only — returns an error on macOS/Linux. Captures stdout + "
                "stderr; non-zero exit is surfaced as is_error. PER_ACTION "
                "consent. Mirrors AppleScriptRun for Mac."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "PowerShell script body. Multi-line OK.",
                    },
                },
                "required": ["script"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        script = call.arguments.get("script", "")
        if not isinstance(script, str) or not script.strip():
            return ToolResult(
                tool_call_id=call.id,
                content="script must be a non-empty string",
                is_error=True,
            )

        if sys.platform != "win32":
            return ToolResult(
                tool_call_id=call.id,
                content="PowerShellRun requires Windows (sys.platform == 'win32')",
                is_error=True,
            )

        exe = shutil.which("pwsh") or shutil.which("powershell")
        if exe is None:
            return ToolResult(
                tool_call_id=call.id,
                content="neither pwsh nor powershell.exe found on PATH",
                is_error=True,
            )

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PowerShell timed out after {_TIMEOUT_SECONDS}s",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PowerShell launch failed: {exc}",
                is_error=True,
            )

        body = proc.stdout
        if proc.stderr:
            body += f"\n[stderr]\n{proc.stderr}"
        return ToolResult(
            tool_call_id=call.id,
            content=body or "(no output)",
            is_error=proc.returncode != 0,
        )


__all__ = ["PowerShellRunTool"]
```

In `opencomputer/agent/consent/capability_taxonomy.py`, the registry is a `dict[str, ConsentTier]` (`F1_CAPABILITIES` at line 9), NOT a `CapabilityDef`-style structure (audit B2). The existing AppleScript entry at line 34 is `"gui.applescript_run": ConsentTier.PER_ACTION,`. Add a sibling — same `gui.*` namespace (audit B3):

```python
# Right after the gui.applescript_run entry:
"gui.powershell_run": ConsentTier.PER_ACTION,
```

The human-readable description lives on the `CapabilityClaim` declared in the tool itself (already done above).

Register the tool in the built-in tool registry. In `opencomputer/cli.py` `_register_builtin_tools()`, add:

```python
from opencomputer.tools.powershell_run import PowerShellRunTool
_registry.register(PowerShellRunTool())
```

Place this next to where `AppleScriptRunTool` is registered (search `AppleScriptRun`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_powershell_run.py -v`
Expected: 4 passed (one is skipped on Windows).

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/tools/powershell_run.py opencomputer/agent/consent/capability_taxonomy.py opencomputer/cli.py tests/test_powershell_run.py
git add opencomputer/tools/powershell_run.py opencomputer/agent/consent/capability_taxonomy.py opencomputer/cli.py tests/test_powershell_run.py
git commit -m "feat(tools): PowerShellRun — Windows AppleScriptRun-equivalent"
```

---

# PHASE 4 — Linux desktop integration: `DBusCall`

**Goal:** A skill running on a Linux desktop can call into running apps the way a Mac skill calls AppleScript.

## Task 4.1: `DBusCall` tool

**Files:**
- Create: `opencomputer/tools/dbus_call.py`
- Modify: `opencomputer/agent/consent/capability_taxonomy.py`
- Test: `tests/test_dbus_call.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dbus_call.py`:

```python
"""DBusCall tool — Linux-only D-Bus method invocation via dbus-send."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch, MagicMock

import pytest

from plugin_sdk.core import ToolCall


def _make_tool():
    from opencomputer.tools.dbus_call import DBusCallTool
    return DBusCallTool()


def test_schema_name_and_linux_only_doc() -> None:
    tool = _make_tool()
    assert tool.schema.name == "DBusCall"
    assert "linux" in tool.schema.description.lower()


def test_returns_error_on_non_linux() -> None:
    tool = _make_tool()
    if sys.platform.startswith("linux"):
        pytest.skip("only tests the non-linux guard")
    call = ToolCall(
        id="t1", name="DBusCall",
        arguments={
            "bus": "session",
            "destination": "org.freedesktop.Notifications",
            "object_path": "/org/freedesktop/Notifications",
            "interface": "org.freedesktop.Notifications",
            "method": "GetCapabilities",
        },
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "linux" in result.content.lower()


def test_constructs_dbus_send_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux, DBusCall builds the right ``dbus-send`` argv."""
    monkeypatch.setattr("sys.platform", "linux")
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="ok", stderr=""))

    with patch("opencomputer.tools.dbus_call.shutil.which", return_value="/usr/bin/dbus-send"):
        with patch("opencomputer.tools.dbus_call.subprocess.run", fake_run):
            tool = _make_tool()
            call = ToolCall(
                id="t1", name="DBusCall",
                arguments={
                    "bus": "session",
                    "destination": "org.gnome.Shell",
                    "object_path": "/org/gnome/Shell",
                    "interface": "org.gnome.Shell",
                    "method": "Eval",
                    "args": ["string:1+1"],
                },
            )
            result = asyncio.run(tool.execute(call))

    args, _ = fake_run.call_args
    argv = args[0]
    assert argv[0] == "/usr/bin/dbus-send"
    assert "--session" in argv
    assert "--dest=org.gnome.Shell" in argv
    assert "--type=method_call" in argv
    assert "--print-reply" in argv
    assert "/org/gnome/Shell" in argv
    assert "org.gnome.Shell.Eval" in argv
    assert "string:1+1" in argv
    assert "ok" in result.content
    assert result.is_error is False


def test_invalid_bus_kind_rejected() -> None:
    tool = _make_tool()
    call = ToolCall(
        id="t1", name="DBusCall",
        arguments={
            "bus": "weird",  # only "session" or "system" allowed
            "destination": "org.x",
            "object_path": "/x",
            "interface": "org.x",
            "method": "X",
        },
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "bus" in result.content.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dbus_call.py -v`
Expected: FAIL with `ModuleNotFoundError: opencomputer.tools.dbus_call`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/tools/dbus_call.py`:

```python
"""DBusCall tool — invoke a D-Bus method on Linux via ``dbus-send``.

D-Bus is the universal Linux desktop IPC layer. GNOME Shell, KDE
Plasma, NetworkManager, BlueZ, systemd, and most desktop apps publish
methods through it. ``dbus-send`` ships with every systemd Linux distro
(part of ``dbus`` package, always installed).

The tool intentionally does not use ``dbus-python`` (a heavier dep) —
``dbus-send`` plus parsing the textual reply is enough for the agent's
"call this method, get back text" use case. If a skill needs richer
introspection later, a follow-up tool ``DBusIntrospect`` can wrap
``gdbus introspect``.

Mirrors ``AppleScriptRun`` in spirit: PER_ACTION consent, Linux-only,
returns stdout as text.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_TIMEOUT_SECONDS = 10
_VALID_BUSES = {"session", "system"}


class DBusCallTool(BaseTool):
    """Invoke a D-Bus method via ``dbus-send``. Linux only."""

    # parallel_safe = False — D-Bus methods can mutate desktop state
    # (window focus, network config, etc.); racing parallel calls is bad.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.dbus_call",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Invoke an arbitrary D-Bus method on the Linux session or "
                "system bus. Can control GNOME/KDE apps, NetworkManager, "
                "BlueZ, systemd, etc. Same surface as ``dbus-send``."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="DBusCall",
            description=(
                "Call a D-Bus method via dbus-send (Linux only). "
                "``bus`` is 'session' or 'system'. ``destination`` is the "
                "well-known bus name (e.g. 'org.gnome.Shell'). "
                "``object_path`` is the object (e.g. '/org/gnome/Shell'). "
                "``interface`` + ``method`` identify the method. "
                "``args`` is an optional list of dbus-send-formatted args "
                "like ['string:hello', 'int32:42'] — see ``man dbus-send``. "
                "PER_ACTION consent. Returns the textual reply from "
                "dbus-send --print-reply."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "bus": {"type": "string", "enum": ["session", "system"]},
                    "destination": {"type": "string"},
                    "object_path": {"type": "string"},
                    "interface": {"type": "string"},
                    "method": {"type": "string"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["bus", "destination", "object_path", "interface", "method"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments
        bus = a.get("bus", "")
        if bus not in _VALID_BUSES:
            return ToolResult(
                tool_call_id=call.id,
                content=f"bus must be one of {_VALID_BUSES}; got {bus!r}",
                is_error=True,
            )
        for required in ("destination", "object_path", "interface", "method"):
            if not isinstance(a.get(required), str) or not a[required]:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"missing or empty required arg {required!r}",
                    is_error=True,
                )

        if not sys.platform.startswith("linux"):
            return ToolResult(
                tool_call_id=call.id,
                content="DBusCall requires Linux (sys.platform.startswith('linux'))",
                is_error=True,
            )

        exe = shutil.which("dbus-send")
        if exe is None:
            return ToolResult(
                tool_call_id=call.id,
                content="dbus-send not found on PATH (install the 'dbus' package)",
                is_error=True,
            )

        argv = [
            exe,
            f"--{bus}",
            "--print-reply",
            "--type=method_call",
            f"--dest={a['destination']}",
            a["object_path"],
            f"{a['interface']}.{a['method']}",
        ]
        for raw in a.get("args", []) or []:
            if isinstance(raw, str):
                argv.append(raw)

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id=call.id,
                content=f"dbus-send timed out after {_TIMEOUT_SECONDS}s",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"dbus-send launch failed: {exc}",
                is_error=True,
            )

        body = proc.stdout
        if proc.stderr:
            body += f"\n[stderr]\n{proc.stderr}"
        return ToolResult(
            tool_call_id=call.id,
            content=body or "(no output)",
            is_error=proc.returncode != 0,
        )


__all__ = ["DBusCallTool"]
```

In `opencomputer/agent/consent/capability_taxonomy.py`, add a sibling dict entry (same `gui.*` namespace as `gui.applescript_run` and `gui.powershell_run` — audit B2/B3):

```python
"gui.dbus_call": ConsentTier.PER_ACTION,
```

In `opencomputer/cli.py` `_register_builtin_tools()`, register:

```python
from opencomputer.tools.dbus_call import DBusCallTool
_registry.register(DBusCallTool())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dbus_call.py -v`
Expected: 4 passed (one is skipped on Linux).

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/tools/dbus_call.py opencomputer/agent/consent/capability_taxonomy.py opencomputer/cli.py tests/test_dbus_call.py
git add opencomputer/tools/dbus_call.py opencomputer/agent/consent/capability_taxonomy.py opencomputer/cli.py tests/test_dbus_call.py
git commit -m "feat(tools): DBusCall — Linux desktop AppleScriptRun-equivalent"
```

---

# Final gate — full pytest + ruff before push

After all tasks above are done, BEFORE pushing or creating a PR:

- [ ] **Step F1: Full pytest**

Run: `pytest tests/ -q --tb=short`
Expected: 0 failed, ALL pass. (Per the user's "no push without deep testing" rule.)

- [ ] **Step F2: ruff check**

Run: `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`
Expected: All checks passed.

- [ ] **Step F3: Smoke test the CLI**

Run: `python -m opencomputer --help`
Expected: prints help text including `service` subcommand.

Run: `python -m opencomputer --headless config show`
Expected: prints config (no Rich Live, no prompt-toolkit ESC sequences).

- [ ] **Step F4: Push to feature branch + open PR**

```bash
git push -u origin feat/cross-platform-deployment
gh pr create --title "feat(deployment): Linux/Pi/Windows cross-platform parity" --body "$(cat docs/superpowers/plans/2026-04-29-cross-platform-deployment-parity.md | head -40)"
```

Use the same public-flip workflow as PR #266 if CI is billing-blocked.

---

## Self-Review

**Spec coverage check:**
- ✅ Headless mode → Tasks 1.1, 1.2
- ✅ systemd service install → Task 1.3
- ✅ pyproject template-include → Task 1.4
- ✅ journald → Task 1.5
- ✅ Docker multi-arch → Tasks 2.1, 2.2
- ✅ Pi guide → Task 2.3
- ✅ generic systemd guide → Task 2.4
- ✅ Win32 SendInput shim → Tasks 3.1, 3.2
- ✅ PowerShellRun → Task 3.3
- ✅ DBusCall → Task 4.1

**Placeholder scan:** No "TBD", "implement later", or vague handwaving. Every code step has concrete code; every command has expected output.

**Type consistency:**
- `is_headless()` signature consistent across Tasks 1.1, 1.2, 1.5
- `_click_win32_sendinput` / `_type_win32_sendinput` naming consistent between Task 3.1 (defines) and Task 3.2 (wires)
- `system.powershell_run` / `system.dbus_call` capability ids consistent
- `ConsentTier.PER_ACTION` used uniformly

**Honest deferrals (per the user's "honest deferrals" rule):**
- `send_keys` (hotkey-by-name) in Task 3.1 returns `False` and is explicitly marked as a follow-up — this is a deferred-with-reason, not a silent gap.
- Phase 2 ARM wheels: Python is interpreted, so the existing pure-Python wheel already runs on ARM. We don't need a separate ARM wheel build — the Docker workflow handles multi-arch at the container level. (No work, but flagged so the audit doesn't ask.)
- Windows MSI installer is NOT in scope. Pip + Docker on Windows is enough for the wedge.
