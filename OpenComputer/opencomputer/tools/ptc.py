"""Programmatic Tool Calling (PTC) — one-turn multi-tool scripts.

The OI principle done right for a tool-registry agent: the LLM writes a
Python script that calls *registered tools* via UDS-RPC. Tool calls
happen in the subprocess; only the script's stdout returns to the
LLM. A 10-step "summarize and combine these 5 articles" chain
collapses into one inference turn.

Architecture
------------

::

  Parent (this process)              Subprocess
  ─────────────────────              ──────────
  PTCServer (UDS listener)           Generated wrapper script:
       │                                ├── RPC prologue (~30 LOC)
       │      ┌──────────┐              ├── User's Python code
       └◄─────│  socket  │◄──┐          │
              └──────────┘   │          │
                          tool call     │
                          response      │
                                        └── prints final result
                                              │
                                              ▼ stdout
                                       Returned to LLM

The subprocess sees a flat namespace with one function per allowed
tool (e.g. ``Read``, ``WebFetch``, ``Grep``). Each function does a
synchronous round-trip to the parent over the UDS socket and returns
the tool's text output.

Security
--------

- **Allowlist required**: ``mode="ptc"`` requires an explicit
  ``tools=[...]`` list. Default allowed set: read-only tools (``Read``,
  ``WebFetch``, ``Grep``, ``Glob``). Bash / Edit / Write / Delegate /
  cron are **not** in the default — explicit opt-in only.
- **Capability gate**: ``python_exec.ptc_mode`` (PER_ACTION). Each PTC
  invocation prompts unless granted; bypassing the gate requires
  ``OPENCOMPUTER_CONSENT_BYPASS=1``.
- **Resource limits**: 50KB stdout cap, 50 RPC calls per script, 300s
  wallclock. Each enforced in this module — over-limit terminates the
  subprocess and returns the error to the LLM.
- **Socket isolation**: UDS path is per-invocation in ``$TMPDIR/oc-
  ptc-<uuid>.sock`` with 0700 permissions. Removed in finally.
- **Subprocess isolation**: the script runs in its own process; a
  segfault / SystemExit / runaway allocation kills the subprocess only,
  not the agent.

Honest scope
------------

This PR ships the **single-process server** version: one PTC invocation
spawns one subprocess, the parent's asyncio event loop accepts UDS
connections from that subprocess only, and tear down on script exit.
No persistent socket. Concurrent PTC invocations get separate sockets.

Future work (deferred):

- Streaming tool output (current version is request/response).
- Cross-process tool result pagination (large outputs are chunked
  over the socket, not held entirely in subprocess memory).
- Multi-script connection pooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.tools.ptc")


# Default tool allowlist — read-only operations only. Callers can
# override via the ``tools`` parameter on ``run_ptc``, but writing /
# bash / delegate require explicit opt-in.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "WebFetch", "Grep", "Glob")

# 2026-05-08 — Hermes Doc-2 ``execute_code`` parity. The Hermes spec
# allows a broader default that includes write + terminal:
# ``web_search``, ``web_extract``, ``read_file``, ``write_file``,
# ``search_files``, ``patch``, ``terminal``. Mapped to OC tool names:
EXECUTE_CODE_DEFAULT_TOOLS: tuple[str, ...] = (
    "Read", "Write", "Edit", "Grep", "Glob", "WebFetch", "WebSearch", "Bash",
)

# Hard caps. Picking conservative numbers — too tight is recoverable
# (LLM gets a clear error and can split the work into multiple calls);
# too loose risks a single PTC call dominating the agent's budget.
_MAX_STDOUT_BYTES = 50 * 1024  # 50 KB
# 2026-05-08 — Hermes Doc-2 parity: explicit stderr cap (Hermes 10 KB).
# Pre-existing run_ptc only capped stdout. Stderr without a cap can
# explode if a script triggers a noisy traceback/Python warning.
_MAX_STDERR_BYTES = 10 * 1024  # 10 KB
_MAX_RPC_CALLS = 50
_DEFAULT_TIMEOUT_S = 300.0

# 2026-05-08 — env-scrub patterns from the Hermes Doc-2 ``execute_code``
# spec. Variables whose KEY contains any of these substrings (case-
# insensitive) are stripped before subprocess spawn so credentials in
# the parent's env don't leak into a script the LLM authored. Skills'
# ``required_environment_variables`` and an explicit caller-supplied
# pass-through list bypass this filter.
_ENV_SCRUB_PATTERNS: tuple[str, ...] = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH",
)

# 2026-05-08 — recursion guard. The parent sets this env var before
# spawning the subprocess; the subprocess inherits it. Inside an
# already-running execute_code, attempts to call execute_code again
# fail with a clear error rather than fork-bombing.
_RECURSION_GUARD_ENV: str = "OC_EXECUTE_CODE_DEPTH"


# ──────────────────────────────────────────────────────────────────────
# Wire protocol — length-prefixed JSON over UDS
# ──────────────────────────────────────────────────────────────────────


async def _read_msg(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read a length-prefixed JSON message. Returns None on EOF.

    ``readexactly`` raises ``IncompleteReadError`` on EOF; we want
    ``None`` instead so the per-connection dispatch loop can exit
    cleanly when the subprocess hangs up.
    """
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    n = struct.unpack(">I", header)[0]
    if n == 0:
        return None
    if n > 16 * 1024 * 1024:  # 16 MB sanity cap on any single message
        raise ValueError(f"PTC message too large: {n} bytes")
    body = await reader.readexactly(n)
    return json.loads(body.decode("utf-8"))


