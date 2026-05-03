"""End-to-end Windows backend test — actually shells out to schtasks.exe.

Existing tests/test_service_windows_backend.py mock _schtasks so the
subprocess never runs; this file complements them with a real-hardware
test that registers a task, queries it, and deletes it. Skipped on
non-Windows so the test suite stays cross-platform.

This test is what gives PR #378's Windows backend real-hardware
verification — the existing mocks only prove "we'd pass the right args
IF schtasks accepted them," not "Windows actually accepts the rendered
Task XML."
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only end-to-end test (real schtasks.exe required)",
)


def _schtasks_available() -> bool:
    return shutil.which("schtasks") is not None


# Use a unique task name so we never collide with a real OpenComputer
# install on the test machine.
_TASK_NAME = "OpenComputerGatewayE2ETest"


def _delete_task() -> None:
    """Best-effort cleanup — never raise."""
    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", _TASK_NAME, "/f"],
            capture_output=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def test_real_schtasks_create_query_delete_roundtrip(tmp_path: Path) -> None:
    """Render a Task XML, register it via real schtasks, query it, delete it.

    This is the moment-of-truth for the rendered XML's Windows-API
    compatibility. If schtasks rejects the XML, the rendering logic is
    broken — and no amount of subprocess mocking would catch it.
    """
    if not _schtasks_available():
        pytest.skip("schtasks.exe not on PATH — likely not a Windows env")

    from opencomputer.service import _windows_schtasks

    # Render the actual XML our backend produces, write to a temp file,
    # then ask Windows to register it under a unique test name.
    body = _windows_schtasks._render_task(
        executable=r"C:\Windows\System32\cmd.exe",  # Anything that exists
        workdir=tmp_path,
        profile="default",
    )
    xml_path = tmp_path / "task.xml"
    xml_path.write_text(body, encoding="utf-16")

    # Best-effort cleanup of any leftover from a prior failed run.
    _delete_task()

    try:
        # /create
        create_proc = subprocess.run(
            ["schtasks", "/create", "/xml", str(xml_path),
             "/tn", _TASK_NAME, "/f"],
            capture_output=True, text=True, timeout=15,
        )
        assert create_proc.returncode == 0, (
            f"schtasks /create rejected our rendered XML.\n"
            f"stdout: {create_proc.stdout}\nstderr: {create_proc.stderr}"
        )

        # /query — verify the task is registered
        query_proc = subprocess.run(
            ["schtasks", "/query", "/tn", _TASK_NAME,
             "/v", "/fo", "list"],
            capture_output=True, text=True, timeout=15,
        )
        assert query_proc.returncode == 0, (
            f"schtasks /query failed: {query_proc.stderr}"
        )
        assert _TASK_NAME in query_proc.stdout, (
            f"task not found in /query output: {query_proc.stdout}"
        )
    finally:
        _delete_task()

    # /query after delete — should fail, proving cleanup worked
    query_after = subprocess.run(
        ["schtasks", "/query", "/tn", _TASK_NAME, "/v", "/fo", "list"],
        capture_output=True, text=True, timeout=15,
    )
    assert query_after.returncode != 0, (
        "task still registered after /delete — cleanup failed"
    )


def test_real_install_uninstall_via_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Drive the same flow through _windows_schtasks.install/uninstall
    instead of bare subprocess calls. Proves the full backend path
    works end-to-end, not just the rendered XML.
    """
    if not _schtasks_available():
        pytest.skip("schtasks.exe not on PATH — likely not a Windows env")

    # Override the task name so we don't touch a real OpenComputerGateway
    # install. The test isolates by patching _TASK_NAME in the module.
    from opencomputer.service import _windows_schtasks

    monkeypatch.setattr(_windows_schtasks, "_TASK_NAME", _TASK_NAME)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(
        _windows_schtasks, "_resolve_executable",
        lambda: r"C:\Windows\System32\cmd.exe",
    )

    # Best-effort pre-clean.
    _delete_task()

    try:
        install_result = _windows_schtasks.install(
            profile="default", extra_args="gateway",
        )
        assert install_result.backend == "schtasks"
        assert install_result.config_path.exists()
        assert install_result.enabled is True, (
            f"install reported enabled=False; notes={install_result.notes}"
        )

        status = _windows_schtasks.status()
        assert status.file_present is True
        assert status.enabled is True

        uninstall_result = _windows_schtasks.uninstall()
        assert uninstall_result.file_removed is True
    finally:
        _delete_task()
