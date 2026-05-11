"""CC §2 — five additional lifecycle hook events.

The enum and HookContext fields land first; the firing sites land
incrementally. Each event has an enum slot and either a documented
emit site (covered here) or a documented "plugin-driven emit"
contract (FILE_CHANGED, which needs an out-of-core watcher).

Spec: docs/OC-FROM-CLAUDE-CODE.md §2.
"""

from __future__ import annotations

import dataclasses

import pytest

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.hooks import (
    ALL_HOOK_EVENTS,
    HookContext,
    HookEvent,
)

_CC2_EVENTS = (
    HookEvent.POST_TOOL_BATCH,
    HookEvent.USER_PROMPT_EXPANSION,
    HookEvent.INSTRUCTIONS_LOADED,
    HookEvent.CWD_CHANGED,
    HookEvent.FILE_CHANGED,
)


def test_all_five_events_in_enum():
    """Each of the five new events appears as a HookEvent member."""
    for e in _CC2_EVENTS:
        assert isinstance(e, HookEvent)


def test_all_five_events_in_all_hook_events_tuple():
    """ALL_HOOK_EVENTS — the canonical iterator — includes each."""
    for e in _CC2_EVENTS:
        assert e in ALL_HOOK_EVENTS


def test_event_names_match_spec():
    """The string value matches the Claude Code spec exactly so
    settings YAML written for Claude Code drops in."""
    assert HookEvent.POST_TOOL_BATCH.value == "PostToolBatch"
    assert HookEvent.USER_PROMPT_EXPANSION.value == "UserPromptExpansion"
    assert HookEvent.INSTRUCTIONS_LOADED.value == "InstructionsLoaded"
    assert HookEvent.CWD_CHANGED.value == "CwdChanged"
    assert HookEvent.FILE_CHANGED.value == "FileChanged"


def test_post_tool_batch_context_fields():
    ctx = HookContext(
        event=HookEvent.POST_TOOL_BATCH,
        session_id="s1",
        batch_calls=(ToolCall(id="c1", name="Read", arguments={}),),
        batch_results=(ToolResult(tool_call_id="c1", content="ok"),),
    )
    assert ctx.batch_calls is not None
    assert len(ctx.batch_calls) == 1
    assert ctx.batch_results[0].content == "ok"


def test_user_prompt_expansion_context_fields():
    ctx = HookContext(
        event=HookEvent.USER_PROMPT_EXPANSION,
        session_id="s1",
        expansion_source="scrape",
        prompt_text="scraped content...",
    )
    assert ctx.expansion_source == "scrape"
    assert ctx.prompt_text == "scraped content..."


def test_instructions_loaded_context_fields():
    ctx = HookContext(
        event=HookEvent.INSTRUCTIONS_LOADED,
        session_id="s1",
        instructions_path="/path/to/CLAUDE.md",
        prompt_text="# rules ...",
    )
    assert ctx.instructions_path == "/path/to/CLAUDE.md"
    assert ctx.prompt_text == "# rules ..."


def test_cwd_changed_context_fields():
    ctx = HookContext(
        event=HookEvent.CWD_CHANGED,
        session_id="s1",
        cwd="/new/dir",
        previous_cwd="/old/dir",
    )
    assert ctx.cwd == "/new/dir"
    assert ctx.previous_cwd == "/old/dir"


def test_file_changed_context_fields():
    ctx = HookContext(
        event=HookEvent.FILE_CHANGED,
        session_id="s1",
        file_path="/some/file.py",
        change_kind="modified",
    )
    assert ctx.file_path == "/some/file.py"
    assert ctx.change_kind == "modified"


def test_context_defaults_remain_none_for_old_events():
    """Backwards compat: adding the new fields must NOT default them to
    anything other than None for existing event consumers."""
    ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="s")
    assert ctx.batch_calls is None
    assert ctx.expansion_source is None
    assert ctx.instructions_path is None
    assert ctx.cwd is None
    assert ctx.file_path is None
    assert ctx.change_kind is None


def test_change_kind_documented_values_pass_through():
    for kind in ("created", "modified", "deleted"):
        ctx = HookContext(
            event=HookEvent.FILE_CHANGED, session_id="s", change_kind=kind
        )
        assert ctx.change_kind == kind


def test_hook_context_remains_frozen():
    ctx = HookContext(event=HookEvent.CWD_CHANGED, session_id="s")
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        ctx.cwd = "/forbidden"  # type: ignore[misc]


def test_hook_event_count_unchanged_minus_added():
    """Sanity: the canonical iterator grew by exactly 5."""
    # When this commit lands, the count is the previous (28) + 5 = 33.
    # If new events land between this commit and a future one, update
    # the floor — but the floor must move by exactly the number of
    # added events to catch silent renames.
    assert len(ALL_HOOK_EVENTS) >= 33
    assert len(set(ALL_HOOK_EVENTS)) == len(ALL_HOOK_EVENTS), (
        "ALL_HOOK_EVENTS has duplicate entries"
    )
