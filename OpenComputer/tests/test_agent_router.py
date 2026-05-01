"""AgentRouter — gateway-level lazy AgentLoop cache.

Production wiring: ``Dispatch._do_dispatch`` calls
``await router.get_or_load(profile_id)`` to get a (possibly cached)
loop, then runs ``loop.run_conversation(...)`` under
``set_profile(profile_home)``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.agent_router import AgentRouter


@pytest.mark.asyncio
async def test_get_or_load_calls_factory_once(tmp_path: Path) -> None:
    factory_calls: list[str] = []

    def factory(profile_id: str, profile_home: Path) -> MagicMock:
        factory_calls.append(profile_id)
        m = MagicMock(name=f"loop-{profile_id}")
        m.profile_id = profile_id
        return m

    router = AgentRouter(
        loop_factory=factory,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )

    loop_a = await router.get_or_load("a")
    loop_a_again = await router.get_or_load("a")
    loop_b = await router.get_or_load("b")

    assert loop_a is loop_a_again
    assert loop_a is not loop_b
    assert factory_calls == ["a", "b"]


@pytest.mark.asyncio
async def test_concurrent_first_load_serializes(tmp_path: Path) -> None:
    """Two simultaneous get_or_load calls for the same profile_id
    must build the loop once, not twice."""
    build_count = 0

    def factory(profile_id: str, profile_home: Path) -> MagicMock:
        nonlocal build_count
        build_count += 1
        return MagicMock(name=profile_id)

    async def slow_get(router: AgentRouter, pid: str) -> MagicMock:
        return await router.get_or_load(pid)

    router = AgentRouter(
        loop_factory=factory,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    a, b = await asyncio.gather(slow_get(router, "x"), slow_get(router, "x"))
    assert a is b
    assert build_count == 1
