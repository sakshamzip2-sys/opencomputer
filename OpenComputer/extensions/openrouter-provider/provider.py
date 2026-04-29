"""OpenRouter provider — thin subclass of OpenAIProvider.

OpenRouter is OpenAI-wire-compatible; the only delta is the API-key env var
and the base URL. We subclass and override ``_api_key_env`` so the parent's
existing env-lookup at ``OpenAIProvider.__init__`` reads
``OPENROUTER_API_KEY`` instead of ``OPENAI_API_KEY``. We pass ``api_key=``
and ``base_url=`` through to the parent's kwargs directly.

CredentialPool rotation works unchanged because the parent's comma-split
happens on whatever ``api_key`` value we pass in
(``OPENROUTER_API_KEY="key1,key2"`` flows through to the parent's
``_split_keys_into_pool`` path).

Env vars:
  OPENROUTER_API_KEY   — required; key from https://openrouter.ai/keys
  OPENROUTER_BASE_URL  — optional override (default: openrouter.ai)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The bundled openai-provider lives at ../openai-provider; make its module
# importable so we can subclass. The plugin loader normally manages this in
# production via PluginAPI; here we add the path explicitly for direct import
# (and tests).
_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    """OpenAI-compatible provider routed through OpenRouter.

    Override ``_api_key_env`` so the parent's env lookup at
    ``OpenAIProvider.__init__`` reads ``OPENROUTER_API_KEY``. Override
    ``default_model`` to a sensible OpenRouter-shaped id (users can still
    pick any model OpenRouter exposes via ``model:`` in config.yaml).
    """

    name = "openrouter"
    default_model = "openai/gpt-4o-mini"
    _api_key_env: str = "OPENROUTER_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Pre-check the env var so the error message names OpenRouter, not
        # OpenAI (the parent's RuntimeError says "Export OPENAI_API_KEY").
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a free key at https://openrouter.ai/keys."
            )
        # Resolve the base URL with our own env precedence so the parent
        # doesn't pull from OPENAI_BASE_URL by accident.
        resolved_base = (
            base_url
            or os.environ.get("OPENROUTER_BASE_URL")
            or DEFAULT_OPENROUTER_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
