"""Tests for /snapshot slash command (Hermes Tier 2.A continuation)."""
from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    is_slash_command,
    resolve_command,
)
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    dispatch_slash,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_snapshot_in_registry():
    cmd = resolve_command("snapshot")
    assert cmd is not None
    assert "snapshot" in cmd.description.lower() or "archive" in cmd.description.lower()


def test_snapshot_listed_in_registry():
    names = {c.name for c in SLASH_REGISTRY}
    assert "snapshot" in names


# ---------------------------------------------------------------------------
# Fixture: SlashContext wired to in-memory snapshot store
# ---------------------------------------------------------------------------


@pytest.fixture
def snap_ctx():
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=200)
    store: dict[str, dict[str, Any]] = {}

    def create(label: str | None) -> str | None:
        sid = f"20260428-180000-{label}" if label else "20260428-180000"
        store[sid] = {"id": sid, "label": label, "file_count": 5, "total_size": 1234}
        return sid

    def lst() -> list[dict]:
        # newest first
        return list(reversed(list(store.values())))

    def restore(sid: str) -> int:
        return 5 if sid in store else 0

    def prune() -> int:
        n = max(0, len(store) - 20)
        # drop oldest n
        keys = list(store.keys())[:n]
        for k in keys:
            del store[k]
        return n

    ctx = SlashContext(
        console=console,
        session_id="test",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
        on_snapshot_create=create,
        on_snapshot_list=lst,
        on_snapshot_restore=restore,
        on_snapshot_prune=prune,
    )
    return ctx, store, buf


# ---------------------------------------------------------------------------
# /snapshot create
# ---------------------------------------------------------------------------


def test_snapshot_create_basic(snap_ctx):
    ctx, store, buf = snap_ctx
    result = dispatch_slash("/snapshot create", ctx)
    assert result.handled is True
    assert len(store) == 1
    assert "snapshot created" in buf.getvalue().lower()


def test_snapshot_create_with_label(snap_ctx):
    ctx, store, buf = snap_ctx
    result = dispatch_slash("/snapshot create pre-experiment", ctx)
    assert result.handled is True
    sid = next(iter(store.keys()))
    assert sid.endswith("-pre-experiment")


def test_snapshot_create_empty_returns_warning(snap_ctx):
    ctx, store, buf = snap_ctx

    # Override create callback to return None (no eligible files).
    def empty_create(_label):
        return None

    ctx2 = SlashContext(
        console=ctx.console,
        session_id=ctx.session_id,
        config=ctx.config,
        on_clear=ctx.on_clear,
        get_cost_summary=ctx.get_cost_summary,
        get_session_list=ctx.get_session_list,
        on_snapshot_create=empty_create,
        on_snapshot_list=ctx.on_snapshot_list,
        on_snapshot_restore=ctx.on_snapshot_restore,
        on_snapshot_prune=ctx.on_snapshot_prune,
    )
    dispatch_slash("/snapshot create", ctx2)
    assert "snapshot empty" in buf.getvalue().lower() or "no eligible" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# /snapshot list
# ---------------------------------------------------------------------------


def test_snapshot_list_empty(snap_ctx):
    ctx, _, buf = snap_ctx
    dispatch_slash("/snapshot list", ctx)
    assert "no snapshots" in buf.getvalue().lower()


def test_snapshot_list_default_subcommand(snap_ctx):
    ctx, _, buf = snap_ctx
    # Bare /snapshot defaults to list
    dispatch_slash("/snapshot", ctx)
    assert "no snapshots" in buf.getvalue().lower()


def test_snapshot_list_with_entries(snap_ctx):
    ctx, store, buf = snap_ctx
    dispatch_slash("/snapshot create first", ctx)
    dispatch_slash("/snapshot create second", ctx)
    buf.seek(0)
    buf.truncate()
    dispatch_slash("/snapshot list", ctx)
    out = buf.getvalue()
    assert "snapshots (2)" in out
    assert "first" in out
    assert "second" in out


# ---------------------------------------------------------------------------
# /snapshot restore
# ---------------------------------------------------------------------------


def test_snapshot_restore_no_id(snap_ctx):
    ctx, _, buf = snap_ctx
    dispatch_slash("/snapshot restore", ctx)
    assert "usage" in buf.getvalue().lower()


def test_snapshot_restore_unknown(snap_ctx):
    ctx, _, buf = snap_ctx
    dispatch_slash("/snapshot restore nonexistent-id", ctx)
    assert "restore failed" in buf.getvalue().lower()


def test_snapshot_restore_known(snap_ctx):
    ctx, store, buf = snap_ctx
    dispatch_slash("/snapshot create alpha", ctx)
    sid = next(iter(store.keys()))
    buf.seek(0)
    buf.truncate()
    dispatch_slash(f"/snapshot restore {sid}", ctx)
    out = buf.getvalue()
    assert "restored 5 files" in out
    assert "restart recommended" in out.lower()


# ---------------------------------------------------------------------------
# /snapshot prune
# ---------------------------------------------------------------------------


def test_snapshot_prune_no_op(snap_ctx):
    ctx, _, buf = snap_ctx
    dispatch_slash("/snapshot prune", ctx)
    assert "0 snapshot" in buf.getvalue()


def test_snapshot_prune_drops_excess(snap_ctx):
    ctx, store, buf = snap_ctx
    # Pre-populate beyond cap.
    for i in range(22):
        store[f"20260428-{i:06d}"] = {"id": f"20260428-{i:06d}", "file_count": 1, "total_size": 1}
    buf.seek(0)
    buf.truncate()
    dispatch_slash("/snapshot prune", ctx)
    assert "2 snapshot" in buf.getvalue()


# ---------------------------------------------------------------------------
# Unknown subcommand
# ---------------------------------------------------------------------------


def test_snapshot_unknown_subcommand(snap_ctx):
    ctx, _, buf = snap_ctx
    dispatch_slash("/snapshot frobnicate", ctx)
    assert "unknown subcommand" in buf.getvalue().lower()


def test_snapshot_recognized_as_slash():
    assert is_slash_command("/snapshot")
    assert is_slash_command("/snapshot create x")
    assert is_slash_command("/snapshot list")
