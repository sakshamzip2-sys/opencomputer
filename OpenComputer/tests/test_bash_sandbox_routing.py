"""Tests for the Bash tool's sandbox-backend routing — Milestone 2 (T2.6).

The agent loop's ``_resolve_sandbox_backend`` publishes the resolved
:class:`~plugin_sdk.SandboxStrategy` on
``runtime.custom["sandbox_backend_strategy"]`` just before each tool
dispatch. :meth:`opencomputer.tools.bash.BashTool.execute` reads it and:

* when a strategy is present → routes the shell command through
  ``strategy.run(...)`` and maps the :class:`~plugin_sdk.SandboxResult`
  onto the Bash tool's :class:`~plugin_sdk.core.ToolResult` in the SAME
  framing the host path produces;
* when none is present (the default — no ``sandbox.backend`` configured)
  → the existing host path runs **byte-identically** to pre-M2.

The hard backward-compat requirement (audit §4.2) is the byte-identical
no-op: a user with no sandbox configured sees zero behavior change. The
first test below pins exactly that.

These tests mock the strategy object — they need no real Docker / E2B /
bwrap. The fallback-policy tests force a backend that fails at run time
and assert ``sandbox.fallback`` (``error`` vs ``local``) is honored.
"""

from __future__ import annotations

import logging

import pytest

from opencomputer.agent.config import Config
from opencomputer.sandbox.policy import SandboxPolicy
from opencomputer.tools.bash import _SANDBOX_STRATEGY_KEY, BashTool
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxUnavailable

_BASH_LOGGER = "opencomputer.tools.bash"


# ─── test doubles ──────────────────────────────────────────────────────


class _RecordingStrategy:
    """A stub :class:`~plugin_sdk.SandboxStrategy` that records its call.

    Returns a caller-supplied :class:`~plugin_sdk.SandboxResult` and
    stashes the argv / config / cwd it was handed so the test can assert
    the Bash tool wrapped the command correctly.
    """

    def __init__(self, result: SandboxResult, *, name: str = "fake") -> None:
        self.name = name
        self._result = result
        self.calls: list[dict[str, object]] = []

    def is_available(self) -> bool:
        # The agent loop's resolver calls is_available() before routing.
        return True

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        self.calls.append(
            {"argv": argv, "config": config, "stdin": stdin, "cwd": cwd}
        )
        return self._result


class _FailingStrategy:
    """A backend that is unreachable at run time — ``run`` raises.

    ``exc`` controls what kind of failure: a :class:`~plugin_sdk.SandboxUnavailable`
    (missing dependency / key) or an arbitrary transport error (E2B
    ``create()`` network failure). Both must be caught by the Bash tool
    and routed through the fallback policy.
    """

    name = "dead"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, argv, *, config, stdin=None, cwd=None):  # noqa: ANN001
        raise self._exc


def _result(
    *, exit_code: int = 0, stdout: str = "", stderr: str = ""
) -> SandboxResult:
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        wrapped_command=["/bin/sh", "-c", "<cmd>"],
        strategy_name="fake",
    )


def _runtime_with(strategy: object) -> RuntimeContext:
    """A ``RuntimeContext`` carrying a resolved sandbox strategy."""
    return RuntimeContext(custom={_SANDBOX_STRATEGY_KEY: strategy})


@pytest.fixture(autouse=True)
def _reset_bash_runtime():
    """Reset ``BashTool._current_runtime`` around every test.

    ``set_runtime`` mutates a class attribute; without this an earlier
    test could leak a strategy onto a later one.
    """
    BashTool.set_runtime(DEFAULT_RUNTIME_CONTEXT)
    yield
    BashTool.set_runtime(DEFAULT_RUNTIME_CONTEXT)


def _call(cmd: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="Bash", arguments={"command": cmd})


# ─── the byte-identical no-op: no backend configured → host path ───────


