"""Generic per-model capability table — provider-agnostic interface.

Modern AI providers ship multiple models with diverging API surfaces.
The same provider often has models that take different kwargs:

* Anthropic Opus 4.7+ rejects ``temperature``/``top_p``/``top_k`` and
  requires ``thinking: {type: adaptive}`` instead of the legacy
  ``enabled+budget_tokens`` shape.
* OpenAI's reasoning models (o1, o3, gpt-5-thinking) reject
  ``temperature`` and use ``reasoning_effort`` instead of vendor-specific
  thinking blocks.
* Local providers (Ollama, llama.cpp) accept whatever the upstream
  generic shape allows but may have their own ``num_predict``-style
  output-token quirks.

This module is a **registry** — providers contribute their model→
capability data here, and the agent loop / runtime_flags consult the
unified API. The framework is generic; the *data* below is currently
Anthropic + OpenAI-reasoning because those are the providers where
model-conditional kwargs matter today. New providers (Kimi/Moonshot,
Z.AI, DeepSeek thinking models, future Llama variants) extend the
appropriate function as they ship support for capability-gated kwargs.

The three questions every provider needs answered:
  * ``supports_adaptive_thinking(model)`` — does this model accept
    Anthropic's ``thinking: {type: adaptive}`` shape? (Today: only
    modern Claude. Future: maybe other providers if they adopt it.)
  * ``supports_temperature(model)`` — does this model accept
    ``temperature``/``top_p``/``top_k``? Reasoning models often don't.
  * ``thinking_display_default(model)`` — recommended ``display`` field
    for the thinking block (only meaningful where adaptive is true).

Detection is allowlist-based with a forward-compatible default: an
unknown ``claude-*`` name is assumed "modern" (adaptive, no
temperature) — Anthropic's trajectory. Other providers default to
"keeps temperature" until they explicitly opt out.
"""

from __future__ import annotations

# Models that explicitly KEEP the legacy "manual extended thinking"
# shape (``thinking: {type: enabled, budget_tokens: N}``) and KEEP
# ``temperature``/``top_p``/``top_k``. Anything else with a ``claude-``
# prefix gets the modern (adaptive, no-temperature) treatment.
_LEGACY_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-sonnet-3-7",
    "claude-haiku-3",
    "claude-3-",  # claude-3-opus, claude-3-sonnet, claude-3-haiku
)


def _is_claude(model: str) -> bool:
    return model.startswith("claude-") or model.startswith("claude/")


def _is_legacy_claude(model: str) -> bool:
    return any(model.startswith(p) for p in _LEGACY_PREFIXES)


def supports_adaptive_thinking(model: str) -> bool:
    """True if the model accepts ``thinking: {type: adaptive}``.

    Modern Anthropic models (Opus 4.6+, Sonnet 4.6+, Mythos, future
    claude-*). Legacy claude-* and non-claude models return False.
    """
    if not _is_claude(model):
        return False
    return not _is_legacy_claude(model)


# OpenAI reasoning models reject ``temperature``/``top_p``/``top_k``.
# Pattern follows Anthropic's lead: any model whose name signals
# "reasoning-tier" drops temperature. Update as OpenAI ships new
# reasoning lineups.
_OPENAI_NO_TEMPERATURE_PREFIXES: tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
    "gpt-5-thinking",
    "gpt-6-thinking",
)


def supports_temperature(model: str) -> bool:
    """True if the model accepts ``temperature``/``top_p``/``top_k`` kwargs.

    Returns False for:
      * Modern Anthropic (Opus 4.7+, Mythos, future claude-* — except 4.6
        which Anthropic explicitly kept temperature on).
      * OpenAI reasoning models (o1, o3, o4, gpt-5-thinking, etc.).

    Returns True for legacy Anthropic, OpenAI chat (gpt-4o, gpt-4),
    and all other models (Kimi, Llama, Mistral, etc.) until those
    providers explicitly opt out.
    """
    # Legacy claude-* keeps temperature.
    if _is_legacy_claude(model):
        return True
    # Modern claude-* (4.6, 4.7, Mythos, unknown-future) — only 4.6 keeps it.
    if _is_claude(model):
        return model.startswith("claude-opus-4-6") or model.startswith(
            "claude-sonnet-4-6"
        )
    # OpenAI reasoning lineups reject temperature. Default for unknown
    # providers: keep temperature. Providers that need stricter behavior
    # can extend this function with their own prefix list (Kimi/Moonshot
    # reasoning, Z.AI, DeepSeek-Reasoner, future Llama-thinking variants).
    return not any(model.startswith(p) for p in _OPENAI_NO_TEMPERATURE_PREFIXES)


def thinking_display_default(model: str) -> str:
    """Recommended ``display`` field value for the thinking block.

    Returns ``"summarized"`` for adaptive-thinking models so the
    streaming Thinking Dropdown receives ``thinking_delta`` events.
    Returns ``""`` for legacy/non-claude models — the caller should
    omit the ``display`` kwarg entirely in that case.
    """
    return "summarized" if supports_adaptive_thinking(model) else ""


__all__ = [
    "supports_adaptive_thinking",
    "supports_temperature",
    "thinking_display_default",
]
