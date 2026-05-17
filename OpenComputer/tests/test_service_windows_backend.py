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
    # argv ends at "gateway" (no "run")
    assert " gateway run<" not in body
    # well-formed XML
    import xml.etree.ElementTree as ET
    ET.fromstring(body)


def test_install_invokes_schtasks_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(_windows_schtasks, "_resolve_executable", lambda: r"C:\bin\oc.exe")

    calls: list = []

    def fake_schtasks(*a):
        calls.append(a)
        return (0, "SUCCESS", "")

    monkeypatch.setattr(_windows_schtasks, "_schtasks", fake_schtasks)

    result = _windows_schtasks.install(profile="default", extra_args="")
    assert result.backend == "schtasks"
    assert any(a[0] == "/create" for a in calls)


def test_uninstall_invokes_schtasks_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")

    user_dir = tmp_path / ".opencomputer"
    user_dir.mkdir()
    xml_path = user_dir / "opencomputer-task.xml"
    xml_path.write_text("<Task/>")

    monkeypatch.setattr(_windows_schtasks, "_xml_path", lambda *a, **kw: xml_path)
    calls: list = []
    monkeypatch.setattr(
        _windows_schtasks, "_schtasks",
        lambda *a: (calls.append(a) or (0, "", "")),
    )

    result = _windows_schtasks.uninstall(profile="default")
    assert result.file_removed is True
    assert any(a[0] == "/delete" for a in calls)


def test_uninstall_targets_named_profile_task_not_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``uninstall(profile=...)`` must delete the GIVEN profile's task +
    XML, not the default one. A named profile gets a sha256-suffixed
    task name (``OpenComputerGateway-<hash>``) and XML filename
    distinct from the default. Pre-fix, ``uninstall`` took no args and
    only ever resolved the default task."""
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        _windows_schtasks, "_resolve_executable", lambda: r"C:\bin\oc.exe",
    )

    calls: list = []
    monkeypatch.setattr(
        _windows_schtasks, "_schtasks",
        lambda *a: (calls.append(a) or (0, "SUCCESS", "")),
    )

    default_result = _windows_schtasks.install(profile="default", extra_args="")
    named_result = _windows_schtasks.install(profile="coder", extra_args="")
    assert named_result.config_path != default_result.config_path
    assert default_result.config_path.exists()
    assert named_result.config_path.exists()

    result_uninstall = _windows_schtasks.uninstall(profile="coder")

    assert result_uninstall.file_removed is True
    assert result_uninstall.config_path == named_result.config_path
    assert not named_result.config_path.exists()
    assert default_result.config_path.exists()
    # /delete targeted the named-profile task name.
    coder_task = _windows_schtasks._task_name("coder")
    assert any(
        a[0] == "/delete" and coder_task in a for a in calls
    ), f"/delete must target {coder_task!r}; calls={calls!r}"


def test_supported_returns_true_only_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setattr("sys.platform", "win32")
    assert _windows_schtasks.supported() is True
    monkeypatch.setattr("sys.platform", "linux")
    assert _windows_schtasks.supported() is False


def test_status_parses_schtasks_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opencomputer.service import _windows_schtasks

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
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

    calls: list = []
    monkeypatch.setattr(
        _windows_schtasks, "_schtasks",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _windows_schtasks.start() is True
    assert any("/run" in a for a in calls)


def test_stop_invokes_schtasks_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _windows_schtasks

    calls: list = []
    monkeypatch.setattr(
        _windows_schtasks, "_schtasks",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _windows_schtasks.stop() is True
    assert any("/end" in a for a in calls)
