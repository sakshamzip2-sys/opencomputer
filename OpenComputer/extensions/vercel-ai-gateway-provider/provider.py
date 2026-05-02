"""Vercel AI Gateway provider — thin subclass of OpenAIProvider.

Verified against Hermes auth.py:
  inference_base_url="https://ai-gateway.vercel.sh/v1",
  api_key_env_vars=("AI_GATEWAY_API_KEY",),
  base_url_env_var="AI_GATEWAY_BASE_URL",

Env vars:
  AI_GATEWAY_API_KEY    — required; key from
                          https://vercel.com/dashboard/ai/gateway/api-keys
  AI_GATEWAY_BASE_URL   — optional override (default: https://ai-gateway.vercel.sh/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


class VercelAIGatewayProvider(OpenAIProvider):
    name = "ai-gateway"
    default_model = "anthropic/claude-sonnet-4"
    _api_key_env: str = "AI_GATEWAY_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://vercel.com/dashboard/ai/gateway/api-keys."
            )
        resolved_base = (
            base_url
            or os.environ.get("AI_GATEWAY_BASE_URL")
            or DEFAULT_AI_GATEWAY_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
