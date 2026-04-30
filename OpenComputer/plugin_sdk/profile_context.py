"""ContextVar-scoped active-profile home — per-task profile selection.

Set by ``opencomputer/gateway/dispatch.py`` once it has resolved an
inbound ``MessageEvent`` to a ``profile_id``; consumed indirectly via
``opencomputer.agent.config._home``, which falls back to
``OPENCOMPUTER_HOME`` env var and then ``~/.opencomputer/default``.

Lives in ``plugin_sdk`` rather than ``opencomputer.agent`` because
plugin code (channel adapters, tools) may need to read the active
profile during a request and must not import internals.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

#: Per-asyncio-Task active profile home. ``None`` means "no profile
#: scope active — fall back to env var / default".
current_profile_home: ContextVar[Path | None] = ContextVar(
    "current_profile_home", default=None
)


@contextmanager
def set_profile(home: Path) -> Iterator[None]:
    """Bind ``current_profile_home`` to ``home`` for the duration of
    the ``with`` block. Restores the prior value on exit (including on
    exception). Safe to nest.

    Each ``asyncio.Task`` inherits the contextvar value at task-creation
    time; mutations within a task are local to that task. So two tasks
    that each ``set_profile(...)`` independently see their own values
    without locking.
    """
    token = current_profile_home.set(home)
    try:
        yield
    finally:
        current_profile_home.reset(token)


__all__ = ["current_profile_home", "set_profile"]
