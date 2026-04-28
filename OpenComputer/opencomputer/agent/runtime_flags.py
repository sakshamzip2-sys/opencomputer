"""Translate runtime.custom flags into provider-specific API kwargs.

Tier 2.A provider integration follow-up: ``/reasoning`` and ``/fast`` slash
commands store flags in ``runtime.custom``; this module translates those
flags into the keyword arguments each provider's API expects, so the
flags actually take effect on the next LLM call.

Translation tables here, not in the providers, so:
  - The mapping is unit-testable in isolation (no provider mocks needed).
  - Adding a third provider just adds one new translator function.
  - The audit-doc-defined effort levels (none/minimal/low/medium/high/xhigh)
    have a single source of truth for their semantic meaning.
"""

from __future__ import annotations

# Anthropic: ``thinking={"type": "enabled", "budget_tokens": N}``.
# Token budgets calibrated to the public guidance (low ≈ short scratch,
# medium ≈ default, high ≈ deep reasoning, xhigh ≈ extended trains of thought).
_ANTHROPIC_REASONING_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    # "none" → omit thinking entirely
}

# OpenAI: ``reasoning_effort`` field accepts {minimal, low, medium, high}.
# OC's ``xhigh`` extends past OpenAI's range; we cap at "high".
_OPENAI_REASONING_MAP: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    # "none" → omit reasoning_effort
}


def anthropic_kwargs_from_runtime(
    *,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
) -> dict:
    """Build the Anthropic-specific kwargs to merge into a ``messages.create`` call.

    Returns an empty dict when no flags are active, so callers can
    unconditionally ``kwargs.update(anthropic_kwargs_from_runtime(...))``
    without branching.
    """
    out: dict = {}
    if reasoning_effort and reasoning_effort != "none":
        budget = _ANTHROPIC_REASONING_BUDGET.get(reasoning_effort)
        if budget is not None:
            out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if service_tier == "priority":
        out["service_tier"] = "priority"
    return out


def openai_kwargs_from_runtime(
    *,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
) -> dict:
    """Build the OpenAI Chat Completions kwargs to merge into the request body."""
    out: dict = {}
    if reasoning_effort and reasoning_effort != "none":
        mapped = _OPENAI_REASONING_MAP.get(reasoning_effort)
        if mapped is not None:
            out["reasoning_effort"] = mapped
    if service_tier == "priority":
        out["service_tier"] = "priority"
    return out


def runtime_flags_from_custom(custom: dict | None) -> dict[str, str | None]:
    """Extract the relevant runtime.custom keys; safe on missing or None.

    Returns ``{"reasoning_effort": ..., "service_tier": ...}`` — values may
    be ``None`` when the flag isn't set. Pass ``**runtime_flags_from_custom(rt.custom)``
    into the translators above.
    """
    if not custom:
        return {"reasoning_effort": None, "service_tier": None}
    re = custom.get("reasoning_effort")
    st = custom.get("service_tier")
    return {
        "reasoning_effort": re if isinstance(re, str) else None,
        "service_tier": st if isinstance(st, str) else None,
    }


__all__ = [
    "anthropic_kwargs_from_runtime",
    "openai_kwargs_from_runtime",
    "runtime_flags_from_custom",
]
