"""HuggingFace Inference Providers — thin subclass of OpenAIProvider.

HF's "Inference Providers" router exposes an OpenAI-compatible endpoint
at https://router.huggingface.co/v1 that fans out to whichever upstream
provider serves the requested model.

Env vars:
  HF_API_KEY    — required; HF token from https://huggingface.co/settings/tokens
  HF_BASE_URL   — optional override (default: https://router.huggingface.co/v1)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_HF_BASE_URL = "https://router.huggingface.co/v1"


class HuggingFaceProvider(OpenAIProvider):
    name = "huggingface"
    default_model = "meta-llama/Llama-3.3-70B-Instruct"
    _api_key_env: str = "HF_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a token at https://huggingface.co/settings/tokens."
            )
        resolved_base = (
            base_url
            or os.environ.get("HF_BASE_URL")
            or DEFAULT_HF_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
