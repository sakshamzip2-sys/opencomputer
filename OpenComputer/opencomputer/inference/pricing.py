"""Per-model price table (USD per 1M tokens).

Update this when Anthropic / OpenAI / others publish new pricing.
Pricing recorded: 2026-05-02. Verify against live pricing pages on update.
"""

# Verify these against the live pricing pages before shipping.
PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-opus-4-7"):    {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_creation": 18.75},
    ("anthropic", "claude-sonnet-4-6"):  {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75},
    ("anthropic", "claude-haiku-4-5"):   {"input": 0.80,  "output": 4.00,  "cache_read": 0.08, "cache_creation": 1.00},
    # OpenAI / DeepSeek / Kimi etc — fill from their pricing pages on first integration.
}


def compute_cost_usd(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """Return cost in USD, or None if the provider/model isn't in the table."""
    key = (provider, model)
    if key not in PRICING:
        return None
    p = PRICING[key]
    cost = (
        (input_tokens / 1_000_000) * p["input"]
        + (output_tokens / 1_000_000) * p["output"]
        + (cache_read_tokens / 1_000_000) * p.get("cache_read", 0.0)
        + (cache_creation_tokens / 1_000_000) * p.get("cache_creation", 0.0)
    )
    return cost
