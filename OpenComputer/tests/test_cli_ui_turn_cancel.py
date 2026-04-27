"""Tests for TurnCancelScope — cooperative cancellation for one chat turn."""
from __future__ import annotations

import asyncio
import os
import signal

import pytest

from opencomputer.cli_ui.turn_cancel import TurnCancelScope


@pytest.mark.asyncio
async def test_scope_starts_uncancelled():
    async with TurnCancelScope() as scope:
        assert scope.is_cancelled() is False


@pytest.mark.asyncio
async def test_scope_cancels_when_requested():
    async with TurnCancelScope() as scope:
        scope.request_cancel()
        assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_run_callable_returns_normally_when_not_cancelled():
    async def work() -> str:
        await asyncio.sleep(0.001)
        return "done"

    async with TurnCancelScope() as scope:
        result = await scope.run(work())
        assert result == "done"


@pytest.mark.asyncio
async def test_run_raises_cancelled_when_cancel_requested_mid_flight():
    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "done"

    async with TurnCancelScope() as scope:
        task = asyncio.create_task(scope.run(slow()))
        await asyncio.sleep(0.01)
        scope.request_cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_callback_observes_cancellation():
    """A streaming callback wired to scope.is_cancelled() can short-circuit."""
    chunks: list[str] = []

    async def streamer(scope: TurnCancelScope) -> None:
        for i in range(100):
            if scope.is_cancelled():
                break
            chunks.append(str(i))
            await asyncio.sleep(0.001)

    async with TurnCancelScope() as scope:
        task = asyncio.create_task(streamer(scope))
        await asyncio.sleep(0.005)
        scope.request_cancel()
        await task
    assert len(chunks) < 100  # stopped before completion


@pytest.mark.asyncio
async def test_install_sigint_handler_sets_scope_on_signal():
    """Sending SIGINT to ourselves while the scope's signal handler is
    installed should set the cancel flag — instead of raising
    ``KeyboardInterrupt`` and killing the loop."""
    async with TurnCancelScope() as scope:
        with scope.install_sigint_handler():
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.05)
            assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_install_sigint_handler_restores_previous_handler():
    async with TurnCancelScope() as scope:
        previous = signal.getsignal(signal.SIGINT)
        with scope.install_sigint_handler():
            assert signal.getsignal(signal.SIGINT) != previous
        # Restored after exit.
        assert signal.getsignal(signal.SIGINT) == previous
