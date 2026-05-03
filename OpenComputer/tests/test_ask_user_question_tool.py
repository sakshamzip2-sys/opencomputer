"""Tool-level tests: handler-installed path + stdin fallback."""
from __future__ import annotations

import asyncio
import io

from opencomputer.tools.ask_user_question import AskUserQuestionTool
from plugin_sdk.core import ToolCall
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    InteractionRequest,
    InteractionResponse,
)


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t1", name="AskUserQuestion", arguments=args)


def test_tool_uses_installed_handler_when_present():
    captured: list[InteractionRequest] = []

    async def fake_handler(req: InteractionRequest) -> InteractionResponse:
        captured.append(req)
        return InteractionResponse(text="forty two", option_index=None)

    token = ASK_USER_QUESTION_HANDLER.set(fake_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(
            tool.execute(_call({"question": "What's the answer?"}))
        )
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "forty two" in result.content
    assert len(captured) == 1
    assert captured[0].question == "What's the answer?"


def test_tool_passes_options_through_to_handler():
    captured: list[InteractionRequest] = []

    async def fake_handler(req):
        captured.append(req)
        return InteractionResponse(text="alpha", option_index=0)

    token = ASK_USER_QUESTION_HANDLER.set(fake_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(
            tool.execute(_call({"question": "?", "options": ["alpha", "beta"]}))
        )
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "alpha" in result.content
    assert captured[0].options == ("alpha", "beta")


def test_tool_falls_back_to_stdin_when_no_handler_installed(monkeypatch):
    """When no handler is installed, the legacy stdin path is used —
    preserves headless / piped input behavior."""
    monkeypatch.setattr("sys.stdin", io.StringIO("piped answer\n"))
    # Belt-and-suspenders: ensure the contextvar is empty.
    if ASK_USER_QUESTION_HANDLER.get() is not None:
        token = ASK_USER_QUESTION_HANDLER.set(None)  # type: ignore[arg-type]
    else:
        token = None

    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(tool.execute(_call({"question": "?"})))
    finally:
        if token is not None:
            ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "piped answer" in result.content


def test_tool_handler_keyboard_interrupt_returns_cancelled():
    async def cancel_handler(req):
        raise KeyboardInterrupt()

    token = ASK_USER_QUESTION_HANDLER.set(cancel_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(tool.execute(_call({"question": "?"})))
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert result.is_error
    assert "cancel" in result.content.lower()


def test_tool_gateway_mode_unchanged_when_no_handler():
    """Async-channel mode is detected by cli_mode=False — must still
    return the existing 'use PushNotification' error message."""
    tool = AskUserQuestionTool(cli_mode=False)
    result = asyncio.run(tool.execute(_call({"question": "?"})))
    assert result.is_error
    assert "PushNotification" in result.content


def test_tool_handler_with_option_index_returns_chose_format():
    """Handler returning option_index=N gets formatted as 'User chose
    option N+1: <option text>' to match the legacy stdin formatting."""

    async def fake_handler(req):
        return InteractionResponse(text="alpha", option_index=0)

    token = ASK_USER_QUESTION_HANDLER.set(fake_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(
            tool.execute(_call({"question": "?", "options": ["alpha", "beta"]}))
        )
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "User chose option 1: alpha" in result.content
