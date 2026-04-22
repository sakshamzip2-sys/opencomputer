"""Shared-deps bag threaded to memory-aware sites (tools, hooks, injection).

Tools that need to read/write MEMORY.md or USER.md receive a ``MemoryContext``
at construction time rather than reaching into globals. This keeps the tools
testable and keeps the threading explicit.

The ``provider`` field is typed as ``Any`` here to avoid a forward-ref to the
``MemoryProvider`` ABC (which lives in ``plugin_sdk/memory.py`` and is added
in sub-phase 10f.F).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryContext:
    """Dependencies shared by memory tools, bridge, and injection providers.

    Fields:
        manager: The ``MemoryManager`` that owns MEMORY.md + USER.md + skills.
        db: The ``SessionDB`` for episodic/FTS5 queries.
        session_id_provider: Callable returning the current session id. A
            callable (not a string) because the agent may rotate sessions and
            tools need the live value, not a snapshot.
        provider: Optional external ``MemoryProvider`` (Honcho, Mem0, etc.).
            ``None`` means built-in memory only.
    """

    manager: Any  # MemoryManager
    db: Any  # SessionDB
    session_id_provider: Callable[[], str]
    provider: Any = None  # Optional[MemoryProvider]
    # Per-session state the bridge uses for failure tracking. Kept here so the
    # bridge stays stateless and the context can be reused across requests.
    _failure_state: dict = field(default_factory=dict)
