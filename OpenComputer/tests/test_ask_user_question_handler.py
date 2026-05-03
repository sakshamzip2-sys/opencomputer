"""Unit tests for the Rich-Prompt-based AskUserQuestion handler."""
from __future__ import annotations

import asyncio
import io

from rich.console import Console

from opencomputer.cli_ui.ask_user_question_handler import (
    install_rich_handler,
    make_rich_handler,
)
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    InteractionRequest,
    InteractionResponse,
)


def _make_console_with_input(text: str) -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=80)
    # Monkey-patch input to deliver our scripted answer.
    console.input = lambda prompt="": text  # type: ignore[method-assign]
    return console, out


def test_handler_returns_text_response_for_freeform_question():
    console, _out = _make_console_with_input("forty two")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(question="What's the answer?")
    resp = asyncio.run(handler(req))
    assert isinstance(resp, InteractionResponse)
    assert resp.text == "forty two"
    assert resp.option_index is None


def test_handler_resolves_numeric_option_to_index():
    console, _out = _make_console_with_input("2")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(
        question="Which?", options=("alpha", "beta", "gamma")
    )
    resp = asyncio.run(handler(req))
    assert resp.text == "beta"
    assert resp.option_index == 1


def test_handler_treats_other_choice_as_freeform_passthrough():
    """Last option is the implicit '(other)' — typing free-form text
    means: pass it through verbatim with option_index=None."""
    console, _out = _make_console_with_input("custom answer")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(question="?", options=("a", "b"))
    resp = asyncio.run(handler(req))
    assert resp.text == "custom answer"
    assert resp.option_index is None


def test_handler_renders_question_and_options_to_console():
    console, out = _make_console_with_input("x")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(
        question="Pick one:", options=("alpha", "beta")
    )
    asyncio.run(handler(req))
    text = out.getvalue()
    assert "Pick one:" in text
    assert "alpha" in text
    assert "beta" in text
    # Numbered.
    assert "1" in text and "2" in text


def test_install_rich_handler_sets_contextvar():
    """install_rich_handler installs the handler and returns a Token
    suitable for ``reset()``."""
    console, _out = _make_console_with_input("ok")
    assert ASK_USER_QUESTION_HANDLER.get() is None
    token = install_rich_handler(console=console)
    try:
        h = ASK_USER_QUESTION_HANDLER.get()
        assert h is not None
        # Round-trip a call.
        resp = asyncio.run(h(InteractionRequest(question="?")))
        assert resp.text == "ok"
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)
    assert ASK_USER_QUESTION_HANDLER.get() is None


def test_handler_pauses_active_streaming_renderer():
    """When a StreamingRenderer is active with a running Live, the
    handler must stop it before reading input so the prompt isn't
    swallowed by the live region. After the handler returns, Live is
    left stopped — the next stream chunk will recreate it."""
    from opencomputer.cli_ui.streaming import StreamingRenderer

    console, _ = _make_console_with_input("yes")

    renderer = StreamingRenderer(console)
    with renderer:
        renderer.start_thinking()
        # Live is now running.
        assert renderer._live is not None

        handler = make_rich_handler(console=console)
        asyncio.run(handler(InteractionRequest(question="proceed?")))

        # Live should be stopped after the prompt.
        assert renderer._live is None
