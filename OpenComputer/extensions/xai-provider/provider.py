"""xAI (Grok) provider — thin subclass of OpenAIProvider.

xAI exposes an OpenAI-wire-compatible API for Grok models.
Pattern mirrors extensions/deepseek-provider exactly — see that
module's docstring for the rationale.

Env vars:
  XAI_API_KEY    — required; key from https://console.x.ai
  XAI_BASE_URL   — optional override (default: https://api.x.ai/v1)

Wave 3 (2026-05-08): every request automatically attaches
``x-grok-conv-id: <uuid>`` (per-process stable). xAI's KV cache
keys requests by this header, so reusing it across turns lets the
upstream skip recomputation for shared prompt prefixes — free perf
win for any multi-turn workload. The conv_id is generated once at
provider construction and reused for the lifetime of the instance.
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
import uuid
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name
# to avoid sys.modules['provider'] collision when multiple
# OpenAI-compat providers are loaded in the same process
# (PR #353 fix for zai-provider/openrouter-provider, extended here).
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_xai", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"


class XAIProvider(OpenAIProvider):
    name = "xai"
    default_model = "grok-2-1212"
    _api_key_env: str = "XAI_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        conv_id: str | None = None,
    ) -> None:
        """Construct an xAI provider.

        ``conv_id`` overrides the auto-generated KV-cache identity
        (``x-grok-conv-id`` header). The default is a fresh UUID4 per
        instance, which is the right behavior for a single CLI session.
        """
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://console.x.ai."
            )
        resolved_base = (
            base_url
            or os.environ.get("XAI_BASE_URL")
            or DEFAULT_XAI_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
        self.conv_id: str = conv_id or str(uuid.uuid4())
        # AsyncOpenAI.with_options merges the extra header into every
        # request the client sends. The returned client is a new instance;
        # rebind self.client so all downstream calls flow through it.
        self.client = self.client.with_options(
            default_headers={"x-grok-conv-id": self.conv_id}
        )
