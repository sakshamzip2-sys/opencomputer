"""MiniMax (Anthropic-shaped) provider — thin subclass of AnthropicProvider.

MiniMax exposes an Anthropic Messages-compatible endpoint for some of
their models. Hermes uses transport=anthropic_messages with
base_url=https://api.minimax.io/v1/messages — same shape as Claude.

Pattern mirrors extensions/openrouter-provider but routes through the
bundled anthropic-provider rather than openai-provider.

Env vars:
  MINIMAX_API_KEY    — required; key from https://www.minimax.io
  MINIMAX_BASE_URL   — optional override (default: https://api.minimax.io/v1)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Bundled anthropic-provider lives at ../anthropic-provider. We can't
# import it via ``sys.path.insert`` + ``from provider import …`` because
# the plugin loader already has *this* file registered as the module
# named ``provider`` in ``sys.modules``, so the bare import resolves
# back to ourselves (circular-import error). Load it under a unique
# synthetic name AND register in ``sys.modules`` BEFORE exec — pydantic
# resolves forward refs (e.g. ``Literal[...]``) via
# ``sys.modules.get(cls.__module__)`` and explodes if the entry is
# absent.
#
# Backwards-compat with the existing test suite (test_mb_anthropic_shaped_providers)
# which pre-populates sys.modules['provider'] with the anthropic-provider
# source: if that fixture is in place, just reuse it so the test's
# tightly-controlled module state survives.
_ANTHROPIC_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "anthropic-provider"
_TEST_SHIM = sys.modules.get("provider")
if _TEST_SHIM is not None and getattr(_TEST_SHIM, "AnthropicProvider", None) is not None:
    _anthropic_module = _TEST_SHIM
else:
    _SYNTHETIC = "_minimax_upstream_anthropic_provider"
    if _SYNTHETIC in sys.modules:
        _anthropic_module = sys.modules[_SYNTHETIC]
    else:
        _spec = importlib.util.spec_from_file_location(
            _SYNTHETIC, _ANTHROPIC_PROVIDER_DIR / "provider.py"
        )
        if _spec is None or _spec.loader is None:
            raise ImportError(
                f"Cannot locate anthropic-provider at {_ANTHROPIC_PROVIDER_DIR}"
            )
        _anthropic_module = importlib.util.module_from_spec(_spec)
        sys.modules[_SYNTHETIC] = _anthropic_module
        _spec.loader.exec_module(_anthropic_module)
AnthropicProvider = _anthropic_module.AnthropicProvider

DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"


class MiniMaxAnthropicProvider(AnthropicProvider):
    """OpenAI-compatible base URL is wrong for MiniMax — use Anthropic.

    MiniMax accepts Anthropic Messages API requests when posted to
    /v1/messages. Re-using AnthropicProvider gives streaming + tool-use
    + cache_control + every other Claude-shaped feature for free.
    """

    name = "minimax"
    default_model = "MiniMax-M1"
    _api_key_env: str = "MINIMAX_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        # Pre-check the env var so the error message names MINIMAX, not
        # ANTHROPIC (the parent's RuntimeError mentions ANTHROPIC_API_KEY).
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a key at https://www.minimax.io."
            )
        # Resolve base URL with our own env precedence so the parent
        # doesn't pull from ANTHROPIC_BASE_URL by accident.
        resolved_base = (
            base_url
            or os.environ.get("MINIMAX_BASE_URL")
            or DEFAULT_MINIMAX_BASE_URL
        )
        # Force bearer auth — MiniMax expects Authorization: Bearer, not
        # x-api-key.
        super().__init__(
            api_key=api_key or os.environ.get(self._api_key_env),
            base_url=resolved_base,
            auth_mode=auth_mode or "bearer",
        )
