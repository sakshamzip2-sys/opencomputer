"""DeepSeek provider — thin subclass of OpenAIProvider.

DeepSeek is OpenAI-wire-compatible; the only delta is the API-key env var
and the base URL. Pattern mirrors extensions/openrouter-provider exactly:
override ``_api_key_env`` so the parent's env-lookup at
``OpenAIProvider.__init__`` reads ``DEEPSEEK_API_KEY``, resolve the base
URL with our own env precedence, then delegate to the parent.

Env vars:
  DEEPSEEK_API_KEY    — required; key from https://platform.deepseek.com/api_keys
  DEEPSEEK_BASE_URL   — optional override (default: https://api.deepseek.com)
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
from pathlib import Path

# The bundled openai-provider lives at ../openai-provider; make its module
# importable so we can subclass. Same pattern openrouter-provider uses.
_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name
# to avoid sys.modules['provider'] collision when multiple
# OpenAI-compat providers are loaded in the same process
# (PR #353 fix for zai-provider/openrouter-provider, extended here).
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_deepseek", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(OpenAIProvider):
    """OpenAI-compatible provider routed through DeepSeek's direct API."""

    name = "deepseek"
    default_model = "deepseek-chat"
    _api_key_env: str = "DEEPSEEK_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Pre-check the env var so the error message names DeepSeek, not
        # OpenAI (the parent's RuntimeError mentions OPENAI_API_KEY).
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://platform.deepseek.com/api_keys."
            )
        # Resolve the base URL with our own env precedence so the parent
        # doesn't pull from OPENAI_BASE_URL by accident.
        resolved_base = (
            base_url
            or os.environ.get("DEEPSEEK_BASE_URL")
            or DEFAULT_DEEPSEEK_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
