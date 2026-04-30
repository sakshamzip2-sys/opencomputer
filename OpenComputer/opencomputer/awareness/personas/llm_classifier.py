"""LLM-based persona classifier — Tier 3 of the v2 pipeline.

Calls a fast model (e.g. ``claude-haiku-4-5``) once per session to
classify the user's persona based on the first user message and the
current foreground context. Result is cached in-process for the
session lifetime so subsequent turns are zero-latency.

Falls back gracefully:
- No API key → returns None (regex pipeline takes over)
- LLM call timeout / error → returns None
- Confidence < 0.7 → returns None (don't override regex on weak signal)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass

from opencomputer.awareness.personas.classifier import ClassificationResult

_LLM_TIMEOUT_S = 4.0
_VALID_PERSONAS = ("trading", "coding", "companion", "learning", "relaxed", "admin")

# Per-session cache: session_id → ClassificationResult.
_session_cache: dict[str, ClassificationResult | None] = {}


@dataclass(frozen=True, slots=True)
class _LLMClassifierConfig:
    model: str = "claude-haiku-4-5"
    max_tokens: int = 80
    timeout_s: float = _LLM_TIMEOUT_S


def _build_prompt(
    *,
    foreground_app: str,
    window_title: str,
    last_messages: tuple[str, ...],
) -> str:
    """Build a tight classification prompt. Output: persona id + confidence."""
    msgs = list(last_messages[-3:])
    msg_block = "\n".join(f"  - {m[:200]}" for m in msgs if m) or "  (none)"
    return (
        "Classify the user's current activity into ONE of these personas:\n"
        "  trading   — discussing stocks, options, markets, portfolio\n"
        "  coding    — writing/reading code, debugging, devops\n"
        "  companion — casual chat, emotional support, hello/how-are-you\n"
        "  learning  — asking questions, reading docs, studying\n"
        "  relaxed   — entertainment, music, gaming, leisure\n"
        "  admin    — task management, scheduling, no clear domain\n"
        "\n"
        f"Foreground app: {foreground_app or '(unknown)'}\n"
        f"Window title:   {window_title or '(unknown)'}\n"
        f"Recent messages:\n{msg_block}\n"
        "\n"
        "Respond with ONLY a single JSON object on one line:\n"
        '  {"persona": "<id>", "confidence": <0.0-1.0>, "why": "<short>"}'
    )


def _parse_response(raw: str) -> ClassificationResult | None:
    """Parse the model's JSON response into a ClassificationResult."""
    if not raw:
        return None
    # Try to find a JSON object — model might wrap in markdown.
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    persona = str(data.get("persona", "")).strip().lower()
    if persona not in _VALID_PERSONAS:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    why = str(data.get("why", ""))[:120]
    return ClassificationResult(persona, confidence, f"LLM: {why}")


async def llm_classify_async(
    *,
    session_id: str,
    foreground_app: str,
    window_title: str,
    last_messages: tuple[str, ...],
    config: _LLMClassifierConfig | None = None,
) -> ClassificationResult | None:
    """Async LLM classify. Cached per session.

    Returns None if no API key, no messages, or timeout/error. Caller
    should fall back to the regex multi-signal pipeline on None.
    """
    if not last_messages or not any(last_messages):
        return None
    if session_id in _session_cache:
        return _session_cache[session_id]
    if not (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    ):
        _session_cache[session_id] = None
        return None

    config = config or _LLMClassifierConfig()
    prompt = _build_prompt(
        foreground_app=foreground_app,
        window_title=window_title,
        last_messages=last_messages,
    )

    try:
        result = await asyncio.wait_for(
            _call_llm(prompt, config),
            timeout=config.timeout_s,
        )
    except (TimeoutError, Exception):  # noqa: BLE001
        _session_cache[session_id] = None
        return None

    if result is None or result.confidence < 0.7:
        _session_cache[session_id] = None
        return None

    _session_cache[session_id] = result
    return result


async def _call_llm(
    prompt: str, config: _LLMClassifierConfig,
) -> ClassificationResult | None:
    """Anthropic-first; OpenAI fallback. Best-effort; any error → None."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-untyped]
            client = AsyncAnthropic()
            resp = await client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in resp.content if hasattr(block, "text")
            )
            return _parse_response(text)
        except Exception:  # noqa: BLE001
            return None

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import AsyncOpenAI  # type: ignore[import-untyped]
            client = AsyncOpenAI()
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=config.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content or ""
            return _parse_response(text)
        except Exception:  # noqa: BLE001
            return None

    return None


def clear_cache(session_id: str | None = None) -> None:
    """Drop the per-session cache. Called on session end."""
    if session_id is None:
        _session_cache.clear()
    else:
        _session_cache.pop(session_id, None)


__all__ = ["llm_classify_async", "clear_cache", "_build_prompt", "_parse_response"]
