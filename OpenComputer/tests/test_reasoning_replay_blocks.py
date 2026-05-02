"""Message + ProviderResponse can carry verbatim provider-side reasoning blocks."""

from plugin_sdk import Message, ProviderResponse, Usage


def test_message_default_no_replay_blocks():
    m = Message(role="assistant", content="hi")
    assert m.reasoning_replay_blocks is None


def test_message_can_carry_replay_blocks():
    blocks = [{"type": "thinking", "thinking": "let me think", "signature": "abc..."}]
    m = Message(role="assistant", content="", reasoning_replay_blocks=blocks)
    assert m.reasoning_replay_blocks == blocks


def test_provider_response_default_no_replay_blocks():
    r = ProviderResponse(
        message=Message(role="assistant", content="ok"),
        stop_reason="end_turn",
        usage=Usage(),
    )
    assert r.reasoning_replay_blocks is None


def test_provider_response_can_carry_replay_blocks():
    blocks = [{"type": "thinking", "thinking": "...", "signature": "sig"}]
    r = ProviderResponse(
        message=Message(role="assistant", content=""),
        stop_reason="tool_use",
        usage=Usage(),
        reasoning_replay_blocks=blocks,
    )
    assert r.reasoning_replay_blocks == blocks
