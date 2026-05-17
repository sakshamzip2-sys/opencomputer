"""Unit tests for the Modal sandbox backend (M2, sandbox-provider-breadth).

The ``modal`` SDK is mocked — no live cloud calls in CI. ``assert_conforms``
runs against the backend via a mock ``Sandbox`` whose ``create.aio``
delegates to :func:`tests.sandbox_conformance.interpret_probe` so the
cloud backend gets exactly the same probe semantics as
``FakeSandboxBackend``.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.sandbox.modal import ModalSandboxStrategy
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxUnavailable
from tests.sandbox_conformance import assert_conforms, interpret_probe


def _make_fake_sandbox(*, returncode=0, stdout="", stderr=""):
    """A fake ``modal.Sandbox`` instance with ``.aio``-styled async methods.

    Mirrors the synchronicity-wrapped surface — ``wait``, ``stdout``,
    ``stderr``, ``terminate`` each carry an ``.aio`` AsyncMock that returns
    the configured value.
    """
    sandbox = MagicMock()
    sandbox.returncode = returncode
    sandbox.wait = MagicMock()
    sandbox.wait.aio = AsyncMock(return_value=returncode)
    sandbox.stdout = MagicMock()
    sandbox.stdout.read = MagicMock()
    sandbox.stdout.read.aio = AsyncMock(return_value=stdout)
    sandbox.stderr = MagicMock()
    sandbox.stderr.read = MagicMock()
    sandbox.stderr.read.aio = AsyncMock(return_value=stderr)
    sandbox.terminate = MagicMock()
    sandbox.terminate.aio = AsyncMock(return_value=None)
    return sandbox


def _make_mock_modal_class(*, create_return=None, create_side_effect=None):
    """Build a mock for ``modal.Sandbox``. ``cls.create.aio`` is the entry point."""
    cls = MagicMock()
    cls.create = MagicMock()
    cls.create.aio = AsyncMock(
        return_value=create_return, side_effect=create_side_effect
    )
    return cls


def _make_mock_modal_app():
    """Mock for ``modal.App`` whose ``lookup`` returns a sentinel app.

    ``run()`` calls ``App.lookup(name, create_if_missing=True)`` (via
    ``asyncio.to_thread``) to obtain the App that ``Sandbox.create``
    runtime-requires. Returns ``(app_cls, sentinel_app)``.
    """
    sentinel_app = MagicMock(name="modal-app")
    app_cls = MagicMock()
    app_cls.lookup = MagicMock(return_value=sentinel_app)
    return app_cls, sentinel_app


def _install_fake_modal(monkeypatch):
    """Inject a bare synthetic ``modal`` module into ``sys.modules``.

    CI does not install the optional ``[modal]`` extra (same posture as
    e2b / daytona — see ``test_e2b_backend.py``). With a placeholder
    ``modal`` module present, the strategy's ``from modal import App,
    Sandbox`` resolves AND ``patch("modal.Sandbox" / "modal.App", …)``
    works — each test then patches in its own fakes.
    """
    modal_mod = types.ModuleType("modal")
    modal_mod.Sandbox = object  # type: ignore[attr-defined]
    modal_mod.App = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modal", modal_mod)


# --- is_available -----------------------------------------------------------


def test_modal_unavailable_without_any_creds(monkeypatch):
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.setattr(
        "opencomputer.sandbox.modal._modal_toml_exists", lambda: False
    )
    # Package present so this genuinely exercises the no-credentials path.
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert ModalSandboxStrategy().is_available() is False


def test_modal_available_with_token_env(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    monkeypatch.setattr(
        "opencomputer.sandbox.modal._modal_toml_exists", lambda: False
    )
    # CI does not install the optional ``[modal]`` extra — stub find_spec.
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "modal" else None,
    )
    assert ModalSandboxStrategy().is_available() is True


def test_modal_available_with_modal_toml_fallback(monkeypatch):
    """An on-disk ``~/.modal.toml`` is the second supported auth path."""
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.setattr(
        "opencomputer.sandbox.modal._modal_toml_exists", lambda: True
    )
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "modal" else None,
    )
    assert ModalSandboxStrategy().is_available() is True


# --- happy path -------------------------------------------------------------


def test_modal_run_happy_path(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    sandbox = _make_fake_sandbox(returncode=0, stdout="hi\n", stderr="")
    cls = _make_mock_modal_class(create_return=sandbox)
    app_cls, sentinel_app = _make_mock_modal_app()
    _install_fake_modal(monkeypatch)
    with patch("modal.Sandbox", cls), patch("modal.App", app_cls):
        result = asyncio.run(
            ModalSandboxStrategy().run(["echo", "hi"], config=SandboxConfig())
        )
    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.stderr == ""
    assert result.strategy_name == "modal"
    cls.create.aio.assert_awaited_once()
    sandbox.terminate.aio.assert_awaited_once()
    # Modal takes argv as varargs — the backend passes argv positionally,
    # NOT a single joined shell string (the e2b/daytona shlex.join pattern).
    assert cls.create.aio.await_args.args == ("echo", "hi")
    # B2 regression: ``Sandbox.create`` runtime-requires an ``App``. The
    # backend MUST look one up and pass it — a backend that omits ``app``
    # (the shipped M2 bug) raises on every real call. Assert the call
    # shape Modal actually requires, so the mock cannot mask a regression.
    app_cls.lookup.assert_called_once()
    assert cls.create.aio.await_args.kwargs["app"] is sentinel_app
    # ``block_network`` enforces the default ``network_allowed=False``.
    assert cls.create.aio.await_args.kwargs["block_network"] is True


# --- non-zero exit ----------------------------------------------------------


def test_modal_non_zero_exit_returns_code(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    sandbox = _make_fake_sandbox(returncode=7, stdout="", stderr="boom")
    cls = _make_mock_modal_class(create_return=sandbox)
    app_cls, _ = _make_mock_modal_app()
    _install_fake_modal(monkeypatch)
    with patch("modal.Sandbox", cls), patch("modal.App", app_cls):
        result = asyncio.run(
            ModalSandboxStrategy().run(["false"], config=SandboxConfig())
        )
    assert result.exit_code == 7
    assert result.stderr == "boom"
    sandbox.terminate.aio.assert_awaited_once()


# --- exception → teardown still runs ----------------------------------------


def test_modal_wait_raises_still_terminates(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    sandbox = _make_fake_sandbox()
    sandbox.wait.aio = AsyncMock(side_effect=RuntimeError("net down"))
    cls = _make_mock_modal_class(create_return=sandbox)
    app_cls, _ = _make_mock_modal_app()
    _install_fake_modal(monkeypatch)
    with (
        patch("modal.Sandbox", cls),
        patch("modal.App", app_cls),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(
            ModalSandboxStrategy().run(["echo", "x"], config=SandboxConfig())
        )
    sandbox.terminate.aio.assert_awaited_once()


# --- SandboxUnavailable when creds dropped between construct and run --------


def test_modal_run_raises_sandbox_unavailable_without_creds(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    monkeypatch.setattr(
        "opencomputer.sandbox.modal._modal_toml_exists", lambda: False
    )
    backend = ModalSandboxStrategy()  # caches available=True
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    # Fake package present so ``run()`` gets PAST the lazy import and
    # genuinely reaches the missing-credentials check.
    _install_fake_modal(monkeypatch)
    with pytest.raises(SandboxUnavailable, match="MODAL"):
        asyncio.run(backend.run(["echo", "x"], config=SandboxConfig()))


# --- conformance suite against the mocked SDK -------------------------------


def test_modal_conforms_against_mocked_sdk(monkeypatch):
    """``assert_conforms`` against the backend with a probe-interpreting mock.

    Unlike Daytona, Modal captures stderr separately (no ``2>&1`` wrap), so
    the backend passes argv to ``create.aio`` unchanged and the mock builds
    a fake Sandbox whose stdout / stderr / returncode come straight from
    :func:`interpret_probe`.
    """
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")

    async def fake_create(*argv, env=None, timeout=None, **_kw):
        del _kw  # absorb extra Modal kwargs (app/image/workdir/etc.) — unused here
        env_dict = dict(env or {})
        exit_code, stdout, stderr = interpret_probe(
            list(argv), env=env_dict, cpu_seconds_limit=timeout or 60,
        )
        return _make_fake_sandbox(
            returncode=exit_code, stdout=stdout, stderr=stderr
        )

    cls = MagicMock()
    cls.create = MagicMock()
    cls.create.aio = AsyncMock(side_effect=fake_create)
    app_cls, _ = _make_mock_modal_app()
    _install_fake_modal(monkeypatch)
    with patch("modal.Sandbox", cls), patch("modal.App", app_cls):
        assert_conforms(ModalSandboxStrategy())


# --- T2.6 wiring ------------------------------------------------------------


def test_modal_in_strategy_name_literal():
    """``"modal"`` is in ``SandboxStrategyName`` — CLI auto-derives via Literal."""
    import typing

    from plugin_sdk.sandbox import SandboxStrategyName

    assert "modal" in typing.get_args(SandboxStrategyName)


def test_modal_resolvable_via_named_strategy(monkeypatch):
    """``runner._named_strategy("modal")`` returns a ``ModalSandboxStrategy``."""
    from opencomputer.sandbox.runner import _named_strategy

    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token")
    monkeypatch.setattr(
        "opencomputer.sandbox.modal._modal_toml_exists", lambda: False
    )
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "modal" else None,
    )
    backend = _named_strategy("modal")
    assert isinstance(backend, ModalSandboxStrategy)
    assert backend.name == "modal"


def test_modal_exported_from_sandbox_package():
    """``from opencomputer.sandbox import ModalSandboxStrategy`` works."""
    from opencomputer.sandbox import ModalSandboxStrategy as Exported

    assert Exported is ModalSandboxStrategy


# --- T2.7 cost rate (F16 gate) ---------------------------------------------


def test_modal_has_nonzero_cost_rate():
    """F16: a paid backend with no rate silently bypasses the session cap."""
    import tempfile
    from pathlib import Path

    from opencomputer.cost_guard.sandbox import (
        DEFAULT_BACKEND_RATES_USD_PER_SECOND,
        SandboxCostGuard,
    )

    assert "modal" in DEFAULT_BACKEND_RATES_USD_PER_SECOND
    assert DEFAULT_BACKEND_RATES_USD_PER_SECOND["modal"] > 0

    with tempfile.TemporaryDirectory() as tmp:
        guard = SandboxCostGuard(storage_path=Path(tmp) / "cost.json")
        assert guard.rate_for("modal") > 0


# --- T2.5 spec gate: module-name shadowing ---------------------------------


def test_modal_module_name_does_not_shadow_real_sdk():
    """`opencomputer/sandbox/modal.py` shares the bare name `modal` with the
    PyPI package. `from modal import App, Sandbox` inside `run()` must
    resolve to the real SDK, not recurse into this strategy module.

    Spec T2.5 mandated this import-order guard. The strategy uses absolute
    imports under `from __future__ import annotations`, so the risk is low
    — but the project has been bitten by module-name collisions before
    (loader `sys.modules` reuse), so the guard is explicit.
    """
    import importlib
    import importlib.util

    if importlib.util.find_spec("modal") is None:
        pytest.skip("modal SDK not installed (optional [modal] extra)")

    import opencomputer.sandbox.modal as oc_modal
    real_modal = importlib.import_module("modal")

    # Distinct modules — the strategy module did not become `modal` itself.
    assert oc_modal is not real_modal
    assert oc_modal.__name__ == "opencomputer.sandbox.modal"
    assert real_modal.__name__ == "modal"
    # The real SDK exposes the names `run()` imports; ours exposes the
    # strategy. `from modal import App, Sandbox` therefore reaches the SDK.
    assert hasattr(real_modal, "Sandbox")
    assert hasattr(real_modal, "App")
    assert hasattr(oc_modal, "ModalSandboxStrategy")
