"""Terminal backend section (S3).

Modeled after Hermes's setup_terminal_backend (hermes_cli/setup.py:1272).
Independently re-implemented (no code copied).

Detects available backends via shutil.which:
  - apptainer / singularity → "apptainer"
  - docker → "docker"
  - always: "local" (fallback / no sandbox; matches Hermes naming)

Builds a radiolist with detected backends + a Skip choice. User picks
which to use; selection writes config.terminal.backend.

Deeper sandbox configuration (image names, mount points, network
isolation level) is deferred — config.yaml editing covers it.
"""
from __future__ import annotations

import shutil

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def _detect_backends() -> list[str]:
    """Return list of available backends, ordered by preference."""
    out: list[str] = []
    if shutil.which("apptainer") or shutil.which("singularity"):
        out.append("apptainer")
    if shutil.which("docker"):
        out.append("docker")
    out.append("local")
    return out


_LABELS = {
    "apptainer": "Apptainer / Singularity (rootless container sandbox; HPC-friendly)",
    "docker": "Docker (container sandbox; runs as user)",
    "local": "Local shell (run directly on this machine — default)",
}


def run_terminal_backend_section(ctx: WizardCtx) -> SectionResult:
    backends = _detect_backends()
    choices: list[Choice] = [
        Choice(_LABELS[b], b) for b in backends
    ]
    choices.append(Choice("Skip — keep current", "__skip__"))

    idx = radiolist(
        "Select terminal backend:",
        choices, default=0,
        description="Where OpenComputer runs shell commands and code. "
                     "Apptainer/Docker isolate file + network access; "
                     "local is unsandboxed.",
    )
    chosen = choices[idx].value
    if chosen == "__skip__":
        return SectionResult.SKIPPED_FRESH

    ctx.config.setdefault("terminal", {})
    ctx.config["terminal"]["backend"] = chosen

    print(f"  ✓ Set terminal backend: {chosen}")
    return SectionResult.CONFIGURED
