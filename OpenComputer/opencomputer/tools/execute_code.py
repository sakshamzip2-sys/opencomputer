"""``ExecuteCode`` — Hermes Doc-2 ``execute_code`` parity tool (2026-05-08).

Hermes ships a tool the agent uses for *multi-step Python that needs
several existing OC tools*. The script runs in a subprocess; only its
``print()`` output enters the conversation context, so a 50-result web
search + filter + summary takes ONE inference turn rather than 50.

OC's ``PythonExec`` with ``mode="ptc"`` already implements the same
core mechanism (UDS-RPC subprocess + tool stubs in a generated
prologue + stdout-only context). This module is a thin alias that:

* Renames the tool to ``ExecuteCode`` so plugins/skills authored
  against the Hermes name resolve cleanly.
* Defaults to a broader tool list (`Read`, `Write`, `Edit`, `Grep`,
  `Glob`, `WebFetch`, `WebSearch`, `Bash`) — Hermes' default includes
  write + terminal, OC's PTC default did not.
* Scrubs sensitive env vars (KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/
  PASSWD/AUTH) before subprocess spawn. Pass-through list configurable
  via ``code_execution.terminal.env_passthrough`` in config.yaml.
* Caps stderr at 10 KB (Hermes spec).
* Supports two modes:
   - ``mode="project"`` (default): subprocess inherits the session's
     working directory + the active venv python (`VIRTUAL_ENV` /
     `CONDA_PREFIX`), matching the project the agent is editing.
   - ``mode="strict"``: subprocess starts in a temp dir + ``sys.executable``,
     decoupled from the project's venv. Use when the project's venv
     would shadow the orchestration stdlib OC needs (rare).
* Refuses recursion via the ``OC_EXECUTE_CODE_DEPTH`` env-var marker —
  a nested ExecuteCode call inside an already-running ExecuteCode
  fails with a clear error rather than fork-bombing.

Linux/macOS only. On Windows, ExecuteCode returns a clear error result
to make the failure mode explicit; Hermes' equivalent silently falls
back to sequential tool calls, but a louder failure pushes Windows
users toward the ``Bash`` / ``Read`` / etc tools that DO work.

Why a thin wrapper, not a fork:

* run_ptc has been battle-tested for ~5 months; cloning it doubles the
  surface to maintain.
* The differences (default tools, env scrub, stderr cap, two modes)
  are all data, not behavior — making them parameters of run_ptc is
  cheap.
* The PythonExec tool stays for users who want PTC's read-only safety
  posture; ExecuteCode opts in to writes + Bash explicitly.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from typing import ClassVar

from opencomputer.tools.ptc import (
    _MAX_STDERR_BYTES,
    _RECURSION_GUARD_ENV,
    EXECUTE_CODE_DEFAULT_TOOLS,
    run_ptc,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.tools.execute_code")


class ExecuteCodeRecursionError(RuntimeError):
    """Raised when ExecuteCode is invoked from within an already-running
    ExecuteCode subprocess. Detected via the ``OC_EXECUTE_CODE_DEPTH``
    env var. Recursion is refused to prevent fork bombs."""


class ExecuteCode(BaseTool):
    """Run a Python script in a subprocess with curated tool RPC access.

    Mirrors Hermes' ``execute_code`` tool. The script can call
    ``Read(...)``, ``Write(...)``, ``Bash(...)``, etc. as if they were
    built-in functions — a generated prologue inserts the stubs.

    Only ``print()`` output enters the parent conversation. Intermediate
    tool results stay in the subprocess. This is the load-bearing
    context-economy property: 50 web fetches + filter + summary
    collapses to one inference turn.
    """

    strict_mode = True
    parallel_safe = False  # the recursion guard relies on serial dispatch
    consent_tier: int = 2

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="execute_code.run",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Execute a Python script in a sandboxed subprocess with "
                "curated tool RPC access (Hermes execute_code parity)."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ExecuteCode",
            description=(
                "Run a multi-step Python script in a sandboxed "
                "subprocess. The script can call OC tools (Read, Write, "
                "Edit, Grep, Glob, WebFetch, WebSearch, Bash by default) "
                "as ordinary functions; only the script's print() output "
                "enters the conversation. Use this when you'd otherwise "
                "make 3+ tool calls with logic between them, or loop over "
                "search results, or filter/transform bulk data — it "
                "collapses orchestration into ONE inference turn. "
                "Linux/macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python source. The harness predefines tool "
                            "stubs — do NOT import them. Stubs raise "
                            "RuntimeError on tool failure (catch with "
                            "try/except). Only print() reaches the "
                            "conversation; everything else stays in the "
                            "subprocess."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["project", "strict"],
                        "description": (
                            "project (default): cwd = session working dir, "
                            "python = active venv. strict: cwd = temp dir, "
                            "python = sys.executable. Pick strict when the "
                            "project venv would shadow the stdlib needed."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Override the tool allowlist. Defaults to "
                            "Read, Write, Edit, Grep, Glob, WebFetch, "
                            "WebSearch, Bash."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Wallclock cap (default 300s). Subprocess is "
                            "killed past this point."
                        ),
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # Refuse if we're already inside an ExecuteCode subprocess.
        if os.environ.get(_RECURSION_GUARD_ENV, "0") not in ("0", ""):
            raise ExecuteCodeRecursionError(
                "ExecuteCode cannot be invoked recursively. Refactor the "
                "outer script to do all the orchestration itself."
            )

        # Linux/macOS only — Windows falls back via clear error.
        if sys.platform == "win32":
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "ExecuteCode is not supported on Windows. Use Read, "
                    "Bash, etc. directly, or run from WSL2."
                ),
                is_error=True,
            )

        args = call.arguments or {}
        code = str(args.get("code") or "").strip()
        if not code:
            return ToolResult(
                tool_call_id=call.id, content="empty 'code'", is_error=True,
            )

        mode = str(args.get("mode") or "project").lower()
        if mode not in ("project", "strict"):
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown mode {mode!r}; pick 'project' or 'strict'",
                is_error=True,
            )

        # Tool allowlist — caller can override.
        raw_tools = args.get("tools")
        allowed_tools: tuple[str, ...]
        if isinstance(raw_tools, list) and raw_tools:
            allowed_tools = tuple(str(t) for t in raw_tools)
        else:
            allowed_tools = EXECUTE_CODE_DEFAULT_TOOLS

        timeout_seconds = float(args.get("timeout_seconds") or 300.0)

        # Mode-specific cwd + python.
        cwd: str | None
        python_executable: str | None
        if mode == "project":
            cwd = os.getcwd()
            # Active venv python — VIRTUAL_ENV / CONDA_PREFIX precedence.
            venv = os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX")
            if venv:
                candidate = os.path.join(venv, "bin", "python")
                python_executable = candidate if os.path.exists(candidate) else None
            else:
                python_executable = None
        else:  # strict
            tmpdir = tempfile.mkdtemp(prefix="oc-execute-code-strict-")
            cwd = tmpdir
            python_executable = sys.executable

        # Resolve env passthrough from config.yaml: code_execution.terminal.env_passthrough
        passthrough: tuple[str, ...] = ()
        config_max_tool_calls: int | None = None
        try:
            from opencomputer.agent.config import default_config

            cfg = default_config()
            ce = getattr(cfg, "code_execution", None)
            if ce is not None:
                terminal_cfg = getattr(ce, "terminal", None)
                if isinstance(terminal_cfg, dict):
                    pt = terminal_cfg.get("env_passthrough")
                    if isinstance(pt, list):
                        passthrough = tuple(str(x) for x in pt)
                # 2026-05-08 G5 — Hermes Doc-2 max_tool_calls config slot.
                mtc = getattr(ce, "max_tool_calls", None)
                if isinstance(mtc, int) and mtc > 0:
                    config_max_tool_calls = mtc
        except Exception:  # noqa: BLE001 — config absence must not block exec
            pass

        # Resolve registry — global tool registry instance.
        from opencomputer.tools.registry import registry

        try:
            ptc_result = await run_ptc(
                code,
                registry=registry,
                allowed_tools=allowed_tools,
                timeout_s=timeout_seconds,
                scrub_env=True,
                env_passthrough=passthrough,
                stderr_cap=_MAX_STDERR_BYTES,
                cwd=cwd,
                python_executable=python_executable,
                max_tool_calls=config_max_tool_calls,
            )
        finally:
            # strict mode created a tempdir — clean it up.
            if mode == "strict" and cwd:
                try:
                    import shutil

                    shutil.rmtree(cwd, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

        if ptc_result.timed_out:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"ExecuteCode timed out after {timeout_seconds:.0f}s.\n"
                    f"stdout (partial):\n{ptc_result.stdout[-2000:]}\n"
                    f"stderr (partial):\n{ptc_result.stderr[-2000:]}"
                ),
                is_error=True,
            )

        # Surface stderr only when nonzero exit + stderr present —
        # otherwise the model gets noisy output for clean runs.
        if ptc_result.exit_code != 0:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"exit_code={ptc_result.exit_code}\n"
                    f"stdout:\n{ptc_result.stdout}\n"
                    f"stderr:\n{ptc_result.stderr}"
                ),
                is_error=True,
            )
        suffix = ""
        if ptc_result.truncated:
            suffix = "\n\n[output truncated; see stdout/stderr caps]"
        return ToolResult(
            tool_call_id=call.id,
            content=ptc_result.stdout + suffix,
        )


__all__ = [
    "ExecuteCode",
    "ExecuteCodeRecursionError",
]
