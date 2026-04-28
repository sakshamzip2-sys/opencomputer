"""Tests for Tier 2.B: bell-on-complete + external editor binding."""
import io

import pytest

from opencomputer.agent.slash_commands_impl.bell_cmd import BellCommand
from opencomputer.cli_ui.bell import maybe_emit_bell
from plugin_sdk.runtime_context import RuntimeContext


def _runtime(**custom) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom))


# ---------- maybe_emit_bell ----------


class _FakeTTY(io.StringIO):
    def __init__(self, is_tty: bool = True) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_bell_emits_when_flag_on_and_tty():
    rt = _runtime(bell_on_complete=True)
    stream = _FakeTTY(is_tty=True)
    emitted = maybe_emit_bell(rt, stream=stream)
    assert emitted is True
    assert stream.getvalue() == "\a"


def test_bell_no_emit_when_flag_off():
    rt = _runtime(bell_on_complete=False)
    stream = _FakeTTY(is_tty=True)
    assert maybe_emit_bell(rt, stream=stream) is False
    assert stream.getvalue() == ""


def test_bell_no_emit_when_flag_unset():
    rt = _runtime()
    stream = _FakeTTY(is_tty=True)
    assert maybe_emit_bell(rt, stream=stream) is False


def test_bell_no_emit_when_not_tty():
    """Don't pollute piped output with \\a control bytes."""
    rt = _runtime(bell_on_complete=True)
    stream = _FakeTTY(is_tty=False)
    assert maybe_emit_bell(rt, stream=stream) is False
    assert stream.getvalue() == ""


def test_bell_handles_stream_without_isatty():
    """Some IO streams don't expose isatty(); fail safely (no bell)."""
    rt = _runtime(bell_on_complete=True)
    class _NoTTY:
        def write(self, s):
            return len(s)
    stream = _NoTTY()
    # No isatty → fall back to default False → no bell
    assert maybe_emit_bell(rt, stream=stream) is False


def test_bell_handles_write_failure_gracefully():
    rt = _runtime(bell_on_complete=True)

    class _BoomStream:
        def isatty(self):
            return True

        def write(self, s):
            raise OSError("device gone")

    assert maybe_emit_bell(rt, stream=_BoomStream()) is False


# ---------- /bell slash command ----------


@pytest.mark.asyncio
async def test_bell_cmd_on():
    rt = _runtime()
    result = await BellCommand().execute("on", rt)
    assert "ON" in result.output
    assert rt.custom["bell_on_complete"] is True


@pytest.mark.asyncio
async def test_bell_cmd_off():
    rt = _runtime(bell_on_complete=True)
    result = await BellCommand().execute("off", rt)
    assert "OFF" in result.output
    assert rt.custom["bell_on_complete"] is False


@pytest.mark.asyncio
async def test_bell_cmd_toggle_no_arg():
    rt = _runtime()
    await BellCommand().execute("", rt)
    assert rt.custom["bell_on_complete"] is True
    await BellCommand().execute("", rt)
    assert rt.custom["bell_on_complete"] is False


@pytest.mark.asyncio
async def test_bell_cmd_status_no_mutate():
    rt = _runtime(bell_on_complete=True)
    result = await BellCommand().execute("status", rt)
    assert "ON" in result.output
    assert rt.custom["bell_on_complete"] is True


@pytest.mark.asyncio
async def test_bell_cmd_invalid_arg():
    rt = _runtime()
    result = await BellCommand().execute("loud", rt)
    assert "Usage" in result.output


def test_bell_cmd_metadata():
    cmd = BellCommand()
    assert cmd.name == "bell"
    assert "bell" in cmd.description.lower()


# ---------- external editor binding (smoke test) ----------


def test_external_editor_binding_present():
    """The Ctrl+X Ctrl+E key chord should register on the prompt session."""
    import tempfile
    from pathlib import Path

    from opencomputer.cli_ui.input_loop import build_prompt_session
    from opencomputer.cli_ui.turn_cancel import TurnCancelScope

    with tempfile.TemporaryDirectory() as tmpdir:
        scope = TurnCancelScope()
        session = build_prompt_session(
            profile_home=Path(tmpdir),
            scope=scope,
        )
        # Find the Ctrl+X Ctrl+E binding in the keybindings registry
        kb = session.key_bindings
        # prompt_toolkit's KeyBindings exposes its raw bindings via .bindings
        bindings = kb.bindings if hasattr(kb, "bindings") else []
        # Look for any binding whose key sequence is the (ControlX, ControlE)
        # chord — prompt_toolkit's enum stringifies as 'Keys.ControlX' so we
        # check the underlying enum value.
        from prompt_toolkit.keys import Keys

        found = any(
            tuple(getattr(b, "keys", ())) == (Keys.ControlX, Keys.ControlE)
            for b in bindings
        )
        assert found, (
            f"Ctrl+X Ctrl+E external-editor binding not found. "
            f"Got: {[(getattr(b, 'keys', ()),) for b in bindings]}"
        )
