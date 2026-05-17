"""PythonExec plain-mode sandbox routing (M5, sandbox-provider-breadth).

Plain mode routes through the resolved sandbox backend when one is
configured; PTC mode does NOT (it is UDS-RPC-coupled to the host
registry). The ``runtime.custom['sandbox_backend_strategy']`` key — set
by ``AgentLoop`` — is the seam, exactly as for the Bash tool.
"""

from __future__ import annotations

import asyncio

from opencomputer.tools.python_exec import PythonExec
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.sandbox import SandboxResult


class _RecordingStrategy:
    """Records the argv it is asked to run; returns a canned SandboxResult."""

    name = "recording"

    def __init__(
        self, *, exit_code: int = 0, stdout: str = "sandboxed-output",
        stderr: str = "",
    ) -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def run(self, argv, *, config, stdin=None, cwd=None):
        del config, stdin, cwd  # ABC signature; this double records argv only
        self.calls.append(list(argv))
        return SandboxResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
            duration_seconds=0.0,
            wrapped_command=list(argv),
            strategy_name=self.name,
        )


def _runtime_with(strategy: object | None) -> RuntimeContext:
    rt = RuntimeContext()
    if strategy is not None:
        rt.custom["sandbox_backend_strategy"] = strategy
    return rt


def _run(tool_args: dict[str, object]) -> ToolResult:
    return asyncio.run(
        PythonExec().execute(
            ToolCall(id="t", name="PythonExec", arguments=tool_args)
        )
    )


def test_plain_mode_routes_through_resolved_backend() -> None:
    """A resolved strategy → the script runs as ``python3 -c <code>`` in it."""
    strat = _RecordingStrategy(stdout="hello-from-sandbox")
    PythonExec.set_runtime(_runtime_with(strat))
    try:
        result = _run({"code": "print('hi')", "mode": "plain"})
    finally:
        PythonExec.set_runtime(RuntimeContext())
    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert "hello-from-sandbox" in result.content
    assert strat.calls == [["python3", "-c", "print('hi')"]]


def test_plain_mode_host_path_when_no_backend() -> None:
    """No resolved strategy → the host subprocess path runs (no routing)."""
    PythonExec.set_runtime(RuntimeContext())  # no sandbox_backend_strategy
    result = _run({"code": "print('host-output')", "mode": "plain"})
    assert result.is_error is False
    assert "host-output" in result.content


def test_plain_mode_nonzero_exit_is_error() -> None:
    """A non-zero sandbox exit maps to an error ToolResult."""
    strat = _RecordingStrategy(exit_code=1, stdout="", stderr="boom")
    PythonExec.set_runtime(_runtime_with(strat))
    try:
        result = _run({"code": "print('x')", "mode": "plain"})
    finally:
        PythonExec.set_runtime(RuntimeContext())
    assert result.is_error is True
    assert "boom" in result.content


def test_ptc_mode_is_not_routed_to_sandbox() -> None:
    """PTC mode must NOT route to the backend — it is UDS-RPC-coupled to the
    host registry. The resolved strategy's ``run()`` is never called."""
    strat = _RecordingStrategy()
    PythonExec.set_runtime(_runtime_with(strat))
    try:
        result = _run({"code": "print('ptc')", "mode": "ptc", "tools": []})
    finally:
        PythonExec.set_runtime(RuntimeContext())
    assert isinstance(result, ToolResult)
    assert strat.calls == []  # the sandbox backend was NOT invoked


def test_python_exec_is_never_parallel() -> None:
    """PythonExec must NOT dispatch concurrently with other tool calls.

    M5 routes plain mode through a per-call sandbox backend that
    ``AgentLoop._resolve_sandbox_backend`` publishes on the SHARED
    ``runtime.custom`` immediately before dispatch. Two concurrent
    PythonExec dispatches would clobber each other's resolved backend —
    in the worst case dropping a sandbox-required call onto the bare host
    (a containment-escape race). Sequential dispatch makes the
    publish-then-consume atomic; this guards both layers that enforce it
    — the ``parallel_safe`` class flag and the loop's hardcoded set.
    """
    from opencomputer.agent.loop import HARDCODED_NEVER_PARALLEL

    assert PythonExec().parallel_safe is False
    assert "PythonExec" in HARDCODED_NEVER_PARALLEL
