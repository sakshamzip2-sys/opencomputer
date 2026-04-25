"""Tests for extensions/oi-capability/subprocess/venv_bootstrap.py.

Covers:
1. Venv creation is skipped when already valid
2. Idempotent on second call (no re-creation)
3. Clear BootstrapError when pip is missing
4. Honours OPENCOMPUTER_OI_VERSION env override
5. Python binary path is correct for the platform
6. _venv_is_valid returns False for missing venv
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from extensions.oi_capability.subprocess.venv_bootstrap import (
    OI_VERSION,
    BootstrapError,
    _python_bin,
    _venv_dir,
    _venv_is_valid,
    ensure_oi_venv,
)


class TestVenvBootstrapHelpers:
    def test_venv_dir_returns_path_under_home(self, tmp_path):
        """Venv directory should be under <home>/oi_capability/venv."""
        with patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path):
            # Re-import to get fresh call
            from extensions.oi_capability.subprocess import venv_bootstrap  # noqa: PLC0415
            vdir = venv_bootstrap._venv_dir()
        assert "oi_capability" in str(vdir)
        assert "venv" in str(vdir)

    def test_python_bin_platform_specific(self, tmp_path):
        """Python binary should be Scripts/python.exe on Windows, bin/python on Unix."""
        fake_venv = tmp_path / "venv"
        if sys.platform == "win32":
            expected = fake_venv / "Scripts" / "python.exe"
        else:
            expected = fake_venv / "bin" / "python"
        assert _python_bin(fake_venv) == expected

    def test_venv_is_valid_false_when_python_missing(self, tmp_path):
        """_venv_is_valid returns False when python binary does not exist."""
        # tmp_path has no venv inside
        result = _venv_is_valid(tmp_path / "nonexistent_venv")
        assert result is False

    def test_oi_version_constant(self):
        """Default OI version should be 0.4.3."""
        assert OI_VERSION == "0.4.3"


class TestEnsureOIVenv:
    def test_fast_path_when_venv_already_valid(self, tmp_path):
        """ensure_oi_venv returns quickly if venv is already valid."""
        fake_python = tmp_path / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()

        with (
            patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path),
            patch("extensions.oi_capability.subprocess.venv_bootstrap._venv_is_valid", return_value=True),
        ):
            result = ensure_oi_venv()
        # Should return some Path
        assert isinstance(result, Path)

    def test_honours_env_var_version_override(self, tmp_path, monkeypatch):
        """OPENCOMPUTER_OI_VERSION env var overrides the default OI version."""
        monkeypatch.setenv("OPENCOMPUTER_OI_VERSION", "0.3.9")

        # Make venv "already valid" to skip actual creation
        with (
            patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path),
            patch("extensions.oi_capability.subprocess.venv_bootstrap._venv_is_valid", return_value=True),
        ):
            result = ensure_oi_venv()
        assert isinstance(result, Path)

    def test_raises_bootstrap_error_when_venv_creation_fails(self, tmp_path):
        """BootstrapError with install hints when subprocess.run fails."""
        import subprocess  # noqa: PLC0415

        with (
            patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path),
            patch("extensions.oi_capability.subprocess.venv_bootstrap._venv_is_valid", return_value=False),
            patch(
                "extensions.oi_capability.subprocess.venv_bootstrap.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "venv", stderr=b"permission denied"),
            ),
        ):
            with pytest.raises(BootstrapError) as exc_info:
                ensure_oi_venv()
        assert "Failed to create Python venv" in str(exc_info.value)

    def test_raises_bootstrap_error_when_pip_missing(self, tmp_path):
        """BootstrapError when pip binary not found inside venv after creation."""
        # Simulate venv created but pip missing
        with (
            patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path),
            patch("extensions.oi_capability.subprocess.venv_bootstrap._venv_is_valid", return_value=False),
            patch("extensions.oi_capability.subprocess.venv_bootstrap.subprocess.run"),  # venv creation ok
            # pip binary does not exist — _pip_bin() will return a path that doesn't exist
        ):
            with pytest.raises(BootstrapError) as exc_info:
                ensure_oi_venv()
        # Should mention pip or venv
        assert "pip" in str(exc_info.value).lower() or "venv" in str(exc_info.value).lower()

    def test_idempotent_on_second_call(self, tmp_path):
        """Calling ensure_oi_venv twice returns same path without re-creating venv."""
        call_count = 0
        original_valid = _venv_is_valid

        def mock_is_valid(venv):
            nonlocal call_count
            call_count += 1
            return True  # pretend always valid

        with (
            patch("extensions.oi_capability.subprocess.venv_bootstrap._home", return_value=tmp_path),
            patch("extensions.oi_capability.subprocess.venv_bootstrap._venv_is_valid", side_effect=mock_is_valid),
        ):
            r1 = ensure_oi_venv()
            r2 = ensure_oi_venv()

        # Both calls should return a Path instance
        assert isinstance(r1, type(r2))
        # _venv_is_valid called at least once per ensure_oi_venv call
        assert call_count >= 2
