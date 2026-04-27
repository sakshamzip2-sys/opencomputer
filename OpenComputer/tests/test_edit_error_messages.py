"""Edit/MultiEdit error messages must teach the model how to recover.

These tests exist to guard against future regressions where an error
message gets simplified back to a generic exception. The model performs
much better when failures point toward the fix.
"""
import asyncio
import sys
from pathlib import Path

import pytest

# Match the conftest pattern other coding-harness tests use:
sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "coding-harness"))


@pytest.fixture(autouse=True)
def reset_sys_path():
    yield
    # Cleanup: remove our added path so other tests aren't affected.
    extensions_path = str(Path(__file__).parent.parent / "extensions" / "coding-harness")
    if extensions_path in sys.path:
        sys.path.remove(extensions_path)


@pytest.fixture(autouse=True)
def reset_read_state():
    """Each test starts with a clean read-state set so prior tests can't
    accidentally satisfy the "Read first" precondition for us."""
    from opencomputer.tools._file_read_state import reset

    reset()
    yield
    reset()


from plugin_sdk.core import ToolCall  # noqa: E402


@pytest.fixture
def edit_tool():
    from tools.edit import EditTool  # type: ignore[import-not-found]

    return EditTool()


@pytest.fixture
def multi_edit_tool():
    from tools.multi_edit import MultiEditTool  # type: ignore[import-not-found]

    return MultiEditTool()


def _read_file_sync(file_path: str):
    """Helper — Edit requires the file be Read first per its contract.

    The fixture is sync so tests can call it inline without juggling
    ``asyncio.run`` themselves.
    """
    from opencomputer.tools.read import ReadTool

    return asyncio.run(
        ReadTool().execute(
            ToolCall(id="r", name="Read", arguments={"file_path": file_path})
        )
    )


# ─── Edit ────────────────────────────────────────────────────────


def test_edit_old_string_not_unique_nudges_toward_fix(edit_tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("aaa\naaa\n")
    _read_file_sync(str(p))

    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(p),
            "old_string": "aaa",
            "new_string": "bbb",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "appears" in msg or "unique" in msg or "context" in msg
    assert "replace_all" in msg


def test_edit_old_string_not_found_nudges_toward_read(edit_tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    _read_file_sync(str(p))

    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(p),
            "old_string": "missing-string",
            "new_string": "x",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "not found" in msg or "match" in msg
    # Should hint about reading or byte-matching
    assert "read" in msg or "match" in msg or "bytes" in msg or "current" in msg


def test_edit_file_not_read_first_nudges(edit_tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    # Deliberately do NOT read first.

    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(p),
            "old_string": "hello",
            "new_string": "world",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "read" in msg


def test_edit_no_op_nudges(edit_tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    _read_file_sync(str(p))

    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(p),
            "old_string": "hello",
            "new_string": "hello",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "identical" in msg or "no-op" in msg or "same" in msg


def test_edit_file_missing_nudges_toward_write(edit_tool, tmp_path):
    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(tmp_path / "does-not-exist.txt"),
            "old_string": "x",
            "new_string": "y",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "exist" in msg or "write" in msg


def test_edit_directory_nudges_toward_glob(edit_tool, tmp_path):
    """Editing a directory should explain why and point at Glob/Read."""
    d = tmp_path / "subdir"
    d.mkdir()
    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": str(d),
            "old_string": "x",
            "new_string": "y",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "directory" in msg
    assert "glob" in msg or "read" in msg


def test_edit_relative_path_nudges_toward_absolute(edit_tool):
    """Relative paths should be rejected with a nudge to use absolute."""
    call = ToolCall(
        id="t",
        name="Edit",
        arguments={
            "file_path": "relative/path.txt",
            "old_string": "x",
            "new_string": "y",
        },
    )
    result = asyncio.run(edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "absolute" in msg


# ─── MultiEdit ───────────────────────────────────────────────────


def test_multi_edit_per_edit_failure_identifies_which_failed(
    multi_edit_tool, tmp_path
):
    """When edit #N of M fails, the error must say which one and rolled
    back so the model can adjust the right entry, not the whole batch."""
    p = tmp_path / "f.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    _read_file_sync(str(p))

    call = ToolCall(
        id="t",
        name="MultiEdit",
        arguments={
            "file_path": str(p),
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "DOES_NOT_EXIST", "new_string": "x"},  # fails
                {"old_string": "gamma", "new_string": "GAMMA"},
            ],
        },
    )
    result = asyncio.run(multi_edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    # Must identify which edit failed (1-indexed for human readability)
    assert "edit #2" in msg or "edit 2" in msg or "second" in msg
    # Must say it rolled back
    assert "rolled back" in msg or "roll back" in msg
    # File must be unchanged
    assert p.read_text() == "alpha\nbeta\ngamma\n"


def test_multi_edit_file_not_read_first_nudges(multi_edit_tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    # Deliberately do NOT read first.

    call = ToolCall(
        id="t",
        name="MultiEdit",
        arguments={
            "file_path": str(p),
            "edits": [{"old_string": "hello", "new_string": "world"}],
        },
    )
    result = asyncio.run(multi_edit_tool.execute(call))
    assert result.is_error
    msg = result.content.lower()
    assert "read" in msg
