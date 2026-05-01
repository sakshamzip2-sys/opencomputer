"""Pure helpers for the Gemini Realtime bridge.

Kept separate so ``realtime.py`` stays focused on the WebSocket lifecycle
and event dispatch (mirrors the openai-provider layout).
"""
from __future__ import annotations

from typing import Any


def vad_threshold_to_sensitivity(threshold: float) -> tuple[str, str]:
    """Map a 0.0-1.0 threshold (OpenAI-style) → (start_sens, end_sens) enums.

    Gemini's automaticActivityDetection accepts only TWO levels per
    boundary plus an UNSPECIFIED escape — there is NO ``MEDIUM`` value
    despite what the symmetric naming suggests. The proto enum is:

        START_SENSITIVITY_UNSPECIFIED | START_SENSITIVITY_HIGH | START_SENSITIVITY_LOW
        END_SENSITIVITY_UNSPECIFIED   | END_SENSITIVITY_HIGH   | END_SENSITIVITY_LOW

    Sending ``START_SENSITIVITY_MEDIUM`` makes Gemini close the
    WebSocket with frame 1007 ``invalid frame payload data — Invalid
    value at 'setup.realtime_input_config.automatic_activity_detection
    .start_of_speech_sensitivity'``.

    So we map a single threshold to a binary choice. Below 0.5 →
    HIGH start (eager: pick up speech fast) + LOW end (patient: don't
    cut off mid-pause) — the natural handsfree-voice default. ≥ 0.5 →
    LOW start + HIGH end (conservative: only fire on clear speech,
    declare-done quickly).
    """
    if threshold < 0.5:
        return "START_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW"
    return "START_SENSITIVITY_LOW", "END_SENSITIVITY_HIGH"


def read_realtime_error_detail(error: Any) -> str:
    """Best-effort extraction of a human-readable error message.

    Mirrors the openai-provider helper. Gemini surfaces errors at top
    level as ``{"error": {"code": ..., "message": ..., "status": ...}}``.
    """
    if error is None:
        return "unknown realtime error"
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
        status = error.get("status")
        if isinstance(status, str) and status:
            return status
    return str(error)


__all__ = [
    "read_realtime_error_detail",
    "vad_threshold_to_sensitivity",
]
