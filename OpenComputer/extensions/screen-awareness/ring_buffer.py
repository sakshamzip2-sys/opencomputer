"""Per-session bounded ring buffer of ScreenCapture entries.

Holds the last N captures (default 20). Older entries are dropped on
overflow. Reads are most-recent-first. Thread-safe via an internal
lock — captures may be appended from PreToolUse/PostToolUse hooks
firing on different threads.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

#: Trigger source for a capture — used by RecallScreen to filter / explain.
TriggerSource = Literal["user_message", "pre_tool_use", "post_tool_use", "manual"]


@dataclass(frozen=True, slots=True)
class ScreenCapture:
    """One ring-buffer entry: OCR text + metadata."""

    captured_at: float  # epoch seconds
    text: str
    sha256: str
    trigger: TriggerSource
    session_id: str
    tool_call_id: str | None = None  # set when trigger ∈ {pre_tool_use, post_tool_use}


class ScreenRingBuffer:
    """Bounded thread-safe ring of ScreenCapture entries."""

    def __init__(self, max_size: int = 20) -> None:
        self._max = max_size
        self._buf: deque[ScreenCapture] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def append(self, cap: ScreenCapture) -> None:
        with self._lock:
            self._buf.append(cap)

    def latest(self) -> ScreenCapture | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def most_recent(
        self, n: int, window_seconds: float | None = None
    ) -> Iterator[ScreenCapture]:
        """Yield up to ``n`` most-recent captures, most-recent first.

        ``window_seconds``: if set, only yield captures whose
        ``captured_at`` is within the last N seconds (vs ``time.time()``).
        """
        import time as _time

        with self._lock:
            snapshot = list(self._buf)
        cutoff = (_time.time() - window_seconds) if window_seconds is not None else None
        out: list[ScreenCapture] = []
        for cap in reversed(snapshot):
            if cutoff is not None and cap.captured_at < cutoff:
                break
            out.append(cap)
            if len(out) >= n:
                break
        yield from out


__all__ = ["ScreenCapture", "ScreenRingBuffer", "TriggerSource"]
