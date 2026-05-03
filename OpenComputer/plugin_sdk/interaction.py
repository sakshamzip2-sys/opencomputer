"""User-interaction primitives — for tools that need a synchronous reply
from the human in the loop (`AskUserQuestion`, future `Confirm` etc.).

CLI mode answers immediately via stdin. Async channel adapters (Telegram /
Discord) need a *suspend the turn → wait for the next inbound message →
resume* dance which is not yet implemented in core; v1 of `AskUserQuestion`
returns an explicit error in those cases. Once Phase 11e adds pending-tool
state to SessionDB, this surface stays the same; only the routing in
`opencomputer/tools/ask_user_question.py` changes.

Source: claude-code's `AskUserQuestion`, hermes's `clarify_tool`.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Protocol


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """A single ask presented to the user."""

    question: str
    #: Optional list of suggested answers. CLI shows them numbered; rich
    #: channels can render as buttons. Empty = free-form text reply.
    options: tuple[str, ...] = field(default_factory=tuple)
    #: Hint to the channel adapter about how to render. "text" works
    #: everywhere; "choice" prefers buttons / list UI when available.
    presentation: str = "text"


@dataclass(frozen=True, slots=True)
class InteractionResponse:
    """The user's reply. `text` is the literal answer string; `option_index`
    is set if the user picked one of the supplied options."""

    text: str
    option_index: int | None = None


class AskUserQuestionHandler(Protocol):
    """Async callable that asks the user a question and returns the reply.

    Installed once per session by whichever surface owns the user-input
    layer (today: the CLI's Rich/prompt_toolkit stack; tomorrow: a
    gateway worker that handles the suspend/resume dance).
    """

    def __call__(
        self, req: InteractionRequest
    ) -> Awaitable[InteractionResponse]: ...


#: ContextVar holding the current handler, or ``None`` if no surface has
#: installed one. Tools call ``ASK_USER_QUESTION_HANDLER.get()`` and use
#: the handler if non-None, else fall back to the legacy stdin path.
#:
#: ContextVar (not a module global) so concurrent sessions / subagent
#: contexts each see their own handler — important for delegate trees.
ASK_USER_QUESTION_HANDLER: ContextVar[AskUserQuestionHandler | None] = (
    ContextVar("ASK_USER_QUESTION_HANDLER", default=None)
)


__all__ = [
    "ASK_USER_QUESTION_HANDLER",
    "AskUserQuestionHandler",
    "InteractionRequest",
    "InteractionResponse",
]
