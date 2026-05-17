"""Unit tests for the Daytona sandbox backend (M2, sandbox-provider-breadth).

The ``daytona`` SDK is mocked — no live cloud calls in CI. ``assert_conforms``
runs against the backend via a mock client whose ``process.exec`` delegates
to :func:`tests.sandbox_conformance.interpret_probe` so the cloud backend
gets exactly the same probe semantics as ``FakeSandboxBackend``.
"""

from __future__ import annotations

import asyncio
import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.sandbox.daytona import DaytonaSandboxStrategy
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxUnavailable
from tests.sandbox_conformance import assert_conforms, interpret_probe


class _FakeExecuteResponse:
    """Stand-in for ``daytona.ExecuteResponse`` — only the attrs we read."""

    def __init__(self, exit_code: int, result: str) -> None:
        self.exit_code = exit_code
        self.result = result


def _make_mock_daytona(*, exec_return=None, exec_side_effect=None):
    """Build (cls, instance, sandbox) patchable at ``daytona.AsyncDaytona``.

    ``cls()`` returns ``instance``; ``async with instance`` yields itself;
    ``instance.create()`` returns ``sandbox``; ``sandbox.process.exec(...)``
    returns ``exec_return`` or raises ``exec_side_effect``.
    """
    sandbox = MagicMock()
    sandbox.process = MagicMock()
    sandbox.process.exec = AsyncMock(
        return_value=exec_return, side_effect=exec_side_effect
    )
    instance = MagicMock()
    instance.create = AsyncMock(return_value=sandbox)
    instance.delete = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    cls = MagicMock(return_value=instance)
    return cls, instance, sandbox


# --- is_available -----------------------------------------------------------


def test_daytona_unavailable_without_api_key(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    assert DaytonaSandboxStrategy().is_available() is False


def test_daytona_available_with_key_and_pkg(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    # ``daytona`` is installed in the venv (M2 setup) → find_spec succeeds.
    assert DaytonaSandboxStrategy().is_available() is True


# --- happy path -------------------------------------------------------------


def test_daytona_run_happy_path(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    cls, instance, sandbox = _make_mock_daytona(
        exec_return=_FakeExecuteResponse(exit_code=0, result="hi\n"),
    )
    with patch("daytona.AsyncDaytona", cls):
        result = asyncio.run(
            DaytonaSandboxStrategy().run(["echo", "hi"], config=SandboxConfig())
        )
    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    # M-2: Daytona's process.exec captures stdout only — stderr is always "".
    assert result.stderr == ""
    assert result.strategy_name == "daytona"
    instance.create.assert_awaited_once()
    instance.delete.assert_awaited_once()  # teardown
    sandbox.process.exec.assert_awaited_once()
    # M-2 wrap: the exec command MUST be wrapped with `(...) 2>&1`.
    sent_command = sandbox.process.exec.await_args.args[0]
    assert sent_command.endswith(") 2>&1"), (
        f"daytona backend must wrap with 2>&1 for stderr capture; got {sent_command!r}"
    )


# --- non-zero exit (no raise — M-3) ----------------------------------------


def test_daytona_non_zero_exit_returns_code(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    cls, instance, _ = _make_mock_daytona(
        exec_return=_FakeExecuteResponse(exit_code=7, result="boom"),
    )
    with patch("daytona.AsyncDaytona", cls):
        result = asyncio.run(
            DaytonaSandboxStrategy().run(["false"], config=SandboxConfig())
        )
    assert result.exit_code == 7
    assert result.stdout == "boom"
    instance.delete.assert_awaited_once()


# --- exception → teardown still runs ----------------------------------------


def test_daytona_exec_raises_still_deletes_sandbox(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    cls, instance, _ = _make_mock_daytona(
        exec_side_effect=RuntimeError("net down"),
    )
    with patch("daytona.AsyncDaytona", cls), pytest.raises(RuntimeError):
        asyncio.run(
            DaytonaSandboxStrategy().run(["echo", "x"], config=SandboxConfig())
        )
    instance.delete.assert_awaited_once()


# --- SandboxUnavailable when key dropped between construct and run ----------


def test_daytona_run_raises_sandbox_unavailable_without_key(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    backend = DaytonaSandboxStrategy()  # caches available=True
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    with pytest.raises(SandboxUnavailable, match="DAYTONA_API_KEY"):
        asyncio.run(backend.run(["echo", "x"], config=SandboxConfig()))


# --- conformance suite against the mocked SDK -------------------------------


def test_daytona_conforms_against_mocked_sdk(monkeypatch):
    """``assert_conforms`` against the backend with a probe-interpreting mock."""
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")

    def fake_exec(command, cwd=None, env=None, timeout=None):
        del cwd  # in the SDK signature but not exercised by the probes
        # The backend wraps as ``(<orig>) 2>&1``; recover the original
        # command, split to argv, delegate to the shared interpreter.
        assert command.startswith("(") and command.endswith(") 2>&1"), command
        inner = command[1:-len(") 2>&1")]
        argv = shlex.split(inner)
        env_dict = dict(env or {})
        exit_code, stdout, stderr = interpret_probe(
            argv,
            env=env_dict,
            cpu_seconds_limit=timeout or 60,
        )
        # Daytona's `result` is stdout-only; the 2>&1 wrap merges stderr in.
        return _FakeExecuteResponse(exit_code=exit_code, result=stdout + stderr)

    cls, _, sandbox = _make_mock_daytona()
    sandbox.process.exec = AsyncMock(side_effect=fake_exec)
    with patch("daytona.AsyncDaytona", cls):
        assert_conforms(DaytonaSandboxStrategy())


# --- T2.2 wiring ------------------------------------------------------------


def test_daytona_in_strategy_name_literal():
    """``"daytona"`` is in ``SandboxStrategyName`` — CLI auto-derives via Literal."""
    import typing

    from plugin_sdk.sandbox import SandboxStrategyName

    assert "daytona" in typing.get_args(SandboxStrategyName)


def test_daytona_resolvable_via_named_strategy(monkeypatch):
    """``runner._named_strategy("daytona")`` returns a ``DaytonaSandboxStrategy``."""
    from opencomputer.sandbox.runner import _named_strategy

    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    backend = _named_strategy("daytona")
    assert isinstance(backend, DaytonaSandboxStrategy)
    assert backend.name == "daytona"


def test_daytona_exported_from_sandbox_package():
    """``from opencomputer.sandbox import DaytonaSandboxStrategy`` works."""
    from opencomputer.sandbox import DaytonaSandboxStrategy as Exported

    assert Exported is DaytonaSandboxStrategy


# --- T2.3 cost rate (F16 gate) ---------------------------------------------


def test_daytona_has_nonzero_cost_rate():
    """F16: a paid backend with no rate silently bypasses the session cap.

    Daytona's seed rate MUST live in
    ``DEFAULT_BACKEND_RATES_USD_PER_SECOND`` AND a fresh
    :class:`SandboxCostGuard` must read it back as ``> 0``.
    """
    import tempfile
    from pathlib import Path

    from opencomputer.cost_guard.sandbox import (
        DEFAULT_BACKEND_RATES_USD_PER_SECOND,
        SandboxCostGuard,
    )

    assert "daytona" in DEFAULT_BACKEND_RATES_USD_PER_SECOND
    assert DEFAULT_BACKEND_RATES_USD_PER_SECOND["daytona"] > 0

    with tempfile.TemporaryDirectory() as tmp:
        guard = SandboxCostGuard(storage_path=Path(tmp) / "cost.json")
        assert guard.rate_for("daytona") > 0
