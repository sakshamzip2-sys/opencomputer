"""Legacy alias — use service_install instead.

This module re-exports the new platform-agnostic section function
under its old name to preserve backward compatibility for one release.
Removed in the next major version.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "opencomputer.cli_setup.section_handlers.launchd_service is deprecated; "
    "use opencomputer.cli_setup.section_handlers.service_install instead.",
    DeprecationWarning,
    stacklevel=2,
)

from .service_install import run_service_install_section as run_launchd_service_section

__all__ = ["run_launchd_service_section"]
