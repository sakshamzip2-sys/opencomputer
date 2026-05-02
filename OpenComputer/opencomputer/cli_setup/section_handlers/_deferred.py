"""Deferred section placeholder — prints a stub that names the
follow-up sub-project and returns SKIPPED_FRESH."""
from __future__ import annotations

from typing import Callable

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def make_deferred_handler(target_subproject: str) -> Callable[[WizardCtx], SectionResult]:
    def handler(ctx: WizardCtx) -> SectionResult:
        print(f"  (deferred — coming in sub-project {target_subproject})")
        return SectionResult.SKIPPED_FRESH
    return handler
