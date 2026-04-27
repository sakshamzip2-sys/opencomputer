"""Voice-mode plugin — continuous push-to-talk audio loop."""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.voice_mode.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry; CLI command does the work."""
    _log.debug("voice-mode plugin registered (use `opencomputer voice talk` to start)")
