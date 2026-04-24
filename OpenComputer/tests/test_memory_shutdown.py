"""II.5 — formal memory-provider shutdown.

Tests the shutdown lifecycle added in II.5 so memory providers (Honcho et al.)
can flush pending writes and close httpx clients on process exit.

Contract:
  * ``MemoryProvider.shutdown()`` default is a no-op — existing providers
    without an override must still work.
  * ``MemoryBridge`` tracks every provider it wraps in a class-level
    registry so ``MemoryBridge.shutdown_all()`` can iterate them at atexit.
  * Multiple providers shut down in registration order — deterministic.
  * One provider raising from ``shutdown()`` MUST NOT stop others from
    shutting down (``asyncio.gather(..., return_exceptions=True)``).
  * ``shutdown_all()`` is idempotent: a second call is a clean no-op.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencomputer.agent.memory_bridge import MemoryBridge
from opencomputer.agent.memory_context import MemoryContext
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.memory import MemoryProvider
from plugin_sdk.tool_contract import ToolSchema


def _make_ctx(provider: Any | None = None) -> MemoryContext:
    """Cheap MemoryContext for bridge construction tests."""

    class _DummyManager:
        pass

    class _DummyDB:
        pass

    return MemoryContext(
        manager=_DummyManager(),
        db=_DummyDB(),
        session_id_provider=lambda: "test-session",
        provider=provider,
    )


class _MinimalProvider(MemoryProvider):
    """Stub that satisfies the abstract methods and records shutdown calls."""

    def __init__(self, *, provider_id: str = "test:minimal") -> None:
        self._id = provider_id
        self.shutdown_calls: int = 0

    @property
    def provider_id(self) -> str:
        return self._id

    def tool_schemas(self) -> list[ToolSchema]:
        return []

    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="", is_error=False)

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        return None

    async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
        return None

    async def health_check(self) -> bool:
        return True


class _ShutdownTrackingProvider(_MinimalProvider):
    """Records that ``shutdown`` was awaited (and how many times)."""

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _ExplodingShutdownProvider(_MinimalProvider):
    """Raises from ``shutdown`` — the bridge must swallow the failure."""

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        raise RuntimeError("provider blew up during shutdown")


@pytest.fixture(autouse=True)
def _reset_bridge_registry():
    """Ensure the class-level registry is empty before/after every test."""
    MemoryBridge._reset_shutdown_registry()
    yield
    MemoryBridge._reset_shutdown_registry()


def test_default_shutdown_is_noop():
    """Providers that don't override ``shutdown`` inherit a no-op default."""
    provider = _MinimalProvider()
    # Must not raise, must not require await-interception — default is an
    # async coroutine that resolves to None.
    result = asyncio.run(provider.shutdown())
    assert result is None


def test_bridge_registers_provider_on_construction():
    """Non-None providers get auto-registered for atexit shutdown."""
    provider = _ShutdownTrackingProvider()
    MemoryBridge(_make_ctx(provider))
    # The provider should be tracked so shutdown_all() can find it.
    assert provider in MemoryBridge._registered_providers()


def test_bridge_does_not_register_none_provider():
    """A bridge without a provider must not pollute the registry."""
    MemoryBridge(_make_ctx(provider=None))
    assert MemoryBridge._registered_providers() == []


def test_shutdown_all_calls_shutdown_once():
    """One provider: ``shutdown`` is awaited exactly once."""
    provider = _ShutdownTrackingProvider()
    MemoryBridge(_make_ctx(provider))
    asyncio.run(MemoryBridge.shutdown_all())
    assert provider.shutdown_calls == 1


def test_shutdown_all_iterates_multiple_providers_in_order():
    """Multiple bridges: shutdown awaits every provider in registration order."""
    call_log: list[str] = []

    class _OrderedProvider(_MinimalProvider):
        async def shutdown(self) -> None:
            call_log.append(self.provider_id)

    a = _OrderedProvider(provider_id="provider-a")
    b = _OrderedProvider(provider_id="provider-b")
    c = _OrderedProvider(provider_id="provider-c")
    MemoryBridge(_make_ctx(a))
    MemoryBridge(_make_ctx(b))
    MemoryBridge(_make_ctx(c))
    asyncio.run(MemoryBridge.shutdown_all())
    assert call_log == ["provider-a", "provider-b", "provider-c"]


