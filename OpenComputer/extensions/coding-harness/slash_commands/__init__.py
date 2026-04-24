"""In-chat slash commands for the coding-harness.

Each command subclasses ``plugin_sdk.SlashCommand`` (Phase 12b6 Task D8
formalization of the earlier Phase 6f duck-typed contract). The harness
base in ``base.py`` extends the SDK's SlashCommand to bind a shared
``HarnessContext`` at construction time; the public
``execute(args, runtime) -> SlashCommandResult`` signature is what the
core dispatcher calls.

Commands bundled here:
    /plan         — enable plan mode
    /plan-off     — disable plan mode
    /accept-edits — toggle accept-edits mode
    /checkpoint   — manually save a named checkpoint
    /undo         — rewind one checkpoint (convenience alias for Rewind)
    /diff         — show changes vs latest checkpoint
"""
