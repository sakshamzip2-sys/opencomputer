"""Azure AI Foundry provider — thin subclass of OpenAIProvider.

Azure exposes an OpenAI-compatible endpoint at the user's deployment
URL. Anthropic-style endpoints (Claude on Azure) need different
transport — deferred to a follow-up that adds api_mode resolution
to ModelConfig.

The user's Azure base URL is mandatory and unique to their
deployment; defaults are intentionally absent. Set
AZURE_FOUNDRY_BASE_URL env var or pass --base-url at setup time.

Env vars:
  AZURE_FOUNDRY_API_KEY    — required; key from your Azure AI deployment
  AZURE_FOUNDRY_BASE_URL   — required (no default); your deployment URL
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402


class AzureFoundryProvider(OpenAIProvider):
    name = "azure-foundry"
    default_model = "gpt-5"
    _api_key_env: str = "AZURE_FOUNDRY_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key from your Azure AI Foundry deployment "
                "at https://ai.azure.com."
            )
        resolved_base = (
            base_url
            or os.environ.get("AZURE_FOUNDRY_BASE_URL")
        )
        if not resolved_base:
            raise RuntimeError(
                "AZURE_FOUNDRY_BASE_URL is not set. Azure deployments "
                "have unique URLs; set AZURE_FOUNDRY_BASE_URL to your "
                "deployment endpoint (e.g. "
                "https://<your-resource>.openai.azure.com/openai/deployments/<deployment>/)."
            )
        super().__init__(api_key=api_key, base_url=resolved_base)
