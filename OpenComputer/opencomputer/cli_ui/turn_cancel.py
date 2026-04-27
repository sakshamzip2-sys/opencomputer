"""TurnCancelScope — cooperative cancellation for one chat turn.

Owns an ``asyncio.Event`` that the ESC key binding (or SIGINT handler,
or KeyboardListener thread) sets when the user wants to interrupt the
in-flight model response. The streaming callback polls
``is_cancelled()`` to short-circuit chunk processing; ``run()`` wraps
an awaitable so a pending ``request_cancel()`` cancels the underlying
task cleanly via ``task.cancel()``.

Pattern adapted from kimi-cli's ``cancel_event`` (single asyncio.Event
threaded through the agent loop) and hermes-agent's polling-flag
interrupt — but unified into one object so the chat loop has a single
handle to pass around.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
from types import TracebackType
from typing import Any, Awaitable, Iterator


class TurnCancelScope:
    """Async context manager that holds the cancel state for one turn."""

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> "TurnCancelScope":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._task = None

    def request_cancel(self) -> None:
        """Signal cancellation. Idempotent. If a task is registered via
        :meth:`run`, ``task.cancel()`` is also invoked so the awaitable
        unwinds promptly."""
        self._event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def run(self, awaitable: Awaitable[Any]) -> Any:
        """Run ``awaitable`` under this scope. If cancellation is requested
        while it's in flight, ``asyncio.CancelledError`` propagates."""
        self._task = asyncio.ensure_future(awaitable)
        try:
            return await self._task
        finally:
            self._task = None

    @contextlib.contextmanager
    def install_sigint_handler(self) -> Iterator[None]:
        """While in this with-block, SIGINT (Ctrl+C) calls
        :meth:`request_cancel` instead of raising ``KeyboardInterrupt``.

        Restored on exit. Best-effort: outside the main thread or on
        platforms where ``signal.signal`` raises (Windows asyncio loops),
        this falls back to a no-op so the chat loop never crashes from
        signal-install failure.
        """
        previous = None
        try:
            previous = signal.getsignal(signal.SIGINT)
        except (ValueError, OSError):  # main-thread restriction
            yield
            return

        def _handler(signum: int, frame: object) -> None:
            self.request_cancel()

        try:
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            yield
            return

        try:
            yield
        finally:
            try:
                if previous is not None:
                    signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass
