"""Tests for ``opencomputer.observability.trace`` — per-turn trace contextvar.

Coverage:
* fresh context starts with ``None``
* ``new_trace_id`` returns a UUID-shaped string
* ``set_trace_id`` / ``reset_trace_id`` round-trip
* ``trace_scope`` context manager set + reset
* ``reset_trace_id`` tolerates stale tokens from different contexts
* contextvar propagates across ``await`` boundaries (asyncio.Task)
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from opencomputer.observability.trace import (
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_scope,
)


def test_default_is_none():
    """A fresh context has no active trace."""
    # Snapshot any current trace (test isolation), reset to None.
    saved_token = set_trace_id(None)
    try:
        assert get_trace_id() is None
    finally:
        reset_trace_id(saved_token)


def test_new_trace_id_is_uuid():
    """``new_trace_id`` returns a parseable UUID4 string."""
    tid = new_trace_id()
    # Round-trip — uuid.UUID raises ValueError on malformed input.
    parsed = uuid.UUID(tid)
    assert str(parsed) == tid


def test_new_trace_id_unique():
    """Every call returns a distinct id."""
    ids = {new_trace_id() for _ in range(100)}
    assert len(ids) == 100


def test_set_and_reset_round_trip():
    """``set_trace_id`` makes the id visible; ``reset_trace_id`` clears it."""
    saved = set_trace_id(None)
    try:
        assert get_trace_id() is None
        token = set_trace_id("abc-123")
        assert get_trace_id() == "abc-123"
        reset_trace_id(token)
        assert get_trace_id() is None
    finally:
        reset_trace_id(saved)


def test_trace_scope_sets_and_clears():
    """The context manager pushes + pops the id correctly."""
    saved = set_trace_id(None)
    try:
        assert get_trace_id() is None
        with trace_scope() as tid:
            assert get_trace_id() == tid
            assert tid is not None
        assert get_trace_id() is None
    finally:
        reset_trace_id(saved)


def test_trace_scope_uses_provided_id():
    """Passing an explicit id uses that id rather than generating one."""
    saved = set_trace_id(None)
    try:
        with trace_scope("explicit-id") as tid:
            assert tid == "explicit-id"
            assert get_trace_id() == "explicit-id"
    finally:
        reset_trace_id(saved)


def test_trace_scope_clears_on_exception():
    """Exception inside the scope still clears the contextvar."""
    saved = set_trace_id(None)
    try:
        with pytest.raises(RuntimeError):
            with trace_scope():
                assert get_trace_id() is not None
                raise RuntimeError("boom")
        assert get_trace_id() is None
    finally:
        reset_trace_id(saved)


def test_reset_with_stale_token_does_not_crash():
    """``reset_trace_id`` swallows ValueError from cross-context tokens.

    Simulates the scenario where set/reset happened in different
    asyncio task contexts. We can't easily construct a real
    cross-context token in a sync test, so we just verify that a
    second ``reset_trace_id`` on an already-consumed token doesn't
    explode.
    """
    saved = set_trace_id(None)
    try:
        token = set_trace_id("temp")
        reset_trace_id(token)
        # Second reset on same token — must not raise.
        reset_trace_id(token)
        # State should be cleared to None.
        assert get_trace_id() is None
    finally:
        reset_trace_id(saved)


def test_trace_propagates_across_await():
    """Setting a trace before an await keeps it visible after."""

    async def _check_after_await() -> str | None:
        await asyncio.sleep(0)
        return get_trace_id()

    async def _runner():
        with trace_scope("await-test") as tid:
            after = await _check_after_await()
            return tid, after

    tid, after = asyncio.run(_runner())
    assert tid == after == "await-test"
