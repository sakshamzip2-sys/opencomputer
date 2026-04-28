"""Tests for /rollback slash command (Hermes Tier 2.A continuation).

Mirrors hermes_cli/checkpoint_manager.py:402-525 list/diff/restore methods
ported atop OC's existing RewindStore. Lives next to /undo (which is
unchanged) and adds the missing list+diff UX surface.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

# coding-harness modules live under extensions/ via sys.path injection.
_EXT_PATH = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "coding-harness"
)
if str(_EXT_PATH) not in sys.path:
    sys.path.insert(0, str(_EXT_PATH))


@pytest.fixture
def harness(tmp_path: Path):
    """Build a real RewindStore + minimal HarnessContext-shaped namespace."""
    from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]
    from rewind.store import RewindStore  # type: ignore[import-not-found]
    from slash_commands.rollback import RollbackCommand  # type: ignore[import-not-found]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RewindStore(tmp_path / "rw", workspace_root=workspace)

    class _HarnessCtx:
        rewind_store = store
        session_state = {}

    cmd = RollbackCommand(harness_ctx=_HarnessCtx())

    return cmd, store, workspace, Checkpoint


# ---------------------------------------------------------------------------
# /rollback list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_list_empty(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("", runtime=None)
    assert result.handled
    assert "no checkpoints" in result.output.lower()


@pytest.mark.asyncio
async def test_rollback_list_with_checkpoints(harness):
    cmd, store, _, Checkpoint = harness
    a = Checkpoint.from_files({"x": b"1"}, label="alpha")
    store.save(a)
    time.sleep(0.01)
    b = Checkpoint.from_files({"x": b"2"}, label="beta")
    store.save(b)

    result = await cmd.execute("list", runtime=None)
    assert result.handled
    assert "Checkpoints (2 total" in result.output
    # Newest first → beta is index 1
    lines = result.output.splitlines()
    assert "[beta]" in lines[1]
    assert "[alpha]" in lines[2]


@pytest.mark.asyncio
async def test_rollback_default_subcommand_is_list(harness):
    cmd, store, _, Checkpoint = harness
    cp = Checkpoint.from_files({"x": b"1"}, label="t")
    store.save(cp)
    result = await cmd.execute("", runtime=None)
    assert "Checkpoints (1 total" in result.output


# ---------------------------------------------------------------------------
# /rollback restore <N>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_restore_basic(harness):
    cmd, store, workspace, Checkpoint = harness
    cp = Checkpoint.from_files({"a.py": b"original"}, label="orig")
    store.save(cp)
    # Mutate workspace
    (workspace / "a.py").write_bytes(b"modified")

    result = await cmd.execute("restore 1", runtime=None)
    assert result.handled
    assert "Rolled back to checkpoint 1" in result.output
    assert (workspace / "a.py").read_bytes() == b"original"


@pytest.mark.asyncio
async def test_rollback_bare_integer_is_restore(harness):
    cmd, store, workspace, Checkpoint = harness
    cp = Checkpoint.from_files({"a.py": b"v1"}, label="v1")
    store.save(cp)
    (workspace / "a.py").write_bytes(b"v2")

    # Hermes-compat: bare /rollback 1 == /rollback restore 1
    result = await cmd.execute("1", runtime=None)
    assert "Rolled back to checkpoint 1" in result.output
    assert (workspace / "a.py").read_bytes() == b"v1"


@pytest.mark.asyncio
async def test_rollback_restore_index_out_of_range(harness):
    cmd, store, _, Checkpoint = harness
    cp = Checkpoint.from_files({"x": b"1"}, label="t")
    store.save(cp)
    result = await cmd.execute("restore 5", runtime=None)
    assert "can't restore index 5" in result.output


@pytest.mark.asyncio
async def test_rollback_restore_no_index(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("restore", runtime=None)
    assert "Usage: /rollback restore" in result.output


@pytest.mark.asyncio
async def test_rollback_restore_nonint(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("restore abc", runtime=None)
    assert "Usage: /rollback restore" in result.output


@pytest.mark.asyncio
async def test_rollback_restore_empty(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("restore 1", runtime=None)
    assert "No checkpoints to restore" in result.output


# ---------------------------------------------------------------------------
# /rollback diff <N>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_diff_no_changes(harness):
    cmd, store, workspace, Checkpoint = harness
    (workspace / "a.py").write_bytes(b"same")
    cp = Checkpoint.from_files({"a.py": b"same"}, label="t")
    store.save(cp)

    result = await cmd.execute("diff 1", runtime=None)
    assert "no differences" in result.output.lower()


@pytest.mark.asyncio
async def test_rollback_diff_changed_file(harness):
    cmd, store, workspace, Checkpoint = harness
    (workspace / "a.py").write_bytes(b"original")
    cp = Checkpoint.from_files({"a.py": b"original"}, label="t")
    store.save(cp)
    (workspace / "a.py").write_bytes(b"changed")

    result = await cmd.execute("diff 1", runtime=None)
    assert "changed (1)" in result.output
    assert "M  a.py" in result.output


@pytest.mark.asyncio
async def test_rollback_diff_missing_file(harness):
    cmd, store, workspace, Checkpoint = harness
    (workspace / "a.py").write_bytes(b"x")
    cp = Checkpoint.from_files({"a.py": b"x"}, label="t")
    store.save(cp)
    (workspace / "a.py").unlink()

    result = await cmd.execute("diff 1", runtime=None)
    assert "missing on disk" in result.output


@pytest.mark.asyncio
async def test_rollback_diff_no_index(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("diff", runtime=None)
    assert "Usage: /rollback diff" in result.output


@pytest.mark.asyncio
async def test_rollback_diff_index_out_of_range(harness):
    cmd, store, _, Checkpoint = harness
    cp = Checkpoint.from_files({"x": b"1"}, label="t")
    store.save(cp)
    result = await cmd.execute("diff 5", runtime=None)
    assert "can't diff index 5" in result.output


@pytest.mark.asyncio
async def test_rollback_diff_empty(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("diff 1", runtime=None)
    assert "No checkpoints to diff" in result.output


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_unknown_subcommand(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("frobnicate", runtime=None)
    assert "Unknown subcommand" in result.output


@pytest.mark.asyncio
async def test_rollback_restore_zero_index(harness):
    cmd, _, _, _ = harness
    result = await cmd.execute("restore 0", runtime=None)
    assert "Index must be ≥1" in result.output


def test_rollback_command_name_and_description():
    from slash_commands.rollback import RollbackCommand  # type: ignore[import-not-found]

    cmd = RollbackCommand(harness_ctx=None)
    assert cmd.name == "rollback"
    assert "checkpoint" in cmd.description.lower()
