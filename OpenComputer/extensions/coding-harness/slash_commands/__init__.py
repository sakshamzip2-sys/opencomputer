"""In-chat slash commands for the coding-harness.

Each command implements `SlashCommand` (from plugin_sdk, Phase 6f core add) OR
quacks the shape `{name: str, description: str, execute(args, runtime) -> str}`.
When the core SDK ships a formal SlashCommand base class the harness upgrades
to subclass it; until then, duck typing keeps these usable by the existing
plugin loader.

Commands bundled here:
    /plan         — enable plan mode
    /plan-off     — disable plan mode
    /accept-edits — toggle accept-edits mode
    /checkpoint   — manually save a named checkpoint
    /undo         — rewind one checkpoint (convenience alias for Rewind)
    /diff         — show changes vs latest checkpoint
"""
