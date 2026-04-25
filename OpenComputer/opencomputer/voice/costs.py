"""Pricing constants + projection helpers for voice operations.

Used by callers to pre-flight :func:`opencomputer.cost_guard.CostGuard.check_budget`
before invoking the actual API. Constants reflect OpenAI public list pricing
as of 2026-04 — bump the version constant when adjusting.

If pricing drifts, callers will under-/over-project but the cost-guard's
record_usage path uses the *actual* charged amount (extracted from the
OpenAI response when available, or these projected values as fallback),
so the daily/monthly counters stay accurate enough for budget enforcement.
"""

from __future__ import annotations

# OpenAI list pricing snapshot — bump on adjustment.
PRICING_VERSION = "2026-04"

# tts-1: $15 per 1M characters → $0.015 per 1k chars
# tts-1-hd: $30 per 1M characters → $0.030 per 1k chars
OPENAI_TTS_USD_PER_1K_CHARS: dict[str, float] = {
    "tts-1": 0.015,
    "tts-1-hd": 0.030,
}

# Whisper: $0.006 per minute (rounded to nearest second by OpenAI)
OPENAI_STT_USD_PER_MINUTE: dict[str, float] = {
    "whisper-1": 0.006,
}


def tts_cost_usd(text: str, model: str = "tts-1") -> float:
    """Project the USD cost of synthesizing ``text`` via ``model``.

    Returns 0.0 for empty text. Unknown models default to tts-1 pricing.
    """
    if not text:
        return 0.0
    rate = OPENAI_TTS_USD_PER_1K_CHARS.get(model, OPENAI_TTS_USD_PER_1K_CHARS["tts-1"])
    return (len(text) / 1000.0) * rate


def stt_cost_usd(audio_duration_s: float, model: str = "whisper-1") -> float:
    """Project the USD cost of transcribing ``audio_duration_s`` via ``model``.

    Whisper rounds to nearest second. Returns 0.0 for non-positive duration.
    """
    if audio_duration_s <= 0:
        return 0.0
    rate = OPENAI_STT_USD_PER_MINUTE.get(model, OPENAI_STT_USD_PER_MINUTE["whisper-1"])
    return (audio_duration_s / 60.0) * rate


__all__ = [
    "OPENAI_STT_USD_PER_MINUTE",
    "OPENAI_TTS_USD_PER_1K_CHARS",
    "PRICING_VERSION",
    "stt_cost_usd",
    "tts_cost_usd",
]
