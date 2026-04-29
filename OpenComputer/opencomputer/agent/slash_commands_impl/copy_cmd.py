"""``/copy <text>`` — copy text to system clipboard via OSC-52.

Tier 2.A.1 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

OSC-52 is a terminal escape sequence that asks the terminal emulator
to write to the system clipboard. Works over SSH/tmux without xclip
because the escape passes through the terminal stack to the user's
local terminal app (iTerm2, Terminal.app, Alacritty, kitty, etc.).

Sequence: ``ESC ] 52 ; c ; <base64-encoded-text> BEL``

Usage:
    /copy hello world          → clipboard now contains "hello world"
    /copy ""                   → reports usage (need text)

Future: ``/copy`` with no args could copy the last assistant response.
That requires session-state access from the slash dispatcher (which
currently only passes RuntimeContext, not the SessionDB or message
history). Deferred — when needed, plumb session_id through
RuntimeContext.custom and read SessionDB here.
"""

from __future__ import annotations

import base64
import sys

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

# OSC-52 byte limit varies per terminal (~4KB iTerm2, ~74KB tmux).
# 4KB is the safe default; over that we truncate with a notice rather
# than fail silently.
MAX_OSC52_BYTES = 4096


def _osc52_payload(text: str) -> str:
    """Build the OSC-52 escape sequence for `text`."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # Format: ESC ] 52 ; c ; <base64> BEL
    return f"\x1b]52;c;{encoded}\x07"


def _emit_osc52(text: str, *, stream=None) -> None:
    """Write the OSC-52 sequence directly to the terminal."""
    if stream is None:
        stream = sys.stdout
    stream.write(_osc52_payload(text))
    try:
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


class CopyCommand(SlashCommand):
    """Copy literal text after /copy to the system clipboard via OSC-52."""

    name = "copy"
    description = "Copy text to system clipboard via OSC-52 (works over SSH/tmux)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        text = args.strip()
        if not text:
            return SlashCommandResult(
                output=(
                    "Usage: /copy <text>\n"
                    "Copies the given text to your system clipboard via OSC-52. "
                    "Works over SSH and tmux because the escape sequence is "
                    "interpreted by your local terminal emulator."
                ),
                handled=True,
            )

        encoded_size = len(base64.b64encode(text.encode("utf-8")))
        truncated = False
        if encoded_size > MAX_OSC52_BYTES:
            # Truncate text so the encoded form fits the limit.
            # Each base64 char encodes 6 bits → 8/6 ratio in the other
            # direction. Cut original to ~3/4 of the limit to be safe.
            cutoff = (MAX_OSC52_BYTES * 3) // 4 - 16  # safety margin
            text = text[:cutoff]
            truncated = True

        _emit_osc52(text)

        msg = f"Copied {len(text)} chars to clipboard"
        if truncated:
            msg += (
                " (truncated to fit OSC-52 limit; not all terminals "
                "support the larger sizes)"
            )
        return SlashCommandResult(output=msg, handled=True)


__all__ = ["CopyCommand", "_osc52_payload"]
