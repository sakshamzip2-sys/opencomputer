"""BaseRealtimeVoiceBridge ABC + the dataclasses around it.

Direct Python port of openclaw/src/realtime-voice/provider-types.ts.
"""
from __future__ import annotations

import inspect

import pytest


def test_realtime_voice_tool_dataclass_shape() -> None:
    from plugin_sdk.realtime_voice import RealtimeVoiceTool

    tool = RealtimeVoiceTool(
        type="function",
        name="Bash",
        description="Run a shell command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
    assert tool.type == "function"
    assert tool.name == "Bash"
    assert tool.parameters["type"] == "object"
    # Frozen — mutation must raise
    with pytest.raises(Exception):
        tool.name = "WriteFile"  # type: ignore[misc]


def test_realtime_voice_tool_call_event_shape() -> None:
    from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent

    ev = RealtimeVoiceToolCallEvent(
        item_id="item_42",
        call_id="call_xyz",
        name="Bash",
        args={"command": "ls"},
    )
    assert ev.item_id == "item_42"
    assert ev.call_id == "call_xyz"
    assert ev.args == {"command": "ls"}


def test_close_reason_literal_accepted_values() -> None:
    """RealtimeVoiceCloseReason must accept 'completed' and 'error'."""
    from plugin_sdk.realtime_voice import RealtimeVoiceCloseReason

    a: RealtimeVoiceCloseReason = "completed"
    b: RealtimeVoiceCloseReason = "error"
    assert a == "completed"
    assert b == "error"


def test_base_realtime_voice_bridge_is_abc() -> None:
    """BaseRealtimeVoiceBridge cannot be instantiated directly."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    with pytest.raises(TypeError):
        BaseRealtimeVoiceBridge()  # type: ignore[abstract]


def test_base_bridge_required_methods() -> None:
    """Mirror the openclaw RealtimeVoiceBridge interface — these abstract
    methods MUST exist or plugin-side ports will silently mis-implement."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    required_abstract = {
        "connect",
        "send_audio",
        "send_user_message",
        "submit_tool_result",
        "trigger_greeting",
        "close",
        "is_connected",
    }
    abstracts = set(BaseRealtimeVoiceBridge.__abstractmethods__)
    missing = required_abstract - abstracts
    assert not missing, f"missing abstract methods: {missing}"


def test_base_bridge_connect_is_async() -> None:
    """connect() must be a coroutine — bridges talk to the network."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    assert inspect.iscoroutinefunction(BaseRealtimeVoiceBridge.connect)


def test_public_exports_in_init() -> None:
    """__init__ must surface the new types so plugins can import them
    via `from plugin_sdk import BaseRealtimeVoiceBridge` etc."""
    import plugin_sdk

    for name in (
        "BaseRealtimeVoiceBridge",
        "RealtimeVoiceTool",
        "RealtimeVoiceToolCallEvent",
        "RealtimeVoiceCloseReason",
    ):
        assert hasattr(plugin_sdk, name), f"plugin_sdk.{name} not exported"
