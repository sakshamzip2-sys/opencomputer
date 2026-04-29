"""``/persona-mode [<id>|auto]`` — list / set / clear the auto-classifier
persona override.

Distinct from:

- ``/persona``     — ensemble profile switcher (different SOUL.md /
                     MEMORY.md per profile dir)
- ``/personality`` — storage-only knob with a different vocabulary
                     (helpful / concise / technical / creative / ...)

This command sets ``runtime.custom["persona_id_override"]``. The agent
loop reads it in :meth:`AgentLoop._build_persona_overlay` (override wins
over the auto-classifier). Setting it ALSO drops a
``runtime.custom["_persona_dirty"]`` flag so the loop can evict its
prompt snapshot for the session and pick up the new overlay on the very
next turn.

``auto`` clears the override and re-enables the classifier.
"""
from __future__ import annotations

from opencomputer.awareness.personas.registry import list_personas
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class PersonaModeCommand(SlashCommand):
    name = "persona-mode"
    description = "Set / clear / list the persona override (see /persona-mode)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        personas = list_personas()
        ids = sorted(p["id"] for p in personas)

        if not ids:
            return SlashCommandResult(
                output=(
                    "No personas configured. Bundled defaults should ship "
                    "in opencomputer/awareness/personas/defaults/. Check "
                    "your install or report a bug."
                ),
                handled=True,
            )

        if sub == "":
            active = runtime.custom.get("active_persona_id", "(unset)")
            override = runtime.custom.get("persona_id_override", "")
            override_line = (
                f"(override: {override})" if override else "(override: none)"
            )
            lines = [
                f"Active persona: {active} {override_line}",
                "",
                "Available:",
            ]
            for pid in ids:
                marker = " (active)" if pid == active else ""
                lines.append(f"  - {pid}{marker}")
            lines.append("")
            lines.append(
                "Usage: /persona-mode <id> | auto      "
                "(`auto` clears the override and re-enables the classifier)"
            )
            return SlashCommandResult(output="\n".join(lines), handled=True)

        if sub == "auto":
            runtime.custom.pop("persona_id_override", None)
            runtime.custom["_persona_dirty"] = True
            return SlashCommandResult(
                output="Persona override cleared — auto-classifier re-enabled.",
                handled=True,
            )

        if sub not in ids:
            return SlashCommandResult(
                output=(
                    f"Unknown persona {sub!r}. "
                    f"Available: {', '.join(ids)}"
                ),
                handled=True,
            )

        runtime.custom["persona_id_override"] = sub
        runtime.custom["_persona_dirty"] = True
        return SlashCommandResult(
            output=f"Persona override set to {sub}. "
                   f"Takes effect on the next turn.",
            handled=True,
        )


__all__ = ["PersonaModeCommand"]
