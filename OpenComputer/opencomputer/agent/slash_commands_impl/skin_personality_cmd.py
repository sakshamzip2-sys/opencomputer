"""``/skin [name]`` and ``/personality [name]`` — runtime-only state setters.

Tier 2.A.5 + 2.A.20 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Both store a name in ``runtime.custom``. Full theme rendering (skin)
and prompt-overlay loading (personality) are deferred follow-ups —
this PR just establishes the configuration knob so users can experiment
with the surface and downstream renderers can pick up the value.

Built-in skin names (storage-only for now):
    default, ares, mono, slate

Built-in personality names (storage-only for now):
    helpful, concise, technical, creative, teacher, hype
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

SKIN_NAMES: tuple[str, ...] = ("default", "ares", "mono", "slate")
PERSONALITY_NAMES: tuple[str, ...] = (
    "helpful", "concise", "technical", "creative", "teacher", "hype",
)


class SkinCommand(SlashCommand):
    name = "skin"
    description = "Get or set the active TUI skin (theme name)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("skin", "default")

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current skin: {current}\n"
                    f"Available: {', '.join(SKIN_NAMES)}\n"
                    f"(rendering follow-up; this only sets the name for now)"
                ),
                handled=True,
            )

        if sub not in SKIN_NAMES:
            return SlashCommandResult(
                output=(
                    f"Unknown skin {sub!r}. "
                    f"Available: {', '.join(SKIN_NAMES)}"
                ),
                handled=True,
            )

        runtime.custom["skin"] = sub
        return SlashCommandResult(
            output=f"Skin set to {sub}",
            handled=True,
        )


class PersonalityCommand(SlashCommand):
    name = "personality"
    description = "Get or set the active prompt-overlay personality"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("personality", "helpful")

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current personality: {current}\n"
                    f"Available: {', '.join(PERSONALITY_NAMES)}\n"
                    f"(prompt-overlay loading is a follow-up; this sets the name)"
                ),
                handled=True,
            )

        if sub not in PERSONALITY_NAMES:
            return SlashCommandResult(
                output=(
                    f"Unknown personality {sub!r}. "
                    f"Available: {', '.join(PERSONALITY_NAMES)}"
                ),
                handled=True,
            )

        runtime.custom["personality"] = sub
        return SlashCommandResult(
            output=f"Personality set to {sub}",
            handled=True,
        )


__all__ = ["SkinCommand", "PersonalityCommand"]
