"""MiniMax China (Anthropic-shaped) provider.

Verified against Hermes auth.py:
  inference_base_url="https://api.minimaxi.com/anthropic",
  api_key_env_vars=("MINIMAX_CN_API_KEY",),
  transport="anthropic_messages"

Env vars:
  MINIMAX_CN_API_KEY   — required; key from https://api.minimaxi.com
  MINIMAX_CN_BASE_URL  — optional override (default: api.minimaxi.com/anthropic)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ANTHROPIC_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "anthropic-provider"
if str(_ANTHROPIC_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANTHROPIC_PROVIDER_DIR))

from provider import AnthropicProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_MINIMAX_CN_BASE_URL = "https://api.minimaxi.com/anthropic"


class MiniMaxChinaAnthropicProvider(AnthropicProvider):
    name = "minimax-cn"
    default_model = "MiniMax-M1"
    _api_key_env: str = "MINIMAX_CN_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://api.minimaxi.com."
            )
        resolved_base = (
            base_url
            or os.environ.get("MINIMAX_CN_BASE_URL")
            or DEFAULT_MINIMAX_CN_BASE_URL
        )
        super().__init__(
            api_key=api_key or os.environ.get(self._api_key_env),
            base_url=resolved_base,
            auth_mode=auth_mode or "bearer",
        )
