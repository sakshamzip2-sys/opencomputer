"""Messaging platforms section. Full impl in Task 9."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def is_messaging_platforms_configured(ctx: WizardCtx) -> bool:
    gw = ctx.config.get("gateway") or {}
    return bool(gw.get("platforms"))


def run_messaging_platforms_section(ctx: WizardCtx) -> SectionResult:
    raise NotImplementedError("messaging_platforms handler lands in Task 9")
