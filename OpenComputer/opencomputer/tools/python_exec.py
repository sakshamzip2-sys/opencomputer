"""PythonExec — sandboxed Python script execution + PTC mode.

Two modes:

- **Default ``mode="plain"``**: runs the script in a subprocess so a
  SystemExit / runaway allocation in the script doesn't kill the
  agent. Output (stdout + stderr) is captured and returned. The
  denylist (python_safety) blocks obvious abuse patterns pre-spawn.

- **``mode="ptc"``** (Tier-A item 8): Programmatic Tool Calling. The
  subprocess gets a small RPC harness prepended to its code so it
  can call OC's registered tools (Read, WebFetch, Grep, Glob by
  default — caller can extend via ``tools=[...]``). Each tool call
  is a synchronous round-trip to the parent over a per-invocation
  Unix domain socket. Only the script's stdout returns to the LLM —
  intermediate tool results never enter the conversation context.

  Use case: "summarize and combine these 5 articles" collapses from
  10 round-trips (5 fetches + interpretation + summary) to ONE
  inference turn. The script does the orchestration; the LLM sees
  the final answer.

Sandbox routing (M5, sandbox-provider-breadth)
----------------------------------------------

``mode="plain"`` routes through the resolved sandbox backend when
``sandbox.backend`` is configured — the script runs as ``python3 -c
<code>`` inside the backend (Docker / e2b / daytona / modal / …). With
no backend configured the host subprocess path runs, byte-identically.

``mode="ptc"`` is deliberately **NOT** routed. PTC's subprocess calls
back into the host tool registry over a per-invocation UDS socket — a
remote sandbox cannot reach that socket, and a local one would only
expose the privileged host. "Run isolated" and "call back into the
trusted host" are opposing goals; PTC keeps its own consent gate +
tool allowlist as its safety surface.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from opencomputer.security.python_safety import is_safe_script
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.tools.python_exec")

#: M5 (sandbox-provider-breadth) — ``runtime.custom`` keys the agent loop
#: publishes the resolved sandbox backend + pooled-container key under.
#: Kept in sync with ``opencomputer.tools.bash`` / ``agent.loop``.
_SANDBOX_STRATEGY_KEY = "sandbox_backend_strategy"
_SANDBOX_CONTAINER_KEY = "sandbox_container_key"


class PythonExec(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Run a Python script in a subprocess; capture stdout + stderr.

    With ``mode="ptc"`` the subprocess can call OC's registered tools
    via UDS-RPC — see module docstring for the orchestration value.
    """

    consent_tier: int = 2
    #: NEVER parallel — also pinned in ``HARDCODED_NEVER_PARALLEL``
    #: (``agent.loop``). M5 routes plain mode through a per-call sandbox
    #: backend published on the SHARED ``runtime.custom`` by
    #: ``AgentLoop._resolve_sandbox_backend``; two concurrent PythonExec
    #: dispatches would clobber each other's resolved backend — in the
    #: worst case dropping a sandbox-required call onto the bare host (a
    #: containment-escape race). Sequential dispatch makes the
    #: publish-then-consume atomic, exactly as for ``BashTool``.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="python_exec.run",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Execute a Python script in a subprocess.",
        ),
        CapabilityClaim(
            capability_id="python_exec.ptc_mode",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Programmatic Tool Calling — script can call OC tools via "
                "RPC. Higher trust tier than plain execution."
            ),
        ),
    )

    #: M5 — class-level "current runtime", set by ``AgentLoop`` before each
    #: tool-dispatch batch (same pattern as ``BashTool._current_runtime``).
    #: ``_execute_plain`` reads ``custom['sandbox_backend_strategy']`` off it
    #: to decide whether to route the script through a sandbox backend. The
    #: shared no-flags default leaves the key absent → the host path runs
    #: (the byte-identical no-op default).
    _current_runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT

    @classmethod
    def set_runtime(cls, runtime: RuntimeContext) -> None:
        """Set the runtime context. Called by ``AgentLoop`` alongside
        ``BashTool.set_runtime`` — the live ``RuntimeContext`` is passed by
        reference so the per-call ``sandbox_backend_strategy`` write is
        visible to :meth:`_execute_plain`.
        """
        cls._current_runtime = runtime

    def _runtime_custom(self) -> dict[str, object]:
        """Return ``runtime.custom`` (or ``{}``). Read defensively — a
        missing / malformed runtime must never break execution."""
        custom = getattr(self._current_runtime, "custom", None)
        return custom if isinstance(custom, dict) else {}

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PythonExec",
            description=(
                "Run a multi-line Python script in a subprocess; returns stdout+stderr "
                "and exit code. Use for ad-hoc data analysis (pandas, sklearn, numpy), "
                "JSON transforms, regex experiments, throwaway computations — anything "
                "where `Bash python3 -c '...'` quoting would be painful. Prefer this over "
                "Bash for non-trivial Python: multi-line scripts execute cleanly and the "
                "denylist (os.system, subprocess, eval, exec, .ssh access) is reviewed "
                "pre-spawn so obvious foot-guns are caught early.\n\n"
                "PTC MODE (mode='ptc'): your script gets predefined functions for "
                "OC's tools — Read(file_path), WebFetch(url), Grep(pattern, path), "
                "Glob(pattern). Each call returns the tool's text output as a string; "
                "errors raise RuntimeError. Use this when you need to orchestrate "
                "MULTIPLE tool calls that condition on each other in ONE inference "
                "turn — e.g. 'fetch these 5 URLs in parallel and combine'. Only the "
                "script's final stdout is returned to you; intermediate tool outputs "
                "never enter context. Default tools list is read-only (Read, WebFetch, "
                "Grep, Glob); pass `tools=['Read','Bash']` to expand. CAUTION: each "
                "PTC invocation requires per-action consent. CAPS: 50 RPC calls per "
                "script, 50 KB stdout, 300s wallclock."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to execute. Must not contain denylisted patterns.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["plain", "ptc"],
                        "description": (
                            "'plain' = subprocess, no tool RPC; 'ptc' = "
                            "subprocess with OC tool stubs predefined."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "PTC mode only — explicit tool allowlist. Default "
                            "(if omitted): Read, WebFetch, Grep, Glob."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Wall-clock timeout. Default 30s; PTC mode allows up to 300s.",
                    },
                },
                "required": ["code"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        code = str(call.arguments.get("code", ""))
        mode = str(call.arguments.get("mode", "plain")).lower()
        timeout = float(call.arguments.get("timeout_seconds", 30.0))

        if mode == "ptc":
            return await self._execute_ptc(call, code, timeout)
        return await self._execute_plain(call, code, timeout)

    async def _execute_plain(
        self, call: ToolCall, code: str, timeout: float,
    ) -> ToolResult:
        if not is_safe_script(code):
            return ToolResult(
                tool_call_id=call.id,
                content="Script rejected by denylist (unsafe pattern detected). Avoid os.system, subprocess, eval, exec, /.ssh/, etc.",
                is_error=True,
            )

        # M5: route plain-mode execution through the resolved sandbox
        # backend when one is configured. The denylist above still runs
        # (defense in depth — sandboxing is *additional* containment). With
        # no ``sandbox.backend`` configured the key is absent, ``strategy``
        # is None, and the host path below runs byte-identically.
        strategy = self._runtime_custom().get(_SANDBOX_STRATEGY_KEY)
        if strategy is not None:
            sandboxed = await self._run_plain_in_sandbox(
                call, code, timeout, strategy
            )
            if sandboxed is not None:
                return sandboxed
            # Backend unreachable + sandbox.fallback=local → fall through
            # to the host path below.

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(code)
            script_path = Path(f.name)

        try:
            from opencomputer.profiles import read_active_profile, scope_subprocess_env

            env = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
        except Exception:  # noqa: BLE001 — fail-soft on profile lookup
            env = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Timed out after {timeout}s",
                    is_error=True,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            combined = (stdout + "\n" + stderr).strip()

            if proc.returncode != 0:
                return ToolResult(
                    tool_call_id=call.id,
                    content=combined or f"Exit code {proc.returncode}",
                    is_error=True,
                )

            return ToolResult(
                tool_call_id=call.id, content=combined or "(no output)",
            )
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass

    async def _run_plain_in_sandbox(
        self,
        call: ToolCall,
        code: str,
        timeout: float,
        strategy: object,
    ) -> ToolResult | None:
        """Run the plain-mode script inside the resolved sandbox backend.

        Wraps the script as ``python3 -c <code>`` and routes it through the
        backend the agent loop's resolver chose. Returns a ToolResult on
        success or a loud failure (``sandbox.fallback=error``); returns
        ``None`` only when the backend is unreachable AND
        ``sandbox.fallback=local`` — the caller then falls through to the
        host path. Only plain mode is routed (PTC mode is UDS-RPC-coupled
        to the host registry — see the module docstring).
        """
        from opencomputer.cost_guard import get_default_sandbox_cost_guard
        from plugin_sdk.sandbox import SandboxConfig, SandboxUnavailable

        backend_name = getattr(strategy, "name", None) or type(strategy).__name__
        custom = self._runtime_custom()
        raw_sid = custom.get("session_id")
        session_id = raw_sid if isinstance(raw_sid, str) else ""

        run = getattr(strategy, "run", None)
        if not callable(run):
            return self._sandbox_unreachable(
                call, backend_name, "resolved backend has no callable run()"
            )

        # Per-session cost cap — a paid backend (e2b / daytona / modal)
        # bills per running second. Refuse before the run if the session is
        # already over cap. A cost-guard hiccup must never block execution.
        if session_id:
            try:
                guard = get_default_sandbox_cost_guard()
                if guard.rate_for(backend_name) > 0:
                    decision = guard.check_session_budget(session_id)
                    if not decision.allowed:
                        return ToolResult(
                            tool_call_id=call.id,
                            content=(
                                f"Refused: {decision.reason}. The "
                                f"{backend_name!r} sandbox backend bills per "
                                "running second — raise the session cap or "
                                "start a new session."
                            ),
                            is_error=True,
                        )
            except Exception as exc:  # noqa: BLE001 — a guard hiccup never blocks
                _log.warning("PythonExec sandbox: cost-cap check failed: %s", exc)

        raw_key = custom.get(_SANDBOX_CONTAINER_KEY)
        container_key = raw_key if isinstance(raw_key, str) and raw_key else None
        cfg = SandboxConfig(
            cpu_seconds_limit=int(timeout),
            network_allowed=False,
            container_key=container_key,
        )
        # ``python3 -c <code>`` runs the script inside the sandbox image
        # (cloud images ship Python; a Docker image must be Python-capable).
        argv = ["python3", "-c", code]
        try:
            result = await run(argv, config=cfg, stdin=None, cwd=None)
        except SandboxUnavailable as exc:
            return self._sandbox_unreachable(call, backend_name, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a backend transport error
            _log.warning(
                "PythonExec sandbox: backend %r failed to run: %s",
                backend_name,
                exc,
            )
            return self._sandbox_unreachable(
                call, backend_name, f"{type(exc).__name__}: {exc}"
            )

        # Record this run's spend ($0 for a free local backend). A telemetry
        # failure must never break the tool result (counter-telemetry pattern).
        if session_id:
            try:
                get_default_sandbox_cost_guard().record_run(
                    session_id,
                    backend=backend_name,
                    duration_seconds=float(
                        getattr(result, "duration_seconds", 0.0) or 0.0
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "PythonExec sandbox: spend recording failed: %s", exc
                )

        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        combined = (stdout + "\n" + stderr).strip()
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            return ToolResult(
                tool_call_id=call.id,
                content=combined or f"Exit code {exit_code}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id, content=combined or "(no output)"
        )

    def _sandbox_unreachable(
        self, call: ToolCall, backend_name: str, reason: str
    ) -> ToolResult | None:
        """Apply ``sandbox.fallback`` when the backend is unreachable.

        ``local`` → return ``None`` (the caller falls through to the host
        path) with a logged WARNING. ``error`` (default) → a loud error
        ToolResult; OC never silently downgrades containment.
        """
        from opencomputer.sandbox.resolver import (
            SANDBOX_FALLBACK_LOCAL,
            fallback_policy,
        )

        config: object | None = None
        try:
            from opencomputer.agent.config_store import load_config

            config = load_config()
        except Exception:  # noqa: BLE001 — config absence → safe `error` default
            config = None
        if fallback_policy(config) == SANDBOX_FALLBACK_LOCAL:
            _log.warning(
                "PythonExec sandbox: backend %r unreachable (%s); "
                "sandbox.fallback=local — running the script on the HOST",
                backend_name,
                reason,
            )
            return None
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Error: sandbox backend {backend_name!r} is unavailable "
                f"({reason}), and sandbox.fallback='error' (the default). "
                "Configure a reachable sandbox.backend, or set "
                "sandbox.fallback=local to permit running on the host."
            ),
            is_error=True,
        )

    async def _execute_ptc(
        self, call: ToolCall, code: str, timeout: float,
    ) -> ToolResult:
        """PTC mode — subprocess can call registered tools via UDS-RPC.

        We DO NOT run the python_safety denylist here: the user's
        script is *expected* to call our tools via the RPC stubs. The
        safety surface is instead:

        - allowlisted tool set (default read-only)
        - per-action consent gate (``python_exec.ptc_mode``)
        - 50 KB stdout, 50 RPC calls, 300s wallclock

        Imports lazily so the module load cost (asyncio UDS server,
        struct, etc.) is paid only when PTC is actually used.
        """
        from opencomputer.tools.ptc import DEFAULT_ALLOWED_TOOLS, run_ptc
        from opencomputer.tools.registry import registry

        tools_arg = call.arguments.get("tools")
        allowed: tuple[str, ...]
        # Honour an explicit empty list (caller wants NO tool stubs)
        # vs absent / non-list (use defaults).
        if isinstance(tools_arg, list):
            allowed = tuple(str(t) for t in tools_arg)
        else:
            allowed = DEFAULT_ALLOWED_TOOLS

        # PTC mode wallclock cap is higher than plain (300s default vs 30s).
        if timeout < 60:
            timeout = 60.0
        timeout = min(timeout, 300.0)

        result = await run_ptc(
            code,
            registry=registry,
            allowed_tools=allowed,
            timeout_s=timeout,
        )

        if result.timed_out:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PTC script timed out after {timeout}s",
                is_error=True,
            )
        if result.exit_code != 0:
            combined = (result.stdout + "\n" + result.stderr).strip()
            return ToolResult(
                tool_call_id=call.id,
                content=combined or f"PTC script exited {result.exit_code}",
                is_error=True,
            )

        out = result.stdout.strip()
        meta = (
            f"\n\n[ptc: {result.rpc_call_count} tool call(s), "
            f"{result.duration_seconds:.1f}s, "
            f"{'truncated' if result.truncated else 'full'}]"
        )
        return ToolResult(
            tool_call_id=call.id,
            content=(out or "(no output)") + meta,
        )
