"""Tests for :class:`opencomputer.sandbox.e2b.E2BSandboxStrategy`.

The ``e2b`` package is an optional extra and is **not** installed in the
test environment. Every test that needs it mocks it:

* ``is_available()`` tests monkeypatch the ``importlib.util.find_spec``
  probe + the ``E2B_API_KEY`` env var.
* ``run()`` tests inject a synthetic ``e2b`` module (with a fake
  ``AsyncSandbox`` + a fake ``CommandExitException``) into ``sys.modules``
  so the strategy's lazy ``from e2b import AsyncSandbox`` resolves to the
  double.

Mirrors the style of ``tests/test_sandbox_ssh.py`` — a layer of pure
checks (name, availability gating) plus behavioural tests against a
mocked SDK (argv shlex-join, result mapping, exit-exception translation,
timeout sentinels, the ``finally`` kill, the network WARNING).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.sandbox._common import TIMEOUT_EXIT_CODE, TIMEOUT_STDERR
from opencomputer.sandbox.e2b import (
    E2BSandboxStrategy,
    _coerce_exit_code,
    _e2b_available,
)
from plugin_sdk.sandbox import SandboxConfig, SandboxUnavailable

# --------------------------------------------------------------------------
# Fake E2B SDK doubles
# --------------------------------------------------------------------------


class _FakeCommandExitException(Exception):  # noqa: N818 — mirrors the real
    # E2B ``CommandExitException``, which has no ``Error`` suffix; the fake
    # keeps the same name so the strategy catches it identically.
    """Stand-in for ``e2b``'s ``CommandExitException`` (raised on non-zero exit)."""

    def __init__(
        self,
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
    ) -> None:
        super().__init__(error or f"command exited with {exit_code}")
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error


class _FakeCommandResult:
    """Stand-in for ``e2b``'s ``CommandResult`` (returned on a zero exit)."""

    def __init__(self, *, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error: str | None = None


class _FakeCommands:
    """The ``sandbox.commands`` sub-object — only ``run`` is exercised."""

    def __init__(self, run_impl) -> None:
        self.run = run_impl


class _FakeAsyncSandbox:
    """Minimal ``AsyncSandbox`` double.

    The class-level ``create`` is an ``AsyncMock`` so a test can assert the
    ``timeout=`` / ``envs=`` kwargs the strategy passes; it returns a fresh
    instance whose ``commands.run`` is the per-test impl and whose ``kill``
    is an ``AsyncMock`` the test can assert was awaited.
    """

    #: Per-test command implementation (callable → result, or raises).
    run_impl = None
    #: Records every ``create`` call's kwargs for assertions.
    create_calls: list[dict] = []

    def __init__(self) -> None:
        self.commands = _FakeCommands(type(self).run_impl)
        self.kill = AsyncMock(return_value=None)

    @classmethod
    async def create(cls, **kwargs):
        cls.create_calls.append(kwargs)
        return cls()


def _install_fake_e2b(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_impl,
    with_exception_at_root: bool = True,
) -> type[_FakeAsyncSandbox]:
    """Inject a synthetic ``e2b`` package into ``sys.modules``.

    Returns the fake ``AsyncSandbox`` class so tests can read
    ``.create_calls`` and the per-instance ``kill`` mock.

    ``with_exception_at_root`` puts ``CommandExitException`` on the package
    root (the strategy's preferred resolution path); when False it goes on
    ``e2b.exceptions`` instead, exercising the fallback path.
    """
    sandbox_cls = type(
        "_FakeAsyncSandbox",
        (_FakeAsyncSandbox,),
        {"run_impl": staticmethod(run_impl), "create_calls": []},
    )

    e2b_mod = types.ModuleType("e2b")
    e2b_mod.AsyncSandbox = sandbox_cls  # type: ignore[attr-defined]
    if with_exception_at_root:
        e2b_mod.CommandExitException = _FakeCommandExitException  # type: ignore[attr-defined]

    exceptions_mod = types.ModuleType("e2b.exceptions")
    exceptions_mod.CommandExitException = _FakeCommandExitException  # type: ignore[attr-defined]
    e2b_mod.exceptions = exceptions_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "e2b", e2b_mod)
    monkeypatch.setitem(sys.modules, "e2b.exceptions", exceptions_mod)
    return sandbox_cls


# --------------------------------------------------------------------------
# Strategy identity
# --------------------------------------------------------------------------


