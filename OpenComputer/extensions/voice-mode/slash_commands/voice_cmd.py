"""``/voice [on|off|tts|join|leave|status]`` — voice-mode controls.

Best-effort runtime toggles for the voice-mode plugin. The actual audio
loop lives in :mod:`extensions.voice_mode.voice_mode`; this slash
command sets a small set of runtime flags (under ``runtime.custom``)
that the loop / TTS path can read.

Subcommands:
    on        — enable voice-mode for this session
    off       — disable voice-mode for this session
    tts       — toggle TTS playback only (no STT)
    join      — placeholder for joining a voice call (not implemented)
    leave     — placeholder for leaving a voice call (not implemented)
    status    — show current state (default when no arg)

Crashes are intentionally swallowed — a slash command must never
break the chat loop.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class VoiceCommand(SlashCommand):
    name = "voice"
    description = "Voice-mode controls (on/off/tts/join/leave/status)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower() or "status"
        # Custom dict is provided by RuntimeContext — defensively coerce.
        custom = runtime.custom if runtime.custom is not None else {}

        if sub == "status":
            return SlashCommandResult(
                output=_format_status(custom), handled=True,
            )

        if sub == "on":
            custom["voice_mode_enabled"] = True
            # runtime.custom is a dict on a frozen dataclass — mutate in place.
            return SlashCommandResult(
                output="voice mode: on (use `/voice off` to disable)",
                handled=True,
            )

        if sub == "off":
            custom["voice_mode_enabled"] = False
            # runtime.custom is a dict on a frozen dataclass — mutate in place.
            return SlashCommandResult(
                output="voice mode: off",
                handled=True,
            )

        if sub == "tts":
            current = bool(custom.get("voice_tts_enabled", False))
            custom["voice_tts_enabled"] = not current
            # runtime.custom is a dict on a frozen dataclass — mutate in place.
            return SlashCommandResult(
                output=(
                    f"voice TTS: {'on' if not current else 'off'} "
                    "(audio replies)"
                ),
                handled=True,
            )

        if sub in ("join", "leave"):
            # Placeholder hooks — the realtime-voice plugin owns the
            # actual call lifecycle. We just report that this surface is
            # unimplemented so users get a clear signal rather than a
            # silent no-op.
            verb = "join" if sub == "join" else "leave"
            return SlashCommandResult(
                output=(
                    f"voice {verb}: not implemented in this build "
                    "— see `oc voice talk` for the standalone loop"
                ),
                handled=True,
            )

        return SlashCommandResult(
            output="usage: /voice [on|off|tts|join|leave|status]",
            handled=True,
        )


def _format_status(custom: dict) -> str:
    enabled = bool(custom.get("voice_mode_enabled", False))
    tts = bool(custom.get("voice_tts_enabled", False))
    return (
        "## Voice mode\n"
        f"  enabled: {'yes' if enabled else 'no'}\n"
        f"  tts:     {'yes' if tts else 'no'}"
    )


__all__ = ["VoiceCommand"]
