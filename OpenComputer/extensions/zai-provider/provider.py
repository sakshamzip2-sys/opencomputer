"""Z.AI / GLM (Zhipu) provider — thin subclass of OpenAIProvider.

Env vars:
  ZAI_API_KEY    — required; key from https://open.bigmodel.cn
  ZAI_BASE_URL   — optional override (default: https://open.bigmodel.cn/api/paas/v4)
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name to
# avoid the sys.modules['provider'] collision that happens when two
# providers both do `from provider import OpenAIProvider`. The collision
# manifests as "cannot import name 'OpenAIProvider' from partially
# initialized module 'provider'" when a test process loads this and another
# provider (e.g. openrouter-provider) — see test_openrouter_inherits_vision_from_openai.
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_zai", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


class ZAIProvider(OpenAIProvider):
    name = "zai"
    default_model = "glm-4-plus"
    _api_key_env: str = "ZAI_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://open.bigmodel.cn."
            )
        resolved_base = (
            base_url
            or os.environ.get("ZAI_BASE_URL")
            or DEFAULT_ZAI_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
