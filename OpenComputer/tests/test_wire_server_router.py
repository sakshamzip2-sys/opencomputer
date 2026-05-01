"""WireServer routes through AgentRouter (Phase 2 Task 2.6, F4 fix).

For v1, wire clients (TUI, IDE) get the default profile only — per-call
binding via wire is a v1.1 follow-up. This test pins the contract:
- WireServer accepts router= or loop= (not both, not neither).
- Wire RPC routes via router.get_or_load("default") + set_profile.
- Backwards-compat: existing loop= callers still work.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.agent_router import AgentRouter
from opencomputer.gateway.wire_server import WireServer


def test_wire_server_init_rejects_both_loop_and_router() -> None:
    """Passing both loop= and router= must raise ValueError."""
    fake_loop = MagicMock()
    fake_router = MagicMock(spec=AgentRouter)
    with pytest.raises(ValueError, match="not both"):
        WireServer(loop=fake_loop, router=fake_router)


def test_wire_server_init_rejects_neither() -> None:
    """Passing neither loop= nor router= must raise ValueError."""
    with pytest.raises(ValueError, match="either"):
        WireServer()


def test_wire_server_init_legacy_loop_wraps_into_router() -> None:
    """Legacy WireServer(loop=...) should wrap the loop into a one-entry router."""
    fake_loop = MagicMock()
    server = WireServer(loop=fake_loop)
    # Legacy attribute must still be accessible for backwards-compat callers.
    assert server.loop is fake_loop
    # The wrapped router must have the loop registered under "default".
    assert server._router._loops.get("default") is fake_loop


def test_wire_server_init_router_works(tmp_path: Path) -> None:
    """Multi-profile WireServer(router=...) construction also works."""
    fake_loop = MagicMock()
    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    router._loops["default"] = fake_loop
    server = WireServer(router=router)
    # Router-only construction: no legacy loop attribute.
    assert server.loop is None
    assert server._router is router
