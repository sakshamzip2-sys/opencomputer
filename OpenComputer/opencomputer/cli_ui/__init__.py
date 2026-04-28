"""Terminal UI helpers (Round 5 — Grok-style chat experience + Phase 1
TUI uplift: PromptSession-based input layer, slash dispatch, cancel scope).

Kept under ``cli_ui/`` (not ``cli/``) to avoid colliding with ``cli.py``.
"""

from opencomputer.cli_ui.input_loop import build_prompt_session, read_user_input
from opencomputer.cli_ui.keyboard_listener import KeyboardListener
from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    CommandDef,
    SlashResult,
    is_slash_command,
    resolve_command,
)
from opencomputer.cli_ui.slash_completer import (
    SlashCommandCompleter,
    longest_common_prefix,
)
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash
from opencomputer.cli_ui.streaming import StreamingRenderer, current_renderer
from opencomputer.cli_ui.turn_cancel import TurnCancelScope

__all__ = [
    "SLASH_REGISTRY",
    "CommandDef",
    "KeyboardListener",
    "SlashCommandCompleter",
    "SlashContext",
    "SlashResult",
    "StreamingRenderer",
    "TurnCancelScope",
    "build_prompt_session",
    "current_renderer",
    "dispatch_slash",
    "is_slash_command",
    "longest_common_prefix",
    "read_user_input",
    "resolve_command",
]
