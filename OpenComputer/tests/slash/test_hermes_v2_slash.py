"""Tests for Hermes-v2 parity slash commands: /rollback /busy /details /mouse."""

import asyncio
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.slash_commands_impl.busy_cmd import BusyCommand
from opencomputer.agent.slash_commands_impl.details_cmd import DetailsCommand
from opencomputer.agent.slash_commands_impl.mouse_cmd import MouseCommand
from opencomputer.agent.slash_commands_impl.rollback_cmd import RollbackCommand
from plugin_sdk.runtime_context import RuntimeContext


def _rt(extras: dict | None = None) -> RuntimeContext:
    rt = RuntimeContext()
    if extras:
        rt.custom.update(extras)
    return rt


# ----- /busy -----------------------------------------------------------------

def test_busy_set_interrupt() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("interrupt", rt))
    assert rt.custom["busy_input_mode"] == "interrupt"


def test_busy_set_queue() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("queue", rt))
    assert rt.custom["busy_input_mode"] == "queue"


def test_busy_set_steer() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("steer", rt))
    assert rt.custom["busy_input_mode"] == "steer"


def test_busy_status_reports() -> None:
    rt = _rt({"busy_input_mode": "queue"})
    res = asyncio.run(BusyCommand().execute("status", rt))
    assert "queue" in res.output


def test_busy_unknown_arg_prints_usage() -> None:
    rt = _rt()
    res = asyncio.run(BusyCommand().execute("foo", rt))
    assert "Usage" in res.output


def test_busy_default_status_when_no_arg() -> None:
    rt = _rt()
    res = asyncio.run(BusyCommand().execute("", rt))
    assert "interrupt" in res.output


# ----- /details --------------------------------------------------------------

def test_details_global_set() -> None:
    rt = _rt()
    asyncio.run(DetailsCommand().execute("expanded", rt))
    assert rt.custom["details_mode"] == "expanded"


def test_details_global_cycle() -> None:
    rt = _rt({"details_mode": "collapsed"})
    asyncio.run(DetailsCommand().execute("cycle", rt))
    assert rt.custom["details_mode"] == "expanded"


def test_details_section_override() -> None:
    rt = _rt()
    asyncio.run(DetailsCommand().execute("thinking expanded", rt))
    assert rt.custom["sections"]["thinking"] == "expanded"


def test_details_section_reset_drops_override() -> None:
    rt = _rt({"sections": {"thinking": "expanded"}})
    asyncio.run(DetailsCommand().execute("thinking reset", rt))
    assert "thinking" not in rt.custom["sections"]


def test_details_unknown_section_returns_usage() -> None:
    rt = _rt()
    res = asyncio.run(DetailsCommand().execute("nope expanded", rt))
    assert "Usage" in res.output


def test_details_unknown_mode_returns_usage() -> None:
    rt = _rt()
    res = asyncio.run(DetailsCommand().execute("thinking foo", rt))
    assert "Usage" in res.output


# ----- /mouse ---------------------------------------------------------------

def test_mouse_toggle() -> None:
    rt = _rt()
    asyncio.run(MouseCommand().execute("on", rt))
    assert rt.custom["mouse_tracking"] is True
    asyncio.run(MouseCommand().execute("off", rt))
    assert rt.custom["mouse_tracking"] is False
    asyncio.run(MouseCommand().execute("toggle", rt))
    assert rt.custom["mouse_tracking"] is True


def test_mouse_status() -> None:
    rt = _rt({"mouse_tracking": False})
    res = asyncio.run(MouseCommand().execute("status", rt))
    assert "OFF" in res.output


# ----- /rollback ------------------------------------------------------------

def test_rollback_no_store_friendly() -> None:
    rt = _rt()
    res = asyncio.run(RollbackCommand().execute("", rt))
    assert "checkpoint" in res.output.lower()


def test_rollback_no_arg_lists_dict_rows() -> None:
    store = MagicMock()
    store.list_checkpoints.return_value = [
        {"id": "c1", "label": "before-edit", "ts": 1700000000, "files": 3},
        {"id": "c2", "label": "after-test", "ts": 1700000100, "files": 5},
    ]
    rt = _rt({"_rewind_store": store})
    res = asyncio.run(RollbackCommand().execute("", rt))
    assert "before-edit" in res.output
    assert "after-test" in res.output


def test_rollback_numeric_restores_nth() -> None:
    store = MagicMock()
    store.list_checkpoints.return_value = [
        {"id": "c1", "label": "a", "ts": 1, "files": 1},
        {"id": "c2", "label": "b", "ts": 2, "files": 2},
    ]
    rt = _rt({"_rewind_store": store})
    res = asyncio.run(RollbackCommand().execute("2", rt))
    store.restore.assert_called_once_with("c2")
    assert "restored" in res.output.lower()


def test_rollback_out_of_range_returns_error() -> None:
    store = MagicMock()
    store.list_checkpoints.return_value = []
    rt = _rt({"_rewind_store": store})
    res = asyncio.run(RollbackCommand().execute("99", rt))
    assert "no checkpoints" in res.output.lower()


def test_rollback_invalid_arg() -> None:
    store = MagicMock()
    store.list_checkpoints.return_value = [
        {"id": "c1", "label": "a", "ts": 1, "files": 1},
    ]
    rt = _rt({"_rewind_store": store})
    res = asyncio.run(RollbackCommand().execute("xyz", rt))
    assert "invalid" in res.output.lower()
