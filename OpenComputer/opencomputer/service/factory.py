"""Pick the right service backend module for the current platform."""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .base import ServiceUnsupportedError

if TYPE_CHECKING:
    from .base import ServiceBackend


def get_backend() -> "ServiceBackend":
    """Return the module-level ServiceBackend conforming to the current platform.

    Lazy imports keep test bootstrap fast and avoid loading unused
    backends (e.g., systemd module on macOS).
    """
    if sys.platform == "darwin":
        from . import _macos_launchd as backend
    elif sys.platform.startswith("linux"):
        from . import _linux_systemd as backend
    elif sys.platform.startswith("win"):
        from . import _windows_schtasks as backend
    else:
        raise ServiceUnsupportedError(
            f"no service backend for sys.platform={sys.platform!r}",
        )
    return backend  # type: ignore[return-value]


__all__ = ["get_backend"]
