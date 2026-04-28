"""Tests for ClarifyTool — thin agent-callable wrapper that asks the user
to disambiguate when there are 2-4 plausible interpretations of a request.

Sub-project 1.G of the openclaw-tier1 plan
(`docs/superpowers/plans/2026-04-28-openclaw-tier1-port.md`).

Reuses the same machinery as `AskUserQuestionTool`: presents the ambiguity
text + the option list to the user (via stdin in CLI mode), returns the
chosen option, errors out in async-channel ("gateway") mode where there's
no synchronous user.
"""

from __future__ import annotations

import io

import pytest

from plugin_sdk.core import ToolCall


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t-1", name="Clarify", arguments=args)


# ─── Schema sanity ─────────────────────────────────────────────────────


def test_clarify_schema_shape() -> None:
    """The schema should advertise both `ambiguity` and `options`, with
    `options` constrained to 2-4 string items."""
    from opencomputer.tools.clarify import ClarifyTool

    schema = ClarifyTool().schema
    assert schema.name == "Clarify"
    props = schema.parameters["properties"]
    assert "ambiguity" in props
    assert "options" in props
    assert props["options"]["minItems"] == 2
    assert props["options"]["maxItems"] == 4
    assert set(schema.parameters["required"]) == {"ambiguity", "options"}


# ─── Happy path ────────────────────────────────────────────────────────


async def test_clarify_returns_selected_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the user picks option 2, the tool returns that option's text
    in the result content (no error)."""
    from opencomputer.tools.clarify import ClarifyTool

    # User types "2" — picks the second option.
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    monkeypatch.setattr("sys.stderr", io.StringIO())

    tool = ClarifyTool(cli_mode=True)
    res = await tool.execute(
        _call({
            "ambiguity": "Which file did you mean?",
            "options": ["foo.py", "bar.py", "baz.py"],
        })
    )
    assert not res.is_error
    assert res.tool_call_id == "t-1"
    # Returns the chosen option text.
    assert "bar.py" in res.content


async def test_clarify_returns_freeform_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user types free-form instead of a numeric choice, the
    raw answer flows through (covers the 'none of the above' case)."""
    from opencomputer.tools.clarify import ClarifyTool

    monkeypatch.setattr("sys.stdin", io.StringIO("actually neither — qux.py\n"))
    monkeypatch.setattr("sys.stderr", io.StringIO())

    tool = ClarifyTool(cli_mode=True)
    res = await tool.execute(
        _call({
            "ambiguity": "Which file?",
            "options": ["foo.py", "bar.py"],
        })
    )
    assert not res.is_error
    assert "qux.py" in res.content


# ─── Validation ────────────────────────────────────────────────────────


async def test_clarify_rejects_too_few_options() -> None:
    """A single option isn't an ambiguity — should error."""
    from opencomputer.tools.clarify import ClarifyTool

    tool = ClarifyTool(cli_mode=True)
    res = await tool.execute(
        _call({
            "ambiguity": "X?",
            "options": ["only-one"],
        })
    )
    assert res.is_error
    assert "options" in res.content.lower()


async def test_clarify_rejects_too_many_options() -> None:
    """5+ options is not "genuine ambiguity" — should error."""
    from opencomputer.tools.clarify import ClarifyTool

    tool = ClarifyTool(cli_mode=True)
    res = await tool.execute(
        _call({
            "ambiguity": "X?",
            "options": ["a", "b", "c", "d", "e"],
        })
    )
    assert res.is_error
    assert "options" in res.content.lower()


async def test_clarify_rejects_empty_ambiguity() -> None:
    """Ambiguity description is required — empty should error."""
    from opencomputer.tools.clarify import ClarifyTool

    tool = ClarifyTool(cli_mode=True)
    res = await tool.execute(
        _call({
            "ambiguity": "",
            "options": ["a", "b"],
        })
    )
    assert res.is_error


# ─── Handler unavailable (gateway / async channel mode) ────────────────


async def test_clarify_propagates_handler_unavailable() -> None:
    """In async-channel mode (no synchronous user available), the tool
    must return is_error=True so the agent knows to use a different
    strategy (PushNotification + wait for next inbound message)."""
    from opencomputer.tools.clarify import ClarifyTool

    tool = ClarifyTool(cli_mode=False)
    res = await tool.execute(
        _call({
            "ambiguity": "Which one?",
            "options": ["a", "b"],
        })
    )
    assert res.is_error
    assert res.tool_call_id == "t-1"


# ─── Registry registration ────────────────────────────────────────────


def test_clarify_registered_with_builtin_tools() -> None:
    """The CLI's `_register_builtin_tools` must include Clarify."""
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    assert "Clarify" in set(registry.names())
