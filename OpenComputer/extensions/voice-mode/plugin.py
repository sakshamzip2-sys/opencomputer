"""Voice-mode plugin — continuous push-to-talk audio loop."""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.voice_mode.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry; CLI command does the work + register /voice slash."""
    _log.debug("voice-mode plugin registered (use `opencomputer voice talk` to start)")

    # Register /voice slash command — messaging-gateway parity (PR-2
    # Task B7). Loaded directly by file path because the plugin loader
    # synthesizes a unique per-file module name and the slash_commands/
    # subdir is not on sys.path until activation.
    if hasattr(api, "register_slash_command"):
        try:
            from slash_commands.voice_cmd import VoiceCommand  # type: ignore[import-not-found]

            api.register_slash_command(VoiceCommand())
        except Exception as exc:  # noqa: BLE001 — never break voice-mode load
            _log.warning("/voice slash command registration failed: %s", exc)
