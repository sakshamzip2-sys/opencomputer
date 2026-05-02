"""Alibaba Coding Plan provider — thin subclass of OpenAIProvider.

Verified against Hermes auth.py:
  inference_base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
  api_key_env_vars=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),

Falls back to DASHSCOPE_API_KEY if ALIBABA_CODING_PLAN_API_KEY isn't set
(matches Hermes's behavior — they share the same key).

Env vars:
  ALIBABA_CODING_PLAN_API_KEY (or DASHSCOPE_API_KEY)
  ALIBABA_CODING_PLAN_BASE_URL — optional override
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
    "_oai_base_for_alibaba_coding_plan", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_ALIBABA_CODING_PLAN_BASE_URL = "https://coding-intl.dashscope.aliyuncs.com/v1"


class AlibabaCodingPlanProvider(OpenAIProvider):
    name = "alibaba-coding-plan"
    default_model = "qwen3-coder-plus"
    # Primary env var; the parent's env-lookup uses this. Fallback to
    # DASHSCOPE_API_KEY is handled in __init__ below.
    _api_key_env: str = "ALIBABA_CODING_PLAN_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            api_key = (
                os.environ.get("ALIBABA_CODING_PLAN_API_KEY")
                or os.environ.get("DASHSCOPE_API_KEY")
            )
        if not api_key:
            raise RuntimeError(
                "ALIBABA_CODING_PLAN_API_KEY (or DASHSCOPE_API_KEY) is not set. "
                "Get a key at https://dashscope.aliyun.com/coding-plan."
            )
        resolved_base = (
            base_url
            or os.environ.get("ALIBABA_CODING_PLAN_BASE_URL")
            or DEFAULT_ALIBABA_CODING_PLAN_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
