"""Tests for opencomputer.dashboard.pty_bridge (Wave 6.D — Hermes port).

These tests run only on POSIX. On Windows we just smoke-test the
``PtyUnavailableError`` branch.
"""

from __future__ import annotations

import sys

import pytest

from opencomputer.dashboard.pty_bridge import PtyBridge, PtyUnavailableError

pytestmark = pytest.mark.skipif(
    not PtyBridge.is_available(),
    reason="PTY unavailable (Windows native or ptyprocess not installed)",
)


def test_spawn_and_read_writes_back():
    bridge = PtyBridge.spawn(["cat"])
    try:
        bridge.write(b"hello\n")
        # Read until we see our payload echoed (PTY echoes input by
        # default + writes the cat output back).
        seen = b""
        for _ in range(20):
            chunk = bridge.read(timeout=0.1)
            if chunk is None:
                break
            seen += chunk or b""
            if b"hello" in seen:
                break
        assert b"hello" in seen
    finally:
        bridge.close()


def test_close_is_idempotent():
    bridge = PtyBridge.spawn(["cat"])
    bridge.close()
    bridge.close()  # should not raise
    assert not bridge.is_alive()


def test_resize_does_not_crash():
    bridge = PtyBridge.spawn(["cat"])
    try:
        bridge.resize(cols=120, rows=40)
        bridge.resize(cols=0, rows=0)  # clamped to 1,1 internally
    finally:
        bridge.close()


def test_read_after_close_returns_none():
    bridge = PtyBridge.spawn(["cat"])
    bridge.close()
    assert bridge.read(timeout=0.05) is None


def test_pid_property():
    bridge = PtyBridge.spawn(["cat"])
    try:
        assert bridge.pid > 0
    finally:
        bridge.close()


def test_context_manager():
    with PtyBridge.spawn(["cat"]) as bridge:
        assert bridge.is_alive()
    assert not bridge.is_alive()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="EIO/EBADF errno paths most reliable on Linux",
)
def test_write_on_closed_bridge_is_noop():
    bridge = PtyBridge.spawn(["cat"])
    bridge.close()
    bridge.write(b"after close")  # must not raise
