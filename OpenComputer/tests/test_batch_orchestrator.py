"""Batch parallel-migration orchestrator tests (v1.1 plan-3 M11.2)."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.batch_orchestrator import (
    MAX_BATCH_SIZE,
    BatchConfig,
    BatchUnit,
    NestedBatchError,
    TooManyUnitsError,
    UnitOutcome,
    run_batch,
    validate_units,
)

# ─── validation ────────────────────────────────────────────────────


def test_validate_empty_unit_list_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_units([])


def test_validate_duplicate_unit_id_raises() -> None:
    with pytest.raises(ValueError, match="duplicate unit_id"):
        validate_units(
            [
                BatchUnit(unit_id="a", description="x"),
                BatchUnit(unit_id="a", description="y"),
            ]
        )


def test_validate_empty_description_raises() -> None:
    with pytest.raises(ValueError, match="empty description"):
        validate_units([BatchUnit(unit_id="a", description="")])


def test_validate_nested_batch_in_description_raises() -> None:
    with pytest.raises(NestedBatchError, match="contains '/batch'"):
        validate_units(
            [BatchUnit(unit_id="a", description="please run /batch on these")]
        )


def test_validate_above_max_total_units_raises() -> None:
    units = [BatchUnit(unit_id=f"u{i}", description="x") for i in range(40)]
    with pytest.raises(TooManyUnitsError, match="40 units"):
        validate_units(units)  # default max_total_units=MAX_BATCH_SIZE=30


def test_validate_max_parallel_above_hard_cap_raises() -> None:
    with pytest.raises(ValueError, match="exceeds hard cap"):
        validate_units(
            [BatchUnit(unit_id="a", description="x")],
            max_parallel=MAX_BATCH_SIZE + 1,
        )


def test_validate_accepts_well_formed_unit_list() -> None:
    units = [BatchUnit(unit_id=f"u{i}", description="run a thing") for i in range(5)]
    validate_units(units)  # no raise


# ─── orchestration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_batch_all_succeed() -> None:
    units = [BatchUnit(unit_id=f"u{i}", description=f"unit {i}") for i in range(5)]

    async def fake_spawn(unit: BatchUnit) -> str:
        return f"https://github.com/x/y/pull/{unit.unit_id}"

    result = await run_batch(units, spawn_fn=fake_spawn)
    assert len(result.units) == 5
    assert len(result.successful) == 5
    assert all(u.outcome == UnitOutcome.SUCCESS for u in result.units)
    # Order preserved
    assert [u.unit_id for u in result.units] == [f"u{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_run_batch_one_unit_fails_others_succeed() -> None:
    units = [BatchUnit(unit_id=f"u{i}", description=f"unit {i}") for i in range(5)]

    async def flaky_spawn(unit: BatchUnit) -> str:
        if unit.unit_id == "u2":
            raise RuntimeError("bad codemod")
        return f"https://github.com/x/y/pull/{unit.unit_id}"

    result = await run_batch(units, spawn_fn=flaky_spawn)
    assert len(result.successful) == 4
    assert len(result.failed) == 1
    failed_unit = next(u for u in result.units if u.unit_id == "u2")
    assert failed_unit.outcome == UnitOutcome.FAILED
    assert "bad codemod" in failed_unit.error


@pytest.mark.asyncio
async def test_run_batch_one_unit_times_out() -> None:
    units = [
        BatchUnit(unit_id="fast", description="fast unit"),
        BatchUnit(unit_id="slow", description="slow unit"),
    ]

    async def variable_spawn(unit: BatchUnit) -> str:
        if unit.unit_id == "slow":
            await asyncio.sleep(10)  # exceed timeout
        return f"https://github.com/x/y/pull/{unit.unit_id}"

    result = await run_batch(
        units,
        spawn_fn=variable_spawn,
        config=BatchConfig(per_unit_timeout_seconds=0.1),
    )
    fast = next(u for u in result.units if u.unit_id == "fast")
    slow = next(u for u in result.units if u.unit_id == "slow")
    assert fast.outcome == UnitOutcome.SUCCESS
    assert slow.outcome == UnitOutcome.TIMED_OUT
    assert "timeout" in slow.error


@pytest.mark.asyncio
async def test_run_batch_concurrent_dispatch_respects_max_parallel() -> None:
    """Spawn 10 units with max_parallel=3 — must observe at most 3
    concurrent in-flight at any time."""
    units = [BatchUnit(unit_id=f"u{i}", description=f"unit {i}") for i in range(10)]

    in_flight = 0
    max_observed = 0
    lock = asyncio.Lock()

    async def counting_spawn(unit: BatchUnit) -> str:
        nonlocal in_flight, max_observed
        async with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return f"https://github.com/x/y/pull/{unit.unit_id}"

    await run_batch(
        units,
        spawn_fn=counting_spawn,
        config=BatchConfig(max_parallel=3),
    )
    assert max_observed <= 3
    # Sanity: we did achieve some parallelism (>1)
    assert max_observed > 1


@pytest.mark.asyncio
async def test_run_batch_validates_before_spawning() -> None:
    """Validation errors are raised before any spawn fires.  An empty
    unit list must NOT call spawn_fn even once."""
    spawn_called = False

    async def must_not_be_called(unit: BatchUnit) -> str:
        nonlocal spawn_called
        spawn_called = True
        return ""

    with pytest.raises(ValueError, match="empty"):
        await run_batch([], spawn_fn=must_not_be_called)
    assert not spawn_called


@pytest.mark.asyncio
async def test_run_batch_aggregates_pr_urls() -> None:
    units = [BatchUnit(unit_id=f"u{i}", description=f"unit {i}") for i in range(3)]

    async def fake_spawn(unit: BatchUnit) -> str:
        return f"https://github.com/owner/repo/pull/{int(unit.unit_id[1:]) + 100}"

    result = await run_batch(units, spawn_fn=fake_spawn)
    urls = [u.pr_url for u in result.units]
    assert urls == [
        "https://github.com/owner/repo/pull/100",
        "https://github.com/owner/repo/pull/101",
        "https://github.com/owner/repo/pull/102",
    ]


@pytest.mark.asyncio
async def test_run_batch_records_elapsed_per_unit() -> None:
    units = [BatchUnit(unit_id="u1", description="x")]

    async def slow_spawn(unit: BatchUnit) -> str:
        await asyncio.sleep(0.05)
        return "url"

    result = await run_batch(units, spawn_fn=slow_spawn)
    assert result.units[0].elapsed_seconds >= 0.04


@pytest.mark.asyncio
async def test_run_batch_preserves_unit_order_in_result() -> None:
    """Even though units finish in arbitrary order, the result list
    preserves the input order so the caller's report is deterministic."""
    units = [BatchUnit(unit_id=f"u{i}", description=f"unit {i}") for i in range(5)]
    delays = [0.05, 0.01, 0.04, 0.02, 0.03]

    async def variable_spawn(unit: BatchUnit) -> str:
        idx = int(unit.unit_id[1:])
        await asyncio.sleep(delays[idx])
        return f"url-{unit.unit_id}"

    result = await run_batch(units, spawn_fn=variable_spawn)
    assert [u.unit_id for u in result.units] == ["u0", "u1", "u2", "u3", "u4"]


# ─── nested batch refusal at validation ────────────────────────────


def test_nested_batch_caught_with_uppercase() -> None:
    """Case-insensitive match — '/Batch' or '/BATCH' also rejected."""
    with pytest.raises(NestedBatchError):
        validate_units(
            [BatchUnit(unit_id="a", description="run /Batch please")]
        )


def test_nested_batch_caught_in_middle_of_string() -> None:
    with pytest.raises(NestedBatchError):
        validate_units(
            [BatchUnit(unit_id="a", description="just kidding /batch is bad")]
        )


def test_word_batch_alone_is_fine() -> None:
    """The word 'batch' alone (without '/') is not a problem — only
    the slash-prefixed command is rejected."""
    validate_units([BatchUnit(unit_id="a", description="run a batch of tests")])
