"""Integration tests for the built-in rebind handlers.

Covers:
  - The dotenv handler is registered at priority 20 and reloads .env
  - The provider handler is registered at priority 60 and rebuilds
    provider client
  - Handler order matches the documented sequence (env BEFORE provider
    so provider reads fresh env keys)
  - Registry invocation through ``_apply_pending_profile_swap`` actually
    fires the built-ins
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Reset dotenv tracker + clear test keys per test."""
    from opencomputer.agent import dotenv_tracker
    for k in ("REBIND_KEY_A", "REBIND_KEY_B", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    dotenv_tracker._reset_for_tests()
    yield
    dotenv_tracker._reset_for_tests()


def test_install_builtin_handlers_registers_dotenv_and_provider(_isolate_env):
    """AgentLoop init must register the two built-in handlers."""
    # Build a minimal AgentLoop without running it — we only inspect the
    # rebind registry. Construct against the smallest viable config.
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop

    cfg = Config(
        model=ModelConfig(provider="anthropic", model="claude-sonnet-4-6"),
        session=SessionConfig(),
    )

    fake_provider = SimpleNamespace(name="fake", model="m")
    loop = AgentLoop.__new__(AgentLoop)
    # Skip __init__; manually install just the registry + handlers so we
    # don't bring up the whole loop (DB, memory, plugins, ...).
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry

    loop._profile_rebind_registry = ProfileRebindRegistry()
    loop.config = cfg
    loop.provider = fake_provider
    AgentLoop._install_builtin_rebind_handlers(loop)

    names = loop.profile_rebind_handler_names
    assert "dotenv" in names
    assert "provider" in names
    # Order assertion: dotenv MUST run before provider so the provider
    # rebuild sees fresh env keys.
    assert names.index("dotenv") < names.index("provider")


@pytest.mark.asyncio
async def test_dotenv_handler_swaps_env_keys(tmp_path: Path, _isolate_env):
    """Direct invocation of the dotenv handler reloads .env."""
    profile_root = tmp_path / "profiles" / "p1"
    profile_root.mkdir(parents=True)
    home_dir = profile_root / "home"
    home_dir.mkdir()
    (profile_root / ".env").write_text("REBIND_KEY_A=alpha\n")

    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry

    cfg = Config(
        model=ModelConfig(provider="anthropic", model="claude-sonnet-4-6"),
        session=SessionConfig(),
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop._profile_rebind_registry = ProfileRebindRegistry()
    loop.config = cfg
    loop.provider = SimpleNamespace(name="x", model="m")
    AgentLoop._install_builtin_rebind_handlers(loop)

    # Invoke the registry directly with profile_root/home as new_home.
    results = await loop._profile_rebind_registry.invoke(home_dir, None)
    by_name = {r.name: r for r in results}

    # dotenv handler must succeed and have set the env key.
    assert by_name["dotenv"].error is None
    assert os.environ["REBIND_KEY_A"] == "alpha"


@pytest.mark.asyncio
async def test_provider_handler_rebuilds_provider(tmp_path: Path, _isolate_env):
    """Provider handler calls lookup_provider for the configured name."""
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = Config(
        model=ModelConfig(provider="fake_test_prov", model="m"),
        session=SessionConfig(),
    )

    rebuilt_count = {"n": 0}

    class _FakeProvider:
        name = "fake_test_prov"
        model = "m"

        def __init__(self) -> None:
            rebuilt_count["n"] += 1

    # Register the fake provider so lookup_provider finds it.
    plugin_registry.providers["fake_test_prov"] = _FakeProvider

    try:
        loop = AgentLoop.__new__(AgentLoop)
        loop._profile_rebind_registry = ProfileRebindRegistry()
        loop.config = cfg
        loop.provider = _FakeProvider()  # initial provider
        # Pre-cache the handoff adapter so we can verify invalidation.
        loop._handoff_provider_adapter = MagicMock()
        AgentLoop._install_builtin_rebind_handlers(loop)
        # Drop the config rebind handler so this test isolates the
        # provider rebuild logic. Without this, the config handler runs
        # first @ priority 50, reloads "default" config from tmp_path
        # (no YAML present), and clobbers our test cfg with the default
        # provider name.
        loop._profile_rebind_registry.unregister("config")
        loop._profile_rebind_registry.unregister("session_db")
        loop._profile_rebind_registry.unregister("consent_gate")

        # The initial __init__ above counted once.
        initial_count = rebuilt_count["n"]

        results = await loop._profile_rebind_registry.invoke(tmp_path, None)
        by_name = {r.name: r for r in results}

        assert by_name["provider"].error is None
        # Provider was rebuilt — count incremented.
        assert rebuilt_count["n"] == initial_count + 1
        # Cached handoff adapter must be invalidated.
        assert loop._handoff_provider_adapter is None
    finally:
        plugin_registry.providers.pop("fake_test_prov", None)


@pytest.mark.asyncio
async def test_provider_handler_closes_old_provider_if_close_exists(
    tmp_path: Path, _isolate_env,
):
    """If the old provider exposes close(), it must be called."""
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = Config(
        model=ModelConfig(provider="closeable_prov", model="m"),
        session=SessionConfig(),
    )

    closed = {"flag": False}

    class _CloseableProvider:
        name = "closeable_prov"
        model = "m"

        def close(self) -> None:
            closed["flag"] = True

    plugin_registry.providers["closeable_prov"] = _CloseableProvider
    try:
        loop = AgentLoop.__new__(AgentLoop)
        loop._profile_rebind_registry = ProfileRebindRegistry()
        loop.config = cfg
        loop.provider = _CloseableProvider()
        AgentLoop._install_builtin_rebind_handlers(loop)
        loop._profile_rebind_registry.unregister("config")
        loop._profile_rebind_registry.unregister("session_db")
        loop._profile_rebind_registry.unregister("consent_gate")

        await loop._profile_rebind_registry.invoke(tmp_path, None)
        assert closed["flag"] is True
    finally:
        plugin_registry.providers.pop("closeable_prov", None)


@pytest.mark.asyncio
async def test_provider_handler_close_exception_does_not_wedge_swap(
    tmp_path: Path, _isolate_env,
):
    """Old provider close() that raises must not stop the new build."""
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = Config(
        model=ModelConfig(provider="bad_close_prov", model="m"),
        session=SessionConfig(),
    )

    class _BadCloseProvider:
        name = "bad_close_prov"
        model = "m"

        def close(self) -> None:
            raise RuntimeError("close failed")

    plugin_registry.providers["bad_close_prov"] = _BadCloseProvider
    try:
        loop = AgentLoop.__new__(AgentLoop)
        loop._profile_rebind_registry = ProfileRebindRegistry()
        loop.config = cfg
        old_provider = _BadCloseProvider()
        loop.provider = old_provider
        AgentLoop._install_builtin_rebind_handlers(loop)
        loop._profile_rebind_registry.unregister("config")
        loop._profile_rebind_registry.unregister("session_db")
        loop._profile_rebind_registry.unregister("consent_gate")

        results = await loop._profile_rebind_registry.invoke(tmp_path, None)
        by_name = {r.name: r for r in results}

        # Provider handler must succeed despite the close() failure.
        assert by_name["provider"].error is None
        # And the provider must have been replaced.
        assert loop.provider is not old_provider
    finally:
        plugin_registry.providers.pop("bad_close_prov", None)


@pytest.mark.asyncio
async def test_apply_pending_profile_swap_invokes_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_env,
):
    """End-to-end: _apply_pending_profile_swap awaits the registry."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    # Seed a "stocks" profile with a .env that sets REBIND_KEY_A.
    profile_root = tmp_path / "profiles" / "stocks"
    profile_root.mkdir(parents=True)
    (profile_root / "home").mkdir()
    (profile_root / "home" / "MEMORY.md").write_text("memory-stocks")
    (profile_root / "home" / "USER.md").write_text("user-stocks")
    (profile_root / "home" / "SOUL.md").write_text("soul-stocks")
    (profile_root / ".env").write_text("REBIND_KEY_A=loaded-from-stocks\n")

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import _apply_pending_profile_swap
    from opencomputer.agent.profile_rebind import ProfileRebindRegistry

    runtime = SimpleNamespace(custom={
        "active_profile_id": "default",
        "pending_profile_id": "stocks",
    })

    registry = ProfileRebindRegistry()
    seen: list[tuple[Path, Path | None]] = []

    def _spy(new_home: Path, old_home: Path | None) -> None:
        seen.append((new_home, old_home))

    registry.register("spy", _spy, priority=10)

    # No memory — we're only verifying registry.invoke happens.
    result = await _apply_pending_profile_swap(
        runtime,
        memory=None,
        prompt_snapshots=None,
        sid="sid-1",
        rebind_registry=registry,
    )

    assert result == "stocks"
    assert len(seen) == 1
    seen_new, seen_old = seen[0]
    # new_home is the stocks profile's home/ subdir.
    assert seen_new == tmp_path / "profiles" / "stocks" / "home"
    # old_home is the default's home/ subdir.
    assert seen_old == tmp_path / "home"
