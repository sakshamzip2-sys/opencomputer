"""Tests for cost_guard batch-discount pricing helpers."""

from __future__ import annotations

import pytest

from opencomputer.cost_guard import (
    batch_discount_for,
    compute_call_cost,
)

# ─── batch_discount_for ──────────────────────────────────────────


def test_anthropic_batch_discount_is_50_percent() -> None:
    assert batch_discount_for("anthropic") == 0.5


def test_openai_batch_discount_is_50_percent() -> None:
    assert batch_discount_for("openai") == 0.5


def test_unknown_provider_no_discount() -> None:
    """Providers without batch discounts return 1.0 (no discount)."""
    assert batch_discount_for("ollama") == 1.0
    assert batch_discount_for("llama-cpp") == 1.0
    assert batch_discount_for("kimi") == 1.0


def test_provider_name_case_insensitive() -> None:
    assert batch_discount_for("Anthropic") == 0.5
    assert batch_discount_for("ANTHROPIC") == 0.5
    assert batch_discount_for("OpenAI") == 0.5


# ─── compute_call_cost ──────────────────────────────────────────


def test_compute_call_cost_anthropic_non_batch(monkeypatch) -> None:
    """Standard rate: 1M input + 1M output → input_rate + output_rate."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: (5.0, 25.0))
    cost = compute_call_cost(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        batch=False,
    )
    assert cost == 30.0  # 5 + 25


def test_compute_call_cost_anthropic_batch_halves(monkeypatch) -> None:
    """Batch=True applies 50% discount."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: (5.0, 25.0))
    cost = compute_call_cost(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        batch=True,
    )
    assert cost == 15.0  # (5 + 25) * 0.5


def test_compute_call_cost_openai_batch_halves(monkeypatch) -> None:
    """OpenAI also offers 50% batch discount."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: (2.0, 8.0))
    cost = compute_call_cost(
        provider="openai",
        model="gpt-4o",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        batch=True,
    )
    assert cost == 5.0  # (2 + 8) * 0.5


def test_compute_call_cost_unknown_pricing_returns_none(monkeypatch) -> None:
    """Missing pricing data → caller-handles None."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: None)
    cost = compute_call_cost(
        provider="kimi",
        model="kimi-k2-future",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost is None


def test_compute_call_cost_zero_tokens_returns_zero(monkeypatch) -> None:
    """Zero tokens → zero cost (no division-by-zero, no NaN)."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: (5.0, 25.0))
    cost = compute_call_cost(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=0,
        output_tokens=0,
    )
    assert cost == 0.0


def test_compute_call_cost_no_batch_discount_for_non_batch_provider(monkeypatch) -> None:
    """Provider without batch discount → batch=True is no-op."""
    from opencomputer.cost_guard import pricing as _p

    monkeypatch.setattr(_p, "cost_per_million", lambda m: (1.0, 2.0))
    standard = compute_call_cost(
        provider="ollama", model="llama-3-70b",
        input_tokens=1_000_000, output_tokens=1_000_000,
        batch=False,
    )
    batched = compute_call_cost(
        provider="ollama", model="llama-3-70b",
        input_tokens=1_000_000, output_tokens=1_000_000,
        batch=True,
    )
    assert standard == batched == 3.0


def test_realistic_pricing_scenarios() -> None:
    """Small input, larger output — typical chat shape."""
    # Real Opus 4.7 pricing per 1M tokens (input $5, output $25)
    cost = compute_call_cost(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=10_000,
        output_tokens=2_000,
        batch=False,
    )
    # If pricing data is available, this should be a real number; if
    # not, None. Either is fine — we just want no crash.
    assert cost is None or cost >= 0
