"""xAI (Grok) provider — thin subclass of OpenAIProvider.

xAI exposes an OpenAI-wire-compatible API for Grok models.
Pattern mirrors extensions/deepseek-provider exactly — see that
module's docstring for the rationale.

Env vars:
  XAI_API_KEY    — required; key from https://console.x.ai
  XAI_BASE_URL   — optional override (default: https://api.x.ai/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"


class XAIProvider(OpenAIProvider):
    name = "xai"
    default_model = "grok-2-1212"
    _api_key_env: str = "XAI_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://console.x.ai."
            )
        resolved_base = (
            base_url
            or os.environ.get("XAI_BASE_URL")
            or DEFAULT_XAI_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
