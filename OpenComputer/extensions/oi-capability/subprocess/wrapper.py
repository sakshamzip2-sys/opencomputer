"""OI subprocess wrapper — parent-side process manager and JSON-RPC client.

Spawns the OI subprocess server, manages its lifecycle, and exposes an async
``call()`` interface for sending JSON-RPC requests and receiving responses.

Design highlights:
- Newline-delimited JSON over stdin/stdout
- Per-call timeout (default 60 s)
- Correlation IDs for request/response matching
- Auto-respawn: if the subprocess is dead when ``call()`` is invoked, starts it
- Resource limit: RLIMIT_AS capped to 4 GB on Unix (skipped on Windows)
- stderr → log file at ``<_home() / "oi_capability" / "subprocess.log">``
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home

from .protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    ProtocolError,
)
from .venv_bootstrap import ensure_oi_venv

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0  # seconds per call
_RLIMIT_AS_BYTES = 4 * 1024 ** 3  # 4 GB address-space limit for subprocess

_id_counter = itertools.count(1)


def _log_path() -> Path:
    p = _home() / "oi_capability"
    p.mkdir(parents=True, exist_ok=True)
    return p / "subprocess.log"


def _make_preexec():
    """Return a preexec_fn that sets RLIMIT_AS, or None on Windows."""
    if sys.platform == "win32":
        return None

    def _set_rlimit():
        try:
            import resource  # noqa: PLC0415
            resource.setrlimit(
                resource.RLIMIT_AS,
                (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES),
            )
        except Exception:
            pass  # best-effort; don't crash the subprocess

    return _set_rlimit


class OISubprocessWrapper:
    """Manages the OI subprocess and provides a JSON-RPC client interface."""

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._log_file: Path = _log_path()
        self._pending: dict[int, asyncio.Future[JSONRPCResponse]] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the OI subprocess server."""
        if self._process is not None and self.is_alive():
            return

        python_bin = ensure_oi_venv()
        server_module = str(
            Path(__file__).parent / "server.py"
        )

        log_fd = self._log_file.open("ab")

        preexec = _make_preexec()
        kwargs: dict[str, Any] = {}
        if preexec is not None:
            kwargs["preexec_fn"] = preexec

        self._process = await asyncio.create_subprocess_exec(
            str(python_bin),
            server_module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fd,
            **kwargs,
        )
        logger.info(
            "OI subprocess started (pid=%d) — logs at %s",
            self._process.pid,
            self._log_file,
        )
        self._reader_task = asyncio.get_event_loop().create_task(
            self._read_loop(), name="oi-subprocess-reader"
        )

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result dict.

        Raises ``TimeoutError`` if no response within ``self._timeout`` seconds.
        Raises ``RuntimeError`` on JSON-RPC error responses.
        Auto-respawns the subprocess if it died between calls.
        """
        if not self.is_alive():
            logger.info("OI subprocess not alive — auto-respawning")
            await self.start()

        req_id = next(_id_counter)
        request = JSONRPCRequest(method=method, id=req_id, params=params or {})

        loop = asyncio.get_event_loop()
        future: asyncio.Future[JSONRPCResponse] = loop.create_future()

        async with self._lock:
            self._pending[req_id] = future
            if self._process is None or self._process.stdin is None:
                del self._pending[req_id]
                raise RuntimeError("OI subprocess stdin not available")
            line = (request.to_json() + "\n").encode()
            self._process.stdin.write(line)
            await self._process.stdin.drain()

        try:
            response = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(
                f"OI subprocess did not respond to '{method}' within {self._timeout}s"
            )

        if response.is_error and response.error is not None:
            raise RuntimeError(
                f"OI subprocess error [{response.error.code}] "
                f"{response.error.message}: {response.error.data}"
            )

        return response.result or {}

    async def stop(self) -> None:
        """Gracefully shut down the subprocess, with SIGTERM fallback."""
        if self._process is None:
            return

        if self.is_alive():
            try:
                await asyncio.wait_for(
                    self.call("shutdown", {}), timeout=5.0
                )
            except Exception:
                pass

        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._process is not None:
            if self.is_alive():
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except Exception:
                    self._process.kill()
            self._process = None

        # Resolve any pending futures with a cancellation error
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("OI subprocess stopped"))
        self._pending.clear()

    def is_alive(self) -> bool:
        """Return True if the subprocess is running."""
        if self._process is None:
            return False
        return self._process.returncode is None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Background task: read newline-delimited JSON from subprocess stdout."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    # EOF — subprocess exited
                    logger.warning("OI subprocess stdout closed (EOF)")
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    response = JSONRPCResponse.from_json(line)
                except ProtocolError as exc:
                    logger.error("OI protocol error reading response: %s", exc)
                    continue

                future = self._pending.pop(response.id, None)
                if future is not None and not future.done():
                    future.set_result(response)
                else:
                    logger.warning(
                        "OI response id=%d has no pending caller (dropped)", response.id
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("OI reader loop crashed: %s", exc, exc_info=True)
        finally:
            # Fail all pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("OI subprocess reader exited"))
            self._pending.clear()


__all__ = ["OISubprocessWrapper"]