def test_strategy_name_is_e2b() -> None:
    assert E2BSandboxStrategy.name == "e2b"


# --------------------------------------------------------------------------
# is_available() — gated on E2B_API_KEY + package presence
# --------------------------------------------------------------------------


def test_is_available_true_when_key_set_and_package_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "e2b" else None,
    )
    assert E2BSandboxStrategy().is_available() is True


def test_is_available_false_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    # Package present, but no key → unavailable. find_spec must not even
    # be consulted (key check short-circuits first), but stub it anyway.
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert E2BSandboxStrategy().is_available() is False


def test_is_available_false_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert E2BSandboxStrategy().is_available() is False


def test_is_available_false_when_key_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert E2BSandboxStrategy().is_available() is False


def test_e2b_available_helper_swallows_find_spec_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken ``e2b`` install (find_spec raises) must report False, not raise."""
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    def _boom(name: str):
        raise ValueError("partial install")

    monkeypatch.setattr("importlib.util.find_spec", _boom)
    assert _e2b_available() is False


def test_is_available_is_cached_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs once at __init__ — later env changes don't flip it."""
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    strategy = E2BSandboxStrategy()
    assert strategy.is_available() is True
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    # Still True — the cached value from construction stands.
    assert strategy.is_available() is True


# --------------------------------------------------------------------------
# explain() — dry-run wrapped command
# --------------------------------------------------------------------------


def test_explain_returns_shlex_joined_marker_argv() -> None:
    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b")
    wrapped = strategy.explain(["echo", "hi there"], config=cfg)
    assert wrapped[0] == "e2b"
    # The space in "hi there" must be shlex-escaped in the trailing arg.
    assert wrapped[-1] == "echo 'hi there'"


# --------------------------------------------------------------------------
# run() — happy path
# --------------------------------------------------------------------------


def test_run_happy_path_maps_result_and_joins_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _FakeCommandResult(exit_code=0, stdout="hello\n", stderr="")

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    result = asyncio.run(strategy.run(["echo", "hello"], config=cfg))

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.strategy_name == "e2b"
    assert result.duration_seconds >= 0.0
    # M-1: argv was shlex-joined into a single command string.
    assert captured["command"] == "echo hello"
    # wrapped_command is the same audit marker explain() returns.
    assert result.wrapped_command[0] == "e2b"
    assert result.wrapped_command[-1] == "echo hello"
    # A sandbox was created exactly once.
    assert len(sandbox_cls.create_calls) == 1


def test_run_passes_timeout_and_filtered_env_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_run(command, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCommandResult(exit_code=0, stdout="ok")

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")
    monkeypatch.setenv("SECRET_TOKEN", "leakme")
    monkeypatch.setenv("PATH", "/usr/bin")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(
        strategy="e2b",
        network_allowed=True,
        cpu_seconds_limit=42,
        allowed_env_vars=("PATH",),
    )
    asyncio.run(strategy.run(["echo", "x"], config=cfg, cwd="/home/user"))

    # The per-command ``timeout`` passed to the SDK is the *server-side
    # backstop* (cap + buffer), not the raw cap — OC's own
    # ``asyncio.wait_for`` is the authoritative wall-clock cap, so the
    # E2B-side timeout is given slack to fire second.
    assert captured["kwargs"]["timeout"] > 42
    assert captured["kwargs"]["cwd"] == "/home/user"
    # Env allowlist applied: PATH passes, SECRET_TOKEN is stripped.
    assert "PATH" in captured["kwargs"]["envs"]
    assert "SECRET_TOKEN" not in captured["kwargs"]["envs"]
    # Sandbox.create got the lifetime backstop (cap + buffer) and the env.
    create_kwargs = sandbox_cls.create_calls[0]
    assert create_kwargs["timeout"] > 42
    assert "PATH" in create_kwargs["envs"]
    assert "SECRET_TOKEN" not in create_kwargs["envs"]


def test_run_maps_nonzero_result_without_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result object carrying a non-zero exit_code is passed through verbatim."""

    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=3, stdout="", stderr="partial fail")

    _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    result = asyncio.run(strategy.run(["false"], config=cfg))

    assert result.exit_code == 3
    assert result.stderr == "partial fail"


# --------------------------------------------------------------------------
# run() — CommandExitException → result with the real exit code (M-3)
# --------------------------------------------------------------------------


def test_run_translates_command_exit_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(command, **kwargs):
        raise _FakeCommandExitException(
            exit_code=127,
            stdout="",
            stderr="command not found",
        )

    _install_fake_e2b(monkeypatch, run_impl=fake_run, with_exception_at_root=True)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    result = asyncio.run(strategy.run(["nonexistent-cmd"], config=cfg))

    # M-3: the exception's exit_code / streams are surfaced as a result,
    # not raised — matching docker / none semantics.
    assert result.exit_code == 127
    assert result.stderr == "command not found"
    assert result.strategy_name == "e2b"
    assert result.wrapped_command[-1] == "nonexistent-cmd"


def test_run_translates_command_exit_exception_via_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CommandExitException is only on ``e2b.exceptions``, it is still caught."""

    async def fake_run(command, **kwargs):
        raise _FakeCommandExitException(exit_code=2, stderr="boom")

    _install_fake_e2b(monkeypatch, run_impl=fake_run, with_exception_at_root=False)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    result = asyncio.run(strategy.run(["false"], config=cfg))

    assert result.exit_code == 2
    assert result.stderr == "boom"


# --------------------------------------------------------------------------
# run() — timeout → sentinels (-1 / "[sandbox timeout]")
# --------------------------------------------------------------------------


def test_run_timeout_returns_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_run(command, **kwargs):
        await asyncio.sleep(5)
        return _FakeCommandResult(exit_code=0)

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=slow_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True, cpu_seconds_limit=1)
    result = asyncio.run(strategy.run(["sleep", "5"], config=cfg))

    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.exit_code == -1
    assert result.stderr == TIMEOUT_STDERR
    assert result.stdout == ""
    assert result.strategy_name == "e2b"
    # Even on timeout the sandbox was created — and must be killed.
    created = sandbox_cls.create_calls
    assert len(created) == 1


