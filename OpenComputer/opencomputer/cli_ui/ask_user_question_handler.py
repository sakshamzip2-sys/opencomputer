"""Rich-Prompt-based handler for the AskUserQuestion tool.

Installed by the CLI at session start (see ``cli.py``). When the agent
calls ``AskUserQuestionTool`` mid-turn, the tool reaches this handler
via the ``ASK_USER_QUESTION_HANDLER`` ContextVar and uses it instead
of the legacy ``sys.stdin.readline()`` path that conflicts with the
active prompt_toolkit / Rich.Live terminal stack.

Lifecycle:
    1. Discover the active StreamingRenderer (if any) via
       :func:`current_renderer`.
    2. Stop its Live region so the prompt isn't swallowed.
    3. Print the question + numbered options to the console.
    4. Read the answer via ``console.input("> ")``.
    5. Return :class:`InteractionResponse`.

The next stream event from the model will recreate the Live region
naturally (the renderer creates Live lazily in ``start_thinking`` /
``_refresh``), so we don't restart it here.
"""
from __future__ import annotations

from contextvars import Token

from rich.console import Console
from rich.text import Text

from opencomputer.cli_ui.streaming import current_renderer
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    AskUserQuestionHandler,
    InteractionRequest,
    InteractionResponse,
)


def _format_question(req: InteractionRequest) -> Text:
    body = Text()
    body.append("\n")
    body.append(req.question, style="bold cyan")
    body.append("\n")
    if req.options:
        for i, opt in enumerate(req.options, 1):
            body.append(f"  {i}. ", style="dim")
            body.append(opt)
            body.append("\n")
        body.append(
            f"  {len(req.options) + 1}. (other — type free-form text)\n",
            style="dim italic",
        )
    return body


def _resolve_option(
    req: InteractionRequest, raw: str
) -> InteractionResponse:
    """Map a numeric pick to its option text + index; otherwise treat
    as free-form."""
    answer = raw.strip()
    if req.options and answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(req.options):
            return InteractionResponse(
                text=req.options[idx], option_index=idx
            )
    return InteractionResponse(text=answer, option_index=None)


def make_rich_handler(*, console: Console) -> AskUserQuestionHandler:
    """Build a handler that reads from the given Rich Console."""

    async def _handler(req: InteractionRequest) -> InteractionResponse:
        # Pause the active Live region so the prompt isn't eaten.
        active = current_renderer()
        if active is not None and active._live is not None:
            try:
                active._live.stop()
            except Exception:  # noqa: BLE001 — never crash the tool
                pass
            active._live = None  # let the next stream tick recreate it
        console.print(_format_question(req))
        raw = console.input("> ")
        return _resolve_option(req, raw)

    return _handler


def install_rich_handler(*, console: Console) -> Token:
    """Install the handler in the current async context and return the
    ContextVar reset token. The CLI calls this once at session start
    and lets the token live until process exit (no reset)."""
    handler = make_rich_handler(console=console)
    return ASK_USER_QUESTION_HANDLER.set(handler)


__all__ = ["install_rich_handler", "make_rich_handler"]
