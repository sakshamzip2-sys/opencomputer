"""Gap F — lease counting (M4 of mcp-openclaw-port).

Per-session runtimes can evict idle sessions via TTL sweep. Lease
counting protects an in-flight tool call from being evicted mid-call:
``MCPTool.execute`` acquires a lease before dispatching, releases on
completion. The sweep checks lease counts and skips runtimes with
active leases.

API:

* ``LeaseRegistry`` — process-local counter keyed by ``session_id``.
  ``acquire(session_id) -> ReleaseFn`` increments and returns a
  callable that decrements. Idempotent release.
* ``SessionMcpRuntimeManager.sweep_idle`` consults the registry's
  ``has_active_lease(session_id)`` and skips eviction when True.
"""

from __future__ import annotations

import threading

import pytest

from opencomputer.mcp.lease import LeaseRegistry


@pytest.fixture
def registry() -> LeaseRegistry:
    return LeaseRegistry()


# ─── acquire / release ────────────────────────────────────────────


def test_acquire_returns_callable(registry: LeaseRegistry) -> None:
    release = registry.acquire("session-a")
    assert callable(release)
    release()


def test_acquire_increments_count(registry: LeaseRegistry) -> None:
    assert registry.active_leases("session-a") == 0
    r1 = registry.acquire("session-a")
    assert registry.active_leases("session-a") == 1
    r2 = registry.acquire("session-a")
    assert registry.active_leases("session-a") == 2
    r1()
    assert registry.active_leases("session-a") == 1
    r2()
    assert registry.active_leases("session-a") == 0


def test_has_active_lease(registry: LeaseRegistry) -> None:
    assert registry.has_active_lease("session-a") is False
    release = registry.acquire("session-a")
    assert registry.has_active_lease("session-a") is True
    release()
    assert registry.has_active_lease("session-a") is False


def test_release_is_idempotent(registry: LeaseRegistry) -> None:
    """Calling the release fn twice doesn't underflow the count."""
    release = registry.acquire("session-a")
    assert registry.active_leases("session-a") == 1
    release()
    assert registry.active_leases("session-a") == 0
    release()  # second call — must be a no-op
    assert registry.active_leases("session-a") == 0


def test_acquire_with_context_manager(registry: LeaseRegistry) -> None:
    """``acquire_cm(session_id)`` is a context manager wrapping the
    acquire/release pair so callers don't need try/finally."""
    with registry.acquire_cm("session-a"):
        assert registry.has_active_lease("session-a")
    assert not registry.has_active_lease("session-a")


def test_acquire_cm_releases_on_exception(registry: LeaseRegistry) -> None:
    """Exception inside the context still releases the lease."""
    with pytest.raises(RuntimeError):
        with registry.acquire_cm("session-a"):
            raise RuntimeError("boom")
    assert not registry.has_active_lease("session-a")


def test_different_session_ids_independent(registry: LeaseRegistry) -> None:
    r_a = registry.acquire("session-a")
    r_b = registry.acquire("session-b")
    assert registry.active_leases("session-a") == 1
    assert registry.active_leases("session-b") == 1
    r_a()
    assert registry.active_leases("session-a") == 0
    assert registry.active_leases("session-b") == 1
    r_b()


# ─── thread safety ────────────────────────────────────────────────


def test_concurrent_acquire_release_keeps_count_correct(
    registry: LeaseRegistry,
) -> None:
    n_threads = 16
    iterations = 100
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(iterations):
            release = registry.acquire("shared")
            release()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All acquire/release pairs should net to zero.
    assert registry.active_leases("shared") == 0


# ─── SessionMcpRuntimeManager integration ────────────────────────


def test_sweep_idle_skips_session_with_active_lease() -> None:
    """When a lease is held, sweep_idle doesn't evict that session."""
    import time
    from unittest.mock import MagicMock

    from opencomputer.mcp.session_runtime import SessionMcpRuntimeManager

    instances: list[MagicMock] = []

    def factory() -> MagicMock:
        m = MagicMock(stop_background_loop=MagicMock(), connections=[])
        instances.append(m)
        return m

    leases = LeaseRegistry()
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory,
        idle_ttl_seconds=0.1,
        lease_registry=leases,
    )
    mgr.get_or_create("active")
    mgr.get_or_create("idle")

    # Hold a lease on the active session
    release = leases.acquire("active")
    try:
        time.sleep(0.2)
        evicted = mgr.sweep_idle()
        # Only the lease-less session should be evicted
        assert "idle" in evicted
        assert "active" not in evicted
    finally:
        release()


def test_sweep_idle_evicts_session_after_lease_released() -> None:
    """Once the lease is released, sweep_idle evicts on the next pass."""
    import time
    from unittest.mock import MagicMock

    from opencomputer.mcp.session_runtime import SessionMcpRuntimeManager

    def factory() -> MagicMock:
        return MagicMock(stop_background_loop=MagicMock(), connections=[])

    leases = LeaseRegistry()
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory,
        idle_ttl_seconds=0.1,
        lease_registry=leases,
    )
    mgr.get_or_create("a")

    release = leases.acquire("a")
    time.sleep(0.2)
    assert mgr.sweep_idle() == []
    release()
    # Touch is required to update last_used after the lease release;
    # without it the slot's last_used_at is already-stale.
    time.sleep(0.05)
    evicted = mgr.sweep_idle()
    assert "a" in evicted


def test_lease_registry_optional_on_runtime_manager() -> None:
    """Back-compat: omitting lease_registry yields a default empty one."""
    from unittest.mock import MagicMock

    from opencomputer.mcp.session_runtime import SessionMcpRuntimeManager

    def factory() -> MagicMock:
        return MagicMock(stop_background_loop=MagicMock(), connections=[])

    mgr = SessionMcpRuntimeManager(mcp_manager_factory=factory)
    mgr.get_or_create("a")
    # Default sweep_idle still works (no leases held → eviction allowed).
    import time
    mgr.idle_ttl_seconds = 0.05
    time.sleep(0.1)
    evicted = mgr.sweep_idle()
    assert "a" in evicted
