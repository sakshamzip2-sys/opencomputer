"""Alibaba DashScope (Qwen) provider — thin subclass of OpenAIProvider.

Env vars:
  DASHSCOPE_API_KEY    — required; key from https://dashscope.aliyun.com
  DASHSCOPE_BASE_URL   — optional override (default:
                         https://dashscope.aliyuncs.com/compatible-mode/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class DashScopeProvider(OpenAIProvider):
    name = "dashscope"
    default_model = "qwen-max"
    _api_key_env: str = "DASHSCOPE_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://dashscope.aliyun.com/apiKey."
            )
        resolved_base = (
            base_url
            or os.environ.get("DASHSCOPE_BASE_URL")
            or DEFAULT_DASHSCOPE_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
