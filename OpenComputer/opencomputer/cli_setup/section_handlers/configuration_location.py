"""Read-only configuration location section."""
from __future__ import annotations

from pathlib import Path

from opencomputer.cli_setup.env_writer import default_env_file
from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def run_configuration_location_section(ctx: WizardCtx) -> SectionResult:
    home = ctx.config_path.parent
    data_dir = home
    install_dir = Path.cwd()
    print(f"  Config file:  {ctx.config_path}")
    print(f"  Secrets file: {default_env_file()}")
    print(f"  Data folder:  {data_dir}")
    print(f"  Install dir:  {install_dir}")
    print()
    print("  You can edit these files directly or use `oc config edit`.")
    return SectionResult.CONFIGURED
