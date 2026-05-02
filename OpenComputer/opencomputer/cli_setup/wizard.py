"""Wizard orchestrator — top-level entry point ``run_setup``.

Re-exports WizardCancelled from cli_ui.menu (single source) so callers
can `from opencomputer.cli_setup.wizard import WizardCancelled` without
having to know that the exception physically lives in the menu module
(it lives there to avoid a circular import: menu raises it,
section handlers catch it).
"""
from __future__ import annotations

from opencomputer.cli_ui.menu import WizardCancelled

__all__ = ["WizardCancelled", "run_setup"]


def run_setup(*, quick: bool = False) -> int:
    """Top-level wizard entry. Stub — full orchestrator lands in Task 7."""
    raise NotImplementedError("run_setup orchestrator lands in Task 7")
