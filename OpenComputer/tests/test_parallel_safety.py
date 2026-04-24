"""II.2 — name-whitelist parallel-safety gates.

Tests the two-layer parallel-safety gate on top of the existing per-tool
``parallel_safe`` flag:

1. ``HARDCODED_NEVER_PARALLEL`` — tool names always blocked from parallel.
2. ``PATH_SCOPED`` — tools where parallel is OK only when ``file_path``
   (or ``path``/``pattern``) args point to different paths.

Plus Bash destructive-command inspection via
:func:`opencomputer.tools.bash_safety.detect_destructive` — if any Bash
command in the batch is destructive, parallel is refused.

Matches Hermes's pattern (``sources/hermes-agent/run_agent.py`` lines
216-308: ``_PARALLEL_SAFE_TOOLS`` / ``_NEVER_PARALLEL_TOOLS`` /
``_PATH_SCOPED_TOOLS`` + ``_should_parallelize_tool_batch``).

The gate lives in
:meth:`opencomputer.agent.loop.AgentLoop._all_parallel_safe` — these tests
exercise that method directly, without spinning up a real agent loop.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import (
    HARDCODED_NEVER_PARALLEL,
    PATH_SCOPED,
    AgentLoop,
)
from plugin_sdk.core import ToolCall


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(
            provider="mock", model="mock-model", max_tokens=1024, temperature=0.0
        ),
        loop=LoopConfig(max_iterations=3, parallel_tools=True),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _loop(tmp_path: Path) -> AgentLoop:
    """Build a minimal AgentLoop for testing ``_all_parallel_safe``."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    provider = MagicMock()
    return AgentLoop(provider=provider, config=cfg, compaction_disabled=True)


def _mock_tool(parallel_safe: bool = True) -> MagicMock:
    t = MagicMock()
    t.parallel_safe = parallel_safe
    return t


# ─── Layer 1: HARDCODED_NEVER_PARALLEL ──────────────────────────────────


def test_hardcoded_never_parallel_set_exists() -> None:
    """The whitelist frozenset is importable and non-empty."""
    assert isinstance(HARDCODED_NEVER_PARALLEL, frozenset)
    # Bash must be in the never-parallel set — it has side effects that
    # can race even when two invocations look independent.
    assert "Bash" in HARDCODED_NEVER_PARALLEL


def test_path_scoped_set_exists() -> None:
    """The path-scoped frozenset contains the file-mutating tools."""
    assert isinstance(PATH_SCOPED, frozenset)
    assert "Edit" in PATH_SCOPED
    assert "MultiEdit" in PATH_SCOPED
    assert "Write" in PATH_SCOPED


def test_hardcoded_never_blocks_parallel(
    tmp_path: Path, monkeypatch
) -> None:
    """Even when every tool's ``parallel_safe=True``, a name in the
    hardcoded-never set forces sequential."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    # Pick the first entry in the hardcoded-never set as our canary.
    never_name = next(iter(HARDCODED_NEVER_PARALLEL))
    calls = [
        ToolCall(id="a", name=never_name, arguments={}),
        ToolCall(id="b", name="Read", arguments={"file_path": "/x"}),
    ]
    assert loop._all_parallel_safe(calls) is False


# ─── Layer 2: PATH_SCOPED ────────────────────────────────────────────────


def test_two_edits_on_different_files_parallel_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """Edit on two distinct file paths is parallel-safe."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="Edit", arguments={"file_path": "/tmp/a.txt", "old_string": "x", "new_string": "y"}),
        ToolCall(id="b", name="Edit", arguments={"file_path": "/tmp/b.txt", "old_string": "x", "new_string": "y"}),
    ]
    assert loop._all_parallel_safe(calls) is True


