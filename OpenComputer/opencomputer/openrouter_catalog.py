"""Shared OpenRouter model catalog for setup, ``oc model``, and context bars."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


OPENROUTER_MODEL_IDS: list[str] = [
    "moonshotai/kimi-k2.6",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.6",
    "qwen/qwen3.6-plus",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-haiku-4.5",
    "openrouter/elephant-alpha",
    "openrouter/owl-alpha",
    "openai/gpt-5.5",
    "openai/gpt-5.4-mini",
    "xiaomi/mimo-v2.5-pro",
    "xiaomi/mimo-v2.5",
    "tencent/hy3-preview:free",
    "tencent/hy3-preview",
    "openai/gpt-5.3-codex",
    "google/gemini-3-pro-image-preview",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-flash-lite-preview",
    "qwen/qwen3.5-plus-02-15",
    "qwen/qwen3.5-35b-a3b",
    "stepfun/step-3.5-flash",
    "minimax/minimax-m2.7",
    "minimax/minimax-m2.5",
    "minimax/minimax-m2.5:free",
    "z-ai/glm-5.1",
    "z-ai/glm-5v-turbo",
    "z-ai/glm-5-turbo",
    "x-ai/grok-4.20",
    "x-ai/grok-4.3",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "arcee-ai/trinity-large-preview:free",
    "arcee-ai/trinity-large-thinking",
    "openai/gpt-5.5-pro",
    "openai/gpt-5.4-nano",
    "deepseek/deepseek-v4-pro",
]


OPENROUTER_CONTEXT_LENGTHS: dict[str, int] = {
    "moonshotai/kimi-k2.6": 262_144,
    "anthropic/claude-opus-4.7": 1_000_000,
    "anthropic/claude-opus-4.6": 1_000_000,
    "anthropic/claude-sonnet-4.6": 1_000_000,
    "qwen/qwen3.6-plus": 1_000_000,
    "anthropic/claude-sonnet-4.5": 1_000_000,
    "anthropic/claude-haiku-4.5": 200_000,
    "openrouter/owl-alpha": 1_048_756,
    "openai/gpt-5.5": 1_050_000,
    "openai/gpt-5.4-mini": 400_000,
    "xiaomi/mimo-v2.5-pro": 1_048_576,
    "xiaomi/mimo-v2.5": 1_048_576,
    "tencent/hy3-preview:free": 262_144,
    "tencent/hy3-preview": 262_144,
    "openai/gpt-5.3-codex": 400_000,
    "google/gemini-3-pro-image-preview": 65_536,
    "google/gemini-3-flash-preview": 1_048_576,
    "google/gemini-3.1-pro-preview": 1_048_576,
    "google/gemini-3.1-flash-lite-preview": 1_048_576,
    "qwen/qwen3.5-plus-02-15": 1_000_000,
    "qwen/qwen3.5-35b-a3b": 262_144,
    "stepfun/step-3.5-flash": 262_144,
    "minimax/minimax-m2.7": 196_608,
    "minimax/minimax-m2.5": 196_608,
    "minimax/minimax-m2.5:free": 196_608,
    "z-ai/glm-5.1": 202_752,
    "z-ai/glm-5v-turbo": 202_752,
    "z-ai/glm-5-turbo": 202_752,
    "x-ai/grok-4.20": 2_000_000,
    "x-ai/grok-4.3": 1_000_000,
    "nvidia/nemotron-3-super-120b-a12b": 262_144,
    "nvidia/nemotron-3-super-120b-a12b:free": 262_144,
    "arcee-ai/trinity-large-preview:free": 131_000,
    "arcee-ai/trinity-large-thinking": 262_144,
    "openai/gpt-5.5-pro": 1_050_000,
    "openai/gpt-5.4-nano": 400_000,
    "deepseek/deepseek-v4-pro": 1_048_576,
    "baidu/cobuddy:free": 131_072,
}


@dataclass(frozen=True, slots=True)
class OpenRouterModel:
    model_id: str
    context_length: int | None = None


def context_length_for_model(model_id: str) -> int | None:
    return OPENROUTER_CONTEXT_LENGTHS.get(model_id)


def display_model_ids() -> list[str]:
    return list(OPENROUTER_MODEL_IDS)


def setup_model_ids(current_model: str = "") -> list[str]:
    models = display_model_ids()
    current = (current_model or "").strip()
    if current and "/" in current and not current.lower().startswith("google/"):
        models = [m for m in models if m != current]
        models.insert(0, current)
    return models


def cache_context_length(model_id: str, context_length: int | None) -> None:
    if not context_length or context_length <= 0:
        return
    try:
        from opencomputer.agent.context_window_probe import cache_context_window

        cache_context_window(model_id, context_length, provider_hint="openrouter")
        cache_context_window(model_id, context_length, provider_hint="any")
    except Exception:
        pass


def cache_static_context_lengths() -> None:
    for model_id, context_length in OPENROUTER_CONTEXT_LENGTHS.items():
        cache_context_length(model_id, context_length)


def _coerce_context_length(item: dict[str, Any]) -> int | None:
    for key in ("context_length",):
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    top_provider = item.get("top_provider")
    if isinstance(top_provider, dict):
        value = top_provider.get("context_length")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def fetch_openrouter_models(
    *,
    api_key: str = "",
    base_url: str = "https://openrouter.ai/api/v1",
    limit: int = 500,
) -> list[OpenRouterModel]:
    req = Request(
        f"{base_url.rstrip('/')}/models",
        headers={
            "Accept": "application/json",
            "User-Agent": "OpenComputer OpenRouter catalog",
        },
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urlopen(req, timeout=8) as resp:  # noqa: S310 - fixed HTTPS API URL.
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, TimeoutError):
        cache_static_context_lengths()
        return [
            OpenRouterModel(model_id, context_length_for_model(model_id))
            for model_id in display_model_ids()[:limit]
        ]

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        cache_static_context_lengths()
        return [
            OpenRouterModel(model_id, context_length_for_model(model_id))
            for model_id in display_model_ids()[:limit]
        ]

    out: list[OpenRouterModel] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        context_length = _coerce_context_length(item)
        cache_context_length(model_id, context_length)
        out.append(OpenRouterModel(model_id, context_length))
    cache_static_context_lengths()
    return out[:limit]


__all__ = [
    "OPENROUTER_CONTEXT_LENGTHS",
    "OPENROUTER_MODEL_IDS",
    "OpenRouterModel",
    "cache_context_length",
    "cache_static_context_lengths",
    "context_length_for_model",
    "display_model_ids",
    "fetch_openrouter_models",
    "setup_model_ids",
]
