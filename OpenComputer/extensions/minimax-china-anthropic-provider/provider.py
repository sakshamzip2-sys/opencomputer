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

import importlib.util
import os
import sys
from pathlib import Path

# See extensions/minimax-anthropic-provider/provider.py for the full
# rationale — same load pattern (sys.modules registration before exec
# + test-shim reuse + per-load idempotence).
_ANTHROPIC_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "anthropic-provider"
_TEST_SHIM = sys.modules.get("provider")
if _TEST_SHIM is not None and getattr(_TEST_SHIM, "AnthropicProvider", None) is not None:
    _anthropic_module = _TEST_SHIM
else:
    _SYNTHETIC = "_minimax_cn_upstream_anthropic_provider"
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
