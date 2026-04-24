"""Tests for extensions/opencli-scraper/subprocess_bootstrap.py.

All shutil.which / subprocess calls are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from subprocess_bootstrap import (  # noqa: E402
    BootstrapError,
    detect_chrome,
    detect_opencli,
    require_chrome,
    require_opencli,
)


class TestDetectOpencli:
    def test_global_install_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/opencli"):
            result = detect_opencli()
        assert result == Path("/usr/local/bin/opencli")

    def test_returns_none_when_not_found_and_no_npx(self):
        with patch("shutil.which", return_value=None):
            result = detect_opencli()
        assert result is None

    def test_npx_fallback_works(self):
        import subprocess  # noqa: F401

        def fake_which(name):
            if name == "opencli":
                return None
            if name == "npx":
                return "/usr/bin/npx"
            return None

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.7.7\n"

        with patch("shutil.which", side_effect=fake_which), patch(
            "subprocess.run", return_value=mock_result
        ):
            result = detect_opencli()
        assert result is not None  # Found via npx

    def test_npx_fallback_fails_gracefully(self):
        import subprocess  # noqa: F401

        def fake_which(name):
            if name == "opencli":
                return None
            if name == "npx":
                return "/usr/bin/npx"
            return None

        mock_result = MagicMock()
        mock_result.returncode = 1  # npx probe fails
        mock_result.stdout = ""

        with patch("shutil.which", side_effect=fake_which), patch(
            "subprocess.run", return_value=mock_result
        ):
            result = detect_opencli()
        assert result is None


class TestRequireOpencli:
    def test_raises_bootstrap_error_when_missing(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(BootstrapError) as exc_info:
                require_opencli()
        # Error message must contain install instructions.
        msg = str(exc_info.value)
        assert "npm install" in msg or "install" in msg.lower()
        assert "opencli" in msg.lower()

    def test_contains_nodejs_requirement_in_message(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(BootstrapError) as exc_info:
                require_opencli()
        msg = str(exc_info.value)
        assert "Node" in msg or "node" in msg


class TestDetectChrome:
    def test_found_via_path(self):
        with patch(
            "shutil.which",
            side_effect=lambda name: "/usr/bin/chromium" if name == "chromium" else None,
        ):
            with patch("subprocess_bootstrap.platform.system", return_value="Linux"):
                result = detect_chrome()
        assert result is not None

    def test_returns_none_when_not_found(self):
        with patch("shutil.which", return_value=None), patch(
            "subprocess_bootstrap.platform.system", return_value="Linux"
        ):
            result = detect_chrome()
        assert result is None


class TestRequireChrome:
    def test_raises_bootstrap_error_with_install_hint(self):
        with patch("shutil.which", return_value=None), patch(
            "subprocess_bootstrap.platform.system", return_value="Linux"
        ):
            with pytest.raises(BootstrapError) as exc_info:
                require_chrome()
        msg = str(exc_info.value)
        assert "chrome" in msg.lower() or "chromium" in msg.lower()
        assert "install" in msg.lower()
