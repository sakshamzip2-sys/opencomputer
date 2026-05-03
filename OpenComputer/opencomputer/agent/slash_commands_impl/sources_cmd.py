"""``/sources [N|all|last]`` — retroactively expand a turn's web sources.

The actual interactive primitive for the Sources block (mirrors how
``/reasoning show`` is the actual primitive for the reasoning card).

The collapsed Sources trigger printed at finalize is plain text in
scrollback — the chevron glyph is decorative, not a click handler.
This command re-renders any past turn's sources from the per-turn
``ReasoningStore`` (already populated by the streaming renderer; see
``ReasoningTurn.sources`` which extracts from WebSearch/WebFetch tool
output).

Examples::

    /sources             → expand the LAST turn's sources
    /sources 5           → expand turn #5 only
    /sources all         → expand every turn that had sources

Output mirrors :func:`opencomputer.cli_ui.sources.render_sources_block`
in ``open=True`` mode — the same per-source rows you'd see in a
default-expanded block, but rendered on demand instead of at finalize.
"""

from __future__ import annotations

import re

from opencomputer.cli_ui.reasoning_store import (
    ReasoningStore,
    ReasoningTurn,
)
from opencomputer.cli_ui.reasoning_store import Source as StoreSource
from opencomputer.cli_ui.sources import (
    Source,
    enrich_url,
    render_sources_block,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_USAGE = (
    "Usage: /sources [N|all|last]\n"
    "  (no args)    → expand the last turn's sources\n"
    "  <N>          → expand turn #N's sources\n"
    "  all          → expand every turn that recorded sources"
)

_SHOW_ID_PATTERN = re.compile(r"^(\d+)$")


def _get_store(runtime: RuntimeContext) -> ReasoningStore | None:
    """Same accessor pattern as ``/reasoning`` — both commands attach
    to the same per-session ReasoningStore via ``runtime.custom``."""
    candidate = runtime.custom.get("_reasoning_store")
    return candidate if isinstance(candidate, ReasoningStore) else None


def _to_render_source(s: StoreSource) -> Source:
    """Adapt :class:`opencomputer.cli_ui.reasoning_store.Source`
    (``href``-based, used by SourcesView in reasoning_view) to
    :class:`opencomputer.cli_ui.sources.Source` (the AI-Elements +
    Anthropic-shaped class used by ``render_sources_block``).

    Both wrap the same underlying URL — the dual class shape is a
    legacy of two parallel ports landing in the same week. This adapter
    keeps the user-visible render consistent with the streaming-time
    Sources block.
    """
    return enrich_url(
        s.href,
        title=s.title,
        snippet=s.snippet or "",
    )


def _render_turn_sources_to_text(turn: ReasoningTurn) -> str:
    """Render one turn's sources as text (for SlashCommandResult.output).

    Uses a recording :class:`rich.console.Console` so we get the same
    OSC 8 hyperlinks + dim styles as the streaming-time block, but
    captured into a string the slash dispatcher can return.
    """
    from rich.console import Console

    # ``highlight=False`` keeps the turn header as one literal span —
    # otherwise Rich's default number-highlighter splits "turn #42" into
    # "turn #" + "42" with separate styling, breaking literal substring
    # asserts and visually fragmenting the header.
    rec = Console(record=True, width=120, force_terminal=True, highlight=False)
    sources = [_to_render_source(s) for s in turn.sources]
    if not sources:
        return f"turn #{turn.turn_id}: no web sources recorded."
    rec.print(f"[dim]turn #{turn.turn_id}[/dim]")
    render_sources_block(rec, sources, open=True)
    return rec.export_text(styles=True, clear=False).rstrip()


class SourcesCommand(SlashCommand):
    name = "sources"
    description = "Expand web sources from a past turn (collapsed by default at finalize)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()

        store = _get_store(runtime)
        if store is None:
            return SlashCommandResult(
                output=(
                    "no source history available "
                    "(reasoning store not attached to this session)."
                ),
                handled=True,
            )

        # --- show last (default) ---
        if sub in ("", "last"):
            turn = store.get_latest()
            if turn is None:
                return SlashCommandResult(
                    output="no turns recorded yet — run a search first.",
                    handled=True,
                )
            return SlashCommandResult(
                output=_render_turn_sources_to_text(turn), handled=True
            )

        # --- show all ---
        if sub == "all":
            turns = [t for t in store.get_all() if t.sources]
            if not turns:
                return SlashCommandResult(
                    output="no turns with web sources recorded yet.",
                    handled=True,
                )
            blocks = [_render_turn_sources_to_text(t) for t in turns]
            return SlashCommandResult(
                output="\n\n".join(blocks), handled=True
            )

        # --- show <N> ---
        m = _SHOW_ID_PATTERN.match(sub)
        if m:
            turn_id = int(m.group(1))
            turn = store.get_by_id(turn_id)
            if turn is None:
                known = [t.turn_id for t in store.get_all()]
                known_str = str(known) if known else "none"
                return SlashCommandResult(
                    output=(
                        f"no turn #{turn_id} in store "
                        f"(known turns: {known_str})."
                    ),
                    handled=True,
                )
            return SlashCommandResult(
                output=_render_turn_sources_to_text(turn), handled=True
            )

        return SlashCommandResult(output=_USAGE, handled=True)


__all__ = ["SourcesCommand"]
