"""Process-tree termination + orphan scanner for MCP subprocesses.

mcp-openclaw-port follow-up (Gap A). The MCP SDK ≥1.x already kills
the subprocess tree on graceful disconnect via
``mcp.client.stdio._terminate_process_tree`` (uses ``os.killpg`` after
spawning with ``start_new_session=True``). This module adds OC-level
**defence-in-depth** for the two cases the SDK doesn't cover:

* **Startup-time orphan cleanup.** A prior crashed OC run might have
  left behind ``npx`` / ``uvx`` MCP subprocesses with the original OC
  process as their parent (now dead, so they reparent to ``init``).
  At OC startup we scan for descendant processes whose argv0 matches
  an MCP command signature and SIGTERM them.

* **Crash-path defence.** If the SDK's cleanup path is interrupted
  (the owner task gets force-cancelled mid-``finally``), a subprocess
  could orphan. OC's ``MCPManager.shutdown`` calls this module after
  the SDK's path runs, so any straggler gets killed before the
  process exits.

API:

* :func:`is_mcp_command` — returns True for commands that look like
  an MCP server (``npx`` / ``uvx`` / ``python -m mcp_*`` etc.).
* :func:`find_mcp_descendants` — psutil walk filtered to MCP-named
  descendants of a given parent PID.
* :func:`kill_mcp_descendants` — SIGTERM-with-grace → SIGKILL
  escalation. Returns ``(n_terminated, n_killed)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

import psutil

logger = logging.getLogger("opencomputer.mcp.process_tree")

#: Command basenames that indicate an MCP server. Conservative —
#: false negatives mean a leftover survives one cycle; false positives
#: mean we'd kill someone else's process. We err on the side of false
#: negatives. Add new launcher names here as the ecosystem grows.
_MCP_LAUNCHER_BASENAMES: frozenset[str] = frozenset({
    "npx",   # npm/node MCP servers — @modelcontextprotocol/server-*
    "uvx",   # uv-tool ephemeral python MCP servers
    "pipx",  # pipx-installed MCP servers
    "bunx",  # bun ephemeral
})

#: Python module prefixes that indicate the subprocess is running an
#: MCP server via ``python -m <module>``. Conservative match — module
#: name must start with one of these.
_MCP_PYTHON_MODULE_PREFIXES: tuple[str, ...] = (
    "mcp_",
    "mcp.",
)


@dataclass(frozen=True, slots=True)
class DescendantInfo:
    """One descendant process matched as an MCP server.

    Used by :func:`find_mcp_descendants`. Returned for read-only
    inspection — callers use :func:`kill_mcp_descendants` to actually
    terminate.
    """

    pid: int
    argv0: str
    cmd_is_mcp: bool
    full_cmdline: tuple[str, ...]


def is_mcp_command(cmdline: list[str] | tuple[str, ...]) -> bool:
    """Heuristic — does this command look like an MCP server invocation?

    Used by the orphan scanner to filter the descendant set. Recognises:

    * ``npx`` / ``uvx`` / ``pipx`` / ``bunx`` launchers (by basename).
    * ``python -m mcp_<x>`` or ``python -m mcp.<x>`` patterns.

    Returns False for ``[]``, unrecognised commands, and Python invocations
    that point at non-MCP modules.
    """
    if not cmdline:
        return False
    head = cmdline[0]
    if not head:
        return False
    basename = PurePosixPath(head).name
    if basename in _MCP_LAUNCHER_BASENAMES:
        return True
    # Python-module pattern: cmdline like ``["python3", "-m", "mcp_x", ...]``
    if basename.startswith("python") and len(cmdline) >= 3 and cmdline[1] == "-m":
        module = cmdline[2]
        return any(
            module.startswith(prefix) for prefix in _MCP_PYTHON_MODULE_PREFIXES
        )
    return False


def find_mcp_descendants(parent_pid: int) -> list[DescendantInfo]:
    """Walk ``parent_pid``'s process tree, returning MCP-looking children.

    Errors during the walk (NoSuchProcess, AccessDenied, ZombieProcess)
    are swallowed — they mean the process disappeared mid-walk or we
    don't have permission, neither of which is a programming error.
    """
    out: list[DescendantInfo] = []
    try:
        parent = psutil.Process(parent_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []
    try:
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    for child in descendants:
        try:
            cmdline = child.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception as exc:  # noqa: BLE001 — psutil edge cases
            logger.debug(
                "psutil cmdline() raised for pid=%s: %s — skipping",
                getattr(child, "pid", "?"), exc,
            )
            continue
        if not cmdline:
            continue
        if not is_mcp_command(cmdline):
            continue
        out.append(DescendantInfo(
            pid=child.pid,
            argv0=cmdline[0],
            cmd_is_mcp=True,
            full_cmdline=tuple(cmdline),
        ))
    return out


def kill_mcp_descendants(
    parent_pid: int,
    *,
    graceful_seconds: float = 2.0,
) -> tuple[int, int]:
    """Terminate every MCP-looking descendant of ``parent_pid``.

    Three-phase termination per process:

    1. SIGTERM.
    2. Wait up to ``graceful_seconds`` for each to exit.
    3. SIGKILL any survivors.

    Returns ``(n_terminated, n_killed)`` — counts of processes that
    exited gracefully (SIGTERM took) vs needed SIGKILL escalation.
    Zero / zero when there are no MCP-named descendants.

    Idempotent and side-effect-free when the descendant set is empty.
    Safe to call repeatedly; safe to call with an invalid parent PID
    (returns ``(0, 0)``).
    """
    descendants = find_mcp_descendants(parent_pid)
    if not descendants:
        return (0, 0)
    n_terminated = 0
    n_killed = 0
    living: list[psutil.Process] = []
    for info in descendants:
        try:
            proc = psutil.Process(info.pid)
            proc.terminate()
            living.append(proc)
            logger.info(
                "SIGTERM sent to orphan MCP subprocess pid=%s argv0=%s",
                info.pid, info.argv0,
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SIGTERM failed for pid=%s (%s): %s",
                info.pid, info.argv0, exc,
            )
            continue
    if living:
        gone, alive = psutil.wait_procs(living, timeout=graceful_seconds)
        n_terminated = len(gone)
        for proc in alive:
            try:
                proc.kill()
                n_killed += 1
                logger.warning(
                    "SIGKILL escalated for pid=%s (SIGTERM grace=%ss elapsed)",
                    proc.pid, graceful_seconds,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                n_terminated += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SIGKILL failed for pid=%s: %s — leaking", proc.pid, exc,
                )
    return (n_terminated, n_killed)


__all__ = [
    "DescendantInfo",
    "find_mcp_descendants",
    "is_mcp_command",
    "kill_mcp_descendants",
]
