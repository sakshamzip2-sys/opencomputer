"""Tests for /queue slash command (Hermes Tier 2.A continuation).

Mirrors Hermes' ``_pending_input = queue.Queue()`` pattern (cli.py:9087)
adapted to OC's per-session callback-driven SlashContext design.
"""
from __future__ import annotations

from io import StringIO

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


def test_queue_in_registry():
    cmd = resolve_command("queue")
    assert cmd is not None
    assert cmd.name == "queue"
    assert "queue" in cmd.description.lower()


def test_queue_listed_in_registry():
    names = {c.name for c in SLASH_REGISTRY}
    assert "queue" in names


# ---------------------------------------------------------------------------
# Test fixture: a SlashContext wired to an in-memory queue
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_ctx():
    """Build a SlashContext whose on_queue_* callbacks share a single mock
    queue (a list). Returns (ctx, queue, console_buffer) so the test can
    assert state changes."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=200)
    queue: list[str] = []
    cap = 50

    def add(text: str) -> bool:
        if len(queue) >= cap:
            return False
        queue.append(text)
        return True

    def lst() -> list[str]:
        return list(queue)

    def clr() -> int:
        n = len(queue)
        queue.clear()
        return n

    ctx = SlashContext(
        console=console,
        session_id="test-session",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
        on_queue_add=add,
        on_queue_list=lst,
        on_queue_clear=clr,
    )
    return ctx, queue, buf


# ---------------------------------------------------------------------------
# /queue <prompt> — adds text
# ---------------------------------------------------------------------------


def test_queue_add_one(queue_ctx):
    ctx, queue, buf = queue_ctx
    result = dispatch_slash("/queue research the rsi of guj alkali", ctx)
    assert result.handled is True
    assert queue == ["research the rsi of guj alkali"]
    assert "queued" in buf.getvalue().lower()


def test_queue_add_multiple_preserves_order(queue_ctx):
    ctx, queue, _ = queue_ctx
    dispatch_slash("/queue first prompt", ctx)
    dispatch_slash("/queue second prompt", ctx)
    dispatch_slash("/queue third prompt", ctx)
    assert queue == ["first prompt", "second prompt", "third prompt"]


def test_queue_add_preserves_spaces_in_prompt(queue_ctx):
    ctx, queue, _ = queue_ctx
    dispatch_slash("/queue what  is the   weather like?", ctx)
    # Args were split + rejoined with single spaces — that's by design (matches
    # Hermes behavior). The user's intent (multi-word prompt) is preserved.
    assert queue == ["what is the weather like?"]


# ---------------------------------------------------------------------------
# /queue list — show pending
# ---------------------------------------------------------------------------


def test_queue_list_empty(queue_ctx):
    ctx, _, buf = queue_ctx
    result = dispatch_slash("/queue list", ctx)
    assert result.handled is True
    assert "empty" in buf.getvalue().lower()


def test_queue_list_with_entries(queue_ctx):
    ctx, queue, buf = queue_ctx
    queue.extend(["first", "second", "third"])
    dispatch_slash("/queue list", ctx)
    out = buf.getvalue()
    assert "3 pending" in out
    assert "first" in out
    assert "second" in out
    assert "third" in out


def test_queue_list_truncates_long_entries(queue_ctx):
    ctx, queue, buf = queue_ctx
    long_text = "x" * 200
    queue.append(long_text)
    dispatch_slash("/queue list", ctx)
    out = buf.getvalue()
    assert "..." in out
    # Full string never appears in display (>80-char preview)
    assert long_text not in out


# ---------------------------------------------------------------------------
# /queue clear — drop all
# ---------------------------------------------------------------------------


def test_queue_clear_empty(queue_ctx):
    ctx, _, buf = queue_ctx
    result = dispatch_slash("/queue clear", ctx)
    assert result.handled is True
    assert "0 dropped" in buf.getvalue()


def test_queue_clear_with_entries(queue_ctx):
    ctx, queue, buf = queue_ctx
    queue.extend(["a", "b", "c"])
    dispatch_slash("/queue clear", ctx)
    assert queue == []
    assert "3 dropped" in buf.getvalue()


# ---------------------------------------------------------------------------
# /queue — bare invocation prints status + usage
# ---------------------------------------------------------------------------


def test_queue_no_args_shows_status_when_empty(queue_ctx):
    ctx, _, buf = queue_ctx
    result = dispatch_slash("/queue", ctx)
    assert result.handled is True
    out = buf.getvalue()
    assert "0 pending" in out
    assert "/queue list" in out


def test_queue_no_args_shows_count_when_nonempty(queue_ctx):
    ctx, queue, buf = queue_ctx
    queue.extend(["x", "y"])
    dispatch_slash("/queue", ctx)
    assert "2 pending" in buf.getvalue()


# ---------------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------------


def test_queue_cap_rejects_over_50(queue_ctx):
    ctx, queue, buf = queue_ctx
    # Pre-fill to cap.
    for i in range(50):
        queue.append(f"prompt {i}")
    result = dispatch_slash("/queue overflow prompt", ctx)
    assert result.handled is True
    # Add was rejected; queue still at cap.
    assert len(queue) == 50
    assert "queue full" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_queue_empty_prompt_rejected(queue_ctx):
    ctx, queue, buf = queue_ctx
    # ``/queue   `` with only whitespace — the whitespace gets stripped by
    # _split_args; the handler sees no args → prints status (not an error).
    result = dispatch_slash("/queue", ctx)
    assert result.handled is True
    assert queue == []


def test_queue_default_callbacks_safe():
    """SlashContext defaults for queue callbacks must not raise."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=200)
    ctx = SlashContext(
        console=console,
        session_id="test-session",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
    )
    # Defaults: on_queue_add → False, on_queue_list → [], on_queue_clear → 0.
    result = dispatch_slash("/queue test prompt", ctx)
    assert result.handled is True
    # Default returned False; handler prints "queue full".
    assert "queue full" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Slash detection
# ---------------------------------------------------------------------------


def test_queue_recognized_as_slash():
    assert is_slash_command("/queue hello")
    assert is_slash_command("/queue list")
    assert is_slash_command("/queue clear")
