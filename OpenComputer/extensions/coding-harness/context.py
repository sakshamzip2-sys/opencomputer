"""HarnessContext — the shared-deps bag passed to every harness tool and hook.

Deliberately narrow: only what the coding-harness itself owns (rewind store,
session state, progress emitter). Core primitives (Runtime, Session) stay on
the plugin_sdk surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class HarnessContext:
    session_id: str
    rewind_store: Any  # rewind.store.RewindStore — forward ref avoids cycle
    session_state: Any  # state.store.SessionStateStore
    emit_progress_fn: Optional[Callable[[dict], None]] = None

    def emit_progress(self, event: dict) -> None:
        """No-op if no transport is attached; wire-event push if one is."""
        if self.emit_progress_fn is not None:
            self.emit_progress_fn(event)


__all__ = ["HarnessContext"]
