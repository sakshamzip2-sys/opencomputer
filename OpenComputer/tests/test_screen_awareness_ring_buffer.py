"""Tests for ScreenRingBuffer — bounded last-N captures."""
from __future__ import annotations

import threading
import time

from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def test_append_and_read():
    buf = ScreenRingBuffer(max_size=5)
    cap = ScreenCapture(
        captured_at=1.0,
        text="hello",
        sha256="abc",
        trigger="user_message",
        session_id="s1",
    )
    buf.append(cap)
    assert len(buf) == 1
    assert buf.latest() is cap


def test_oldest_evicted_at_max_size():
    buf = ScreenRingBuffer(max_size=3)
    for i in range(5):
        buf.append(ScreenCapture(
            captured_at=float(i),
            text=f"text{i}",
            sha256=str(i),
            trigger="user_message",
            session_id="s1",
        ))
    assert len(buf) == 3
    most_recent = list(buf.most_recent(n=3))
    assert most_recent[0].text == "text4"
    assert most_recent[1].text == "text3"
    assert most_recent[2].text == "text2"


def test_window_seconds_filter():
    buf = ScreenRingBuffer(max_size=10)
    now = time.time()
    buf.append(ScreenCapture(
        captured_at=now - 100, text="old", sha256="o", trigger="user_message", session_id="s",
    ))
    buf.append(ScreenCapture(
        captured_at=now - 5, text="recent", sha256="r", trigger="user_message", session_id="s",
    ))
    in_window = list(buf.most_recent(n=10, window_seconds=10))
    assert len(in_window) == 1
    assert in_window[0].text == "recent"


def test_thread_safe_concurrent_append():
    buf = ScreenRingBuffer(max_size=200)

    def append_one(i: int) -> None:
        buf.append(ScreenCapture(
            captured_at=float(i),
            text=f"t{i}",
            sha256=str(i),
            trigger="user_message",
            session_id="s",
        ))

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(buf) == 100


def test_latest_on_empty_buffer_returns_none():
    buf = ScreenRingBuffer(max_size=5)
    assert buf.latest() is None
    assert list(buf.most_recent(n=5)) == []
