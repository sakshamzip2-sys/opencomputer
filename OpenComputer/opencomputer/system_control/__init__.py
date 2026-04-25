"""3.F — OS feature flag + invisible-by-default UI for autonomous mode.

This package is the master administrative gate for "full system control"
mode. It is **independent of** the F1 consent layer:

- **F1 consent** (`opencomputer.agent.consent`) gates *individual capabilities*.
- **3.F** (this package) gates the *whole autonomous-mode personality*.

Both must be on for autonomous tool execution. With 3.F disabled (the
default), the agent behaves exactly like a standard chat agent and
nothing visible changes — the structured ``agent.log`` is not written,
the menu-bar indicator is not started, and bus subscribers are not
attached.

Pieces
------

- :mod:`opencomputer.system_control.logger` — append-only JSON-line log
  with rotation + OSError tolerance.
- :mod:`opencomputer.system_control.bus_listener` — subscribes the
  structured logger to ``default_bus`` for ALL events while system-control
  is on.
- :mod:`opencomputer.system_control.menu_bar` — optional macOS rumps
  indicator. Soft-dep; on non-Darwin / no-rumps hosts it short-circuits.
"""

from __future__ import annotations

from opencomputer.system_control.bus_listener import attach_to_bus, detach_from_bus
from opencomputer.system_control.logger import StructuredAgentLogger, default_logger

__all__ = [
    "StructuredAgentLogger",
    "default_logger",
    "attach_to_bus",
    "detach_from_bus",
]
