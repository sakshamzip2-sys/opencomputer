"""A6 — the Bash tool runs in the per-turn working directory.

On the gateway a binding's ``cwd:`` is bound through the
``plugin_sdk.working_directory`` ContextVar around ``run_conversation``;
``BashTool.execute`` reads it and passes ``cwd`` to the host subprocess.
On the CLI nothing is bound → behaviour is byte-identical to before.
"""
from __future__ import annotations

import os

import pytest

from opencomputer.tools.bash import BashTool
from plugin_sdk.core import ToolCall
from plugin_sdk.working_directory import working_directory


def _call(cmd: str) -> ToolCall:
    return ToolCall(id="c1", name="Bash", arguments={"command": cmd})


def _stdout(content: str) -> str:
    """Extract the stdout section from the Bash tool's framed output."""
    marker = "--- stdout ---"
    if marker not in content:
        return content.strip()
    tail = content.split(marker, 1)[1]
    # Stop at a trailing stderr section if one is present.
    tail = tail.split("--- stderr ---", 1)[0]
    return tail.strip()


@pytest.mark.asyncio
async def test_bash_runs_in_bound_working_directory(tmp_path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    with working_directory(str(target)):
        result = await BashTool().execute(_call("pwd"))
    assert not result.is_error
    # macOS /tmp is a symlink to /private/tmp — compare realpaths.
    assert os.path.realpath(_stdout(result.content)) == os.path.realpath(
        str(target)
    )


@pytest.mark.asyncio
async def test_bash_unbound_uses_process_cwd(tmp_path) -> None:
    result = await BashTool().execute(_call("pwd"))
    assert not result.is_error
    assert os.path.realpath(_stdout(result.content)) == os.path.realpath(
        os.getcwd()
    )


@pytest.mark.asyncio
async def test_bash_stale_bound_dir_falls_back(tmp_path) -> None:
    """A binding cwd that no longer exists must not break every Bash
    call — the tool falls back to inheriting the process cwd."""
    gone = tmp_path / "deleted"
    gone.mkdir()
    gone.rmdir()
    with working_directory(str(gone)):
        result = await BashTool().execute(_call("pwd"))
    assert not result.is_error
    assert os.path.realpath(_stdout(result.content)) == os.path.realpath(
        os.getcwd()
    )
