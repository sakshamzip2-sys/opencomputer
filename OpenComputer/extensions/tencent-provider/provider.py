"""Tencent TokenHub (Hunyuan) provider — thin subclass of OpenAIProvider.

Env vars:
  TENCENT_API_KEY    — required; key from https://tokenhub.tencentmaas.com
  TENCENT_BASE_URL   — optional override (default: https://api.lkeap.cloud.tencent.com/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_TENCENT_BASE_URL = "https://tokenhub.tencentmaas.com/v1"


class TencentProvider(OpenAIProvider):
    name = "tencent"
    default_model = "hunyuan-pro"
    _api_key_env: str = "TOKENHUB_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://tokenhub.tencentmaas.com."
            )
        resolved_base = (
            base_url
            or os.environ.get("TOKENHUB_BASE_URL")
            or DEFAULT_TENCENT_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