async def test_no_backend_runs_on_host_byte_identical() -> None:
    """With no resolved strategy the Bash tool runs on the host, unchanged.

    This is the hard backward-compat requirement: a user with no
    ``sandbox.backend`` configured sees byte-identical behavior to
    pre-M2. The default ``RuntimeContext`` carries no
    ``sandbox_backend_strategy`` key, so ``_resolved_sandbox_strategy``
    returns ``None`` and execution falls through to the host path.
    """
    tool = BashTool()
    result = await tool.execute(_call("echo host-path-output"))
    assert isinstance(result, ToolResult)
    assert not result.is_error
    # The exact host framing — proves the no-op path is the pre-M2 shape.
    assert result.content == (
        "$ echo host-path-output\n"
        "exit=0\n"
        "--- stdout ---\n"
        "host-path-output\n"
    )


async def test_default_runtime_context_resolves_no_strategy() -> None:
    """The shared default ``RuntimeContext`` carries no backend strategy."""
    assert BashTool()._resolved_sandbox_strategy() is None


async def test_empty_custom_dict_resolves_no_strategy() -> None:
    """An explicit empty ``custom`` dict still resolves to the host path."""
    BashTool.set_runtime(RuntimeContext(custom={}))
    assert BashTool()._resolved_sandbox_strategy() is None


# ─── routing: a resolved backend executes the command ──────────────────


async def test_command_routes_through_resolved_backend() -> None:
    """When a strategy is resolved the command runs through ``strategy.run``."""
    strat = _RecordingStrategy(_result(stdout="sandboxed-stdout\n"))
    BashTool.set_runtime(_runtime_with(strat))

    result = await BashTool().execute(_call("echo hi"))

    # The backend's run() was invoked exactly once.
    assert len(strat.calls) == 1
    # The shell command was wrapped as ``sh -c`` — the same shape the
    # host path's create_subprocess_shell uses.
    assert strat.calls[0]["argv"] == ["/bin/sh", "-c", "echo hi"]
    # The SandboxResult was mapped onto the ToolResult in the host
    # framing — identical to what a host run of the same command emits.
    assert result.content == (
        "$ echo hi\n"
        "exit=0\n"
        "--- stdout ---\n"
        "sandboxed-stdout\n"
    )
    assert not result.is_error


async def test_backend_nonzero_exit_marks_tool_result_error() -> None:
    """A non-zero exit from the backend → ``is_error=True`` (host parity)."""
    strat = _RecordingStrategy(
        _result(exit_code=2, stdout="partial\n", stderr="boom\n")
    )
    BashTool.set_runtime(_runtime_with(strat))

    result = await BashTool().execute(_call("false"))

    assert result.is_error
    # The stderr block is appended with a leading "\n"; combined with the
    # trailing "\n" of the stdout payload this yields one blank line
    # between the stdout and stderr sections — byte-identical to the host
    # path's framing for the same (stdout, stderr) pair.
    assert result.content == (
        "$ false\n"
        "exit=2\n"
        "--- stdout ---\n"
        "partial\n"
        "\n"
        "--- stderr ---\n"
        "boom\n"
    )


async def test_backend_stderr_only_appended_when_present() -> None:
    """No stderr block when the backend returns empty stderr (host parity)."""
    strat = _RecordingStrategy(_result(stdout="only-stdout\n", stderr=""))
    BashTool.set_runtime(_runtime_with(strat))

    result = await BashTool().execute(_call("echo x"))

    assert "--- stderr ---" not in result.content


async def test_backend_run_receives_default_deny_network_config() -> None:
    """The Bash tool passes a default-deny ``SandboxConfig`` to the backend."""
    strat = _RecordingStrategy(_result(stdout="ok\n"))
    BashTool.set_runtime(_runtime_with(strat))

    await BashTool().execute(_call("echo x"))

    cfg = strat.calls[0]["config"]
    assert isinstance(cfg, SandboxConfig)
    assert cfg.network_allowed is False


async def test_backend_run_receives_resolved_timeout() -> None:
    """The tool's resolved ``timeout_s`` becomes the sandbox wall-clock cap."""
    strat = _RecordingStrategy(_result(stdout="ok\n"))
    BashTool.set_runtime(_runtime_with(strat))

    call = ToolCall(
        id="t1", name="Bash", arguments={"command": "echo x", "timeout_s": 42}
    )
    await BashTool().execute(call)

    cfg = strat.calls[0]["config"]
    assert isinstance(cfg, SandboxConfig)
    assert cfg.cpu_seconds_limit == 42


