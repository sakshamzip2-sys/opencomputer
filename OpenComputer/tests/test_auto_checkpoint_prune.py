"""Tests for auto_checkpoint hook auto-prune wiring."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "extensions" / "coding-harness"
sys.path.insert(0, str(HARNESS))

from hooks.auto_checkpoint import (  # type: ignore[import-not-found]  # noqa: E402
    build_auto_checkpoint_hook_spec,
)
from rewind.store import RewindStore  # type: ignore[import-not-found]  # noqa: E402


class _FakeSessionState:
    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)

    def set(self, k: str, v: Any) -> None:
        self._d[k] = v


class _FakeHarnessCtx:
    def __init__(self, root: Path) -> None:
        self.rewind_store = RewindStore(root, workspace_root=root)
        self.session_state = _FakeSessionState()


class _FakeToolCall:
    def __init__(self, name: str, args: dict) -> None:
        self.name = name
        self.arguments = args


class _FakeHookCtx:
    def __init__(self, tool_call: _FakeToolCall | None) -> None:
        self.tool_call = tool_call


def test_first_fire_triggers_prune(tmp_path: Path) -> None:
    """First save should mark the auto-prune marker."""
    ctx = _FakeHarnessCtx(tmp_path / "rw")
    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    asyncio.run(spec.handler(_FakeHookCtx(_FakeToolCall("Edit", {"path": "x"}))))
    assert (ctx.rewind_store.root / RewindStore.LAST_PRUNE_MARKER).exists()


def test_within_min_interval_skips(tmp_path: Path) -> None:
    """Second save within 24h window should NOT touch the marker."""
    ctx = _FakeHarnessCtx(tmp_path / "rw")
    ctx.rewind_store.mark_pruned()
    marker = ctx.rewind_store.root / RewindStore.LAST_PRUNE_MARKER
    mtime_before = marker.stat().st_mtime

    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    asyncio.run(spec.handler(_FakeHookCtx(_FakeToolCall("Edit", {"path": "x"}))))
    mtime_after = marker.stat().st_mtime
    assert mtime_after == mtime_before


def test_failure_does_not_block_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If prune raises, the save path still runs."""
    ctx = _FakeHarnessCtx(tmp_path / "rw")

    def _boom(**_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ctx.rewind_store, "prune", _boom)
    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    # Should NOT raise — failure is swallowed + logged.
    asyncio.run(spec.handler(_FakeHookCtx(_FakeToolCall("Edit", {"path": "x"}))))


def test_non_destructive_tool_skips(tmp_path: Path) -> None:
    """Non-Edit/Write/Bash/MultiEdit tools should not even consult prune."""
    ctx = _FakeHarnessCtx(tmp_path / "rw")
    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    # Read is not in DESTRUCTIVE_TOOLS.
    asyncio.run(spec.handler(_FakeHookCtx(_FakeToolCall("Read", {"path": "x"}))))
    # No prune marker should be set.
    assert not (ctx.rewind_store.root / RewindStore.LAST_PRUNE_MARKER).exists()


def test_no_tool_call_returns_early(tmp_path: Path) -> None:
    ctx = _FakeHarnessCtx(tmp_path / "rw")
    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    result = asyncio.run(spec.handler(_FakeHookCtx(None)))
    assert result is None


def test_destructive_tool_with_existing_file_creates_checkpoint(tmp_path: Path) -> None:
    """End-to-end: Edit on a real file should produce a checkpoint."""
    ctx = _FakeHarnessCtx(tmp_path / "rw")
    target = tmp_path / "target.txt"
    target.write_text("original content")
    ctx.session_state.set("edited_files", [str(target)])

    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    asyncio.run(
        spec.handler(_FakeHookCtx(_FakeToolCall("Edit", {"path": str(target)})))
    )
    # At least one checkpoint should have been saved.
    assert ctx.rewind_store.count() >= 1
