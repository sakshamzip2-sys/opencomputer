"""B2: /voice slash command must register from voice-mode/slash_commands/."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

VOICE_DIR = Path(__file__).resolve().parent.parent / "extensions" / "voice-mode"


def _load_voice_plugin():
    """Load extensions/voice-mode/plugin.py by file path (matches loader)."""
    name = "_test_voice_plugin"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, VOICE_DIR / "plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_voice_slash_registration_finds_voice_cmd_when_other_slash_commands_module_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even when another plugin's slash_commands package is cached in
    sys.modules (sibling-name collision), voice-mode must still find
    its own VoiceCommand."""
    # Simulate a different plugin having cached its slash_commands/ — without
    # voice_cmd in it. This is what coding-harness does in real life.
    fake_other = types.ModuleType("slash_commands")
    fake_other.__path__ = [str(tmp_path)]  # empty dir, no voice_cmd.py
    monkeypatch.setitem(sys.modules, "slash_commands", fake_other)
    # Also clean any prior synthetic voice_cmd module from other tests
    monkeypatch.delitem(sys.modules, "_voice_mode_voice_cmd", raising=False)

    plugin = _load_voice_plugin()
    api = MagicMock()
    api.slash_commands = {}

    plugin.register(api)

    api.register_slash_command.assert_called_once()
    cmd = api.register_slash_command.call_args[0][0]
    assert getattr(cmd, "name", None) == "voice"
