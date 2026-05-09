"""Tests for /batch slash command (M11.2 wiring)."""

from __future__ import annotations

import json

import pytest

from opencomputer.agent.batch_orchestrator import BatchUnit
from opencomputer.agent.slash_commands_impl.batch_cmd import (
    BatchCommand,
    _format_result,
    _parse_unit,
)
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.runtime_context import RuntimeContext

# ─── _parse_unit ────────────────────────────────────────────────────


def test_parse_unit_minimal() -> None:
    u = _parse_unit({"unit_id": "a", "description": "do it"}, idx=0)
    assert u.unit_id == "a"
    assert u.description == "do it"
    assert u.verify == ""


def test_parse_unit_with_verify() -> None:
    u = _parse_unit(
        {"unit_id": "b", "description": "do", "verify": "pytest -x"},
        idx=0,
    )
    assert u.verify == "pytest -x"


def test_parse_unit_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="not an object"):
        _parse_unit("not-a-dict", idx=3)


def test_parse_unit_rejects_missing_id() -> None:
    with pytest.raises(ValueError, match="unit_id"):
        _parse_unit({"description": "x"}, idx=0)


def test_parse_unit_rejects_empty_description() -> None:
    with pytest.raises(ValueError, match="description"):
        _parse_unit({"unit_id": "a", "description": "  "}, idx=0)


def test_parse_unit_rejects_non_string_verify() -> None:
    with pytest.raises(ValueError, match="verify"):
        _parse_unit(
            {"unit_id": "a", "description": "x", "verify": 42},
            idx=0,
        )


# ─── /batch slash command ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_no_args_prints_usage() -> None:
    cmd = BatchCommand()
    result = await cmd.execute("", RuntimeContext())
    assert "Usage" in result.output


@pytest.mark.asyncio
async def test_batch_invalid_json_prints_usage() -> None:
    cmd = BatchCommand()
    result = await cmd.execute("not json", RuntimeContext())
    assert "JSON" in result.output


@pytest.mark.asyncio
async def test_batch_empty_list_rejected() -> None:
    cmd = BatchCommand()
    result = await cmd.execute("[]", RuntimeContext())
    assert "non-empty" in result.output


@pytest.mark.asyncio
async def test_batch_malformed_unit_rejected() -> None:
    cmd = BatchCommand()
    result = await cmd.execute(
        json.dumps([{"unit_id": "a"}]),  # missing description
        RuntimeContext(),
    )
    assert "description" in result.output


@pytest.mark.asyncio
async def test_batch_no_factory_returns_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a DelegateTool factory, /batch refuses cleanly instead
    of crashing in the orchestrator."""
    monkeypatch.setattr(DelegateTool, "_factory_class_level", None)
    cmd = BatchCommand()
    units = [{"unit_id": "u", "description": "task"}]
    result = await cmd.execute(json.dumps(units), RuntimeContext())
    assert "DelegateTool factory not initialized" in result.output


@pytest.mark.asyncio
async def test_batch_with_factory_runs_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: /batch with valid units + a DelegateTool factory
    runs run_batch_via_delegate and aggregates a result."""
    from opencomputer.agent import batch_runner
    from opencomputer.tools.delegate import DelegateTool

    # Set the class-level factory to a no-op so DelegateTool() instances
    # have a non-None ._factory.
    monkeypatch.setattr(
        DelegateTool, "_factory_class_level", lambda: None
    )

    captured: list[BatchUnit] = []

    async def _fake_run(units, *, delegate_tool, config):
        captured.extend(units)
        from opencomputer.agent.batch_orchestrator import (
            BatchRunResult,
            UnitOutcome,
            UnitResult,
        )

        return BatchRunResult(
            units=tuple(
                UnitResult(
                    unit_id=u.unit_id,
                    outcome=UnitOutcome.SUCCESS,
                    pr_url=f"https://github.com/o/r/pull/{i+1}",
                    elapsed_seconds=0.1,
                )
                for i, u in enumerate(units)
            ),
            aborted_before_spawn=(),
        )

    monkeypatch.setattr(batch_runner, "run_batch_via_delegate", _fake_run)

    cmd = BatchCommand()
    units_json = json.dumps(
        [
            {"unit_id": "a", "description": "rename in foo"},
            {"unit_id": "b", "description": "rename in bar"},
        ]
    )
    result = await cmd.execute(units_json, RuntimeContext())
    assert "/batch finished" in result.output
    assert "2 success" in result.output
    assert "https://github.com/o/r/pull/1" in result.output
    assert "https://github.com/o/r/pull/2" in result.output
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_batch_nested_batch_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unit description containing /batch must be rejected — defence
    in depth on top of orchestrator's own check."""
    from opencomputer.tools.delegate import DelegateTool

    monkeypatch.setattr(
        DelegateTool, "_factory_class_level", lambda: None
    )
    cmd = BatchCommand()
    units = [
        {"unit_id": "evil", "description": "do something with /batch sub"}
    ]
    result = await cmd.execute(json.dumps(units), RuntimeContext())
    assert "validation failed" in result.output or "/batch" in result.output


# ─── _format_result ────────────────────────────────────────────────


def test_format_result_mixed_outcomes() -> None:
    from opencomputer.agent.batch_orchestrator import (
        BatchRunResult,
        UnitOutcome,
        UnitResult,
    )

    rr = BatchRunResult(
        units=(
            UnitResult(
                unit_id="a",
                outcome=UnitOutcome.SUCCESS,
                pr_url="https://github.com/x/y/pull/1",
                elapsed_seconds=2.0,
            ),
            UnitResult(
                unit_id="b",
                outcome=UnitOutcome.FAILED,
                error="oops",
                elapsed_seconds=1.0,
            ),
            UnitResult(
                unit_id="c",
                outcome=UnitOutcome.TIMED_OUT,
                elapsed_seconds=600.0,
            ),
        ),
        aborted_before_spawn=("d",),
    )
    s = _format_result(rr)
    assert "1 success" in s
    assert "1 failed" in s
    assert "1 timed out" in s
    assert "1 aborted" in s
    assert "https://github.com/x/y/pull/1" in s
    assert "oops" in s
