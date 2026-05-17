"""``/indicator <style>`` — override the busy-spinner face style.

Best-of-three Recipe 7. The streaming spinner normally draws its face
from the active skin (``streaming._skin_spinner_text``). ``/indicator``
lets the user override just the face — independent of the skin —
choosing one of the :data:`busy_indicator.STYLES`. ``minimal`` and
``none`` are the spinner-fatigue escape hatches the audit asked for.

Session-scoped: the override is a module global, not persisted. A new
session starts back on the skin's faces.
"""
from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class IndicatorCommand(SlashCommand):
    name = "indicator"
    description = (
        "Override the busy-spinner face style "
        "(kawaii/minimal/dots/wings/none/skin)"
    )

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        from opencomputer.cli_ui.busy_indicator import (
            STYLES,
            current_indicator_style,
            set_indicator_style,
        )

        choices = ", ".join([*sorted(STYLES), "skin"])
        sub = (args or "").strip().lower()

        if sub == "":
            current = current_indicator_style() or "skin (default)"
            return SlashCommandResult(
                output=(
                    f"Current indicator: {current}\n"
                    f"Available: {choices}\n"
                    f"'minimal' / 'none' reduce spinner motion; "
                    f"'skin' returns to the active skin's faces."
                ),
                handled=True,
            )

        if set_indicator_style(sub):
            # Mirror into runtime.custom so a surface that rebuilds the
            # override from runtime (e.g. after a swap) can restore it.
            runtime.custom["indicator"] = current_indicator_style()
            shown = current_indicator_style() or "skin (default)"
            return SlashCommandResult(
                output=f"Indicator set to {shown}.", handled=True
            )

        return SlashCommandResult(
            output=f"Unknown indicator {sub!r}. Available: {choices}",
            handled=True,
        )


__all__ = ["IndicatorCommand"]
