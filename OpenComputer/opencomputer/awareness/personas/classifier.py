"""PersonaClassifier — heuristic mapping from context to persona id.

Reads (foreground_app, time_of_day, recent_files, last_3_messages) and
returns one of the registered persona ids. Heuristic-based for V2.C — V2.D
may swap in an LLM-based classifier.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    foreground_app: str = ""
    time_of_day_hour: int = 12
    recent_file_paths: tuple[str, ...] = ()
    last_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    persona_id: str
    confidence: float
    reason: str


_CODING_APPS = ("code", "cursor", "pycharm", "iterm", "terminal", "warp", "neovim")
_TRADING_APPS = ("zerodha", "groww", "kite", "tradingview", "screener", "marketsmojo")
_RELAXED_APPS = ("animepahe", "youtube", "spotify", "netflix", "reddit", "instagram")


def classify(ctx: ClassificationContext) -> ClassificationResult:
    app_lower = ctx.foreground_app.lower()
    if any(a in app_lower for a in _CODING_APPS):
        return ClassificationResult("coding", 0.85, f"foreground app '{ctx.foreground_app}' suggests coding")
    if any(a in app_lower for a in _TRADING_APPS):
        return ClassificationResult("trading", 0.85, f"foreground app '{ctx.foreground_app}' suggests trading")
    if any(a in app_lower for a in _RELAXED_APPS):
        return ClassificationResult("relaxed", 0.8, f"foreground app '{ctx.foreground_app}' suggests relaxed mode")

    # File-based fallback
    py_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".py"))
    md_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".md"))
    if py_files >= 3:
        return ClassificationResult("coding", 0.7, f"{py_files} recent .py files")
    if md_files >= 3:
        return ClassificationResult("learning", 0.6, f"{md_files} recent .md files")

    # Time-of-day fallback
    if 21 <= ctx.time_of_day_hour or ctx.time_of_day_hour < 6:
        return ClassificationResult("relaxed", 0.5, f"hour={ctx.time_of_day_hour} (evening/late)")
    if 9 <= ctx.time_of_day_hour < 12:
        return ClassificationResult("coding", 0.4, "morning hours, default to coding")

    return ClassificationResult("admin", 0.3, "no strong signal — default admin")
