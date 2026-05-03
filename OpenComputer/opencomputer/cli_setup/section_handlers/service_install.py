"""Platform-agnostic service-install wizard section.

Replaces the macOS-only ``launchd_service.py``. Calls
``opencomputer.service.factory.get_backend()`` so the wizard works
identically on Linux, macOS, and Windows.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def run_service_install_section(ctx: WizardCtx) -> SectionResult:
    from opencomputer.service.base import ServiceUnsupportedError
    from opencomputer.service.factory import get_backend

    try:
        backend = get_backend()
    except ServiceUnsupportedError as exc:
        print(f"  ({exc} — service install skipped)")
        return SectionResult.SKIPPED_FRESH

    if not backend.supported():
        print(f"  ({backend.NAME} backend reports unsupported — skipped)")
        return SectionResult.SKIPPED_FRESH

    choices = [
        Choice("Install gateway as a system service", "install"),
        Choice("Skip — run gateway manually with `oc gateway`", "skip"),
    ]
    idx = radiolist(
        f"Install the gateway as a {backend.NAME} service? "
        "(runs in background, starts on login)",
        choices, default=0,
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    profile = ctx.config.get("active_profile", "default")
    result = backend.install(profile=profile, extra_args="gateway")
    print(f"  ✓ Installed {result.backend} service at {result.config_path}")
    for note in result.notes:
        print(f"    note: {note}")

    ctx.config.setdefault("gateway", {})
    ctx.config["gateway"]["service_installed"] = True
    ctx.config["gateway"]["service_backend"] = result.backend

    return SectionResult.CONFIGURED
