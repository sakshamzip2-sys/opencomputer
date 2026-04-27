"""Integration tests — read/write tools record state via file_state."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.tools import file_state
from opencomputer.tools.read import ReadTool
from opencomputer.tools.write import WriteTool
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def reset_state():
    file_state.get_registry().clear()
    yield
    file_state.get_registry().clear()


def _call(tool_name: str, **kwargs) -> ToolCall:
    return ToolCall(id="c1", name=tool_name, arguments=kwargs)


def test_read_records_full_read_no_partial(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("hello\n")
    asyncio.run(ReadTool().execute(_call("Read", file_path=str(p))))
    # Direct check_stale should return None — we read it, no sibling
    # has touched it.
    assert file_state.check_stale(p) is None


def test_read_with_offset_records_partial_warns_on_write(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("\n".join(f"line{i}" for i in range(20)))
    # Read only lines 5–10
    asyncio.run(
        ReadTool().execute(_call("Read", file_path=str(p), offset=5, limit=5))
    )
    warning = file_state.check_stale(p)
    assert warning is not None
    assert "partial" in warning.lower() or "pagination" in warning.lower()


def test_write_emits_warning_when_sibling_wrote_after_read(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("orig\n")
    # task A reads
    file_state.record_read(p, task_id="task-a")
    # sibling B writes
    p.write_text("changed by sibling\n")
    file_state.note_write(p, task_id="task-b")
    # task A now writes via WriteTool — staleness check uses the
    # current ContextVar task. We override via the file_state
    # convenience APIs by injecting via ContextVar patch.
    from opencomputer.observability.logging_config import set_session_id

    set_session_id("task-a")
    try:
        result = asyncio.run(
            WriteTool().execute(
                _call("Write", file_path=str(p), content="my new content\n")
            )
        )
    finally:
        set_session_id(None)
    assert not result.is_error
    # Warning should be in the message body.
    assert "WARNING" in result.content
    assert "sibling" in result.content.lower()


def test_write_normal_path_no_warning(tmp_path):
    p = tmp_path / "y.py"
    result = asyncio.run(
        WriteTool().execute(_call("Write", file_path=str(p), content="hello\n"))
    )
    assert not result.is_error
    assert "WARNING" not in result.content


def test_write_then_read_loop_no_warning(tmp_path):
    """Same task: write → another write should not warn (own write
    counts as implicit read)."""
    p = tmp_path / "y.py"
    asyncio.run(
        WriteTool().execute(_call("Write", file_path=str(p), content="v1\n"))
    )
    result = asyncio.run(
        WriteTool().execute(_call("Write", file_path=str(p), content="v2\n"))
    )
    assert "WARNING" not in result.content
