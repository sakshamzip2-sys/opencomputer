"""``/update`` — show whether a newer OpenComputer is available.

Wraps the existing background update check (``opencomputer.cli_update_check``)
in an explicit slash command so users don't have to wait for the
end-of-session hint to surface. Mirrors the Hermes pattern where the
banner shows "X commits ahead, run /update" when a new release is
published.

Behavior:

  - If a newer version is on PyPI → print the upgrade hint and the
    suggested command (``pip install -U opencomputer``).
  - If up-to-date → confirm explicitly so the user knows the check
    actually ran.
  - If the background check is still in flight or PyPI is unreachable
    → tell the user without erroring out.

Honors ``OPENCOMPUTER_NO_UPDATE_CHECK`` opt-out (consistent with the
end-of-session hint).
"""
from __future__ import annotations

import os

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class UpdateCommand(SlashCommand):
    name = "update"
    description = "Check for a newer OpenComputer release on PyPI"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        if os.environ.get("OPENCOMPUTER_NO_UPDATE_CHECK"):
            return SlashCommandResult(
                output=(
                    "Update check is disabled (OPENCOMPUTER_NO_UPDATE_CHECK "
                    "is set). Unset the env var to re-enable."
                ),
                handled=True,
            )

        # Lazy import — keeps slash-command imports cheap and avoids a
        # circular when this module is loaded at registry time.
        from opencomputer import __version__
        from opencomputer.cli_update_check import (
            get_update_hint,
            prefetch_update_check,
        )

        # Make sure a check is running (idempotent — no-op if already
        # in flight or freshly cached).
        prefetch_update_check()

        # 1.5s is generous: PyPI usually answers in <500ms; this lets a
        # cold check complete within the slash command rather than
        # punting to the end-of-session hint.
        hint = get_update_hint(timeout=1.5)

        if hint:
            return SlashCommandResult(
                output=(
                    f"{hint}\n\n"
                    f"  Current: {__version__}\n"
                    f"  Run: pip install -U opencomputer"
                ),
                handled=True,
            )

        return SlashCommandResult(
            output=(
                f"OpenComputer is up to date (v{__version__}).\n"
                "  (PyPI checked; cached for 24 hours via "
                "``~/.opencomputer/.update_check.json``.)"
            ),
            handled=True,
        )


__all__ = ["UpdateCommand"]
