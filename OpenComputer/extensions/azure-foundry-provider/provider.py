"""Azure AI Foundry provider — picks OpenAI vs Anthropic transport per ``api_mode``.

Azure AI Foundry deployments can host either:

  - **OpenAI-shaped models** (GPT-4o, GPT-5, etc.) at deployments speaking
    chat/completions; OR
  - **Anthropic-shaped models** (Claude 3.5 Sonnet, Claude Opus 4) at
    deployments speaking the Anthropic /v1/messages wire format.

Both deployment URLs route through the same ``azure-foundry`` provider name;
the wire shape is selected per-call by ``ModelConfig.api_mode``:

    api_mode="auto"      → defaults to "openai" (legacy behavior)
    api_mode="openai"    → subclass OpenAIProvider transport
    api_mode="anthropic" → subclass AnthropicProvider transport

Resolution order for api_mode:
  1. Constructor kwarg (passed by ``_resolve_provider`` from ModelConfig)
  2. ``AZURE_FOUNDRY_API_MODE`` env var
  3. ``"auto"`` → defaults to OpenAI shape

Env vars:
  AZURE_FOUNDRY_API_KEY     — required
  AZURE_FOUNDRY_BASE_URL    — required (no default; per-deployment)
  AZURE_FOUNDRY_API_MODE    — optional; "openai" / "anthropic" / "auto"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
_ANTHROPIC_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "anthropic-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

VALID_API_MODES = frozenset({"auto", "openai", "anthropic"})


def _validate_api_mode(api_mode: str) -> str:
    if api_mode not in VALID_API_MODES:
        raise ValueError(
            f"api_mode must be one of {sorted(VALID_API_MODES)!r}, got {api_mode!r}"
        )
    return api_mode


def _resolve_required_env() -> tuple[str, str]:
    """Read AZURE_FOUNDRY_API_KEY + AZURE_FOUNDRY_BASE_URL; raise on absence."""
    key = os.environ.get("AZURE_FOUNDRY_API_KEY")
    base = os.environ.get("AZURE_FOUNDRY_BASE_URL")
    if not key:
        raise RuntimeError(
            "AZURE_FOUNDRY_API_KEY is not set. "
            "Get a key from your Azure AI Foundry deployment "
            "at https://ai.azure.com."
        )
    if not base:
        raise RuntimeError(
            "AZURE_FOUNDRY_BASE_URL is not set. Azure deployments have unique "
            "URLs; set AZURE_FOUNDRY_BASE_URL to your deployment endpoint."
        )
    return key, base


class AzureFoundryProvider:
    """Azure Foundry provider — picks OpenAI or Anthropic transport per ``api_mode``.

    Constructed via ``__new__`` so callers see the right subclass instance for
    their ``api_mode``. ``api_mode='openai'`` (or 'auto') yields an
    OpenAI-shaped subclass; ``api_mode='anthropic'`` yields an
    Anthropic-shaped wrapper. Class-level attributes (``name``,
    ``_api_key_env``, ``default_model``) remain on this top-level class so
    plugin-registry introspection keeps working.
    """

    name = "azure-foundry"
    default_model = "gpt-5"
    _api_key_env: str = "AZURE_FOUNDRY_API_KEY"

    def __new__(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
        api_mode: str | None = None,
    ):
        resolved_api_mode = _validate_api_mode(
            api_mode or os.environ.get("AZURE_FOUNDRY_API_MODE", "auto")
        )
        if resolved_api_mode == "anthropic":
            return _AzureFoundryAnthropicProvider(api_key=api_key, base_url=base_url)
        return _AzureFoundryOpenAIProvider(api_key=api_key, base_url=base_url)


class _AzureFoundryOpenAIProvider(OpenAIProvider):
    """Azure Foundry over OpenAI chat/completions wire shape."""

    name = "azure-foundry"
    default_model = "gpt-5"
    _api_key_env: str = "AZURE_FOUNDRY_API_KEY"
    _api_mode = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get(self._api_key_env)):
            _resolve_required_env()  # raise with friendly message
        resolved_base = (
            base_url
            or os.environ.get("AZURE_FOUNDRY_BASE_URL")
        )
        if not resolved_base:
            _resolve_required_env()  # raise with friendly message
        super().__init__(api_key=api_key, base_url=resolved_base)


def _build_anthropic_subclass():
    """Lazily import AnthropicProvider so its heavy deps don't load by default.

    Returned only when api_mode='anthropic' is actually selected. The Anthropic
    SDK pulls in pydantic + anthropic; non-Anthropic Azure users shouldn't pay
    that cost.
    """
    if str(_ANTHROPIC_PROVIDER_DIR) not in sys.path:
        sys.path.insert(0, str(_ANTHROPIC_PROVIDER_DIR))
    sys.modules.pop("provider_anthropic", None)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "provider_anthropic", _ANTHROPIC_PROVIDER_DIR / "provider.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provider_anthropic"] = mod
    spec.loader.exec_module(mod)
    return mod.AnthropicProvider


class _AzureFoundryAnthropicProvider:
    """Azure Foundry over Anthropic /v1/messages wire shape.

    Wraps an AnthropicProvider rather than subclassing — the Anthropic SDK
    is imported lazily so non-Anthropic Azure users don't pay the import
    cost. All BaseProvider methods are delegated to the inner instance.
    """

    name = "azure-foundry"
    default_model = "claude-3-5-sonnet-20241022"
    _api_mode = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("AZURE_FOUNDRY_API_KEY")
        base = base_url or os.environ.get("AZURE_FOUNDRY_BASE_URL")
        if not key or not base:
            _resolve_required_env()
        AnthropicProviderCls = _build_anthropic_subclass()
        self._inner = AnthropicProviderCls(
            api_key=key,
            base_url=base,
            auth_mode="bearer",  # Azure expects Bearer; native Anthropic uses x-api-key
        )
        self._api_key = self._inner._api_key
        self._base = base

    def __getattr__(self, name):
        # Delegate any BaseProvider method (complete, stream_complete, etc.)
        return getattr(self._inner, name)
