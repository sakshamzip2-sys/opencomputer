"""
Dynamic injection providers — cross-cutting system-prompt modifiers.

A provider declares a piece of text to inject into the system prompt when
certain runtime conditions apply (e.g. plan mode active). The agent loop
queries all registered providers at the start of each turn; whichever
return non-empty strings get appended to the system prompt.

This is kimi-cli's pattern — keeps cross-cutting concerns (plan mode, yolo
mode, custom modes) out of the main loop as if-branches.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from plugin_sdk.core import Message
from plugin_sdk.runtime_context import RuntimeContext


@dataclass(frozen=True, slots=True)
class InjectionContext:
    """Read-only snapshot passed to each provider's collect() call."""

    #: Full message history so far (same list the LLM will see this turn).
    messages: tuple[Message, ...]
    #: Per-invocation flags (plan_mode, yolo_mode, etc.).
    runtime: RuntimeContext
    #: Session id — useful for session-scoped caches or per-chat behaviors.
    session_id: str = ""
    #: Per-turn monotonic counter. Providers can use this to throttle heavy
    #: content to every Nth turn. Positive ``int`` means "this is turn N"
    #: with ``N`` starting at 1. Default ``0`` is the neutral "caller did not
    #: thread the counter" sentinel — throttling providers should treat it as
    #: equivalent to the first exposure (return FULL content) rather than
    #: silently falling into sparse mode forever.
    turn_index: int = 0


class DynamicInjectionProvider(ABC):
    """Base class for providers that inject text into the system prompt.

    Implement `collect()`. Return a string (the injection) or None/empty
    (this provider is not applicable this turn).

    `priority` orders providers in the final prompt — lower first.
    `provider_id` must be unique per registration; it's also used for
    deterministic ordering when two providers share a priority.

    `collect` is ``async`` — the engine gathers all providers concurrently
    so an I/O-bound provider (Honcho, a remote vector index, etc.) can't
    become a serial bottleneck. Pure-function providers can simply
    ``async def`` and ``return`` without awaiting anything.
    """

    #: Lower runs first. Plan mode is 10, yolo is 20, user-added modes 50+.
    priority: int = 100

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique id per provider. Used for dedup + ordering stability."""
        ...

    @abstractmethod
    async def collect(self, ctx: InjectionContext) -> str | None:
        """Return injection text or None if this provider doesn't apply."""
        ...


__all__ = ["DynamicInjectionProvider", "InjectionContext"]
