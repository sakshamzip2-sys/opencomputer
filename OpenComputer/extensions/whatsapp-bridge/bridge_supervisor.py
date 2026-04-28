"""Cross-platform Node-bridge subprocess supervisor (PR 6.2).

Spawns the Baileys bridge as a Node child process and owns its
lifecycle. Handles two cross-platform pain points the WhatsApp adapter
can't ignore:

1. **Process-group kill on shutdown.** A `SIGTERM` to the parent
   doesn't reliably reap Node's children (Baileys spins up workers).
   On POSIX we put the child in its own session
   (``start_new_session=True``) and `os.killpg(SIGTERM)` on the whole
   group. On Windows we set
   ``CREATE_NEW_PROCESS_GROUP`` and shell out to ``taskkill /T /F``,
   which terminates the entire descendant tree.

2. **Stale-port reaper.** A previous bridge that crashed without
   cleaning up may still be holding the port. Before spawning we run
   ``_kill_port_process`` to find and force-kill any listener — this
   keeps adapter restarts deterministic.

The supervisor is intentionally agnostic about *what* the bridge does
— it just owns ``Popen`` + ``stdout`` capture so the caller (the
adapter) can read the QR text and health URL from stdout lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.ext.whatsapp_bridge.supervisor")


_IS_WINDOWS = sys.platform == "win32"


def _kill_port_process(
    port: int,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> list[int]:
    """Find and kill any process listening on *port*.

    Returns the list of PIDs killed (empty on no-op or failure). Uses
    ``lsof -ti tcp:<port>`` on POSIX and ``netstat`` + ``taskkill /F``
    on Windows. *runner* lets unit tests inject a fake subprocess
    runner without touching the real shell.
    """
    if runner is None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

    pids: list[int] = []
    try:
        if _IS_WINDOWS:
            # netstat -ano outputs lines like:
            #   TCP 127.0.0.1:3001 0.0.0.0:0 LISTENING 1234
            res = runner(["netstat", "-ano"])
            for line in (res.stdout or "").splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            pids.append(int(parts[-1]))
                        except ValueError:
                            continue
            for pid in pids:
                runner(["taskkill", "/F", "/T", "/PID", str(pid)])
        else:
            # lsof -ti tcp:<port> prints one PID per line.
            res = runner(["lsof", "-ti", f"tcp:{port}"])
            for line in (res.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid = int(line)
                except ValueError:
                    continue
                pids.append(pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError) as e:
                    logger.debug("kill_port_process: pid %s: %s", pid, e)
    except FileNotFoundError as e:
        # lsof / netstat missing — best-effort, not fatal.
        logger.debug("kill_port_process: tool missing: %s", e)
    except subprocess.TimeoutExpired:
        logger.warning("kill_port_process: timed out polling port %s", port)
    return pids


class BridgeSupervisor:
    """Owns the Node bridge subprocess.

    The adapter creates this once per ``connect()`` and tears it down on
    ``disconnect()``. Stdout from the bridge is captured (line-buffered)
    and exposed via ``stdout_lines()`` so the adapter can scrape QR-text
    + health hints during startup.
    """

    def __init__(
        self,
        *,
        bridge_dir: str | Path,
        host: str,
        port: int,
        auth_dir: str | Path,
        node_bin: str = "node",
    ) -> None:
        self.bridge_dir = Path(bridge_dir)
        self.host = host
        self.port = port
        self.auth_dir = Path(auth_dir)
        self.node_bin = node_bin
        self._proc: subprocess.Popen | None = None
        self._stdout_buffer: list[str] = []
        self._reader_task: asyncio.Task | None = None

    def _spawn_kwargs(self) -> dict[str, Any]:
        """Platform-specific kwargs for ``Popen`` so the kill path works."""
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "bufsize": 1,
            "text": True,
        }
        if _IS_WINDOWS:
            # CREATE_NEW_PROCESS_GROUP makes the child the root of its own
            # group so taskkill /T /F can sweep it cleanly.
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            # start_new_session = setsid() in the child so SIGTERM to the
            # whole pgid (via os.killpg) reaps any grandchildren too.
            kwargs["start_new_session"] = True
        return kwargs

    def spawn(self) -> subprocess.Popen:
        """Start the Node bridge. Caller must check ``proc.poll()``."""
        # Reap any stale listener BEFORE spawning so the new process can
        # actually bind. We swallow errors — a failed reap doesn't
        # necessarily mean the port is held.
        _kill_port_process(self.port)
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "WHATSAPP_BRIDGE_PORT": str(self.port),
                "WHATSAPP_BRIDGE_HOST": self.host,
                "WHATSAPP_BRIDGE_AUTH_DIR": str(self.auth_dir),
            }
        )
        argv = [self.node_bin, "index.js"]
        logger.info(
            "whatsapp-bridge: spawning %s (cwd=%s host=%s port=%s)",
            " ".join(argv),
            self.bridge_dir,
            self.host,
            self.port,
        )
        self._proc = subprocess.Popen(
            argv,
            cwd=str(self.bridge_dir),
            env=env,
            **self._spawn_kwargs(),
        )
        return self._proc

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stdout_lines(self) -> list[str]:
        """Snapshot of buffered stdout lines (used to scrape QR text)."""
        return list(self._stdout_buffer)

    def append_stdout(self, line: str) -> None:
        """Append a single line to the stdout buffer (called by reader)."""
        self._stdout_buffer.append(line)
        # Cap the buffer so a chatty bridge doesn't unbound-grow memory.
        if len(self._stdout_buffer) > 500:
            del self._stdout_buffer[:250]

    def terminate(self, *, timeout: float = 5.0) -> None:
        """Cross-platform clean termination of the bridge."""
        if self._proc is None or self._proc.poll() is not None:
            return
        pid = self._proc.pid
        try:
            if _IS_WINDOWS:
                # taskkill /T /F walks the whole tree.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
            else:
                # killpg on the session/pgid we created via setsid.
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    # Fall back to the process directly.
                    self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "whatsapp-bridge: pid %s didn't exit in %.1fs — SIGKILL",
                    pid,
                    timeout,
                )
                if _IS_WINDOWS:
                    # taskkill /F should already be hard — nothing more to do
                    pass
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        self._proc.kill()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "whatsapp-bridge: pid %s still alive after SIGKILL", pid
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("whatsapp-bridge: terminate failed: %s", e)


__all__ = ["BridgeSupervisor", "_kill_port_process"]