# --------------------------------------------------------------------------
# run() — kill() always runs (finally block)
# --------------------------------------------------------------------------


def test_run_kills_sandbox_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list = []

    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0, stdout="ok")

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    # Wrap create so we can grab the live instance and inspect its kill mock.
    orig_create = sandbox_cls.create.__func__

    async def tracking_create(cls, **kwargs):
        inst = await orig_create(cls, **kwargs)
        killed.append(inst)
        return inst

    monkeypatch.setattr(sandbox_cls, "create", classmethod(tracking_create))

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    asyncio.run(strategy.run(["echo", "ok"], config=cfg))

    assert len(killed) == 1
    killed[0].kill.assert_awaited_once()


def test_run_kills_sandbox_on_command_exit_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list = []

    async def fake_run(command, **kwargs):
        raise _FakeCommandExitException(exit_code=1, stderr="fail")

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    orig_create = sandbox_cls.create.__func__

    async def tracking_create(cls, **kwargs):
        inst = await orig_create(cls, **kwargs)
        killed.append(inst)
        return inst

    monkeypatch.setattr(sandbox_cls, "create", classmethod(tracking_create))

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    asyncio.run(strategy.run(["false"], config=cfg))

    # finally still fires on the exit-exception path.
    assert len(killed) == 1
    killed[0].kill.assert_awaited_once()


def test_run_kills_sandbox_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list = []

    async def slow_run(command, **kwargs):
        await asyncio.sleep(5)
        return _FakeCommandResult(exit_code=0)

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=slow_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    orig_create = sandbox_cls.create.__func__

    async def tracking_create(cls, **kwargs):
        inst = await orig_create(cls, **kwargs)
        killed.append(inst)
        return inst

    monkeypatch.setattr(sandbox_cls, "create", classmethod(tracking_create))

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True, cpu_seconds_limit=1)
    asyncio.run(strategy.run(["sleep", "5"], config=cfg))

    assert len(killed) == 1
    killed[0].kill.assert_awaited_once()


