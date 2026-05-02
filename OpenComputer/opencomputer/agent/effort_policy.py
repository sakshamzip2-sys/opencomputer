"""Per-context reasoning_effort policy — provider-agnostic defaults.

Most calls don't need maximum reasoning, but the API defaults to ``high``
on every modern reasoning-capable model. That's wasteful for subagents
(which Doc 5 explicitly calls out as a ``low`` use case), latency-
sensitive surfaces like voice mode, and Sonnet 4.6 chat (which Doc 5
warns about: "explicitly set effort when using Sonnet 4.6 to avoid
unexpected latency").

This module answers one question: "Given the current context — model,
runtime flags, subagent depth — what's a sensible default
``reasoning_effort``?"

Returns a value in OpenComputer's internal scale
(``minimal``/``low``/``medium``/``high``/``xhigh``/``max``). The
provider's ``*_kwargs_from_runtime`` translator maps it to the provider's
native shape (Anthropic ``output_config.effort`` for adaptive models,
``budget_tokens`` for legacy, OpenAI ``reasoning_effort``, etc.).

Provider-agnostic: applies to any provider whose translator accepts
``reasoning_effort``. Models that don't support reasoning at all
(legacy Claude 3, base Llama, etc.) are unaffected — their
translators ignore the field.

The policy is **deferential**: it only suggests a default when the
user hasn't explicitly set ``reasoning_effort`` via ``/reasoning``.
A user-set value always wins.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext


def _model_default(model: str) -> str | None:
    """Per-model effort default. ``None`` = no recommendation."""
    # Claude — per Doc 5 guidance
    if model.startswith("claude-opus-4-7"):
        return "xhigh"  # recommended starting point for coding/agentic
    if model.startswith("claude-sonnet-4-6"):
        return "medium"  # explicit Doc 5 warning about default-high latency
    if model.startswith("claude-sonnet-4-5"):
        return "medium"  # same latency profile as 4.6
    # OpenAI reasoning models — sensible mid default for paid usage.
    # Users with cost-sensitive workloads override via /reasoning low.
    for p in ("o1", "o3", "o4", "gpt-5-thinking"):
        if model.startswith(p):
            return "medium"
    # Default: no recommendation. The API/provider default applies.
    return None


def recommended_effort(
    *,
    runtime: RuntimeContext | None,
    model: str,
) -> str | None:
    """Recommend a ``reasoning_effort`` value, or ``None`` to use the API default.

    Priority order:
      1. **Subagent context** (``runtime.delegation_depth > 0``) → ``low``.
         Per Doc 5: subagents are the canonical low-effort use case.
      2. **Voice mode** (``runtime.custom["voice_mode"] is True``) → ``low``.
         Realtime voice is latency-bound; thinking budget kills round-trip.
      3. **Per-model defaults** (see ``_model_default``) — Doc 5 calibrated
         tiers per model lineage.
      4. ``None`` — use the API's own default.

    The caller is responsible for checking that the user hasn't already
    set ``reasoning_effort`` via ``/reasoning``. This function returns
    the recommendation independent of user-set state.
    """
    # Subagent override wins over everything: a coding subagent on Opus
    # 4.7 should still be ``low``, not ``xhigh``. Subagents are by
    # definition narrow-scoped tasks delegated from a richer parent.
    if runtime is not None and getattr(runtime, "delegation_depth", 0) > 0:
        return "low"

    # Voice mode: latency-sensitive. Realtime voice (PR #270) cannot
    # afford a thinking budget on the critical path.
    if runtime is not None and runtime.custom:
        if runtime.custom.get("voice_mode") is True:
            return "low"

    # Per-model default.
    return _model_default(model)


__all__ = ["recommended_effort"]
