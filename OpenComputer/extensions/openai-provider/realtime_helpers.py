"""Pure helpers for the OpenAI Realtime bridge.

Direct port of openclaw/extensions/openai/realtime-provider-shared.ts (commit 2026-04-23).
Kept separate from ``realtime.py`` so the bridge stays focused on the
WebSocket lifecycle and event dispatch.
"""
from __future__ import annotations

import math
from typing import Any


def as_finite_number(value: Any) -> float | None:
    """Return ``value`` as float iff it is a finite int/float; else None.

    Strings are rejected — config values reach here as already-parsed
    numbers (Pydantic does the str→float conversion upstream).
    """
    if isinstance(value, bool):
        # bool is a subclass of int — exclude explicitly to avoid surprises
        return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def trim_or_none(value: Any) -> str | None:
    """Strip a string; return None if it ends up empty (or wasn't a string)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def read_realtime_error_detail(error: Any) -> str:
    """Best-effort extraction of a human-readable error message.

    Mirrors the TS helper which reads ``error.message`` first, falls
    back to ``error.type``, then stringifies. ``None`` returns a stable
    fallback so callers don't have to special-case it.
    """
    if error is None:
        return "unknown realtime error"
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
        typ = error.get("type")
        if isinstance(typ, str) and typ:
            return typ
    return str(error)


__all__ = ["as_finite_number", "read_realtime_error_detail", "trim_or_none"]
