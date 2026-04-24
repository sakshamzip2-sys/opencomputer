"""Tests for extensions/opencli-scraper/wrapper.py.

All subprocess calls are mocked — no live opencli binary required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add plugin dir to path so imports work without package install.
_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from wrapper import (  # noqa: E402
    MIN_OPENCLI_VERSION,
    OpenCLIAuthError,
    OpenCLIError,
    OpenCLINetworkError,
    OpenCLITimeoutError,
    OpenCLIVersionError,
    OpenCLIWrapper,
    _parse_version,
    _port_is_free,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ── Unit tests ─────────────────────────────────────────────────────────────────


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("1.7.0") == (1, 7, 0)

    def test_with_prefix(self):
        assert _parse_version("v1.7.0") == (1, 7, 0)

    def test_two_part(self):
        assert _parse_version("2.0") == (2, 0)

    def test_empty_returns_zero(self):
        assert _parse_version("") == (0,)


class TestPortIsFree:
    def test_returns_bool(self):
        # Cannot guarantee a port is truly free, but the function returns a bool.
        result = _port_is_free(19825)
        assert isinstance(result, bool)


# ── OpenCLIWrapper tests ───────────────────────────────────────────────────────


class TestOpenCLIWrapperRun:
    async def test_happy_path_returns_parsed_json(self):
        payload = {"data": {"login": "octocat", "name": "The Octocat"}}
        proc = _make_proc(stdout=json.dumps(payload).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            wrapper = OpenCLIWrapper()
            result = await wrapper.run("github/user", "octocat")

        assert result == payload
        assert mock_exec.call_count == 1

    async def test_env_var_port_override_applied(self):
        """OPENCLI_DAEMON_PORT must appear in the env passed to the subprocess."""
        proc = _make_proc(stdout=b"{}")
        captured_env: dict = {}

        async def fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            wrapper = OpenCLIWrapper()
            await wrapper.run("hackernews/user", "pg")

        assert "OPENCLI_DAEMON_PORT" in captured_env
        port_val = int(captured_env["OPENCLI_DAEMON_PORT"])
        assert 19825 <= port_val <= 19899

    async def test_timeout_kills_subprocess_and_raises(self):
        """A slow subprocess triggers OpenCLITimeoutError + kill."""
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def slow_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        proc.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper(default_timeout_s=1)
            with pytest.raises(OpenCLITimeoutError):
                await wrapper.run("github/user", "octocat", timeout_s=1)

        proc.kill.assert_called_once()

    async def test_encoding_errors_dont_crash(self):
        """Malformed bytes in stdout should not crash — replaced with U+FFFD."""
        bad_bytes = b'{"data": "\xff\xfe"}' + b"\n"
        proc = _make_proc(stdout=bad_bytes)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            # Should return parsed JSON without raising.
            result = await wrapper.run("github/user", "octocat")
        assert isinstance(result, dict)

    async def test_nonzero_exit_raises_opencli_error(self):
        proc = _make_proc(stdout=b"", stderr=b"something failed", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLIError):
                await wrapper.run("github/user", "octocat")

    async def test_exit_code_69_raises_network_error(self):
        proc = _make_proc(returncode=69, stderr=b"browser not connected")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLINetworkError):
                await wrapper.run("github/user", "octocat")

    async def test_exit_code_77_raises_auth_error(self):
        proc = _make_proc(returncode=77, stderr=b"not logged in")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLIAuthError):
                await wrapper.run("twitter/profile", "elonmusk")

    async def test_empty_stdout_returns_empty_dict(self):
        proc = _make_proc(stdout=b"   \n", returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            result = await wrapper.run("github/user", "octocat")
        assert result == {}

    async def test_non_json_stdout_raises_opencli_error(self):
        proc = _make_proc(stdout=b"not json at all!!!", returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLIError, match="non-JSON"):
                await wrapper.run("github/user", "octocat")

    async def test_missing_binary_raises_opencli_error(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file"),
        ):
            wrapper = OpenCLIWrapper(opencli_binary="/no/such/binary")
            with pytest.raises(OpenCLIError, match="not found"):
                await wrapper.run("github/user", "octocat")


class TestOpenCLIWrapperCheckVersion:
    async def test_valid_version_returns_string(self):
        proc = _make_proc(stdout=b"opencli 1.7.7\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            version = await wrapper._check_version()
        assert version == "1.7.7"

    async def test_version_too_old_raises(self):
        proc = _make_proc(stdout=b"opencli 1.6.0\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLIVersionError, match="too old"):
                await wrapper._check_version()

    async def test_missing_binary_raises(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError(),
        ):
            wrapper = OpenCLIWrapper()
            with pytest.raises(OpenCLIError, match="not found"):
                await wrapper._check_version()
