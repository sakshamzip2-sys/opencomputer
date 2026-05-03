"""Tests for /sources slash command (port of /reasoning show pattern).

The /sources command is the actual interactive primitive for the
collapsed Sources block printed at finalize. The chevron glyph in the
trigger is decorative — Rich scrollback is immutable post-Live.stop —
so retroactive expansion goes through this command.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.sources_cmd import SourcesCommand
from opencomputer.cli_ui.reasoning_store import ReasoningStore, ReasoningTurn, ToolAction
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_runtime(store: ReasoningStore | None = None) -> RuntimeContext:
    custom: dict = {}
    if store is not None:
        custom["_reasoning_store"] = store
    return RuntimeContext(custom=custom)


_WEBSEARCH_MD = (
    "# Results for: india gdp  [provider: ddg]\n\n"
    "1. **India Q1 GDP up 8.4%**\n"
    "   https://indianexpress.com/x\n"
    "   GDP grew 8.4% YoY in Q1 2026.\n\n"
    "2. **AI in Indian fintech 2026**\n"
    "   https://pcquest.com/y\n"
    "   PCQuest reviews fintech AI trends.\n"
)


def _websearch_action() -> ToolAction:
    return ToolAction(
        name="WebSearch",
        args_preview="query=india gdp",
        ok=True,
        duration_s=0.4,
        output=_WEBSEARCH_MD,
    )


def _store_with_websearch_turn(turn_id: int = 1) -> ReasoningStore:
    """Build a ReasoningStore with one WebSearch turn that surfaced 2
    sources. ReasoningTurn.sources extracts from the markdown output the
    WebSearch tool emits (``N. **Title**\\n   url``), so we mirror that
    exact format in the ToolAction.output.

    ReasoningStore auto-increments turn_id starting at 1 — to land on
    a specific id we seed ``_next_id`` and append blanks until needed.
    """
    store = ReasoningStore()
    # Fast-forward the next id to the requested value (private attr is
    # the documented seam — peek_next_id() is the corresponding reader).
    store._next_id = turn_id  # noqa: SLF001
    store.append(thinking="", duration_s=0.5, tool_actions=(_websearch_action(),))
    return store


# ─── store wiring ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_store_returns_friendly_message() -> None:
    rt = _fresh_runtime()
    cmd = SourcesCommand()
    result = await cmd.execute("", rt)
    assert "no source history" in result.output.lower()


@pytest.mark.asyncio
async def test_empty_store_returns_no_turns_yet() -> None:
    rt = _fresh_runtime(ReasoningStore())
    cmd = SourcesCommand()
    result = await cmd.execute("", rt)
    assert "no turns recorded yet" in result.output.lower()


# ─── default (last turn) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_args_shows_last_turn_expanded() -> None:
    store = _store_with_websearch_turn(turn_id=3)
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("", rt)
    # Header (turn id) + expanded chevron + per-source titles all present.
    assert "turn #3" in result.output
    assert "Used 2 sources" in result.output
    assert "⌄" in result.output                  # expanded chevron
    assert "India Q1 GDP" in result.output
    assert "AI in Indian fintech" in result.output


@pytest.mark.asyncio
async def test_last_keyword_equivalent_to_no_args() -> None:
    """Both invocations target the same turn — visually identical
    (OSC 8 ``id=`` counters differ between calls; we strip them)."""
    import re

    def _strip_osc8_ids(s: str) -> str:
        # Rich emits OSC 8 with auto-incrementing ``id=NNN`` cookies;
        # compare the rendered content sans cookie noise.
        return re.sub(r"id=\d+;", "id=;", s)

    store = _store_with_websearch_turn(turn_id=7)
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result_default = await cmd.execute("", rt)
    result_last = await cmd.execute("last", rt)
    assert _strip_osc8_ids(result_default.output) == _strip_osc8_ids(
        result_last.output
    )


# ─── show <N> ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_specific_turn_id() -> None:
    store = _store_with_websearch_turn(turn_id=42)
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("42", rt)
    assert "turn #42" in result.output
    assert "indianexpress.com" in result.output


@pytest.mark.asyncio
async def test_show_unknown_turn_id_lists_known() -> None:
    store = _store_with_websearch_turn(turn_id=1)
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("99", rt)
    assert "no turn #99" in result.output
    assert "[1]" in result.output                # known turn list


# ─── show all ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_all_renders_every_turn_with_sources() -> None:
    # Three turns: 1 + 3 with WebSearch sources, 2 with only thinking.
    store = ReasoningStore()
    store.append(thinking="", duration_s=0.5, tool_actions=(_websearch_action(),))
    store.append(thinking="just thinking, no tools", duration_s=0.1, tool_actions=())
    store.append(thinking="", duration_s=0.5, tool_actions=(_websearch_action(),))
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("all", rt)
    assert "turn #1" in result.output
    assert "turn #3" in result.output
    # Turn #2 had no sources — must NOT appear.
    assert "turn #2" not in result.output


# ─── turn with no sources ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_with_no_sources_shows_friendly_message() -> None:
    store = ReasoningStore()
    store.append(
        thinking="reasoned but no web tools used",
        duration_s=0.1,
        tool_actions=(),
    )
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("", rt)
    assert "no web sources recorded" in result.output


# ─── invalid args ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_arg_shows_usage() -> None:
    store = _store_with_websearch_turn()
    rt = _fresh_runtime(store)
    cmd = SourcesCommand()
    result = await cmd.execute("unknown-arg", rt)
    assert "Usage" in result.output
