"""SkinSpec: colors + spinner verbs + branding + tool prefix."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SkinSpec:
    """Visual theme for the chat REPL.

    Fields are flat (not nested dataclasses) so YAML round-trip is
    trivial and missing keys can be filled by ``default.yaml`` with a
    one-level dict merge.
    """

    name: str
    description: str
    colors: dict[str, str]                      # hex strings, keyed by Rich style name

    spinner_thinking_verbs: tuple[str, ...]     # ("thinking", "pondering", ...)
    spinner_wings: tuple[tuple[str, str], ...]  # decoration around spinner glyph
    # Hermes v2 D5 (2026-05-08) — distinct face cycles for two phases:
    # ``waiting_faces`` cycles while the agent is awaiting an HTTP
    # response from the provider (network round-trip — no text yet);
    # ``thinking_faces`` cycles once the model starts emitting
    # reasoning content. Both are optional — empty tuple keeps the
    # legacy single-face-pool behavior. Hermes' default skin uses
    # face glyphs like ``(⊙‿⊙)`` waiting → ``(◉‿◉)`` thinking.
    spinner_waiting_faces: tuple[str, ...] = ()
    spinner_thinking_faces: tuple[str, ...] = ()

    agent_name: str = "OpenComputer"
    response_label: str = " ✦ OC "
    prompt_symbol: str = ">"

    banner_logo: str = ""                       # rich-markup ascii (may be empty)
    banner_hero: str = ""                       # rich-markup ascii (may be empty)

    tool_prefix: str = "┊"
    tool_emojis: dict[str, str] = field(default_factory=dict)


__all__ = ["SkinSpec"]
