"""Ollama Cloud provider — thin subclass of OpenAIProvider.

Env vars:
  OLLAMA_API_KEY    — required; key from https://ollama.com/settings/keys
  OLLAMA_BASE_URL   — optional override (default: https://ollama.com/api/v1)
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name
# to avoid sys.modules['provider'] collision when multiple
# OpenAI-compat providers are loaded in the same process
# (PR #353 fix for zai-provider/openrouter-provider, extended here).
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_ollama_cloud", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_OLLAMA_BASE_URL = "https://ollama.com/v1"


class OllamaCloudProvider(OpenAIProvider):
    name = "ollama-cloud"
    default_model = "gpt-oss:120b"
    _api_key_env: str = "OLLAMA_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://ollama.com/settings/keys."
            )
        resolved_base = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or DEFAULT_OLLAMA_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
