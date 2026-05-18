"""Per-turn working directory — A6, gateway-vs-CLI parity Wave 1.

The gateway daemon runs from wherever it was launched (typically the
profile home), not the user's project. Without a per-chat working
directory, ``Bash`` runs in the wrong place and the agent's system
prompt advertises the wrong ``cwd``.

This module holds a :class:`~contextvars.ContextVar` that the gateway
dispatcher binds around a turn (from the matched binding's ``cwd``
field). Consumers — the ``Bash`` tool and the prompt builder — read it
through :func:`get_working_directory`, which falls back to the process
cwd when nothing is bound. On the CLI the var is never set, so the
fallback gives byte-identical behaviour to before A6.

``os.chdir`` is deliberately NOT used: the gateway runs many sessions
concurrently in one process and a global chdir would race between them.
A ContextVar is task-local and propagates to the tool-dispatch tasks
spawned inside ``run_conversation``.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Generator

_cwd_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "oc_working_directory", default=None,
)


def get_working_directory() -> str:
    """Return the working directory bound for the current turn.

    Falls back to the process cwd when nothing is bound (the CLI case),
    and to the user's home if even ``os.getcwd()`` raises (the launch
    directory was deleted out from under a long-lived daemon).
    """
    bound = _cwd_var.get()
    if bound:
        return bound
    try:
        return os.getcwd()
    except OSError:
        return os.path.expanduser("~")


@contextlib.contextmanager
def working_directory(path: str | None) -> Generator[None]:
    """Bind ``path`` as the working directory for the enclosed block.

    A falsy ``path`` is a no-op — the context manager still works so
    callers need no branching. The bound value is always reset on exit,
    including on exception.
    """
    if not path:
        yield
        return
    token = _cwd_var.set(path)
    try:
        yield
    finally:
        _cwd_var.reset(token)


__all__ = ["get_working_directory", "working_directory"]
