"""Tests for /provider mid-session swap + cross-provider /model (Sub-project D)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    _handle_model,
    _handle_provider,
)


def test_handle_provider_no_args_shows_current():
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.config = MagicMock()
    ctx.config.model.provider = "anthropic"
    _handle_provider(ctx, [])
    arg = ctx.console.print.call_args.args[0]
    assert "anthropic" in arg
    assert "active provider" in arg


def test_handle_provider_with_arg_calls_callback():
    captured: dict = {}

    def _swap(p: str) -> tuple[bool, str]:
        captured["provider"] = p
        return (True, p)

    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_provider_swap = _swap
    _handle_provider(ctx, ["openai"])
    assert captured["provider"] == "openai"
    arg = ctx.console.print.call_args.args[0]
    assert "provider →" in arg


def test_handle_provider_failure_echoes_reason():
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_provider_swap = lambda p: (False, f"unknown {p!r}")
    _handle_provider(ctx, ["xyz"])
    arg = ctx.console.print.call_args.args[0]
    assert "swap failed" in arg


def test_model_with_anthropic_prefix_swaps_provider_and_strips_prefix():
    """`/model anthropic/claude-opus-4-7` → on_provider_swap(anthropic) +
    on_model_swap(claude-opus-4-7) (prefix stripped for native providers)."""
    swaps: dict = {}

    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (swaps.setdefault("model", m), True, m)[1:]  # type: ignore
    ctx.on_provider_swap = lambda p: (swaps.setdefault("provider", p), True, p)[1:]  # type: ignore

    _handle_model(ctx, ["anthropic/claude-opus-4-7"])
    assert swaps["provider"] == "anthropic"
    assert swaps["model"] == "claude-opus-4-7"


def test_model_with_openai_prefix_swaps_provider_and_strips_prefix():
    swaps: dict = {}
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (swaps.setdefault("model", m), True, m)[1:]  # type: ignore
    ctx.on_provider_swap = lambda p: (swaps.setdefault("provider", p), True, p)[1:]  # type: ignore

    _handle_model(ctx, ["openai/gpt-4o-mini"])
    assert swaps["provider"] == "openai"
    assert swaps["model"] == "gpt-4o-mini"  # prefix stripped


def test_model_with_openrouter_prefix_keeps_full_id():
    """OpenRouter routes vendor/model verbatim — pass through."""
    swaps: dict = {}
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (swaps.setdefault("model", m), True, m)[1:]  # type: ignore
    ctx.on_provider_swap = lambda p: (swaps.setdefault("provider", p), True, p)[1:]  # type: ignore

    _handle_model(ctx, ["openrouter/anthropic/claude-opus-4-7"])
    assert swaps["provider"] == "openrouter"
    # OpenRouter expects the FULL "vendor/model" id (including the part
    # we'd strip for native providers). We pass the original through.
    assert swaps["model"] == "openrouter/anthropic/claude-opus-4-7"


def test_model_with_unknown_vendor_prefix_treated_as_plain_model_id():
    """`/model some-random/path` with non-recognized vendor → passes
    the whole string to on_model_swap as a model id (no provider swap)."""
    swaps: dict = {}
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (swaps.setdefault("model", m), True, m)[1:]  # type: ignore
    ctx.on_provider_swap = lambda p: (swaps.setdefault("provider", p), True, p)[1:]  # type: ignore

    _handle_model(ctx, ["unknown-vendor/foo-bar"])
    assert "provider" not in swaps  # no provider swap
    assert swaps["model"] == "unknown-vendor/foo-bar"  # full id


def test_default_on_provider_swap_returns_not_wired():
    from rich.console import Console

    ctx = SlashContext(
        console=Console(),
        session_id="t",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
    )
    ok, msg = ctx.on_provider_swap("any")
    assert ok is False
    assert "not wired" in msg


# ── lookup_provider helper tests ─────────────────────────────────────


def test_lookup_provider_returns_instance(monkeypatch):
    from opencomputer.agent.provider_swap import lookup_provider
    from opencomputer.plugins.registry import registry

    class _StubProvider:
        def __init__(self):
            self.tag = "stub-instance"

    monkeypatch.setitem(registry.providers, "stub-test-only", _StubProvider)
    p = lookup_provider("stub-test-only")
    assert p.tag == "stub-instance"


def test_lookup_provider_unknown_raises():
    from opencomputer.agent.provider_swap import lookup_provider

    with pytest.raises(ValueError, match="unknown provider"):
        lookup_provider("definitely-not-registered-xyz")


def test_lookup_provider_propagates_init_error(monkeypatch):
    from opencomputer.agent.provider_swap import lookup_provider
    from opencomputer.plugins.registry import registry

    class _BoomProvider:
        def __init__(self):
            raise RuntimeError("MISSING_API_KEY env var")

    monkeypatch.setitem(registry.providers, "boom-test-only", _BoomProvider)
    with pytest.raises(RuntimeError, match="MISSING_API_KEY"):
        lookup_provider("boom-test-only")


def test_slash_registry_contains_provider():
    """Regression: /provider entry is in the SLASH_REGISTRY literal so
    _LOOKUP picks it up (built once at module import)."""
    from opencomputer.cli_ui.slash import SLASH_REGISTRY

    names = [c.name for c in SLASH_REGISTRY]
    assert "provider" in names


def test_handlers_dict_contains_provider():
    from opencomputer.cli_ui.slash_handlers import _HANDLERS, _handle_provider

    assert "provider" in _HANDLERS
    assert _HANDLERS["provider"] is _handle_provider