def test_shutdown_all_survives_exception_in_one_provider():
    """If one provider raises, the others still shut down."""
    good_before = _ShutdownTrackingProvider(provider_id="good-before")
    bad = _ExplodingShutdownProvider(provider_id="bad")
    good_after = _ShutdownTrackingProvider(provider_id="good-after")
    MemoryBridge(_make_ctx(good_before))
    MemoryBridge(_make_ctx(bad))
    MemoryBridge(_make_ctx(good_after))

    # Must not raise — bridge uses gather(return_exceptions=True).
    asyncio.run(MemoryBridge.shutdown_all())

    assert good_before.shutdown_calls == 1
    assert bad.shutdown_calls == 1
    assert good_after.shutdown_calls == 1


def test_shutdown_all_is_idempotent():
    """Calling shutdown_all twice must not re-shut-down providers or error."""
    provider = _ShutdownTrackingProvider()
    MemoryBridge(_make_ctx(provider))
    asyncio.run(MemoryBridge.shutdown_all())
    asyncio.run(MemoryBridge.shutdown_all())
    # Idempotent: the second call is a no-op, not a second shutdown.
    assert provider.shutdown_calls == 1


def test_shutdown_all_empty_registry_is_noop():
    """With no providers registered, shutdown_all is a clean no-op."""
    # Must not raise even when nothing was ever registered.
    asyncio.run(MemoryBridge.shutdown_all())


def test_duplicate_registration_registers_once():
    """Same provider wrapped by two bridges is tracked only once.

    Protects against ``shutdown_all`` awaiting the same provider twice
    (which could double-close an httpx client).
    """
    provider = _ShutdownTrackingProvider()
    MemoryBridge(_make_ctx(provider))
    MemoryBridge(_make_ctx(provider))  # same instance, second wrap
    asyncio.run(MemoryBridge.shutdown_all())
    assert provider.shutdown_calls == 1


def test_honcho_provider_shutdown_closes_http_client():
    """HonchoSelfHostedProvider.shutdown() must await aclose on its httpx client."""
    # Load the Honcho provider via the same synthetic-package loader used
    # elsewhere in the suite (extension dir has a hyphen, not importable).
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path

    ext_dir = (
        Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"
    )
    pkg_name = "_honcho_ii5_shutdown_pkg"
    if f"{pkg_name}.provider" not in sys.modules:
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(ext_dir), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(ext_dir)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", ext_dir / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)
    provider_mod = sys.modules[f"{pkg_name}.provider"]

    import httpx

    closed: dict[str, bool] = {"val": False}

    class _TrackingAsyncClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            closed["val"] = True
            await super().aclose()

    client = _TrackingAsyncClient(base_url="http://test.invalid")
    prov = provider_mod.HonchoSelfHostedProvider(http_client=client)
    asyncio.run(prov.shutdown())
    assert closed["val"] is True


def test_honcho_provider_shutdown_is_idempotent_when_already_closed():
    """Double-shutdown on HonchoSelfHostedProvider must not raise."""
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path

    ext_dir = (
        Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"
    )
    pkg_name = "_honcho_ii5_shutdown_pkg"
    if f"{pkg_name}.provider" not in sys.modules:
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(ext_dir), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(ext_dir)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", ext_dir / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)
    provider_mod = sys.modules[f"{pkg_name}.provider"]

    import httpx

    prov = provider_mod.HonchoSelfHostedProvider(
        http_client=httpx.AsyncClient(base_url="http://test.invalid")
    )
    asyncio.run(prov.shutdown())
    # Second shutdown must be a clean no-op even though the client is closed.
    asyncio.run(prov.shutdown())


def test_cli_atexit_handler_runs_shutdown_all(monkeypatch):
    """The CLI registers an atexit handler that drains MemoryBridge.shutdown_all.

    Verifies that ``opencomputer.cli`` exposes a module-level function
    (``_memory_shutdown_atexit``) which, when invoked, awaits
    ``MemoryBridge.shutdown_all`` — this is what the atexit callback runs
    at CLI process exit.
    """
    import opencomputer.cli as cli_module

    # The helper must exist as a stable symbol so atexit.register can hold it.
    assert hasattr(cli_module, "_memory_shutdown_atexit"), (
        "cli must expose _memory_shutdown_atexit for atexit to call"
    )

    provider = _ShutdownTrackingProvider()
    MemoryBridge(_make_ctx(provider))
    # Run the atexit callback synchronously — it's responsible for its own
    # event-loop setup (atexit runs outside any loop).
    cli_module._memory_shutdown_atexit()
    assert provider.shutdown_calls == 1
