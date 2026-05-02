"""Pure-function capability table for model-conditional API kwargs.

Anthropic's model lineup has diverged enough that one provider can't
send identical kwargs to every model:

* Opus 4.7+ and Mythos reject ``temperature``/``top_p``/``top_k`` and
  reject manual extended thinking (``thinking: {type: enabled,
  budget_tokens: N}``); they require ``thinking: {type: adaptive}``
  with ``output_config.effort``.
* Opus 4.6 / Sonnet 4.6 accept both shapes but adaptive is recommended
  and ``temperature`` is still allowed.
* Opus 4.5 and older only support the legacy thinking shape.

This module answers three yes/no questions per model so the provider
and runtime_flags can pick the right shape without each rolling its
own table.

Detection is allowlist-based with a forward-compatible default: an
unknown ``claude-*`` model name is assumed "modern" (adaptive,
no temperature). Anthropic's trajectory is everything moves to that
shape; a wrong guess on a future model is one-line to fix.
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


def supports_temperature(model: str) -> bool:
    """True if the model accepts ``temperature``/``top_p``/``top_k`` kwargs.

    Opus 4.7+, Mythos, and future modern claude-* reject these (return
    False). Legacy claude-* and all non-claude models accept them
    (return True). 4.6 specifically still accepts temperature even
    though adaptive thinking is recommended there.
    """
    # Legacy claude-* keeps temperature.
    if _is_legacy_claude(model):
        return True
    # Modern claude-* (4.6, 4.7, Mythos, unknown-future) — only 4.6 keeps it.
    if _is_claude(model):
        if model.startswith("claude-opus-4-6") or model.startswith(
            "claude-sonnet-4-6"
        ):
            return True
        return False
    # Non-claude models: providers handle their own param names; we
    # never strip temperature from them here.
    return True


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
