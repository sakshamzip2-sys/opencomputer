"""Profile-context ContextVar — task-scoped active profile home.

Tests the primitive that lets two concurrent asyncio.Task instances each
see a different `current_profile_home`, which is what makes parallel
multi-profile routing safe in `Dispatch._do_dispatch`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugin_sdk.profile_context import current_profile_home, set_profile


def test_default_value_is_none() -> None:
    assert current_profile_home.get() is None


def test_set_profile_sets_value() -> None:
    p = Path("/tmp/profile-a")
    with set_profile(p):
        assert current_profile_home.get() == p
    assert current_profile_home.get() is None


def test_set_profile_resets_on_exception() -> None:
    p = Path("/tmp/profile-a")
    with pytest.raises(RuntimeError):
        with set_profile(p):
            raise RuntimeError("boom")
    assert current_profile_home.get() is None


def test_nested_set_profile_restores_outer() -> None:
    a = Path("/tmp/profile-a")
    b = Path("/tmp/profile-b")
    with set_profile(a):
        assert current_profile_home.get() == a
        with set_profile(b):
            assert current_profile_home.get() == b
        assert current_profile_home.get() == a
    assert current_profile_home.get() is None


@pytest.mark.asyncio
async def test_isolation_between_concurrent_tasks() -> None:
    """Two simultaneous tasks each set their own profile and observe
    only their own value — the central guarantee Option A relies on."""
    a = Path("/tmp/profile-a")
    b = Path("/tmp/profile-b")
    barrier = asyncio.Barrier(2)
    a_seen: list[Path | None] = []
    b_seen: list[Path | None] = []

    async def in_a() -> None:
        with set_profile(a):
            await barrier.wait()
            await asyncio.sleep(0.01)
            a_seen.append(current_profile_home.get())

    async def in_b() -> None:
        with set_profile(b):
            await barrier.wait()
            await asyncio.sleep(0.01)
            b_seen.append(current_profile_home.get())

    await asyncio.gather(in_a(), in_b())
    assert a_seen == [a]
    assert b_seen == [b]
