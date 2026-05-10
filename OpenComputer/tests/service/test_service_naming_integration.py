"""Integration tests: service_label() plumbed through the 3 backends.

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (Task 1.2).

Asserts that for canonical home + ``default`` profile each backend keeps its
historical unit/plist/task name (backwards compat), and that any non-default
profile produces a hash-suffixed variant — so two installs can coexist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.service import (
    _linux_systemd,
    _macos_launchd,
    _naming,
    _windows_schtasks,
)

# ---------------------------------------------------------------------------
# Linux systemd: _unit_filename(profile)
# ---------------------------------------------------------------------------


def test_systemd_default_canonical_keeps_legacy_unit(monkeypatch):
    """Default profile + canonical home → ``opencomputer.service``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _linux_systemd._unit_filename("default") == "opencomputer.service"


def test_systemd_named_profile_appends_hash(monkeypatch):
    """Named profile under canonical home → ``opencomputer-<hash>.service``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    name = _linux_systemd._unit_filename("work")
    assert name.startswith("opencomputer-")
    assert name.endswith(".service")
    suffix = name.removeprefix("opencomputer-").removesuffix(".service")
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_systemd_non_canonical_home_default_profile_appends_hash(
    monkeypatch, tmp_path,
):
    """Non-canonical home + default profile still hash-suffixed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    name = _linux_systemd._unit_filename("default")
    assert name != "opencomputer.service"
    assert name.startswith("opencomputer-")
    assert name.endswith(".service")


def test_systemd_user_unit_path_includes_hashed_filename(
    monkeypatch, tmp_path,
):
    """``_user_unit_path`` propagates the hashed filename to the on-disk path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    p = _linux_systemd._user_unit_path("default")
    assert p.parent == tmp_path / ".config" / "systemd" / "user"
    assert p.name.startswith("opencomputer-")
    assert p.name.endswith(".service")


# ---------------------------------------------------------------------------
# macOS launchd: _label(profile) + _plist_filename(profile)
# ---------------------------------------------------------------------------


def test_launchd_default_canonical_keeps_legacy_label(monkeypatch):
    """Default profile + canonical home → ``com.opencomputer.gateway``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _macos_launchd._label("default") == "com.opencomputer.gateway"
    assert (
        _macos_launchd._plist_filename("default")
        == "com.opencomputer.gateway.plist"
    )


def test_launchd_named_profile_appends_hash(monkeypatch):
    """Named profile under canonical home → ``com.opencomputer.gateway.<hash>``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    label = _macos_launchd._label("work")
    assert label.startswith("com.opencomputer.gateway.")
    suffix = label.removeprefix("com.opencomputer.gateway.")
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_launchd_non_canonical_home_default_profile_appends_hash(
    monkeypatch, tmp_path,
):
    """Non-canonical home + default profile still hash-suffixed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    label = _macos_launchd._label("default")
    assert label != "com.opencomputer.gateway"
    assert label.startswith("com.opencomputer.gateway.")


def test_launchd_plist_path_includes_hashed_filename(
    monkeypatch, tmp_path,
):
    """``_plist_path`` propagates the hashed filename to the on-disk path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = _macos_launchd._plist_path("default")
    assert p.name.startswith("com.opencomputer.gateway.")
    assert p.name.endswith(".plist")


def test_launchd_render_plist_uses_profile_label(
    monkeypatch, tmp_path,
):
    """``_render_plist`` writes the profile-derived label into the plist body."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    body_default = _macos_launchd._render_plist(
        executable="/usr/local/bin/oc",
        workdir=tmp_path,
        profile="default",
        stdout_log=tmp_path / "o.log",
        stderr_log=tmp_path / "e.log",
    )
    assert "<string>com.opencomputer.gateway</string>" in body_default

    body_named = _macos_launchd._render_plist(
        executable="/usr/local/bin/oc",
        workdir=tmp_path,
        profile="work",
        stdout_log=tmp_path / "o.log",
        stderr_log=tmp_path / "e.log",
    )
    assert "<string>com.opencomputer.gateway.</string>" not in body_named
    assert "<string>com.opencomputer.gateway.work</string>" not in body_named
    # Body must contain the hashed label exactly once for the work profile.
    expected_label = _macos_launchd._label("work")
    assert f"<string>{expected_label}</string>" in body_named


# ---------------------------------------------------------------------------
# Windows schtasks: _task_name(profile) + _xml_filename(profile)
# ---------------------------------------------------------------------------


def test_schtasks_default_canonical_keeps_legacy_task_name(monkeypatch):
    """Default profile + canonical home → ``OpenComputerGateway``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _windows_schtasks._task_name("default") == "OpenComputerGateway"
    assert _windows_schtasks._xml_filename("default") == "opencomputer-task.xml"


def test_schtasks_named_profile_appends_hash(monkeypatch):
    """Named profile under canonical home → ``OpenComputerGateway-<hash>``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    name = _windows_schtasks._task_name("work")
    assert name.startswith("OpenComputerGateway-")
    suffix = name.removeprefix("OpenComputerGateway-")
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    xml_name = _windows_schtasks._xml_filename("work")
    assert xml_name.startswith("opencomputer-task-")
    assert xml_name.endswith(".xml")


def test_schtasks_non_canonical_home_default_profile_appends_hash(
    monkeypatch, tmp_path,
):
    """Non-canonical home + default profile still hash-suffixed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    name = _windows_schtasks._task_name("default")
    assert name != "OpenComputerGateway"
    assert name.startswith("OpenComputerGateway-")


def test_schtasks_xml_path_includes_hashed_filename(
    monkeypatch, tmp_path,
):
    """``_xml_path`` propagates the hashed filename to the on-disk path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = _windows_schtasks._xml_path("default")
    assert p.name.startswith("opencomputer-task-")
    assert p.name.endswith(".xml")


# ---------------------------------------------------------------------------
# Cross-backend invariants
# ---------------------------------------------------------------------------


def test_distinct_profiles_produce_distinct_names_in_each_backend(monkeypatch):
    """Two named profiles must map to distinct unit/plist/task names per backend."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert (
        _linux_systemd._unit_filename("a")
        != _linux_systemd._unit_filename("b")
    )
    assert _macos_launchd._label("a") != _macos_launchd._label("b")
    assert (
        _windows_schtasks._task_name("a")
        != _windows_schtasks._task_name("b")
    )


def test_canonical_label_at_default_matches_naming_module(monkeypatch):
    """Sanity: the legacy fallback path is gated on the canonical label."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _naming.service_label("default") == _naming._CANONICAL_LABEL


def test_install_writes_hashed_unit_under_named_profile_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """systemd install for a named profile writes a hash-suffixed unit file."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        _linux_systemd, "_resolve_executable",
        lambda: "/usr/local/bin/oc",
    )
    monkeypatch.setattr(_linux_systemd, "_systemctl", lambda *a: (0, "active", ""))

    result = _linux_systemd.install(profile="work", extra_args="gateway")
    assert result.config_path.parent == fake_home / ".config" / "systemd" / "user"
    assert result.config_path.name != "opencomputer.service"
    assert result.config_path.name.startswith("opencomputer-")
    assert result.config_path.name.endswith(".service")
    assert result.config_path.exists()
