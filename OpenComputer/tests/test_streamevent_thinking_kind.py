"""StreamEvent must accept a ``thinking_delta`` kind so providers can
stream reasoning chunks alongside ``text_delta`` chunks."""
from __future__ import annotations

from plugin_sdk.provider_contract import StreamEvent


def test_streamevent_accepts_thinking_delta_kind() -> None:
    ev = StreamEvent(kind="thinking_delta", text="step 1: ")
    assert ev.kind == "thinking_delta"
    assert ev.text == "step 1: "
    assert ev.response is None


def test_streamevent_existing_kinds_still_work() -> None:
    """Backwards-compat — text_delta + done + tool_call must keep working."""
    assert StreamEvent(kind="text_delta", text="hi").kind == "text_delta"
    assert StreamEvent(kind="done").kind == "done"
    assert StreamEvent(kind="tool_call").kind == "tool_call"
