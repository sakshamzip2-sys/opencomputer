"""OpenCode Go provider — thin subclass of OpenAIProvider.

Env vars:
  OPENCODE_GO_API_KEY    — required; key from https://opencode.ai/go
  OPENCODE_GO_BASE_URL   — optional override (default: https://opencode.ai/go/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"


class OpenCodeGoProvider(OpenAIProvider):
    name = "opencode-go"
    default_model = "qwen/qwen2.5-coder-32b-instruct"
    _api_key_env: str = "OPENCODE_GO_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://opencode.ai/go."
            )
        resolved_base = (
            base_url
            or os.environ.get("OPENCODE_GO_BASE_URL")
            or DEFAULT_OPENCODE_GO_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