async def test_empty_command_short_circuits_before_backend() -> None:
    """An empty command is rejected before the backend is ever consulted."""
    strat = _RecordingStrategy(_result())
    BashTool.set_runtime(_runtime_with(strat))

    result = await BashTool().execute(_call("   "))

    assert result.is_error
    assert "empty command" in result.content
    # The backend must NOT have been touched.
    assert strat.calls == []


# ─── fallback policy on a run-time-unreachable backend ─────────────────


async def test_unreachable_backend_error_fallback_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox.fallback=error`` → an unreachable backend yields an error.

    OC never silently downgrades containment: under the default
    ``error`` policy a backend that fails at run time produces an error
    ``ToolResult`` rather than falling back to the host.
    """
    monkeypatch.setattr(
        BashTool,
        "_active_config",
        staticmethod(
            lambda: Config(sandbox=SandboxPolicy(backend="dead", fallback="error"))
        ),
    )
    BashTool.set_runtime(
        _runtime_with(_FailingStrategy(SandboxUnavailable("e2b key missing")))
    )

    result = await BashTool().execute(_call("echo never-runs"))

    assert result.is_error
    assert "unavailable" in result.content
    assert "sandbox.fallback='error'" in result.content
    # The command must NOT have been run on the host — no host stdout.
    assert "never-runs" not in result.content


async def test_unreachable_backend_local_fallback_runs_on_host(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``sandbox.fallback=local`` → an unreachable backend falls to the host.

    The command runs on the host and a WARNING is logged — the
    downgrade is never silent.
    """
    monkeypatch.setattr(
        BashTool,
        "_active_config",
        staticmethod(
            lambda: Config(sandbox=SandboxPolicy(backend="dead", fallback="local"))
        ),
    )
    BashTool.set_runtime(
        _runtime_with(_FailingStrategy(SandboxUnavailable("backend down")))
    )

    with caplog.at_level(logging.WARNING, logger=_BASH_LOGGER):
        result = await BashTool().execute(_call("echo ran-on-host"))

    # The command actually ran on the host — its stdout is present.
    assert not result.is_error
    assert result.content == (
        "$ echo ran-on-host\n"
        "exit=0\n"
        "--- stdout ---\n"
        "ran-on-host\n"
    )
    # The host downgrade was logged loudly.
    assert any(
        "running the command on the HOST" in r.message for r in caplog.records
    )


async def test_transport_error_during_run_treated_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-``SandboxUnavailable`` transport error is handled, not crashed.

    E2B's ``AsyncSandbox.create()`` can raise an arbitrary network / auth
    error. The Bash tool must treat it as "backend unreachable" and
    apply the fallback policy rather than letting it crash dispatch.
    """
    monkeypatch.setattr(
        BashTool,
        "_active_config",
        staticmethod(
            lambda: Config(sandbox=SandboxPolicy(backend="dead", fallback="local"))
        ),
    )
    BashTool.set_runtime(
        _runtime_with(_FailingStrategy(ConnectionError("network blip")))
    )

    with caplog.at_level(logging.WARNING, logger=_BASH_LOGGER):
        result = await BashTool().execute(_call("echo survived"))

    # Fell through to the host — the command ran, no crash.
    assert not result.is_error
    assert "survived" in result.content


async def test_malformed_strategy_object_treated_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-strategy object on the runtime key is handled defensively.

    If something other than a real strategy lands on
    ``custom['sandbox_backend_strategy']`` the tool must not crash — it
    treats it like an unreachable backend and applies the fallback.
    """
    monkeypatch.setattr(
        BashTool,
        "_active_config",
        staticmethod(
            lambda: Config(sandbox=SandboxPolicy(backend="x", fallback="error"))
        ),
    )
    # An object with no callable ``run``.
    BashTool.set_runtime(_runtime_with(object()))

    result = await BashTool().execute(_call("echo x"))

    assert result.is_error
    assert "unavailable" in result.content


# ─── set_runtime propagation ───────────────────────────────────────────


def test_set_runtime_updates_the_resolved_strategy() -> None:
    """``set_runtime`` is the channel the loop uses to hand over the runtime."""
    strat = _RecordingStrategy(_result())
    BashTool.set_runtime(_runtime_with(strat))
    assert BashTool()._resolved_sandbox_strategy() is strat
    # Re-setting to a strategy-free runtime clears it.
    BashTool.set_runtime(RuntimeContext())
    assert BashTool()._resolved_sandbox_strategy() is None


