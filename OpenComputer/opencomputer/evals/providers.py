"""Thin adapter from OpenComputer's provider plugins to the eval-grader/generator interface.

The eval graders/generators only need a .complete(prompt: str) -> obj
with a .text attribute. This adapter wraps any registered provider and
bridges async -> sync.
"""

from __future__ import annotations

import asyncio
from typing import Any


class ProviderShim:
    """Wraps a BaseProvider into the minimal .complete(prompt) interface.

    Bridges OpenComputer's async provider API to the sync interface the
    grader/generator code expects.
    """

    def __init__(self, provider, model: str):
        self._provider = provider
        self._model = model

    def complete(self, prompt: str) -> Any:
        from plugin_sdk.core import Message

        response = asyncio.run(
            self._provider.complete(
                model=self._model,
                messages=[Message(role="user", content=prompt)],
                max_tokens=2048,
                temperature=0.3,
                site="eval_grader",
            )
        )
        # ProviderResponse.message.content is the assistant text.
        text = response.message.content if hasattr(response, "message") else str(response)
        return type("ShimResponse", (), {"text": text})()


def get_grader_provider(model_override: str | None = None, provider_override: str | None = None) -> ProviderShim:
    """Pick a grader provider/model that DIFFERS from the default chat model.

    Resolution order:
      1. Explicit overrides (--grader-model + optional --grader-provider).
      2. Auto-pick: if chat is Sonnet 4.6, grade with Opus 4.7 (same provider).
         If chat is Opus 4.7, grade with Sonnet 4.6 (same provider).
      3. For non-Anthropic chat models: explicit --grader-model required.

    Works with any registered provider — not Anthropic-specific.
    """
    from opencomputer.agent.config_store import load_config
    from opencomputer.plugins.registry import registry

    config = load_config()
    chat_model = config.model.model
    chat_provider = config.model.provider

    if model_override is not None:
        target_model = model_override
        target_provider = provider_override or chat_provider
    elif "sonnet" in chat_model.lower():
        target_model = "claude-opus-4-7"
        target_provider = chat_provider
    elif "opus" in chat_model.lower():
        target_model = "claude-sonnet-4-6"
        target_provider = chat_provider
    else:
        raise RuntimeError(
            f"Cannot auto-pick a grader model for chat model {chat_model!r}. "
            "Pass --grader-model and optionally --grader-provider explicitly."
        )

    provider = registry.providers.get(target_provider)
    if provider is None:
        raise RuntimeError(
            f"Provider {target_provider!r} not registered; cannot use rubric grader. "
            "Configure the provider or pass --grader-provider with one that's installed."
        )
    return ProviderShim(provider, target_model)
