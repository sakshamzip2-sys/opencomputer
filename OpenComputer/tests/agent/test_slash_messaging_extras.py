"""Tests for the messaging-gateway parity slash commands (PR-2 Task B7).

Covers six new slash commands wired into the SlashCommand-class dispatch
path so they fire from any surface (CLI / gateway / wire / ACP):

- ``/sethome`` (built-in) — write/list/clear ``home_channels.json``
- ``/voice`` (voice-mode plugin) — best-effort voice-mode toggle
- ``/approve`` (coding-harness plugin) — approve most-recent pending consent
- ``/deny`` (coding-harness plugin) — deny most-recent pending consent
- ``/status`` (built-in) — show platform/chat/session/model/queue summary
- ``/footer`` (built-in) — toggle ``display.runtime_footer.enabled``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from plugin_sdk.runtime_context import RuntimeContext

# ─── /sethome ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sethome_writes_home_channels_json(tmp_path, monkeypatch):
    from opencomputer.agent.slash_commands_impl.sethome_cmd import SethomeCommand

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    rt = RuntimeContext(custom={"profile_home": tmp_path / "default"})

    result = await SethomeCommand().execute("telegram 12345", rt)
    assert result.handled
    assert "telegram" in result.output

    home_path = tmp_path / "default" / "gateway" / "home_channels.json"
    assert home_path.exists()
    data = json.loads(home_path.read_text())
    assert data["telegram"] == "12345"


@pytest.mark.asyncio
async def test_sethome_list_shows_entries(tmp_path):
    from opencomputer.agent.slash_commands_impl.sethome_cmd import SethomeCommand

    home = tmp_path / "default"
    gw = home / "gateway"
    gw.mkdir(parents=True, exist_ok=True)
    (gw / "home_channels.json").write_text(
        json.dumps({"telegram": "111", "discord": "222"})
    )
    rt = RuntimeContext(custom={"profile_home": home})

    result = await SethomeCommand().execute("--list", rt)
    assert result.handled
    assert "telegram" in result.output and "111" in result.output
    assert "discord" in result.output and "222" in result.output


@pytest.mark.asyncio
async def test_sethome_clear_removes_entry(tmp_path):
    from opencomputer.agent.slash_commands_impl.sethome_cmd import SethomeCommand

    home = tmp_path / "default"
    gw = home / "gateway"
    gw.mkdir(parents=True, exist_ok=True)
    home_path = gw / "home_channels.json"
    home_path.write_text(json.dumps({"telegram": "111", "discord": "222"}))
    rt = RuntimeContext(custom={"profile_home": home})

    result = await SethomeCommand().execute("--clear telegram", rt)
    assert result.handled
    data = json.loads(home_path.read_text())
    assert "telegram" not in data
    assert "discord" in data


# ─── /voice ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_status_returns_current_state():
    # Plugin lives outside the importable namespace; load from path.
    voice_cmd_mod = _load_voice_cmd()
    rt = RuntimeContext(custom={})

    result = await voice_cmd_mod.VoiceCommand().execute("status", rt)
    assert result.handled
    assert "voice" in result.output.lower()


@pytest.mark.asyncio
async def test_voice_on_toggles_state():
    voice_cmd_mod = _load_voice_cmd()
    rt = RuntimeContext(custom={})

    result = await voice_cmd_mod.VoiceCommand().execute("on", rt)
    assert result.handled
    assert rt.custom.get("voice_mode_enabled") is True


# ─── /approve + /deny ────────────────────────────────────────────────


class _FakeGate:
    """Minimal gate stand-in exposing the API our slash commands expect."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, str]] = []
        self.resolutions: list[tuple[str, str, bool, bool]] = []

    def list_pending(self) -> list[tuple[str, str]]:
        return list(self._pending)

    def add_pending(self, session_id: str, capability_id: str) -> None:
        self._pending.append((session_id, capability_id))

    def resolve_pending(
        self, *, session_id: str, capability_id: str, decision: bool, persist: bool
    ) -> bool:
        # Mimic the real ConsentGate API.
        self.resolutions.append((session_id, capability_id, decision, persist))
        # Pop the matching pending entry (last match — most recent).
        for i in range(len(self._pending) - 1, -1, -1):
            if self._pending[i] == (session_id, capability_id):
                self._pending.pop(i)
                return True
        return False


@pytest.mark.asyncio
async def test_approve_no_pending_returns_message():
    approve_mod = _load_coding_harness_cmd("approve_cmd")
    gate = _FakeGate()
    rt = RuntimeContext(custom={"consent_gate": gate})

    result = await approve_mod.ApproveCommand(harness_ctx=None).execute("", rt)
    assert result.handled
    assert "no pending" in result.output.lower()


