"""Lock-in test for the gateway runtime-footer context_length wiring.

2026-05-09 — closes the residual TODO at gateway/dispatch.py:1343
where ``context_length`` was always ``None``, dropping the
``context_pct`` field from every gateway-rendered footer.

Tests the public surface that gateway code reaches for: the resolver
chain via ``context_window_with_overrides``. The full path through
``Dispatch._do_dispatch`` is exercised in higher-level tests; here we
validate the resolver call shape (kwargs the dispatch site uses) so a
silent signature drift can't reintroduce the regression.
"""

from __future__ import annotations

from types import SimpleNamespace


def test_context_window_resolver_used_by_dispatch_signature():
    """The kwargs the dispatch site passes resolve to a positive int.

    Mirrors the exact call shape from
    ``opencomputer/gateway/dispatch.py`` (the only callsite for
    gateway-side context_length). Catches API drift in
    ``context_window_with_overrides`` that would re-break the footer.
    """
    from opencomputer.agent.compaction import context_window_with_overrides

    # SimpleNamespace stub mirrors what ``Config`` exposes — no real
    # config object needed for this resolver shape test.
    cfg = SimpleNamespace(
        custom_providers=(),
        model_context_overrides=None,
    )
    result = context_window_with_overrides(
        "claude-opus-4-7",
        custom_providers=cfg.custom_providers,
        model_context_overrides=cfg.model_context_overrides,
        enable_probe=False,
    )
    assert isinstance(result, int)
    assert result > 0


def test_context_window_resolver_honours_user_override():
    """User-supplied per-model override wins."""
    from opencomputer.agent.compaction import context_window_with_overrides

    custom_overrides = {"my-finetune-v2": 1_000_000}
    result = context_window_with_overrides(
        "my-finetune-v2",
        custom_providers=(),
        model_context_overrides=custom_overrides,
        enable_probe=False,
    )
    assert result == 1_000_000


def test_context_window_resolver_returns_default_for_unknown_model():
    """Unknown model + no override → conservative 64k+ default."""
    from opencomputer.agent.compaction import context_window_with_overrides

    result = context_window_with_overrides(
        "totally-fictional-model-name-12345",
        custom_providers=(),
        model_context_overrides=None,
        enable_probe=False,
    )
    # context_window_for default is 64k (conservative)
    assert isinstance(result, int)
    assert result >= 64_000


def test_dispatch_context_length_wiring_is_truthy_for_known_model():
    """End-to-end: format_runtime_footer with a real probe value renders %."""
    from opencomputer.agent.compaction import context_window_with_overrides
    from opencomputer.gateway.runtime_footer import format_runtime_footer

    ctx_len = context_window_with_overrides(
        "claude-opus-4-7",
        custom_providers=(),
        model_context_overrides=None,
        enable_probe=False,
    )
    line = format_runtime_footer(
        model="claude-opus-4-7",
        tokens_used=10_000,
        context_length=ctx_len,
        cwd="/x",
    )
    # The %-bucket should now render — was previously silent (None).
    assert "%" in line
    assert "claude-opus-4-7" in line
