"""Provider-agnostic auxiliary LLM helpers.

Auxiliary code paths (the /btw slash command, vision_analyze tool, batch
processing, profile bootstrap LLM extractor) historically called
Anthropic directly via :mod:`opencomputer.agent.anthropic_client`. That
broke the model-agnosticism contract: a user with only an OpenAI / Groq /
Ollama key got working chat but no /btw, no vision, no profile bootstrap.

This module routes those auxiliary calls through whatever provider the
user configured (``config.model.provider``), the same way the chat loop
+ ``recall_synthesizer`` + ``title_generator`` do.

Two entry points:

  - :func:`complete_text` — plain text in, plain text out
  - :func:`complete_vision` — text + image, returns text (raises if
    the configured provider doesn't expose vision via its
    ``complete()`` API; the caller decides whether to fall back)

Both inherit the user's auth + base URL config because the configured
provider plugin already knows them.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _resolve_provider() -> Any:
    """Resolve the user's configured provider plugin instance.

    Mirrors :func:`opencomputer.agent.title_generator._resolve_cheap_provider`
    so all auxiliary paths inherit the same auth + base URL config without
    new setup.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = default_config()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; cannot run auxiliary call"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def _resolve_default_model() -> str:
    """Pick a sensible model name to send to the configured provider.

    Auxiliary calls don't dictate the model — they piggyback on whichever
    model the user already runs chat on. Provider-side logic decides
    whether the model accepts the call (e.g. Ollama provider routes any
    model name to its local daemon; OpenAI rejects unknown names).
    """
    from opencomputer.agent.config import default_config

    cfg = default_config()
    return cfg.model.name


async def complete_text(
    *,
    messages: list[dict[str, Any]],
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 1.0,
    model: str | None = None,
) -> str:
    """Run a single text completion through the configured provider.

    ``messages`` is a list of dicts with ``role`` and ``content`` keys
    (the plain Anthropic / OpenAI shape). Returns the assistant text.

    Raises ``RuntimeError`` if no provider is configured. Surfaces the
    underlying provider error otherwise — callers should wrap in their
    own try/except if they want to convert SDK errors into user-facing
    text.
    """
    from plugin_sdk.core import Message

    provider = _resolve_provider()
    sdk_messages = [
        Message(role=m["role"], content=m.get("content"))
        for m in messages
    ]
    resolved_model = model or _resolve_default_model()
    resp = await provider.complete(
        model=resolved_model,
        messages=sdk_messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.message.content if resp and resp.message else ""


def complete_text_sync(
    *,
    messages: list[dict[str, Any]],
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 1.0,
    model: str | None = None,
) -> str:
    """Sync wrapper for :func:`complete_text`.

    Use this from sync code paths (profile bootstrap, batch fallback) that
    don't already have an event loop. Internally drives the async call
    with ``asyncio.run`` — same pattern as ``title_generator.call_llm``.
    """
    return asyncio.run(
        complete_text(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
        )
    )


async def complete_vision(
    *,
    image_base64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 1024,
    model: str | None = None,
) -> str:
    """Run a vision completion (text + image) through the configured provider.

    Both Anthropic and OpenAI use the multimodal-content-array shape
    where each item is either ``{"type": "text", "text": "..."}`` or
    ``{"type": "image", "source": {"type": "base64", "media_type": "...",
    "data": "..."}}``. We pass that shape verbatim — providers whose
    ``complete()`` understands it (anthropic, openai-compat with vision
    models, gemini) handle the call; providers without vision raise.

    Caller should catch the resulting error and surface a clean
    "vision not available on <provider>" message rather than a stack trace.
    """
    from plugin_sdk.core import Message

    provider = _resolve_provider()
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": image_base64,
            },
        },
        {"type": "text", "text": prompt},
    ]
    resolved_model = model or _resolve_default_model()
    resp = await provider.complete(
        model=resolved_model,
        messages=[Message(role="user", content=content)],
        max_tokens=max_tokens,
    )
    return resp.message.content if resp and resp.message else ""


async def complete_video(
    *,
    video_base64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 1024,
    model: str | None = None,
) -> str:
    """Run a video completion (text + base64 video) through the configured provider.

    Wave 5 T7 — Hermes-port (c9a3f36f5). Uses the OpenRouter / Gemini-style
    multimodal-content-array shape with a ``video_url`` block whose URL
    is a ``data:<mime>;base64,<b64>`` URI. Providers that can't decode
    a video block raise; the caller (``video_analyze`` tool) catches
    and surfaces a clean error.
    """
    from plugin_sdk.core import Message

    provider = _resolve_provider()
    data_url = f"data:{mime_type};base64,{video_base64}"
    content = [
        {"type": "video_url", "video_url": {"url": data_url}},
        {"type": "text", "text": prompt},
    ]
    resolved_model = model or _resolve_default_model()
    resp = await provider.complete(
        model=resolved_model,
        messages=[Message(role="user", content=content)],
        max_tokens=max_tokens,
    )
    return resp.message.content if resp and resp.message else ""


__all__ = [
    "complete_text",
    "complete_text_sync",
    "complete_video",
    "complete_vision",
]
