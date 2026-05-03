"""Strategy enum for ``@adapter``-decorated recipes.

Mirrors OpenCLI's ``Strategy`` enum exactly — 4 values that classify
how the adapter authenticates / interacts with the target site:

  - ``PUBLIC``    — pure HTTP, no auth, no browser. ``ctx.fetch`` only.
  - ``COOKIE``    — needs the user's logged-in browser session. The
                    adapter calls ``ctx.fetch_in_page`` so cookies ride
                    along automatically. (Header-token sites are a
                    sub-case — pass the token via env var, not enum.)
  - ``UI``        — drives the browser like a human (click / fill /
                    snapshot). Slowest tier; use only when no API
                    surface exists.
  - ``INTERCEPT`` — needs full browser control (CDP / Electron
                    devtools), e.g. for app-control adapters.

The enum is used in two places:
  - The ``@adapter`` decorator's ``strategy=`` kwarg.
  - ``AdapterContext`` decides whether to lazy-bootstrap the browser
    dispatcher (PUBLIC skips it; everything else triggers it on first
    network / page op).

We deliberately do NOT add a ``HEADER`` value — header-token sites are
``COOKIE`` adapters that pull the token from an env var or from
``ctx.site_memory.read("token")``. Adding ``HEADER`` would force every
adapter author to think about auth-source semantics that the COOKIE
path already handles.
"""

from __future__ import annotations

from enum import Enum


class Strategy(str, Enum):
    """Authentication / interaction tier for an adapter."""

    PUBLIC = "public"
    COOKIE = "cookie"
    UI = "ui"
    INTERCEPT = "intercept"


__all__ = ["Strategy"]
