"""Kimi (China region) provider — thin subclass of OpenAIProvider.

Verified against Hermes auth.py:
  inference_base_url="https://api.moonshot.cn/v1",
  api_key_env_vars=("KIMI_CN_API_KEY",),

Env vars:
  KIMI_CN_API_KEY    — required; key from https://platform.moonshot.cn
  KIMI_CN_BASE_URL   — optional override (default: https://api.moonshot.cn/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_KIMI_CN_BASE_URL = "https://api.moonshot.cn/v1"


class KimiChinaProvider(OpenAIProvider):
    name = "kimi-cn"
    default_model = "moonshot-v1-128k"
    _api_key_env: str = "KIMI_CN_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://platform.moonshot.cn."
            )
        resolved_base = (
            base_url
            or os.environ.get("KIMI_CN_BASE_URL")
            or DEFAULT_KIMI_CN_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
