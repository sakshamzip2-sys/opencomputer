"""Tests for opencomputer/gateway/replay_sanitizer.py (OpenClaw 1.D)."""
from __future__ import annotations

import time

import pytest

from opencomputer.gateway.replay_sanitizer import sanitize_for_replay


def _msg(role, content, *, ts=None, replay=False, in_flight=False):
    if ts is None:
        ts = time.time()
    return {
        "role": role,
        "content": content,
        "ts": ts,
        "replay": replay,
        "in_flight": in_flight,
    }


def test_strip_replay_marked_assistant():
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "buffered reply", replay=True),
        _msg("user", "next"),
    ]
    out = sanitize_for_replay(msgs, max_age_seconds=300)
    assert len(out) == 2
    assert all(not m.get("replay") for m in out)


def test_drop_in_flight_outgoing():
    msgs = [
        _msg("user", "ping"),
        _msg("assistant", "pong", in_flight=True),
        _msg("user", "next"),
    ]
    out = sanitize_for_replay(msgs)
    assert all(not m.get("in_flight") for m in out)


def test_drop_user_messages_older_than_max_age():
    now = time.time()
    msgs = [
        _msg("user", "stale", ts=now - 600),
        _msg("user", "fresh", ts=now - 10),
    ]
    out = sanitize_for_replay(msgs, max_age_seconds=300, now=now)
    assert [m["content"] for m in out] == ["fresh"]


def test_assistant_messages_pass_through_regardless_of_age():
    """Only user messages get age-checked; assistant turns are not dropped for age."""
    now = time.time()
    msgs = [
        _msg("assistant", "old reply", ts=now - 600),
        _msg("user", "fresh user", ts=now - 10),
    ]
    out = sanitize_for_replay(msgs, max_age_seconds=300, now=now)
    assert [m["content"] for m in out] == ["old reply", "fresh user"]


def test_messages_without_markers_pass_through_unchanged():
    """Bare dicts (no replay/in_flight/ts) survive — backwards compat."""
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    out = sanitize_for_replay(msgs)
    assert out == msgs


def test_attribute_style_object_input():
    """Works with dataclass-like objects, not just dicts."""

    class _M:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    msgs = [
        _M(role="user", content="hi", ts=time.time(), replay=False, in_flight=False),
        _M(role="assistant", content="x", replay=True),
    ]
    out = sanitize_for_replay(msgs)
    assert len(out) == 1
    assert out[0].content == "hi"


def test_does_not_mutate_input():
    msgs = [_msg("user", "hi"), _msg("assistant", "x", replay=True)]
    snapshot = [dict(m) for m in msgs]
    sanitize_for_replay(msgs)
    assert msgs == snapshot


def test_preserves_order_of_survivors():
    msgs = [
        _msg("user", "1"),
        _msg("assistant", "drop-1", replay=True),
        _msg("user", "2"),
        _msg("assistant", "drop-2", in_flight=True),
        _msg("user", "3"),
    ]
    out = sanitize_for_replay(msgs)
    assert [m["content"] for m in out] == ["1", "2", "3"]


def test_empty_input():
    assert sanitize_for_replay([]) == []
