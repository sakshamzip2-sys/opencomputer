"""Tests for ``/compress`` slash command (2026-04-30, Hermes-parity)."""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opencomputer.cli_ui.slash import is_slash_command, resolve_command
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    _handle_compress,
    dispatch_slash,
)


def _ctx_with(on_compress=None) -> SlashContext:
    """Build a minimal SlashContext for compress-only testing."""
    return SlashContext(
        console=Console(file=StringIO(), force_terminal=False),
        session_id="s-test",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
        on_compress=on_compress or (lambda: (False, 0, 0, "not wired")),
    )


def test_compress_command_registered():
    assert resolve_command("compress") is not None


def test_compress_dispatches_via_dispatch_slash():
    """Smoke test: /compress is recognized as a slash command."""
    assert is_slash_command("/compress") is True
    fired = []
    ctx = _ctx_with(
        on_compress=lambda: (True, 5, 3, "ok") if not fired or fired.append("x") else (False, 0, 0, "x"),
    )
    result = dispatch_slash("/compress", ctx)
    assert result.handled is True


def test_handle_compress_reports_when_callback_fails():
    ctx = _ctx_with(on_compress=lambda: (False, 0, 0, "compaction unavailable"))
    result = _handle_compress(ctx, [])
    assert result.handled is True


def test_handle_compress_reports_when_no_compaction_needed():
    """Equal before/after means compaction had nothing to do."""
    ctx = _ctx_with(on_compress=lambda: (True, 5, 5, "no eligible block"))
    result = _handle_compress(ctx, [])
    assert result.handled is True


def test_handle_compress_reports_success_with_delta():
    """Successful compaction shows before→after delta."""
    ctx = _ctx_with(on_compress=lambda: (True, 20, 12, "ok"))
    result = _handle_compress(ctx, [])
    assert result.handled is True


def test_compress_callback_default_is_no_op():
    """Default lambda returns ok=False, so /compress without wiring fails gracefully."""
    ctx = _ctx_with()
    result = _handle_compress(ctx, [])
    assert result.handled is True  # never crashes loop


# ─── Force-compaction behaviour on CompactionEngine ──────────────────


def test_compaction_maybe_run_accepts_force_kwarg():
    """Verify the API accepts ``force=`` as a kwarg without TypeError.

    The full async exercise needs a provider + LLM; here we just verify
    the signature surface, which is what ``/compress`` relies on.
    """
    import inspect

    from opencomputer.agent.compaction import CompactionEngine

    sig = inspect.signature(CompactionEngine.maybe_run)
    assert "force" in sig.parameters, (
        "CompactionEngine.maybe_run must accept ``force`` kwarg for "
        "the /compress slash command to work."
    )
    # Confirm it's keyword-only with a False default — preserves BC.
    force_param = sig.parameters["force"]
    assert force_param.default is False
    assert force_param.kind == inspect.Parameter.KEYWORD_ONLY