@pytest.mark.asyncio
async def test_approve_with_pending_resolves():
    approve_mod = _load_coding_harness_cmd("approve_cmd")
    gate = _FakeGate()
    gate.add_pending("s1", "read_files.metadata")
    rt = RuntimeContext(custom={"consent_gate": gate})

    result = await approve_mod.ApproveCommand(harness_ctx=None).execute("", rt)
    assert result.handled
    assert "approved" in result.output.lower()
    assert gate.resolutions == [("s1", "read_files.metadata", True, False)]
    assert gate.list_pending() == []


@pytest.mark.asyncio
async def test_deny_no_pending_returns_message():
    deny_mod = _load_coding_harness_cmd("deny_cmd")
    gate = _FakeGate()
    rt = RuntimeContext(custom={"consent_gate": gate})

    result = await deny_mod.DenyCommand(harness_ctx=None).execute("", rt)
    assert result.handled
    assert "no pending" in result.output.lower()


@pytest.mark.asyncio
async def test_deny_with_pending_rejects():
    deny_mod = _load_coding_harness_cmd("deny_cmd")
    gate = _FakeGate()
    gate.add_pending("s1", "system_control.shell")
    rt = RuntimeContext(custom={"consent_gate": gate})

    result = await deny_mod.DenyCommand(harness_ctx=None).execute("", rt)
    assert result.handled
    assert "denied" in result.output.lower()
    assert gate.resolutions == [("s1", "system_control.shell", False, False)]
    assert gate.list_pending() == []


# ─── /status ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_session_info():
    from opencomputer.agent.slash_commands_impl.status_cmd import StatusCommand

    rt = RuntimeContext(
        custom={
            "session_id": "abc12345",
            "platform": "telegram",
            "chat_id": "999",
            "model": "claude-opus-4-7",
        }
    )
    result = await StatusCommand().execute("", rt)
    assert result.handled
    assert "abc12345" in result.output
    assert "telegram" in result.output


@pytest.mark.asyncio
async def test_status_with_no_runtime_defaults():
    from opencomputer.agent.slash_commands_impl.status_cmd import StatusCommand

    rt = RuntimeContext(custom={})
    result = await StatusCommand().execute("", rt)
    assert result.handled
    # No crash; some marker for unknown values present.
    assert ("(none)" in result.output) or ("unknown" in result.output.lower())


# ─── /footer ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_footer_status_returns_current_setting(tmp_path):
    from opencomputer.agent.slash_commands_impl.footer_cmd import FooterCommand

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "display:\n  runtime_footer:\n    enabled: false\n"
    )
    rt = RuntimeContext(custom={"profile_home": tmp_path})

    result = await FooterCommand().execute("status", rt)
    assert result.handled
    assert "footer" in result.output.lower()
    assert "off" in result.output.lower() or "disabled" in result.output.lower()


@pytest.mark.asyncio
async def test_footer_on_persists_to_config(tmp_path):
    from opencomputer.agent.slash_commands_impl.footer_cmd import FooterCommand

    rt = RuntimeContext(custom={"profile_home": tmp_path})

    result = await FooterCommand().execute("on", rt)
    assert result.handled
    cfg_path = tmp_path / "config.yaml"
    assert cfg_path.exists()
    text = cfg_path.read_text()
    assert "runtime_footer" in text
    assert "true" in text.lower()


# ─── Registry presence (CommandDef entries) ──────────────────────────


def test_command_defs_registered():
    from opencomputer.cli_ui.slash import resolve_command

    for name in ("sethome", "voice", "approve", "deny", "status"):
        assert resolve_command(name) is not None, f"/{name} missing from registry"


# ─── helpers — load plugin-side modules without full plugin loading ──


def _load_voice_cmd():
    """Load extensions/voice-mode/slash_commands/voice_cmd.py by file path.

    Plugins live outside the normal Python package surface; the loader
    synthesizes unique module names at runtime. For tests we just import
    the file directly via importlib so we exercise the SlashCommand
    subclass without standing up a full plugin lifecycle.
    """
    return _load_module(
        Path(__file__).parents[2]
        / "extensions"
        / "voice-mode"
        / "slash_commands"
        / "voice_cmd.py",
        "test_voice_mode_voice_cmd",
    )


def _load_coding_harness_cmd(name: str):
    return _load_module(
        Path(__file__).parents[2]
        / "extensions"
        / "coding-harness"
        / "slash_commands"
        / f"{name}.py",
        f"test_coding_harness_{name}",
    )


def _load_module(path: Path, mod_name: str):
    """Load a plugin-side module file by path, no package machinery needed.

    Plugin commands here import only from ``plugin_sdk`` so we can side-
    step the production plugin loader (which synthesizes per-plugin
    sys.path entries). Tests run faster this way and stay independent of
    plugin-discovery state.
    """
    import importlib.util

    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
