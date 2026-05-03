"""Importing the legacy launchd_service module emits DeprecationWarning."""
from __future__ import annotations

import importlib
import sys
import warnings


def test_legacy_launchd_service_emits_deprecation_warning() -> None:
    sys.modules.pop(
        "opencomputer.cli_setup.section_handlers.launchd_service", None,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module(
            "opencomputer.cli_setup.section_handlers.launchd_service",
        )
    assert any(
        issubclass(w.category, DeprecationWarning) for w in caught
    ), (
        f"expected DeprecationWarning, got categories: "
        f"{[w.category.__name__ for w in caught]}"
    )


def test_legacy_run_launchd_service_section_still_callable() -> None:
    """Old function name continues to work as alias."""
    from opencomputer.cli_setup.section_handlers.launchd_service import (
        run_launchd_service_section,
    )
    from opencomputer.cli_setup.section_handlers.service_install import (
        run_service_install_section,
    )
    assert run_launchd_service_section is run_service_install_section
