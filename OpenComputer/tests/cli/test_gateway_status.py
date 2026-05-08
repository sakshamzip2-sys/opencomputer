"""Tests for ``opencomputer.cli_gateway_status``.

All subprocess and psutil calls are mocked so the suite is platform-independent
(runs identically on macOS, Linux, and Windows CI runners).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CompletedProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_subprocess_runner(
    responses: dict[str, _CompletedProc] | None = None,
    *,
    timeout_argv: tuple[str, ...] | None = None,
) -> Any:
    """Return a fake ``subprocess.run`` that dispatches by argv[0]+argv[1].

    ``responses`` maps a key like ``"systemctl --user"`` or ``"launchctl"``
    to the canned ``_CompletedProc`` to return. Unknown commands raise
    ``FileNotFoundError`` so callers can prove their fallthrough handling.
    ``timeout_argv`` if provided causes a ``TimeoutExpired`` for that exact
    argv prefix.
    """
    responses = responses or {}

    def _run(argv: list[str], **_kwargs: Any) -> _CompletedProc:
        if timeout_argv is not None and tuple(argv[: len(timeout_argv)]) == timeout_argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5)
        # Try a few key shapes most-specific to least
        for k_len in (3, 2, 1):
            key = " ".join(argv[:k_len])
            if key in responses:
                return responses[key]
        raise FileNotFoundError(argv[0])

    return _run


# ---------------------------------------------------------------------------
# 1. Manager detection per platform (parameterized — covers all three)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "system_name,expected_manager,responses",
    [
        (
            "Linux",
            "systemd-user",
            {
                "systemctl --user": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user list-units": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user show": _CompletedProc(
                    returncode=0, stdout="12345\n"
                ),
            },
        ),
        (
            "Darwin",
            "launchd",
            {
                "launchctl list": _CompletedProc(
                    returncode=0,
                    stdout='{\n\t"PID" = 4242;\n\t"Label" = "com.opencomputer.gateway";\n};\n',
                ),
            },
        ),
        (
            "Windows",
            "schtasks",
            {
                "schtasks /Query": _CompletedProc(
                    returncode=0,
                    stdout='"opencomputer-gateway","N/A","Ready"\n',
                ),
            },
        ),
    ],
)
def test_manager_detection_per_platform(
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
    expected_manager: str,
    responses: dict[str, _CompletedProc],
) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: system_name)
    monkeypatch.setattr(mod.subprocess, "run", _make_subprocess_runner(responses))
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.manager == expected_manager


# ---------------------------------------------------------------------------
# 2. service_installed true / false
# ---------------------------------------------------------------------------


def test_service_installed_true_on_linux_when_unit_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user list-units": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user show": _CompletedProc(
                    returncode=0, stdout="9911\n"
                ),
            }
        ),
    )
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.service_installed is True


# ---------------------------------------------------------------------------
# 3. service_running active vs inactive
# ---------------------------------------------------------------------------


def test_service_running_inactive_when_state_not_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded inactive dead\n",
                ),
                "systemctl --user list-units": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded inactive dead\n",
                ),
                "systemctl --user show": _CompletedProc(
                    returncode=0, stdout="0\n"
                ),
            }
        ),
    )
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.service_installed is True
    assert snap.service_running is False
    assert snap.main_pid is None


# ---------------------------------------------------------------------------
# 4. main_pid extracted correctly
# ---------------------------------------------------------------------------


def test_main_pid_extracted_from_systemctl_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user list-units": _CompletedProc(
                    returncode=0,
                    stdout="opencomputer-gateway.service loaded active running\n",
                ),
                "systemctl --user show": _CompletedProc(
                    returncode=0, stdout="98765\n"
                ),
            }
        ),
    )
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.main_pid == 98765
    assert snap.service_running is True


# ---------------------------------------------------------------------------
# 5. Manual PIDs detected via pgrep
# ---------------------------------------------------------------------------


def test_manual_pids_detected_via_pgrep(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    # No service installed
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(returncode=0, stdout=""),
                "systemctl --user list-units": _CompletedProc(returncode=0, stdout=""),
                "systemctl list-units": _CompletedProc(returncode=0, stdout=""),
                "pgrep -af": _CompletedProc(
                    returncode=0,
                    stdout=(
                        "5551 python -m opencomputer.gateway.run\n"
                        "5552 oc gateway run\n"
                    ),
                ),
            }
        ),
    )
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert 5551 in snap.gateway_pids
    assert 5552 in snap.gateway_pids
    assert snap.service_installed is False


# ---------------------------------------------------------------------------
# 6. Foreign-home PIDs via psutil
# ---------------------------------------------------------------------------


def test_foreign_home_pids_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from opencomputer import cli_gateway_status as mod

    other_home = tmp_path / "other-home"
    other_home.mkdir()
    own_home = tmp_path / "my-home"
    own_home.mkdir()

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mod, "_resolve_home", lambda: str(own_home))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(returncode=0, stdout=""),
                "systemctl --user list-units": _CompletedProc(returncode=0, stdout=""),
                "systemctl list-units": _CompletedProc(returncode=0, stdout=""),
            }
        ),
    )
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())

    fake_proc = MagicMock()
    fake_proc.info = {
        "pid": 7777,
        "cmdline": ["python", "-m", "opencomputer.gateway.run"],
        "environ": {"OPENCOMPUTER_HOME": str(other_home)},
    }
    other_proc = MagicMock()
    other_proc.info = {
        "pid": 8888,
        "cmdline": ["python", "-m", "something.else"],
        "environ": {"OPENCOMPUTER_HOME": str(other_home)},
    }
    own_proc = MagicMock()
    own_proc.info = {
        "pid": 9999,
        "cmdline": ["python", "-m", "opencomputer.gateway.run"],
        "environ": {"OPENCOMPUTER_HOME": str(own_home)},
    }

    fake_psutil = MagicMock()
    fake_psutil.process_iter = MagicMock(
        return_value=iter([fake_proc, other_proc, own_proc])
    )
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    monkeypatch.setattr(mod, "psutil", fake_psutil)

    snap = mod.get_gateway_runtime_snapshot()
    assert len(snap.foreign_home_pids) == 1
    fp = snap.foreign_home_pids[0]
    assert fp.pid == 7777
    assert fp.home == other_home


# ---------------------------------------------------------------------------
# 7. running property — service running
# ---------------------------------------------------------------------------


def test_running_property_true_when_service_running() -> None:
    from opencomputer.cli_gateway_status import GatewayRuntimeSnapshot

    snap = GatewayRuntimeSnapshot(
        manager="systemd-user",
        service_installed=True,
        service_running=True,
        main_pid=42,
    )
    assert snap.running is True


# ---------------------------------------------------------------------------
# 8. running property — manual PIDs only
# ---------------------------------------------------------------------------


def test_running_property_true_when_manual_pids_present_no_service() -> None:
    from opencomputer.cli_gateway_status import GatewayRuntimeSnapshot

    snap = GatewayRuntimeSnapshot(manager="none", gateway_pids=(101,))
    assert snap.running is True


# ---------------------------------------------------------------------------
# 9. has_process_service_mismatch correctness
# ---------------------------------------------------------------------------


def test_has_process_service_mismatch_correctness() -> None:
    from opencomputer.cli_gateway_status import GatewayRuntimeSnapshot

    # Service installed + manual PIDs exist + service NOT running --> mismatch
    snap_mismatch = GatewayRuntimeSnapshot(
        manager="systemd-user",
        service_installed=True,
        service_running=False,
        gateway_pids=(7777,),
    )
    assert snap_mismatch.has_process_service_mismatch is True

    # Service installed + service running --> no mismatch
    snap_clean = GatewayRuntimeSnapshot(
        manager="systemd-user",
        service_installed=True,
        service_running=True,
        main_pid=42,
    )
    assert snap_clean.has_process_service_mismatch is False

    # Service not installed --> no mismatch even with manual PIDs
    snap_manual = GatewayRuntimeSnapshot(
        manager="none",
        service_installed=False,
        gateway_pids=(7777,),
    )
    assert snap_manual.has_process_service_mismatch is False


# ---------------------------------------------------------------------------
# 10. manager == "none" on unknown OS
# ---------------------------------------------------------------------------


def test_manager_none_on_unknown_os(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(mod, "_pgrep_pids", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.manager == "none"
    assert snap.service_installed is False
    assert snap.service_running is False


# ---------------------------------------------------------------------------
# 11. Empty state (nothing running) returns clean snapshot
# ---------------------------------------------------------------------------


def test_empty_state_returns_clean_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        _make_subprocess_runner(
            {
                "systemctl --user": _CompletedProc(returncode=0, stdout=""),
                "systemctl --user list-units": _CompletedProc(returncode=0, stdout=""),
                "systemctl list-units": _CompletedProc(returncode=0, stdout=""),
                "pgrep -af": _CompletedProc(returncode=1, stdout=""),
            }
        ),
    )
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    snap = mod.get_gateway_runtime_snapshot()
    assert snap.service_installed is False
    assert snap.service_running is False
    assert snap.main_pid is None
    assert snap.gateway_pids == ()
    assert snap.foreign_home_pids == ()
    assert snap.running is False
    assert snap.has_process_service_mismatch is False


# ---------------------------------------------------------------------------
# 12. Subprocess timeouts fall through gracefully
# ---------------------------------------------------------------------------


def test_subprocess_timeouts_do_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer import cli_gateway_status as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")

    def _always_timeout(argv: list[str], **_kw: Any) -> _CompletedProc:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(mod.subprocess, "run", _always_timeout)
    monkeypatch.setattr(mod, "_foreign_home_pids", lambda *_a, **_k: ())

    # Must not raise — every probe should swallow TimeoutExpired
    snap = mod.get_gateway_runtime_snapshot()
    assert snap.service_installed is False
    assert snap.gateway_pids == ()