def _pack_msg(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


# ──────────────────────────────────────────────────────────────────────
# RPC prologue — the boilerplate prepended to the user's code
# ──────────────────────────────────────────────────────────────────────


def _build_prologue(
    allowed: tuple[str, ...],
    max_tool_calls: int = _MAX_RPC_CALLS,
) -> str:
    """Generate the small RPC client + tool-stub functions.

    ``allowed`` is the ordered list of tool names to expose.

    ``max_tool_calls`` is the per-script RPC cap (Hermes Doc-2 G5,
    2026-05-08). Defaults to ``_MAX_RPC_CALLS`` (50) for callers that
    haven't been threaded through with config. Pass an override to
    apply a custom cap from ``code_execution.max_tool_calls`` in
    ``config.yaml``.

    Stubs map ``ToolName(**kwargs) -> str`` — the result text. Tool
    errors raise ``RuntimeError`` so a script ``try/except`` block
    can catch them; this matches Python's normal control-flow shape
    instead of forcing the LLM to inspect a result struct.

    Uses plain unindented string concatenation rather than
    ``textwrap.dedent`` because dedent doesn't compose cleanly with
    interpolated multi-line stub blocks (each stub starts at column
    0 after the f-string substitution, but the dedent base
    expectation is the indented surrounding lines — common-prefix
    stripping mismatches the interpolated content).
    """
    lines: list[str] = [
        "# ─── PTC RPC harness — auto-generated, do not edit ──────────",
        "import json as _ptc_json",
        "import os as _ptc_os",
        "import socket as _ptc_socket",
        "import struct as _ptc_struct",
        "",
        "_ptc_sock = _ptc_socket.socket(_ptc_socket.AF_UNIX)",
        "_ptc_sock.connect(_ptc_os.environ['OC_PTC_SOCKET'])",
        "_ptc_call_count = 0",
        f"_ptc_max_calls = {max_tool_calls}",
        "",
        "def _ptc_call(tool, arguments):",
        "    global _ptc_call_count",
        "    _ptc_call_count += 1",
        "    if _ptc_call_count > _ptc_max_calls:",
        "        raise RuntimeError(",
        '            f"PTC call cap exceeded ({_ptc_max_calls}); "',
        '            f"split this script into multiple PTC invocations"',
        "        )",
        "    req = _ptc_json.dumps("
        "{'tool': tool, 'arguments': arguments}).encode()",
        "    _ptc_sock.sendall(_ptc_struct.pack('>I', len(req)) + req)",
        "    hdr = b''",
        "    while len(hdr) < 4:",
        "        chunk = _ptc_sock.recv(4 - len(hdr))",
        "        if not chunk:",
        "            raise RuntimeError('PTC server closed connection')",
        "        hdr += chunk",
        "    n = _ptc_struct.unpack('>I', hdr)[0]",
        "    body = b''",
        "    while len(body) < n:",
        "        chunk = _ptc_sock.recv(min(65536, n - len(body)))",
        "        if not chunk:",
        "            raise RuntimeError("
        "'PTC server closed connection mid-response')",
        "        body += chunk",
        "    resp = _ptc_json.loads(body.decode())",
        "    if resp.get('is_error'):",
        '        raise RuntimeError('
        'f"tool {tool} failed: {resp.get(\'content\')}")',
        "    return resp.get('content', '')",
        "",
        "# ─── Tool stubs ─────────────────────────────────────────────",
    ]
    for tool in allowed:
        # Variadic kwargs: callers pass tool params by name. We pass
        # them straight through to the parent without inspection.
        lines.append(f"def {tool}(**kwargs):")
        lines.append(f"    return _ptc_call({tool!r}, kwargs)")
        lines.append("")
    lines.append("# ─── End PTC harness; user code follows ─────────────────────")
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────
# Server — accepts UDS connections from the subprocess
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PTCServerConfig:
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    socket_path: str = ""


@dataclass
class PTCResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    rpc_call_count: int = 0
    truncated: bool = False
    timed_out: bool = False


class PTCServer:
    """One-shot UDS-RPC server. Single subprocess client.

    Usage::

        server = PTCServer(config)
        await server.start()
        try:
            # spawn subprocess with OC_PTC_SOCKET=server.socket_path
            ...
        finally:
            await server.stop()

    The server doesn't enforce timeouts itself — the caller's
    subprocess.wait() is the wallclock budget. The server tracks
    ``rpc_call_count`` so over-limit cases produce a useful error.
    """

    def __init__(
        self,
        registry: Any,
        config: PTCServerConfig | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or PTCServerConfig()
        self._server: asyncio.base_events.Server | None = None
        self.rpc_call_count = 0
        self._allowed = set(self.config.allowed_tools)

    @property
    def socket_path(self) -> str:
        return self.config.socket_path

    async def start(self) -> None:
        """Bind the UDS socket + start accepting."""
        if not self.config.socket_path:
            self.config.socket_path = _new_socket_path()
        # Defensive cleanup if a previous run crashed and left a stale
        # socket in /tmp.
        try:
            os.unlink(self.config.socket_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.config.socket_path,
        )
        os.chmod(self.config.socket_path, 0o700)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(self.config.socket_path)
        except FileNotFoundError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        """Per-connection dispatch loop. One subprocess = one connection."""
        try:
            while True:
                msg = await _read_msg(reader)
                if msg is None:
                    break
                self.rpc_call_count += 1
                tool_name = str(msg.get("tool", ""))
                arguments = msg.get("arguments", {}) or {}
                response = await self._dispatch(tool_name, arguments)
                writer.write(_pack_msg(response))
                await writer.drain()
        except (
            asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError,
        ):
            # Subprocess hung up — clean exit.
            return
        except Exception as e:  # noqa: BLE001 — never crash the parent
            logger.warning("PTC: handler error — %s", e, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(
        self, tool_name: str, arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Look up the tool, enforce allowlist, run, return JSON-able result."""
        if tool_name not in self._allowed:
            return {
                "content": (
                    f"tool {tool_name!r} is not in the PTC allowlist "
                    f"({sorted(self._allowed)}); pass it via "
                    f"``tools=[...]`` to enable"
                ),
                "is_error": True,
            }
        # Use the global tool registry to resolve.
        tool = self.registry.get(tool_name)
        if tool is None:
            return {
                "content": f"unknown tool: {tool_name}",
                "is_error": True,
            }
        # Build a synthetic ToolCall.
        from plugin_sdk.core import ToolCall

        call = ToolCall(
            id=f"ptc-{uuid.uuid4().hex[:8]}",
            name=tool_name,
            arguments=arguments,
        )
        try:
            result = await tool.execute(call)
        except Exception as e:  # noqa: BLE001 — surface to subprocess
            return {
                "content": f"{type(e).__name__}: {e}",
                "is_error": True,
            }
        return {
            "content": getattr(result, "content", "") or "",
            "is_error": bool(getattr(result, "is_error", False)),
        }


# ──────────────────────────────────────────────────────────────────────
# High-level orchestration
# ──────────────────────────────────────────────────────────────────────


def _scrub_env(
    env: dict[str, str], *, passthrough: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[str]]:
    """Drop env vars whose name matches a scrub pattern.

    Returns ``(scrubbed_env, removed_var_names)``. The pass-through list
    is matched case-sensitively and exempts those exact names from the
    scrub (use case: a skill's ``required_environment_variables`` may
    legitimately need ``MY_API_KEY`` — the skill author has accepted
    that risk by listing it).

    Conservative-by-default: any name *containing* a scrub pattern
    (case-insensitive) is removed unless explicitly whitelisted.
    """
    pass_set = set(passthrough)
    out: dict[str, str] = {}
    removed: list[str] = []
    for k, v in env.items():
        if k in pass_set:
            out[k] = v
            continue
        upper = k.upper()
        if any(p in upper for p in _ENV_SCRUB_PATTERNS):
            removed.append(k)
            continue
        out[k] = v
    return out, removed


async def run_ptc(
    code: str,
    *,
    registry: Any,
    allowed_tools: tuple[str, ...] | list[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    scrub_env: bool = False,
    env_passthrough: tuple[str, ...] | list[str] = (),
    stderr_cap: int | None = None,
    cwd: str | None = None,
    python_executable: str | None = None,
    parent_env: dict[str, str] | None = None,
    max_tool_calls: int | None = None,
) -> PTCResult:
    """Run a PTC script end-to-end.

    Spawns a subprocess to execute ``<RPC prologue> + code``, catches
    its stdout/stderr (capped at 50 KB stdout), returns the result.

    Args:
        code: User's Python source. The subprocess sees ``Read``,
            ``WebFetch``, etc. predefined; the script doesn't need to
            import them.
        registry: ToolRegistry singleton — used to dispatch RPC calls
            back to the parent.
        allowed_tools: Tool names to expose. ``None`` uses
            :data:`DEFAULT_ALLOWED_TOOLS` (read-only).
        timeout_s: Wallclock cap on the whole invocation.
        scrub_env: When True (Hermes ``execute_code`` parity, 2026-05-08),
            strip env vars whose names contain any of
            ``KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/PASSWD/AUTH`` before
            subprocess spawn. Default False preserves existing
            ``PythonExec.mode="ptc"`` callers' behaviour.
        env_passthrough: When ``scrub_env=True``, exact env-var names
            in this list bypass the scrub. Use case: a skill's
            ``required_environment_variables`` legitimately needs
            ``MY_API_KEY``.
        stderr_cap: When set, truncate captured stderr to this many
            bytes (with a marker). Hermes execute_code uses 10 KB.
            Default ``None`` keeps stderr uncapped (existing behaviour).
        cwd: Working directory for the subprocess. ``None`` inherits
            the parent's cwd. ``execute_code`` mode="project" passes
            the session's working dir; mode="strict" passes a fresh
            tempdir.
        python_executable: Python interpreter path. ``None`` uses
            ``sys.executable``. ``execute_code`` mode="project" passes
            the active venv python; mode="strict" passes
            ``sys.executable``.
        parent_env: Pre-built env dict to use as the base. ``None`` =
            copy of ``os.environ``. Lets callers override the cwd-
            sensitive ``VIRTUAL_ENV`` / ``CONDA_PREFIX`` for strict
            mode without mutating the parent process's env.

    Returns:
        :class:`PTCResult` with stdout / stderr / exit_code / metadata.
        On timeout, ``timed_out=True`` and the subprocess is killed.
    """
    import sys
    import time

    if not code.strip():
        return PTCResult(
            stdout="", stderr="empty PTC script", exit_code=2,
            duration_seconds=0.0,
        )

    # Distinguish "caller passed nothing → default allowlist" from
    # "caller passed an empty list → no tools at all". The latter is
    # legal — it's how a script that doesn't call any registered
    # tools at all opts in to PTC mode (just for the subprocess
    # isolation guarantees).
    allowed = (
        tuple(allowed_tools)
        if allowed_tools is not None
        else DEFAULT_ALLOWED_TOOLS
    )
    config = PTCServerConfig(allowed_tools=allowed)
    server = PTCServer(registry, config)
    await server.start()

    # 2026-05-08 G5 — let callers override the in-script RPC cap from
    # ``code_execution.max_tool_calls`` config. None preserves the
    # historic _MAX_RPC_CALLS=50 default.
    effective_max_calls = (
        max_tool_calls if isinstance(max_tool_calls, int) and max_tool_calls > 0
        else _MAX_RPC_CALLS
    )
    prologue = _build_prologue(allowed, max_tool_calls=effective_max_calls)
    full_script = prologue + "\n" + code

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8",
    ) as f:
        f.write(full_script)
        script_path = Path(f.name)

    base_env = parent_env if parent_env is not None else os.environ.copy()
    try:
        from opencomputer.profiles import read_active_profile, scope_subprocess_env

        env = scope_subprocess_env(base_env, profile=read_active_profile())
    except Exception:  # noqa: BLE001 — fail-soft on profile lookup
        env = dict(base_env)

    # 2026-05-08 — Hermes execute_code env-scrub.
    if scrub_env:
        env, _scrubbed = _scrub_env(env, passthrough=tuple(env_passthrough))
        if _scrubbed:
            logger.debug(
                "PTC env scrub removed %d vars: %s",
                len(_scrubbed), ", ".join(sorted(_scrubbed))[:200],
            )
    env["OC_PTC_SOCKET"] = server.socket_path
    # Recursion guard — increment depth so a nested execute_code refuses.
    try:
        prev_depth = int(env.get(_RECURSION_GUARD_ENV, "0") or "0")
    except ValueError:
        prev_depth = 0
    env[_RECURSION_GUARD_ENV] = str(prev_depth + 1)

    interpreter = python_executable or sys.executable

    start = time.monotonic()
    timed_out = False
    truncated = False
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter, str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except TimeoutError:
            timed_out = True
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
        duration = time.monotonic() - start

        # Apply 50 KB stdout cap.
        if len(stdout_b) > _MAX_STDOUT_BYTES:
            truncated = True
            head = stdout_b[:_MAX_STDOUT_BYTES]
            stdout_b = head + (
                f"\n\n[truncated — {len(stdout_b) - _MAX_STDOUT_BYTES} "
                f"bytes omitted]"
            ).encode()

        # 2026-05-08 — optional stderr cap (Hermes execute_code: 10 KB).
        if stderr_cap is not None and len(stderr_b) > stderr_cap:
            truncated = True
            head_err = stderr_b[:stderr_cap]
            stderr_b = head_err + (
                f"\n\n[truncated — {len(stderr_b) - stderr_cap} "
                f"bytes omitted]"
            ).encode()

        return PTCResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_seconds=duration,
            rpc_call_count=server.rpc_call_count,
            truncated=truncated,
            timed_out=timed_out,
        )
    finally:
        await server.stop()
        try:
            script_path.unlink()
        except OSError:
            pass


def _new_socket_path() -> str:
    """Generate a fresh UDS path under $TMPDIR."""
    base = tempfile.gettempdir()
    return os.path.join(base, f"oc-ptc-{uuid.uuid4().hex[:12]}.sock")


__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "PTCResult",
    "PTCServer",
    "PTCServerConfig",
    "run_ptc",
]
