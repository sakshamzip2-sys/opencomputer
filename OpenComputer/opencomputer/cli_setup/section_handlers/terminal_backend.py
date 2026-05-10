"""Terminal backend setup section."""
from __future__ import annotations

import shutil

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def _detect_backends() -> list[str]:
    out: list[str] = ["local"]
    if shutil.which("docker"):
        out.append("docker")
    if shutil.which("ssh"):
        out.append("ssh")
    if shutil.which("modal"):
        out.append("modal")
    if shutil.which("daytona"):
        out.append("daytona")
    if shutil.which("vercel"):
        out.append("vercel")
    if shutil.which("apptainer") or shutil.which("singularity"):
        out.append("apptainer")
    return out


_LABELS = {
    "local": "Local - run directly on this machine (default)",
    "docker": "Docker - isolated container with configurable resources",
    "modal": "Modal - serverless cloud sandbox",
    "ssh": "SSH - run on a remote machine",
    "daytona": "Daytona - persistent cloud development environment",
    "vercel": "Vercel Sandbox - cloud microVM with persistent snapshots",
    "apptainer": "Apptainer / Singularity - rootless container sandbox",
}


def run_terminal_backend_section(ctx: WizardCtx) -> SectionResult:
    current = (ctx.config.get("terminal") or {}).get("backend") or "local"
    detected = _detect_backends()
    choices = [Choice(_LABELS[b], b) for b in detected]
    choices.append(Choice(f"Keep current ({current})", "__skip__"))
    default = next((i for i, c in enumerate(choices) if c.value == current), 0)

    idx = radiolist("Select terminal backend:", choices, default=default)
    chosen = choices[idx].value
    if chosen == "__skip__":
        print(f"  Keeping current backend: {current}")
        return SectionResult.SKIPPED_FRESH

    ctx.config.setdefault("terminal", {})["backend"] = chosen
    print(f"  ✓ Set terminal backend: {chosen}")
    return SectionResult.CONFIGURED
