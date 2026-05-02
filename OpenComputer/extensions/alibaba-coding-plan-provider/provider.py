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

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

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
