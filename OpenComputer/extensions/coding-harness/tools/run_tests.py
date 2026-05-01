"""RunTests tool — auto-detect project type and invoke the matching runner.

Supports pytest (Python), vitest / jest (Node), cargo test (Rust), go test.
Detection is by file-marker; explicit `runner` argument overrides.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_MARKERS = [
    ("pytest", ("pyproject.toml", "pytest.ini", "setup.cfg", "tests")),
    ("vitest", ("vitest.config.ts", "vitest.config.js")),
    ("jest", ("jest.config.ts", "jest.config.js")),
    ("cargo", ("Cargo.toml",)),
    ("go", ("go.mod",)),
]

_COMMANDS: dict[str, list[str]] = {
    "pytest": ["pytest", "-q"],
    "vitest": ["npx", "vitest", "run"],
    "jest": ["npx", "jest"],
    "cargo": ["cargo", "test"],
    "go": ["go", "test", "./..."],
}


def _detect(root: Path) -> str | None:
    for runner, markers in _MARKERS:
        for m in markers:
            if (root / m).exists():
                return runner
    return None


class RunTestsTool(BaseTool):
    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="RunTests",
            description=(
                "Run the project's test suite. Auto-detects the runner from project "
                "markers: pyproject.toml/pytest.ini → pytest, vitest.config → vitest, "
                "jest.config → jest, Cargo.toml → cargo test, go.mod → go test. Pass "
                "`runner` to force a specific one (overrides detection), and `filter` "
                "to narrow to a subset (passed straight to the runner — pytest test ids, "
                "go's regex pattern, etc.). Use this rather than Bash + remembering the "
                "right command per project: same exit-code discipline, structured timeout, "
                "and emits progress events. Default timeout is 120s (max 600s); for "
                "longer suites raise `timeout_s`. If detection finds nothing the tool "
                "errors and lists supported runners."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "runner": {
                        "type": "string",
                        "enum": list(_COMMANDS.keys()),
                        "description": "Force a specific test runner.",
                    },
                    "filter": {
                        "type": "string",
                        "description": "Test selector passed to the runner.",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "default": 120,
                        "minimum": 5,
                        "maximum": 600,
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = dict(call.arguments)
        root = self._ctx.rewind_store.workspace_root
        runner = args.get("runner") or _detect(root)
        if runner is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Could not detect a test runner. Pass `runner` explicitly "
                    f"(options: {', '.join(_COMMANDS)})."
                ),
                is_error=True,
            )

        cmd = list(_COMMANDS[runner])
        if args.get("filter"):
            cmd.append(str(args["filter"]))
        timeout = int(args.get("timeout_s") or 120)

        self._ctx.emit_progress(
            {"msg": f"running {runner}", "pct": 0, "cmd": cmd}
        )

        try:
            from opencomputer.profiles import read_active_profile, scope_subprocess_env

            env = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
        except Exception:  # noqa: BLE001 — fail-soft on profile lookup
            env = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"{runner} timed out after {timeout}s",
                    is_error=True,
                )
        except FileNotFoundError:
            return ToolResult(
                tool_call_id=call.id,
                content=f"{runner} not found on PATH. Install it or pass `runner`.",
                is_error=True,
            )

        text = stdout.decode("utf-8", errors="replace")
        self._ctx.emit_progress({"msg": f"{runner} done", "pct": 100})

        is_error = proc.returncode != 0
        suffix = f"\n\n({runner} exited with code {proc.returncode})"
        return ToolResult(
            tool_call_id=call.id, content=text + suffix, is_error=is_error
        )


__all__ = ["RunTestsTool", "_detect"]
