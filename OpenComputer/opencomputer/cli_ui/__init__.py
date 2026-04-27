"""Terminal UI helpers (Round 5 — Grok-style chat experience + Phase 1
TUI uplift: PromptSession-based input layer, slash dispatch, cancel scope).

Kept under ``cli_ui/`` (not ``cli/``) to avoid colliding with ``cli.py``.
"""

from opencomputer.cli_ui.streaming import StreamingRenderer, current_renderer
from opencomputer.cli_ui.turn_cancel import TurnCancelScope

__all__ = [
    "StreamingRenderer",
    "TurnCancelScope",
    "current_renderer",
]
