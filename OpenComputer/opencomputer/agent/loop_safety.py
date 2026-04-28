"""Anti-loop / repetition detector for the agent loop.

Tracks the last N ``(tool_name, args_hash)`` pairs in a sliding window per
``(session_id, delegation_depth)`` frame. If the same pair recurs more than
``max_tool_repeats`` times within the window the frame is *flagged* and a
warning is surfaced. After ``max_consecutive_flags`` consecutive flagged
records, ``must_stop()`` becomes true and the agent loop should raise
:class:`LoopAbortError`.

Symmetric for assistant-text repetition (``record_assistant_text``).

Per-frame scoping (AMENDMENTS H5 fix): :class:`opencomputer.tools.delegate.DelegateTool`
spawns a *fresh* :class:`AgentLoop` per subagent (Phase 0.8 verified — see
``docs/superpowers/plans/2026-04-28-openclaw-tier1-port-DECISIONS.md``), so in
practice each agent loop owns its own ``LoopDetector`` instance and frames
are usually keyed on a single ``(session_id, 0)`` pair. The frame-keyed
design is kept anyway so a single :class:`LoopDetector` instance shared
across nested calls (tests, future hot-loop reuse) cannot leak repetition
state from one subagent into another.

Default thresholds are permissive — healthy sessions never trigger.

This module is internal to :mod:`opencomputer.agent`; it is intentionally
NOT re-exported through ``plugin_sdk`` because plugins should never need
to mutate the loop's safety state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque


class LoopAbortError(RuntimeError):
    """Raised by the agent loop when ``LoopDetector.must_stop()`` returns True.

    Subclassing :class:`RuntimeError` lets generic ``except RuntimeError``
    paths catch the abort the same way they catch other terminal-control
    exceptions (e.g. ``SingleInstanceError`` in plugin loading). The agent
    loop catches this explicitly and surfaces the warning text as the
    final assistant message rather than re-raising.
    """


@dataclass
class _Frame:
    """Per-(session, depth) sliding-window state.

    Kept private — callers go through :class:`LoopDetector` methods which
    do the dict lookup. ``deque(maxlen=...)`` bounds memory at
    ``window_size`` entries per frame, regardless of how many calls land
    on that frame.
    """

    window_size: int
    tool_window: Deque[tuple[str, str]] = field(default_factory=deque)
    text_window: Deque[str] = field(default_factory=deque)
    consecutive_flags: int = 0
    last_warning: str = ""

    def __post_init__(self) -> None:
        # ``maxlen`` can't be set via field(default_factory=...) without an
        # explicit constructor — bind it here so each frame keeps its own
        # bounded deque.
        self.tool_window = deque(maxlen=self.window_size)
        self.text_window = deque(maxlen=self.window_size)


class LoopDetector:
    """Per-frame sliding-window repetition detector.

    Methods take ``session_id`` + ``depth`` and look up the matching
    :class:`_Frame`. A missing frame is treated as "no repetition seen"
    — ``flagged()``/``must_stop()`` return False and ``record_*`` is a
    no-op. This keeps the agent loop's wiring tolerant of edge cases
    where a frame gets popped before a callback fires.

    Thread-safety: the detector is single-task per agent loop. The
    agent loop is itself single-task per ``run_conversation`` call —
    asyncio coroutines on one event loop run sequentially when not
    awaiting, so the dict mutation here is safe without a lock.
    Concurrent ``run_conversation`` calls on the same instance would
    need external synchronisation (none of our call sites do this).
    """

    def __init__(
        self,
        *,
        max_tool_repeats: int = 3,
        max_text_repeats: int = 2,
        window_size: int = 10,
        max_consecutive_flags: int = 2,
    ) -> None:
        self.max_tool_repeats = max_tool_repeats
        self.max_text_repeats = max_text_repeats
        self.window_size = window_size
        self.max_consecutive_flags = max_consecutive_flags
        self._frames: dict[tuple[str, int], _Frame] = {}

    # ─── frame lifecycle ───────────────────────────────────────────────

    def push_frame(self, session_id: str, depth: int) -> None:
        """Open a new frame. Idempotent — pushing an existing key is a no-op.

        Idempotence matters because the agent loop calls ``push_frame`` at
        the top of every ``run_conversation``, and a session may be
        resumed (same ``session_id``) without ever having been popped (e.g.
        an exception during a prior turn left the frame in place).
        Resetting on push would silently clear genuine repetition history;
        leaving the existing frame keeps detection live across turns.
        """
        key = (session_id, depth)
        if key not in self._frames:
            self._frames[key] = _Frame(window_size=self.window_size)

    def pop_frame(self, session_id: str, depth: int) -> None:
        """Discard the frame's state. Safe on an absent frame."""
        self._frames.pop((session_id, depth), None)

    def reset_frame(self, session_id: str, depth: int) -> None:
        """Clear the frame's history but keep the frame allocated.

        Used when a turn finishes cleanly and the caller wants the next
        turn to start fresh without re-pushing. Safe on an absent frame
        — no-op rather than KeyError so tear-down paths stay tolerant.
        """
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return
        frame.tool_window.clear()
        frame.text_window.clear()
        frame.consecutive_flags = 0
        frame.last_warning = ""

    # ─── recording (the hot path called from the loop) ─────────────────

    def record_tool_call(
        self, session_id: str, depth: int, name: str, args_hash: str
    ) -> None:
        """Append a (name, args_hash) pair to the frame's window.

        On flag (count ≥ ``max_tool_repeats`` for the same key within
        the window): bump ``consecutive_flags`` and stash a warning
        message. On a *non-flagging* record: reset ``consecutive_flags``
        to 0 and clear ``last_warning`` — a unique tool call breaks the
        run of identical ones, so the must-stop counter restarts.

        Absent frame → no-op (callers may push lazily).
        """
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return
        key = (name, args_hash)
        frame.tool_window.append(key)
        count = sum(1 for k in frame.tool_window if k == key)
        if count >= self.max_tool_repeats:
            frame.consecutive_flags += 1
            frame.last_warning = (
                f"You are repeating tool call {name} with identical args "
                f"({count}x within last {self.window_size}). "
                f"Either change approach or call AskUserQuestion."
            )
        else:
            frame.consecutive_flags = 0
            frame.last_warning = ""

    def record_assistant_text(
        self, session_id: str, depth: int, text_hash: str
    ) -> None:
        """Append a text-hash to the frame's text window.

        Symmetric to :meth:`record_tool_call` but on a separate window
        — text repetition is a distinct degenerate mode (the model
        emitting the same paragraph over and over) and shouldn't share
        the tool window's count budget.
        """
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return
        frame.text_window.append(text_hash)
        count = sum(1 for h in frame.text_window if h == text_hash)
        if count >= self.max_text_repeats:
            frame.consecutive_flags += 1
            frame.last_warning = (
                f"You are repeating the same assistant message "
                f"({count}x within last {self.window_size}). "
                f"Try a fresh approach or stop."
            )
        else:
            frame.consecutive_flags = 0
            frame.last_warning = ""

    # ─── queries ───────────────────────────────────────────────────────

    def flagged(self, session_id: str, depth: int) -> bool:
        """True iff the most recent record produced a warning."""
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return False
        return bool(frame.last_warning)

    def warning(self, session_id: str, depth: int) -> str:
        """Latest warning text for the frame, or "" if none."""
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return ""
        return frame.last_warning

    def must_stop(self, session_id: str, depth: int) -> bool:
        """True once consecutive flags reach the configured threshold.

        The agent loop checks this after every ``record_*`` call and
        raises :class:`LoopAbortError` so the caller can surface a
        single, clean stop signal rather than letting the model spin.
        """
        frame = self._frames.get((session_id, depth))
        if frame is None:
            return False
        return frame.consecutive_flags >= self.max_consecutive_flags


__all__ = ["LoopAbortError", "LoopDetector"]
