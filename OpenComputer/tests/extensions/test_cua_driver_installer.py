"""Tests for the cua-driver installer.

Ported from hermes-agent ``tests/hermes_cli/test_install_cua_driver.py``,
adapted to the standalone ``installer`` module in the computer-use plugin.

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


installer = _load("_cu_test_installer", PLUGIN_DIR / "installer.py")


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
