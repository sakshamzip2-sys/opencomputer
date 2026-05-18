"""Tests for the cua-driver installer.

Ported from hermes-agent ``tests/hermes_cli/test_install_cua_driver.py``,
adapted to the standalone ``cu_installer`` module in the computer-use plugin.

``install_cua_driver(upgrade=True)`` must:

* Be macOS-only — silent no-op on Linux/Windows so callers can invoke it
  unconditionally without warning every non-macOS user.
* Re-run the installer even when the binary is already on PATH (the
  canonical upgrade path — the upstream script always pulls latest).
* Preserve ``upgrade=False`` behaviour: skip if installed, install
  otherwise, warn on non-macOS.

No subprocess is ever spawned — every test patches ``_run_cua_driver_installer``
or ``subprocess``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "computer-use"
)


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


installer = _load("_cu_test_installer", PLUGIN_DIR / "cu_installer.py")


@pytest.fixture(autouse=True)
def _no_well_known_cua_driver(monkeypatch, tmp_path):
    """Point ``find_cua_driver``'s well-known fallback locations at
    nonexistent paths so the existing PATH-driven tests stay deterministic
    regardless of whether cua-driver is installed on the test host.

    Tests that exercise the fallbacks override these explicitly.
    """
    monkeypatch.setattr(
        installer, "_LOCAL_BIN_CUA_DRIVER", tmp_path / "no-local-bin-cua-driver",
    )
    monkeypatch.setattr(
        installer, "_APP_BUNDLE_CUA_DRIVER", tmp_path / "no-app-bundle-cua-driver",
    )
    monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)


class TestInstallCuaDriverUpgrade:
    def test_upgrade_on_non_macos_is_silent_noop(self):
        with patch.object(installer, "_print_warning") as warn, \
             patch("platform.system", return_value="Linux"):
            assert installer.install_cua_driver(upgrade=True) is False
            warn.assert_not_called()

    def test_non_upgrade_on_non_macos_warns(self):
        with patch.object(installer, "_print_warning") as warn, \
             patch("platform.system", return_value="Linux"):
            assert installer.install_cua_driver(upgrade=False) is False
            warn.assert_called()

    def test_upgrade_on_macos_with_binary_runs_installer(self):
        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                          if n in ("cua-driver", "curl") else None), \
             patch.object(installer, "_run_cua_driver_installer",
                          return_value=True) as runner, \
             patch.object(installer, "cua_driver_version", return_value="0.5.0"):
            assert installer.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            # Refresh path uses non-verbose mode (no repeated permission block).
            assert runner.call_args.kwargs.get("verbose") is False

    def test_upgrade_on_macos_without_binary_runs_installer(self):
        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(installer, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert installer.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()

    def test_non_upgrade_on_macos_with_binary_skips_install(self):
        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                          if n in ("cua-driver", "curl") else None), \
             patch.object(installer, "_run_cua_driver_installer") as runner, \
             patch.object(installer, "cua_driver_version", return_value="0.5.0"):
            assert installer.install_cua_driver(upgrade=False) is True
            runner.assert_not_called()

    def test_non_upgrade_on_macos_without_binary_runs_installer(self):
        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(installer, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert installer.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()

    def test_upgrade_without_curl_does_not_crash(self):
        def _which(name):
            return "/usr/local/bin/cua-driver" if name == "cua-driver" else None

        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which", side_effect=_which), \
             patch.object(installer, "_print_warning"):
            assert installer.install_cua_driver(upgrade=True) is True

    def test_non_upgrade_without_curl_warns_and_returns_false(self):
        with patch("platform.system", return_value="Darwin"), \
             patch.object(installer.shutil, "which", return_value=None), \
             patch.object(installer, "_print_warning") as warn, \
             patch.object(installer, "_print_info"):
            assert installer.install_cua_driver(upgrade=False) is False
            warn.assert_called()


class TestRunCuaDriverInstaller:
    def test_installer_success_path(self):
        class _Result:
            returncode = 0

        with patch.object(installer.subprocess, "run", return_value=_Result()), \
             patch.object(installer.shutil, "which", return_value="/usr/local/bin/cua-driver"), \
             patch.object(installer, "_print_info"), \
             patch.object(installer, "_print_success"):
            assert installer._run_cua_driver_installer() is True

    def test_installer_nonzero_exit_returns_false(self):
        class _Result:
            returncode = 1

        with patch.object(installer.subprocess, "run", return_value=_Result()), \
             patch.object(installer.shutil, "which", return_value=None), \
             patch.object(installer, "_print_info"), \
             patch.object(installer, "_print_warning"):
            assert installer._run_cua_driver_installer() is False

    def test_installer_timeout_returns_false(self):
        with patch.object(installer.subprocess, "run",
                          side_effect=installer.subprocess.TimeoutExpired("x", 1)), \
             patch.object(installer, "_print_info"), \
             patch.object(installer, "_print_warning"):
            assert installer._run_cua_driver_installer() is False


class TestInstallCommandShape:
    def test_install_cmd_is_the_upstream_curl_script(self):
        assert "curl -fsSL" in installer.INSTALL_CMD
        assert "trycua/cua" in installer.INSTALL_CMD
        assert "cua-driver/scripts/install.sh" in installer.INSTALL_CMD


class TestFindCuaDriver:
    """``find_cua_driver`` resolves the binary even when ``~/.local/bin`` is
    not yet on ``$PATH`` — the exact field condition right after install."""

    def test_returns_path_hit_when_on_path(self, monkeypatch):
        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr(
            installer.shutil, "which",
            lambda n: "/usr/local/bin/cua-driver" if n == "cua-driver" else None,
        )
        assert installer.find_cua_driver() == "/usr/local/bin/cua-driver"

    def test_returns_local_bin_symlink_when_not_on_path(self, monkeypatch, tmp_path):
        """Binary reachable ONLY via ``~/.local/bin/cua-driver`` — the upstream
        installer's symlink, not yet exported onto ``$PATH``."""
        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        symlink = local_bin / "cua-driver"
        symlink.write_text("#!/bin/sh\n")
        monkeypatch.setattr(installer, "_LOCAL_BIN_CUA_DRIVER", symlink)
        monkeypatch.setattr(
            installer, "_APP_BUNDLE_CUA_DRIVER", tmp_path / "missing-bundle",
        )
        assert installer.find_cua_driver() == str(symlink)

    def test_returns_app_bundle_when_only_that_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        monkeypatch.setattr(
            installer, "_LOCAL_BIN_CUA_DRIVER", tmp_path / "missing-local",
        )
        bundle = tmp_path / "CuaDriver.app" / "cua-driver"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("#!/bin/sh\n")
        monkeypatch.setattr(installer, "_APP_BUNDLE_CUA_DRIVER", bundle)
        assert installer.find_cua_driver() == str(bundle)

    def test_returns_none_when_nothing_resolves(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        monkeypatch.setattr(
            installer, "_LOCAL_BIN_CUA_DRIVER", tmp_path / "missing-local",
        )
        monkeypatch.setattr(
            installer, "_APP_BUNDLE_CUA_DRIVER", tmp_path / "missing-bundle",
        )
        assert installer.find_cua_driver() is None

    def test_env_override_absolute_path_is_honored(self, monkeypatch, tmp_path):
        custom = tmp_path / "my-cua-driver"
        custom.write_text("#!/bin/sh\n")
        monkeypatch.setenv("OPENCOMPUTER_CUA_DRIVER_CMD", str(custom))
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        assert installer.find_cua_driver() == str(custom)

    def test_env_override_command_name_resolved_via_which(self, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_CUA_DRIVER_CMD", "my-driver")
        monkeypatch.setattr(
            installer.shutil, "which",
            lambda n: "/opt/bin/my-driver" if n == "my-driver" else None,
        )
        assert installer.find_cua_driver() == "/opt/bin/my-driver"

    def test_env_override_unresolvable_falls_through(self, monkeypatch):
        """An override that resolves to nothing must not shadow auto-detection."""
        monkeypatch.setenv("OPENCOMPUTER_CUA_DRIVER_CMD", "/no/such/binary")
        monkeypatch.setattr(
            installer.shutil, "which",
            lambda n: "/usr/local/bin/cua-driver" if n == "cua-driver" else None,
        )
        assert installer.find_cua_driver() == "/usr/local/bin/cua-driver"


class TestInstallerSucceedsWhenBinaryOffPath:
    """Regression for the field bug: ``oc computer-use install`` ran the
    upstream script, which genuinely installed cua-driver, but the success
    check used ``shutil.which`` — and ``~/.local/bin`` was not yet on
    ``$PATH`` — so the tool printed 'installing did not complete'."""

    def test_run_installer_reports_success_via_local_bin(self, monkeypatch, tmp_path):
        class _Result:
            returncode = 0

        # Binary reachable ONLY via the installer's ~/.local/bin symlink.
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        symlink = local_bin / "cua-driver"
        symlink.write_text("#!/bin/sh\n")

        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr(installer.subprocess, "run",
                            lambda *a, **k: _Result())
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        monkeypatch.setattr(installer, "_LOCAL_BIN_CUA_DRIVER", symlink)
        monkeypatch.setattr(
            installer, "_APP_BUNDLE_CUA_DRIVER", tmp_path / "missing-bundle",
        )

        warnings: list[str] = []
        successes: list[str] = []
        monkeypatch.setattr(installer, "_print_warning", warnings.append)
        monkeypatch.setattr(installer, "_print_success", successes.append)
        monkeypatch.setattr(installer, "_print_info", lambda _msg: None)

        assert installer._run_cua_driver_installer() is True
        assert warnings == []
        assert any("installed" in s for s in successes)

    def test_install_cua_driver_fresh_reports_success_via_local_bin(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end: ``install_cua_driver(upgrade=False)`` on a fresh
        macOS host where the script succeeds and the binary lands only in
        ``~/.local/bin`` must return True, not warn."""
        class _Result:
            returncode = 0

        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        symlink = local_bin / "cua-driver"
        symlink.write_text("#!/bin/sh\n")

        monkeypatch.delenv("OPENCOMPUTER_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(installer.subprocess, "run",
                            lambda *a, **k: _Result())
        # curl present, cua-driver NOT on PATH (the field condition).
        monkeypatch.setattr(
            installer.shutil, "which",
            lambda n: "/usr/bin/curl" if n == "curl" else None,
        )
        monkeypatch.setattr(installer, "_LOCAL_BIN_CUA_DRIVER", symlink)
        monkeypatch.setattr(
            installer, "_APP_BUNDLE_CUA_DRIVER", tmp_path / "missing-bundle",
        )
        monkeypatch.setattr(installer, "cua_driver_version", lambda: "0.1.9")

        warnings: list[str] = []
        monkeypatch.setattr(installer, "_print_warning", warnings.append)
        monkeypatch.setattr(installer, "_print_success", lambda _msg: None)
        monkeypatch.setattr(installer, "_print_info", lambda _msg: None)

        assert installer.install_cua_driver(upgrade=False) is True
        assert warnings == []