# ─── loop wiring: _resolve_sandbox_backend publishes onto the runtime ──
#
# These pin the OTHER half of T2.6 — the agent loop publishing the
# resolved backend so the Bash tool can consume it. Without the publish,
# the resolver's decision would have zero runtime effect (the original
# defect). The Bash-tool tests above prove the consume half.


def _agent_loop_with_config(config: Config) -> object:
    """Build a minimal ``AgentLoop`` carrying ``config`` for a wiring test.

    The loop is constructed only to exercise ``_resolve_sandbox_backend``
    — no conversation is run.
    """
    from opencomputer.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.config = config
    loop._runtime = RuntimeContext(custom={})
    return loop


def test_loop_publishes_resolved_strategy_for_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_sandbox_backend`` publishes the strategy the Bash tool reads.

    The loop resolves a backend for a Bash call and writes the resolved
    :class:`~plugin_sdk.SandboxStrategy` onto ``runtime.custom`` under
    the SAME key ``BashTool`` reads — closing the resolve→consume loop.
    """
    from opencomputer.agent import loop as loop_mod
    from opencomputer.sandbox import resolver as resolver_mod

    strat = _RecordingStrategy(_result(), name="e2b")
    # The resolver resolves backends by name via ``_named_strategy`` —
    # patch that boundary so no real backend is needed.
    monkeypatch.setattr(
        resolver_mod, "_named_strategy", lambda name: strat
    )
    # The loop looks the tool up in the global registry; return a real
    # BashTool so ``sandbox_preference`` (default) reads correctly.
    monkeypatch.setattr(loop_mod.registry, "get", lambda name: BashTool())

    loop = _agent_loop_with_config(
        Config(sandbox=SandboxPolicy(backend="e2b", fallback="error"))
    )
    name = loop._resolve_sandbox_backend(  # type: ignore[attr-defined]
        ToolCall(id="c1", name="Bash", arguments={"command": "echo x"})
    )

    assert name == "e2b"
    custom = loop._runtime.custom  # type: ignore[attr-defined]
    # Both keys are published: the name (observability) and the object.
    assert custom[_SANDBOX_STRATEGY_KEY] is strat
    assert custom["sandbox_backend"] == "e2b"


def test_loop_no_backend_clears_keys_no_runtime_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``sandbox.backend`` configured the loop publishes nothing.

    The no-op guarantee at the loop layer: a default ``Config`` resolves
    no backend, so ``_resolve_sandbox_backend`` returns ``None`` and
    leaves no strategy key on the runtime — the Bash tool then takes the
    host path.
    """
    from opencomputer.agent import loop as loop_mod

    monkeypatch.setattr(loop_mod.registry, "get", lambda name: BashTool())

    loop = _agent_loop_with_config(Config())  # default — no sandbox.backend
    name = loop._resolve_sandbox_backend(  # type: ignore[attr-defined]
        ToolCall(id="c1", name="Bash", arguments={"command": "echo x"})
    )

    assert name is None
    custom = loop._runtime.custom  # type: ignore[attr-defined]
    assert _SANDBOX_STRATEGY_KEY not in custom
    assert "sandbox_backend" not in custom


def test_loop_clears_stale_strategy_when_next_call_resolves_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior call's resolved backend never leaks into a later un-sandboxed call."""
    from opencomputer.agent import loop as loop_mod

    monkeypatch.setattr(loop_mod.registry, "get", lambda name: BashTool())

    loop = _agent_loop_with_config(Config())
    # Simulate a stale key left by an earlier sandboxed call.
    loop._runtime.custom[_SANDBOX_STRATEGY_KEY] = object()  # type: ignore[attr-defined]
    loop._runtime.custom["sandbox_backend"] = "e2b"  # type: ignore[attr-defined]

    loop._resolve_sandbox_backend(  # type: ignore[attr-defined]
        ToolCall(id="c2", name="Bash", arguments={"command": "echo x"})
    )

    # The default config resolves no backend → the stale keys are dropped.
    custom = loop._runtime.custom  # type: ignore[attr-defined]
    assert _SANDBOX_STRATEGY_KEY not in custom
    assert "sandbox_backend" not in custom
