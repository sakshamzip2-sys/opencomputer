"""Per-session MCP runtime manager (M2 — mcp-openclaw-port).

Validates the opt-in ``SessionMcpRuntimeManager`` that scopes a
:class:`opencomputer.mcp.client.MCPManager` per session-id with idle TTL
+ LRU eviction.

These tests use a stub ``_FakeManager`` factory so we don't spawn real
subprocesses; the manager only exercises lifecycle bookkeeping.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest

from opencomputer.mcp.session_runtime import (
    SessionMcpRuntimeManager,
    SessionMcpRuntimeStats,
)


@pytest.fixture
def fake_factory() -> Generator[tuple[list, MagicMock], None, None]:
    """Build a fake MCPManager factory + list of every instance created."""
    instances: list[MagicMock] = []

    def factory() -> MagicMock:
        m = MagicMock()
        m.start_background_loop = MagicMock()
        m.stop_background_loop = MagicMock()
        m.connect_all_sync = MagicMock(return_value=0)
        m.connections = []
        instances.append(m)
        return m

    mock_factory = MagicMock(side_effect=factory)
    yield instances, mock_factory


# ─── basic lifecycle ─────────────────────────────────────────────


def test_default_idle_ttl_is_300s(fake_factory) -> None:
    _, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    assert mgr.idle_ttl_seconds == 300.0
    assert mgr.max_sessions == 20


def test_get_or_create_returns_same_manager(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    m1 = mgr.get_or_create("session-a")
    m2 = mgr.get_or_create("session-a")
    assert m1 is m2
    assert len(instances) == 1


def test_get_or_create_different_sessions_yield_different_managers(
    fake_factory,
) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    m1 = mgr.get_or_create("session-a")
    m2 = mgr.get_or_create("session-b")
    assert m1 is not m2
    assert len(instances) == 2


def test_dispose_drops_and_stops_manager(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    m1 = mgr.get_or_create("session-a")
    assert mgr.dispose("session-a") is True
    m1.stop_background_loop.assert_called()
    # Re-creating with same id yields a fresh manager.
    m2 = mgr.get_or_create("session-a")
    assert m2 is not m1
    assert len(instances) == 2


def test_dispose_unknown_session_returns_false(fake_factory) -> None:
    _, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    assert mgr.dispose("nope") is False


def test_dispose_all_stops_every_manager(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    mgr.get_or_create("a")
    mgr.get_or_create("b")
    n = mgr.dispose_all()
    assert n == 2
    for inst in instances:
        inst.stop_background_loop.assert_called()


# ─── idle TTL eviction ───────────────────────────────────────────


def test_sweep_idle_evicts_after_ttl(fake_factory) -> None:
    instances, factory = fake_factory
    # Tight TTL so we can test without sleeping real seconds.
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, idle_ttl_seconds=0.1,
    )
    mgr.get_or_create("a")
    time.sleep(0.2)
    evicted = mgr.sweep_idle()
    assert "a" in evicted
    instances[0].stop_background_loop.assert_called()
    assert mgr.active_session_ids() == []


def test_sweep_idle_keeps_recent_sessions(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, idle_ttl_seconds=10.0,
    )
    mgr.get_or_create("recent")
    evicted = mgr.sweep_idle()
    assert evicted == []
    assert "recent" in mgr.active_session_ids()


def test_touch_extends_idle_lifetime(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, idle_ttl_seconds=0.2,
    )
    mgr.get_or_create("a")
    time.sleep(0.15)
    mgr.touch("a")
    time.sleep(0.15)
    # Total elapsed >0.2 but the touch reset the clock < 0.2 ago.
    evicted = mgr.sweep_idle()
    assert evicted == []
    assert "a" in mgr.active_session_ids()


# ─── LRU eviction ────────────────────────────────────────────────


def test_lru_eviction_drops_oldest(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, max_sessions=3,
    )
    a = mgr.get_or_create("a")
    time.sleep(0.01)
    b = mgr.get_or_create("b")
    time.sleep(0.01)
    c = mgr.get_or_create("c")
    time.sleep(0.01)
    d = mgr.get_or_create("d")
    # ``a`` should have been LRU-evicted; manager STOPPED.
    a.stop_background_loop.assert_called()
    assert "a" not in mgr.active_session_ids()
    assert {"b", "c", "d"} == set(mgr.active_session_ids())


def test_lru_eviction_respects_touch_recency(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, max_sessions=3,
    )
    a = mgr.get_or_create("a")
    time.sleep(0.01)
    mgr.get_or_create("b")
    time.sleep(0.01)
    mgr.get_or_create("c")
    time.sleep(0.01)
    # Touch ``a`` so it's now most-recently-used; ``b`` becomes LRU.
    mgr.touch("a")
    time.sleep(0.01)
    mgr.get_or_create("d")
    # ``b`` should be the one evicted.
    assert "a" in mgr.active_session_ids()
    assert "b" not in mgr.active_session_ids()


# ─── stats + introspection ──────────────────────────────────────


def test_active_session_ids_listing(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    mgr.get_or_create("a")
    mgr.get_or_create("b")
    assert sorted(mgr.active_session_ids()) == ["a", "b"]


def test_stats_for_session(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    mgr.get_or_create("a")
    stats = mgr.stats_for_session("a")
    assert stats is not None
    assert isinstance(stats, SessionMcpRuntimeStats)
    assert stats.session_id == "a"
    assert stats.created_at <= time.time()
    assert stats.last_used_at <= time.time()


def test_stats_for_unknown_returns_none(fake_factory) -> None:
    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    assert mgr.stats_for_session("nope") is None


# ─── thread-safety (basic — coarse lock contract) ──────────────


def test_concurrent_get_or_create_returns_same_instance(fake_factory) -> None:
    """Two threads asking for the same session id get the same manager."""
    import threading

    instances, factory = fake_factory
    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    results: list = []

    def worker():
        results.append(mgr.get_or_create("shared"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(r) for r in results}) == 1
    # And the factory was called exactly once.
    assert len(instances) == 1


# ─── config plumbing ────────────────────────────────────────────


def test_mcp_config_session_scoped_defaults_false() -> None:
    from opencomputer.agent.config import MCPConfig
    cfg = MCPConfig()
    assert cfg.session_scoped is False


def test_mcp_config_session_scoped_overrides() -> None:
    from opencomputer.agent.config import MCPConfig
    cfg = MCPConfig(session_scoped=True)
    assert cfg.session_scoped is True
