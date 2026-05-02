"""Cost computation helpers — batch-discount aware.

Subsystem E follow-up (2026-05-02). Wraps the model_metadata pricing
table with a batch-discount multiplier so callers recording batch-API
usage get the correct cost without rolling their own arithmetic.

Used by: any caller recording usage after a batch call (Anthropic
``messages.batches.results``, OpenAI ``batches.retrieve`` →
``files.content``). The batch-API discount is provider-policy: both
Anthropic and OpenAI advertise 50% off both input and output tokens
for batch usage.

Provider-agnostic: ``compute_call_cost`` takes the provider name and a
batch flag — providers that don't offer batch discounts (Llama,
Ollama, etc.) simply pass ``batch=False`` always. Future providers
that ship batch APIs add their multiplier here.
"""

from __future__ import annotations

from opencomputer.agent.model_metadata import cost_per_million

# Provider-specific batch discount multipliers (1.0 = no discount).
# Both Anthropic and OpenAI offer 50% off batch usage (per their docs).
_BATCH_DISCOUNT: dict[str, float] = {
    "anthropic": 0.5,
    "openai": 0.5,
    # Other providers: add here when they ship batch APIs.
}


def batch_discount_for(provider: str) -> float:
    """Return the batch discount multiplier for a provider (1.0 = no discount)."""
    return _BATCH_DISCOUNT.get(provider.lower(), 1.0)


def compute_call_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    batch: bool = False,
) -> float | None:
    """Compute USD cost for a single call.

    Looks up per-million pricing via ``model_metadata.cost_per_million``,
    applies the batch discount if ``batch=True`` for a provider that
    offers one, returns the total cost in USD.

    Returns ``None`` when no pricing data is available — caller decides
    whether to skip recording, log a warning, or fall back to a free-
    tier estimate.

    Parameters
    ----------
    provider:
        Provider id (e.g. ``"anthropic"``, ``"openai"``). Case-insensitive.
    model:
        Model id (e.g. ``"claude-opus-4-7"``, ``"gpt-4o"``).
    input_tokens:
        Prompt-side token count for this call.
    output_tokens:
        Completion-side token count.
    batch:
        ``True`` if the call was submitted via the provider's batch
        API. Applies the per-provider batch discount.
    """
    pricing = cost_per_million(model)
    if pricing is None:
        return None
    input_per_m, output_per_m = pricing
    cost = (input_tokens * input_per_m + output_tokens * output_per_m) / 1_000_000
    if batch:
        cost *= batch_discount_for(provider)
    return cost


__all__ = ["batch_discount_for", "compute_call_cost"]