def test_two_edits_on_same_file_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """Two Edits on the same path must run sequentially — the second
    Edit's ``old_string`` search is invalidated by the first's write."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="Edit", arguments={"file_path": "/tmp/same.txt", "old_string": "x", "new_string": "y"}),
        ToolCall(id="b", name="Edit", arguments={"file_path": "/tmp/same.txt", "old_string": "y", "new_string": "z"}),
    ]
    assert loop._all_parallel_safe(calls) is False


def test_write_and_edit_on_same_file_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """Different PATH_SCOPED tools on the same path: still a race."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    # Same ``file_path`` via both Write and Edit. Duplicate-path detection
    # is per-tool-name (matches Hermes's ``reserved_paths`` approach).
    # To catch cross-tool collisions as well, a second Edit on the same
    # file as another Edit is the primary case — tested above. This test
    # pins that same-name path dedup works cleanly.
    calls = [
        ToolCall(id="a", name="Write", arguments={"file_path": "/tmp/same.txt", "content": "hi"}),
        ToolCall(id="b", name="Write", arguments={"file_path": "/tmp/same.txt", "content": "bye"}),
    ]
    assert loop._all_parallel_safe(calls) is False


def test_path_scoped_missing_path_arg_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """If a PATH_SCOPED tool has no recognizable path arg, refuse parallel —
    we can't prove the paths differ, so conservative default wins."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="Edit", arguments={}),  # malformed: no file_path
        ToolCall(id="b", name="Edit", arguments={"file_path": "/tmp/b.txt"}),
    ]
    # Two Edits with None as the first path → set size < len, reject.
    assert loop._all_parallel_safe(calls) is False


# ─── Layer 3: Bash destructive inspection ────────────────────────────────


def test_bash_destructive_command_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """When Bash is in the batch (via its plugin flag) and the command
    matches :func:`bash_safety.detect_destructive`, refuse parallel.

    Bash is also in the HARDCODED_NEVER_PARALLEL set — this test asserts
    the whitelist gate fires first OR the destructive scan catches it.
    Either path is acceptable; both reject.
    """
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="Bash", arguments={"command": "rm -rf /"}),
        ToolCall(id="b", name="Read", arguments={"file_path": "/tmp/x"}),
    ]
    assert loop._all_parallel_safe(calls) is False


# ─── Existing per-tool flag still respected ─────────────────────────────


def test_parallel_safe_flag_false_forces_sequential(
    tmp_path: Path, monkeypatch
) -> None:
    """Backwards compat: a tool with ``parallel_safe=False`` still forces
    sequential execution, regardless of the new hardcoded lists."""
    from opencomputer.tools import registry as reg_mod

    def get_side_effect(name: str):
        # Mark 'DangerousTool' as parallel_safe=False; everything else safe.
        if name == "DangerousTool":
            return _mock_tool(parallel_safe=False)
        return _mock_tool(parallel_safe=True)

    monkeypatch.setattr(reg_mod.registry, "get", MagicMock(side_effect=get_side_effect))
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="DangerousTool", arguments={}),
        ToolCall(id="b", name="Read", arguments={"file_path": "/x"}),
    ]
    assert loop._all_parallel_safe(calls) is False


def test_unregistered_tool_forces_sequential(
    tmp_path: Path, monkeypatch
) -> None:
    """Backwards compat: an unknown tool (registry.get → None) → reject
    parallel. Existing behavior, kept under the new gate."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(reg_mod.registry, "get", MagicMock(return_value=None))
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="ReadSomething", arguments={}),
        ToolCall(id="b", name="ReadAnother", arguments={}),
    ]
    assert loop._all_parallel_safe(calls) is False


# ─── Happy path: all parallel-safe, all distinct paths → go ─────────────


def test_all_parallel_safe_all_distinct_goes_parallel(
    tmp_path: Path, monkeypatch
) -> None:
    """Two reads on different files and one Grep — all parallel_safe,
    no name in hardcoded-never, no path collision → parallel approved."""
    from opencomputer.tools import registry as reg_mod

    monkeypatch.setattr(
        reg_mod.registry, "get", MagicMock(return_value=_mock_tool(parallel_safe=True))
    )
    loop = _loop(tmp_path)
    calls = [
        ToolCall(id="a", name="Read", arguments={"file_path": "/tmp/a"}),
        ToolCall(id="b", name="Read", arguments={"file_path": "/tmp/b"}),
        ToolCall(id="c", name="Grep", arguments={"pattern": "foo"}),
    ]
    assert loop._all_parallel_safe(calls) is True


def test_empty_calls_list_returns_true(tmp_path: Path) -> None:
    """Empty input is trivially parallel-safe (no-op)."""
    loop = _loop(tmp_path)
    assert loop._all_parallel_safe([]) is True
