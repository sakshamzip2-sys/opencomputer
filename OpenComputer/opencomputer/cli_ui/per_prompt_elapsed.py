"""Per-prompt elapsed-time clock for the CLI status line.

Hermes-CLI parity (doc line 351). Independent of the session-wide
duration shown elsewhere — this resets on every user prompt and shows:

- ⏱ NN s    while the agent is running (live tick)
- ⏲ NN s / total MM s    once the turn finalises (frozen until next prompt)

Pure stateful object — accepts a ``now`` callable for tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


def _fmt_secs(s: float) -> str:
    """Format seconds as `12s` or `3m 45s` for compact display."""
    s_int = int(max(0, s))
    if s_int < 60:
        return f"{s_int}s"
    m, sec = divmod(s_int, 60)
    return f"{m}m {sec}s"


@dataclass
class PromptClock:
    """Tracks per-prompt elapsed time for the status line.

    Lifecycle:
        clock.start()    # called when the user sends a prompt
        # ... agent runs, tick() called each render
        clock.stop()     # called when turn finalises
        clock.reset()    # called on Ctrl+C / new prompt
    """

    _now: Callable[[], float] = field(default_factory=lambda: time.time)
    session_start: float = 0.0
    _prompt_start: float | None = None
    _prompt_stop: float | None = None

    def __post_init__(self) -> None:
        if self.session_start == 0.0:
            self.session_start = float(self._now())

    def start(self) -> None:
        self._prompt_start = float(self._now())
        self._prompt_stop = None

    def stop(self) -> None:
        if self._prompt_start is not None and self._prompt_stop is None:
            self._prompt_stop = float(self._now())

    def reset(self) -> None:
        self._prompt_start = None
        self._prompt_stop = None

    def render(self) -> str:
        if self._prompt_start is None:
            return ""
        if self._prompt_stop is None:
            elapsed = float(self._now()) - self._prompt_start
            return f"⏱ {_fmt_secs(elapsed)}"
        elapsed = self._prompt_stop - self._prompt_start
        total = float(self._now()) - self.session_start
        return f"⏲ {_fmt_secs(elapsed)} / {_fmt_secs(total)}"
