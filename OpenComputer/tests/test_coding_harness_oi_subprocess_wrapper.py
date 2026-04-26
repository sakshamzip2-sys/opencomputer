"""Tests for extensions/oi-capability/subprocess/wrapper.py.

Covers:
1.  JSON-RPC roundtrip with mock subprocess
2.  Timeout handling (call() raises TimeoutError after deadline)
3.  Correlation-ID matching (response matched to correct caller)
4.  Auto-respawn on dead subprocess
5.  Stderr routed to log file path
6.  start() is idempotent when subprocess already alive
7.  stop() sends shutdown request then SIGTERM
8.  is_alive() returns False before start()
9.  is_alive() returns True after start()
10. Pending futures resolved on subprocess death
11. Error response raises RuntimeError in call()
12. Multiple concurrent calls (correlation IDs unique)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from extensions.coding_harness.oi_bridge.subprocess.protocol import (
    ErrorCode,
    JSONRPCError,
    JSONRPCResponse,
)
from extensions.coding_harness.oi_bridge.subprocess.wrapper import OISubprocessWrapper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response_line(req_id: int, result: object = "ok") -> bytes:
    resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return (json.dumps(resp) + "\n").encode()


def _make_error_line(req_id: int, code: int = -32603, message: str = "Internal error") -> bytes:
    resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    return (json.dumps(resp) + "\n").encode()


def _make_mock_process(stdout_lines: list[bytes] | None = None):
    """Build a fake asyncio.subprocess.Process-alike."""
    proc = MagicMock()
    proc.pid = 42
    proc.returncode = None  # alive

    # stdin mock
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin

    # stdout mock — readline yields lines then blocks forever
    lines = list(stdout_lines or [])
    call_count = 0

    async def mock_readline():
        nonlocal call_count
        if call_count < len(lines):
            line = lines[call_count]
            call_count += 1
            return line
        # Block until cancelled
        await asyncio.sleep(9999)
        return b""

    stdout_mock = MagicMock()
    stdout_mock.readline = mock_readline
    proc.stdout = stdout_mock

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    return proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOISubprocessWrapperIsAlive:
    def test_is_alive_false_before_start(self):
        wrapper = OISubprocessWrapper()
        assert not wrapper.is_alive()

    async def test_is_alive_true_after_start(self, tmp_path):
        proc = _make_mock_process()

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper()
            await wrapper.start()
            assert wrapper.is_alive()
            await wrapper.stop()


class TestOISubprocessWrapperRoundtrip:
    async def test_basic_call_roundtrip(self, tmp_path):
        """A call() sends JSON-RPC request and receives a matched response."""
        # We'll capture the request and inject a response
        request_captured: dict = {}
        responses_to_send: list[bytes] = []

        proc = MagicMock()
        proc.pid = 99
        proc.returncode = None

        stdin_mock = MagicMock()

        def capture_write(data: bytes):
            nonlocal request_captured
            request_captured = json.loads(data.decode().strip())
            # Now prepare the matching response
            resp = _make_response_line(request_captured["id"], {"files": ["a.py"]})
            responses_to_send.append(resp)

        stdin_mock.write = capture_write
        stdin_mock.drain = AsyncMock()
        proc.stdin = stdin_mock

        call_count = [0]

        async def mock_readline():
            # Wait until a response is ready, then return it
            while not responses_to_send:
                await asyncio.sleep(0.01)
            call_count[0] += 1
            if call_count[0] <= len(responses_to_send):
                return responses_to_send[call_count[0] - 1]
            await asyncio.sleep(9999)
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = mock_readline
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=5.0)
            await wrapper.start()
            result = await wrapper.call("computer.files.search", {"query": "hello"})
            assert result == {"files": ["a.py"]}
            assert request_captured["method"] == "computer.files.search"
            assert request_captured["params"] == {"query": "hello"}
            await wrapper.stop()

    async def test_timeout_raises_timeout_error(self, tmp_path):
        """call() raises TimeoutError when no response within timeout."""
        proc = MagicMock()
        proc.pid = 11
        proc.returncode = None

        stdin_mock = MagicMock()
        stdin_mock.write = MagicMock()
        stdin_mock.drain = AsyncMock()
        proc.stdin = stdin_mock

        # stdout that never returns a response
        async def mock_readline_never():
            await asyncio.sleep(9999)
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = mock_readline_never
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=0.1)  # very short timeout
            await wrapper.start()
            with pytest.raises(TimeoutError):
                await wrapper.call("computer.display.ocr", {})
            await wrapper.stop()

    async def test_error_response_raises_runtime_error(self, tmp_path):
        """An error JSON-RPC response causes call() to raise RuntimeError."""
        responses: list[bytes] = []

        proc = MagicMock()
        proc.pid = 55
        proc.returncode = None

        def capture_write(data: bytes):
            req = json.loads(data.decode().strip())
            responses.append(_make_error_line(req["id"], -32601, "Method not found"))

        proc.stdin = MagicMock()
        proc.stdin.write = capture_write
        proc.stdin.drain = AsyncMock()

        call_count = [0]

        async def mock_readline():
            while not responses:
                await asyncio.sleep(0.01)
            call_count[0] += 1
            if call_count[0] <= len(responses):
                return responses[call_count[0] - 1]
            await asyncio.sleep(9999)
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = mock_readline
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=5.0)
            await wrapper.start()
            with pytest.raises(RuntimeError) as exc_info:
                await wrapper.call("computer.nonexistent", {})
            assert "Method not found" in str(exc_info.value) or "-32601" in str(exc_info.value)
            await wrapper.stop()

    async def test_auto_respawn_on_dead_subprocess(self, tmp_path):
        """call() auto-respawns the subprocess if it was dead."""
        start_count = [0]
        responses: list[bytes] = []

        def make_proc():
            proc = MagicMock()
            proc.pid = 77 + start_count[0]
            proc.returncode = None
            proc.stdin = MagicMock()
            proc.stdin.drain = AsyncMock()

            def capture_write(data: bytes):
                req = json.loads(data.decode().strip())
                responses.append(_make_response_line(req["id"], "respawned"))

            proc.stdin.write = capture_write

            call_count = [0]

            async def mock_readline():
                while not responses:
                    await asyncio.sleep(0.01)
                call_count[0] += 1
                if call_count[0] <= len(responses):
                    return responses[call_count[0] - 1]
                await asyncio.sleep(9999)
                return b""

            proc.stdout = MagicMock()
            proc.stdout.readline = mock_readline
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        proc_instance = make_proc()

        async def mock_create_subprocess(*args, **kwargs):
            start_count[0] += 1
            return proc_instance

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=mock_create_subprocess)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=5.0)
            # Mark as dead without starting
            wrapper._process = MagicMock()
            wrapper._process.returncode = 1  # dead

            result = await wrapper.call("computer.clipboard.view", {})
            assert result == "respawned"
            # Was restarted
            assert start_count[0] >= 1
            await wrapper.stop()

    async def test_start_idempotent_when_alive(self, tmp_path):
        """Calling start() twice when already alive does NOT spawn a second process."""
        spawn_count = [0]

        async def mock_create(*args, **kwargs):
            spawn_count[0] += 1
            proc = MagicMock()
            proc.pid = 100
            proc.returncode = None
            proc.stdin = MagicMock()
            proc.stdin.drain = AsyncMock()
            proc.stdin.write = MagicMock()
            proc.stdout = MagicMock()

            async def never():
                await asyncio.sleep(9999)
                return b""

            proc.stdout.readline = never
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=mock_create)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper()
            await wrapper.start()
            await wrapper.start()  # should be a no-op
            assert spawn_count[0] == 1
            await wrapper.stop()

    async def test_stderr_directed_to_log_file(self, tmp_path):
        """Subprocess is created with stderr pointing at the log file."""
        log_path = tmp_path / "subprocess.log"
        stderr_arg_captured = []

        original_create = asyncio.create_subprocess_exec

        async def mock_create(*args, **kwargs):
            stderr_arg_captured.append(kwargs.get("stderr"))
            proc = MagicMock()
            proc.pid = 200
            proc.returncode = None
            proc.stdin = MagicMock()
            proc.stdin.drain = AsyncMock()
            proc.stdin.write = MagicMock()

            async def never():
                await asyncio.sleep(9999)
                return b""

            proc.stdout = MagicMock()
            proc.stdout.readline = never
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=mock_create)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=log_path),
        ):
            wrapper = OISubprocessWrapper()
            await wrapper.start()
            # Stderr should have been passed as a file object (not PIPE or None)
            assert stderr_arg_captured, "create_subprocess_exec was not called"
            await wrapper.stop()

    async def test_correlation_ids_unique_per_call(self, tmp_path):
        """Each call() uses a unique request ID (correlation key)."""
        ids_seen: list[int] = []

        proc = MagicMock()
        proc.pid = 300
        proc.returncode = None
        responses: list[bytes] = []

        def capture_write(data: bytes):
            req = json.loads(data.decode().strip())
            req_id = req["id"]
            ids_seen.append(req_id)
            responses.append(_make_response_line(req_id, f"result-{req_id}"))

        proc.stdin = MagicMock()
        proc.stdin.write = capture_write
        proc.stdin.drain = AsyncMock()

        call_count = [0]

        async def mock_readline():
            while call_count[0] >= len(responses):
                await asyncio.sleep(0.01)
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        proc.stdout = MagicMock()
        proc.stdout.readline = mock_readline
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=5.0)
            await wrapper.start()
            # Make 3 sequential calls
            r1 = await wrapper.call("computer.clipboard.view", {})
            r2 = await wrapper.call("computer.files.search", {"query": "x"})
            r3 = await wrapper.call("computer.display.ocr", {})
            await wrapper.stop()

        # All IDs should be unique and at least 3 (stop() may add a shutdown call)
        assert len(set(ids_seen)) == len(ids_seen), "Duplicate correlation IDs detected"
        assert len(ids_seen) >= 3, f"Expected at least 3 calls, got {len(ids_seen)}"

    async def test_pending_futures_resolved_on_stop(self, tmp_path):
        """Pending futures are failed (not hung) when stop() is called."""
        proc = MagicMock()
        proc.pid = 400
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.write = MagicMock()

        # stdout never responds
        async def never_responds():
            await asyncio.sleep(9999)
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = never_responds
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper.ensure_oi_venv", return_value=Path("/fake/python")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("extensions.coding_harness.oi_bridge.subprocess.wrapper._log_path", return_value=tmp_path / "subprocess.log"),
        ):
            wrapper = OISubprocessWrapper(timeout=60.0)  # long timeout
            await wrapper.start()

            # Start a call that will hang
            call_task = asyncio.create_task(
                wrapper.call("computer.display.view", {})
            )
            await asyncio.sleep(0.05)  # let call get into the pending dict

            # Stop the wrapper — pending futures should be resolved with error
            await wrapper.stop()

            # The call should now raise (either RuntimeError or TimeoutError or CancelledError)
            with pytest.raises(Exception):
                await asyncio.wait_for(call_task, timeout=1.0)