def test_run_kill_failure_does_not_mask_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A kill() that raises must be swallowed (logged WARNING) — result stands."""

    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0, stdout="ok")

    sandbox_cls = _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    orig_create = sandbox_cls.create.__func__

    async def failing_kill_create(cls, **kwargs):
        inst = await orig_create(cls, **kwargs)
        inst.kill = AsyncMock(side_effect=RuntimeError("network blip"))
        return inst

    monkeypatch.setattr(sandbox_cls, "create", classmethod(failing_kill_create))

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.e2b"):
        result = asyncio.run(strategy.run(["echo", "ok"], config=cfg))

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert any("failed to kill sandbox" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# run() — network WARNING when network_allowed=False (M-7)
# --------------------------------------------------------------------------


def test_run_warns_when_network_denied(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0, stdout="ok")

    _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    # network_allowed=False is the SandboxConfig default — the call must
    # proceed (not refuse) but emit a WARNING.
    cfg = SandboxConfig(strategy="e2b")
    assert cfg.network_allowed is False

    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.e2b"):
        result = asyncio.run(strategy.run(["echo", "ok"], config=cfg))

    # The call still ran — E2B is not refused on network-deny (M-7 policy).
    assert result.exit_code == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("network containment was requested" in r.message for r in warnings)


def test_run_no_network_warning_when_network_allowed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0, stdout="ok")

    _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)

    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.e2b"):
        asyncio.run(strategy.run(["echo", "ok"], config=cfg))

    assert not any(
        "network containment" in r.message for r in caplog.records
    )


def test_run_warns_when_stdin_supplied(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """E2B has no stdin channel — a supplied ``stdin`` must WARN, not be dropped silently."""

    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0, stdout="ok")

    _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)

    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.e2b"):
        result = asyncio.run(
            strategy.run(["cat"], config=cfg, stdin=b"piped input")
        )

    # The call still completes — stdin is warned-about, not fatal.
    assert result.exit_code == 0
    assert any("stdin was supplied" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# run() — SandboxUnavailable when prerequisites are missing
# --------------------------------------------------------------------------


def test_run_raises_unavailable_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``e2b`` cannot be imported, run() raises SandboxUnavailable cleanly."""
    monkeypatch.setenv("E2B_API_KEY", "e2b_testkey")
    # Ensure no fake e2b lingers in sys.modules, and that an import attempt
    # fails deterministically.
    monkeypatch.delitem(sys.modules, "e2b", raising=False)
    monkeypatch.delitem(sys.modules, "e2b.exceptions", raising=False)

    real_import = __import__

    def _no_e2b(name, *args, **kwargs):
        if name == "e2b" or name.startswith("e2b."):
            raise ImportError("No module named 'e2b'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_e2b)

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    with pytest.raises(SandboxUnavailable, match="not installed"):
        asyncio.run(strategy.run(["echo", "x"], config=cfg))


def test_run_raises_unavailable_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Package present but no key → run() raises SandboxUnavailable."""

    async def fake_run(command, **kwargs):
        return _FakeCommandResult(exit_code=0)

    _install_fake_e2b(monkeypatch, run_impl=fake_run)
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    strategy = E2BSandboxStrategy()
    cfg = SandboxConfig(strategy="e2b", network_allowed=True)
    with pytest.raises(SandboxUnavailable, match="E2B_API_KEY"):
        asyncio.run(strategy.run(["echo", "x"], config=cfg))


# --------------------------------------------------------------------------
# _coerce_exit_code helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (127, 127),
        (-1, -1),
        (None, -1),
        ("5", 5),
        ("not-a-number", -1),
        (object(), -1),
    ],
)
def test_coerce_exit_code(value: object, expected: int) -> None:
    assert _coerce_exit_code(value) == expected


# --------------------------------------------------------------------------
# runner._named_strategy dispatch (M-5)
# --------------------------------------------------------------------------


def test_runner_named_strategy_resolves_e2b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.sandbox.runner import _named_strategy

    # Make the strategy report available so _named_strategy returns it
    # rather than raising the not-available SandboxUnavailable.
    monkeypatch.setattr(
        "opencomputer.sandbox.e2b._e2b_available", lambda: True
    )
    strategy = _named_strategy("e2b")
    assert isinstance(strategy, E2BSandboxStrategy)


def test_runner_named_strategy_e2b_unavailable_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.sandbox.runner import _named_strategy

    monkeypatch.setattr(
        "opencomputer.sandbox.e2b._e2b_available", lambda: False
    )
    with pytest.raises(SandboxUnavailable, match="not available"):
        _named_strategy("e2b")


def test_runner_unknown_strategy_error_lists_e2b() -> None:
    """The 'unknown strategy' help text must advertise 'e2b'."""
    from opencomputer.sandbox.runner import _named_strategy

    with pytest.raises(SandboxUnavailable, match="e2b"):
        _named_strategy("bogus-strategy")
