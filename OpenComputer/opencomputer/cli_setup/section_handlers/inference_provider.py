"""Inference provider section. Full impl in Task 8."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def is_inference_provider_configured(ctx: WizardCtx) -> bool:
    model = ctx.config.get("model") or {}
    provider = model.get("provider")
    return bool(provider) and provider != "none"


def run_inference_provider_section(ctx: WizardCtx) -> SectionResult:
    raise NotImplementedError("inference_provider handler lands in Task 8")
