"""Voice-mode plugin — continuous push-to-talk audio loop."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

_log = logging.getLogger("opencomputer.voice_mode.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry; CLI command does the work + register /voice slash."""
    _log.debug("voice-mode plugin registered (use `opencomputer voice talk` to start)")

    if not hasattr(api, "register_slash_command"):
        return

    try:
        # File-path import bypasses sys.modules collisions when another
        # plugin's `slash_commands` package is cached. The plugin loader
        # clears `slash_commands` from sys.modules before each plugin
        # loads (loader.py:_clear_plugin_local_cache), but by the time
        # register() runs the cache may have been re-populated by
        # whichever plugin loaded most recently. coding-harness's
        # slash_commands/ has no voice_cmd.py, so a plain
        # `from slash_commands.voice_cmd import VoiceCommand` resolves
        # against that cached package and raises ModuleNotFoundError.
        voice_cmd_path = Path(__file__).resolve().parent / "slash_commands" / "voice_cmd.py"
        synthetic = "_voice_mode_voice_cmd"
        if synthetic in sys.modules:
            del sys.modules[synthetic]
        spec = importlib.util.spec_from_file_location(synthetic, voice_cmd_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no spec for {voice_cmd_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[synthetic] = mod
        spec.loader.exec_module(mod)
        api.register_slash_command(mod.VoiceCommand())
    except Exception as exc:  # noqa: BLE001 — never break voice-mode load
        _log.warning("/voice slash command registration failed: %s", exc)
