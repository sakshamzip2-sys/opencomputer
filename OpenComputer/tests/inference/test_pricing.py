from opencomputer.inference.pricing import compute_cost_usd


def test_compute_cost_for_known_anthropic_model():
    cost = compute_cost_usd(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )
    # Sonnet 4-6 list price (verify against current Anthropic pricing page).
    assert cost is not None
    assert cost > 0


def test_unknown_model_returns_none():
    cost = compute_cost_usd(
        provider="some-provider",
        model="unknown-model",
        input_tokens=100,
        output_tokens=100,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )
    assert cost is None
