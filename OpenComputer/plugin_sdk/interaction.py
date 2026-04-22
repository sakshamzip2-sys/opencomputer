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

from dataclasses import dataclass, field


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


__all__ = ["InteractionRequest", "InteractionResponse"]
