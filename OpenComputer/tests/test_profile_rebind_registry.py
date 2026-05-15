"""Tests for the ProfileRebindRegistry — the rebind-handler composition
primitive that closes the §3 split-brain documented in
``docs/plans/profile-handoff-investigation.md``.

Coverage:
  - Empty registry no-op
  - Single-handler invocation with correct args (new_home, old_home)
  - Multi-handler ordered invocation (priority controls order)
  - Handler exception isolation (one raise doesn't stop the rest)
  - Idempotent re-registration of the same handler
  - Sync + async handler support
  - Unregister
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencomputer.agent.profile_rebind import (
    ProfileRebindRegistry,
    RebindHandlerResult,
)


@pytest.mark.asyncio
async def test_empty_registry_invoke_is_noop(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    # Must not raise and must return an empty result list.
    results = await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert results == []


@pytest.mark.asyncio
async def test_single_sync_handler_called_with_args(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    seen: list[tuple[Path, Path]] = []

    def handler(new_home: Path, old_home: Path) -> None:
        seen.append((new_home, old_home))

    reg.register("h1", handler)
    new = tmp_path / "new"
    old = tmp_path / "old"
    await reg.invoke(new, old)

    assert seen == [(new, old)]


@pytest.mark.asyncio
async def test_single_async_handler_awaited(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    awaited: list[str] = []

    async def handler(new_home: Path, old_home: Path) -> None:
        await asyncio.sleep(0)
        awaited.append("ran")

    reg.register("async_h", handler)
    await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert awaited == ["ran"]


@pytest.mark.asyncio
async def test_multi_handler_ordered_by_priority(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    order: list[str] = []

    def h_late(new: Path, old: Path) -> None:
        order.append("late")

    def h_first(new: Path, old: Path) -> None:
        order.append("first")

    def h_mid(new: Path, old: Path) -> None:
        order.append("mid")

    # Lower priority value = runs earlier (similar to InjectionEngine).
    reg.register("late", h_late, priority=100)
    reg.register("first", h_first, priority=0)
    reg.register("mid", h_mid, priority=50)

    await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert order == ["first", "mid", "late"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_stop_others(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    fired: list[str] = []

    def broken(new: Path, old: Path) -> None:
        fired.append("broken")
        raise RuntimeError("boom")

    def ok(new: Path, old: Path) -> None:
        fired.append("ok")

    reg.register("broken", broken, priority=0)
    reg.register("ok", ok, priority=10)

    results = await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert fired == ["broken", "ok"]
    assert results[0].error is not None
    assert results[0].name == "broken"
    assert results[1].error is None
    assert results[1].name == "ok"


@pytest.mark.asyncio
async def test_async_handler_exception_isolated(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    fired: list[str] = []

    async def broken(new: Path, old: Path) -> None:
        fired.append("broken")
        raise ValueError("async boom")

    def ok(new: Path, old: Path) -> None:
        fired.append("ok")

    reg.register("a_broken", broken, priority=0)
    reg.register("a_ok", ok, priority=10)
    results = await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert fired == ["broken", "ok"]
    assert isinstance(results[0].error, ValueError)


def test_re_register_same_name_replaces(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()

    def h1(new: Path, old: Path) -> None:
        pass

    def h2(new: Path, old: Path) -> None:
        pass

    reg.register("dup", h1)
    reg.register("dup", h2)  # idempotent replace, not duplicate

    assert reg.handler_count == 1
    assert reg.get("dup") is h2


def test_unregister_returns_true_when_present(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    reg.register("h", lambda new, old: None)
    assert reg.unregister("h") is True
    assert reg.handler_count == 0
    assert reg.unregister("h") is False


def test_register_rejects_invalid_handler(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    with pytest.raises(TypeError, match="callable"):
        reg.register("bad", "not a callable")  # type: ignore[arg-type]


def test_register_rejects_empty_name(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        reg.register("", lambda new, old: None)


@pytest.mark.asyncio
async def test_invoke_validates_path_args(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    reg.register("h", lambda new, old: None)
    with pytest.raises(TypeError, match="Path"):
        await reg.invoke("not a path", tmp_path / "old")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Path"):
        await reg.invoke(tmp_path / "new", "not a path")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_results_preserve_invocation_order(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()

    def h_a(new: Path, old: Path) -> None:
        pass

    def h_b(new: Path, old: Path) -> None:
        pass

    def h_c(new: Path, old: Path) -> None:
        pass

    reg.register("c", h_c, priority=30)
    reg.register("a", h_a, priority=10)
    reg.register("b", h_b, priority=20)

    results = await reg.invoke(tmp_path / "new", tmp_path / "old")
    assert [r.name for r in results] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_handler_receives_actual_path_objects(tmp_path: Path) -> None:
    reg = ProfileRebindRegistry()
    captured: dict[str, object] = {}

    def h(new: Path, old: Path) -> None:
        captured["new_type"] = type(new)
        captured["old_type"] = type(old)

    reg.register("h", h)
    await reg.invoke(tmp_path / "new_home", tmp_path / "old_home")
    assert captured["new_type"] is type(tmp_path)
    assert captured["old_type"] is type(tmp_path)


@pytest.mark.asyncio
async def test_invoke_with_none_old_home_for_first_swap(tmp_path: Path) -> None:
    """First-ever swap has no old profile home — handlers must tolerate None."""
    reg = ProfileRebindRegistry()
    seen: list[tuple[Path, Path | None]] = []

    def h(new: Path, old: Path | None) -> None:
        seen.append((new, old))

    reg.register("h", h)
    await reg.invoke(tmp_path / "new", None)  # type: ignore[arg-type]
    assert seen == [(tmp_path / "new", None)]


def test_registry_handler_result_dataclass() -> None:
    r = RebindHandlerResult(name="x", error=None, duration_ms=0.5)
    assert r.name == "x"
    assert r.error is None
    assert r.duration_ms == 0.5

    err = RuntimeError("nope")
    r2 = RebindHandlerResult(name="y", error=err, duration_ms=1.2)
    assert r2.error is err
