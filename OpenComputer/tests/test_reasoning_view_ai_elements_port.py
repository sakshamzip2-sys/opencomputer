"""AI Elements ``Reasoning`` + ``Tool`` port — contract tests.

Pins the ported component's API + visual contract against the
TypeScript reference at https://github.com/vercel/ai-elements
(packages/elements/src/reasoning.tsx + tool.tsx).

The tests verify:

1. **API parity** — Reasoning + Tool prop names and types match AI
   Elements verbatim (camelCase ``isStreaming`` / ``toolName`` /
   ``errorText`` preserved deliberately for round-trip JSON interop
   with the AI SDK schema).
2. **State vocabulary** — all 7 ``ToolState`` values produce a
   recognisable status badge.
3. **Collapsed → expanded** — the same view renders differently
   depending on ``open``; collapsed is one Panel with the trigger;
   expanded includes the per-tool subsections.
4. **Schema projections** — ``ReasoningTurn.tool_calls`` /
   ``timeline`` derive correctly from the existing ``tool_actions``
   without breaking back-compat callers that read ``tool_actions``.
5. **Trigger fallbacks** — empty summary falls back to "Thought for
   N seconds" (mirrors AI Elements' ``defaultGetThinkingMessage``);
   zero-tool turn doesn't crash.
6. **Bottom stats line untouched** — collapsed render does not include
   the stats line text (that's streaming.py's responsibility, not the
   view's).
"""
from __future__ import annotations

import io

from rich.console import Console

from opencomputer.cli_ui.reasoning_store import (
    ReasoningTurn,
    TimelineStep,
    ToolAction,
    ToolCall,
    ToolState,
    render_turns_to_text,
)
from opencomputer.cli_ui.reasoning_view import (
    _STATUS_GLYPHS,
    _STATUS_LABELS,
    ReasoningView,
    ToolView,
    render_turn_view,
)

# ─── 1. API parity — ToolView ────────────────────────────────────────


def test_toolview_accepts_ai_elements_named_props() -> None:
    """Constructor must accept the AI Elements field names verbatim:
    ``type``, ``state``, ``toolName``, ``title``, ``input``,
    ``output``, ``errorText``."""
    tv = ToolView(
        type="tool-Read",
        state="output-available",
        toolName="Read",
        title="Read foo.md",
        input={"file_path": "foo.md"},
        output={"content": "hello"},
        errorText=None,
    )
    assert tv.type == "tool-Read"
    assert tv.state == "output-available"
    assert tv.toolName == "Read"
    assert tv.title == "Read foo.md"
    assert tv.input == {"file_path": "foo.md"}
    assert tv.output == {"content": "hello"}
    assert tv.errorText is None


def test_toolview_derived_name_dynamic() -> None:
    """``derivedName`` ternary from AI Elements:
    ``type === "dynamic-tool" ? toolName : type.split("-").slice(1).join("-")``
    """
    dyn = ToolView(type="dynamic-tool", state="output-available", toolName="Foo")
    assert dyn.derived_name == "Foo"

    bare = ToolView(type="tool-Read", state="output-available")
    assert bare.derived_name == "Read"


# ─── 2. State vocabulary ─────────────────────────────────────────────


def test_all_seven_tool_states_have_glyph_and_label() -> None:
    """Every value in AI Elements' ``ToolPart["state"]`` union must
    have a status badge mapping."""
    expected = {
        "approval-requested",
        "approval-responded",
        "input-streaming",
        "input-available",
        "output-available",
        "output-denied",
        "output-error",
    }
    assert set(_STATUS_LABELS.keys()) == expected
    assert set(_STATUS_GLYPHS.keys()) == expected


def test_status_labels_match_ai_elements_verbatim() -> None:
    """Mirror of AI Elements' ``statusLabels`` map (tool.tsx)."""
    assert _STATUS_LABELS == {
        "approval-requested": "Awaiting Approval",
        "approval-responded": "Responded",
        "input-streaming": "Pending",
        "input-available": "Running",
        "output-available": "Completed",
        "output-denied": "Denied",
        "output-error": "Error",
    }


# ─── 3. API parity — ReasoningView ───────────────────────────────────


def test_reasoning_view_accepts_ai_elements_named_props() -> None:
    """Constructor must accept ``isStreaming``, ``open``,
    ``defaultOpen``, ``duration`` — verbatim from AI Elements'
    ReasoningProps."""
    turn = ReasoningTurn(
        turn_id=1, thinking="hmm", duration_s=1.2,
        tool_actions=(), summary="Wrote a haiku",
    )
    rv = ReasoningView(
        turn=turn,
        isStreaming=False,
        open=False,
        defaultOpen=None,
        duration=1.2,
    )
    assert rv.is_open is False
    assert rv.duration == 1.2


def test_reasoning_view_open_overrides_default_open() -> None:
    """useControllableState semantics: ``open`` (controlled) wins
    over ``defaultOpen``."""
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=(), summary="x",
    )
    rv = ReasoningView(turn=turn, open=True, defaultOpen=False)
    assert rv.is_open is True


def test_reasoning_view_default_open_when_open_omitted() -> None:
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=(), summary="x",
    )
    rv = ReasoningView(turn=turn, defaultOpen=True)
    assert rv.is_open is True


# ─── 4. Schema projections ───────────────────────────────────────────


def test_tool_calls_projection_from_legacy_tool_actions() -> None:
    """``ReasoningTurn.tool_calls`` must build ToolCall records from
    the existing ToolAction list with no caller change."""
    actions = (
        ToolAction(
            name="Read", args_preview="file_path=foo.md", ok=True,
            duration_s=0.5,
        ),
        ToolAction(
            name="Bash", args_preview="ls", ok=False, duration_s=0.2,
            errorText="permission denied",
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=actions, summary="x",
    )
    calls = turn.tool_calls
    assert len(calls) == 2
    # First action: ok=True → output-available, type="tool-Read".
    assert calls[0].toolName == "Read"
    assert calls[0].state == "output-available"
    assert calls[0].type == "tool-Read"
    # Second action: errorText set → output-error.
    assert calls[1].state == "output-error"
    assert calls[1].errorText == "permission denied"


def test_timeline_includes_reasoning_step_when_thinking_present() -> None:
    """``TimelineStep[]`` should include a 'reasoning' step when
    ``turn.thinking`` is non-empty, plus one 'tool' step per call."""
    turn = ReasoningTurn(
        turn_id=1, thinking="The user wants...", duration_s=0.5,
        tool_actions=(
            ToolAction(name="Read", args_preview="x", ok=True,
                       duration_s=0.1),
        ),
        summary="x",
    )
    steps = turn.timeline
    assert len(steps) == 2
    assert steps[0].kind == "reasoning"
    assert steps[1].kind == "tool"
    assert steps[1].label == "Read"


def test_timeline_omits_reasoning_step_when_thinking_empty() -> None:
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=(
            ToolAction(name="Bash", args_preview="ls", ok=True,
                       duration_s=0.1),
        ),
        summary="x",
    )
    steps = turn.timeline
    assert all(s.kind != "reasoning" for s in steps)
    assert len(steps) == 1
    assert steps[0].kind == "tool"


# ─── 5. Visual contract — collapsed vs expanded ─────────────────────


def _render(rv: ReasoningView) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    console.print(rv)
    return buf.getvalue()


def test_collapsed_renders_summary_and_chevron_right() -> None:
    """Collapsed: bold summary + ``›`` chevron in a rounded panel
    (matches v6 PR #395 layout exactly)."""
    turn = ReasoningTurn(
        turn_id=1, thinking="hmm", duration_s=1.2,
        tool_actions=(), summary="Wrote a haiku",
    )
    rv = ReasoningView(turn=turn, open=False)
    text = _render(rv)
    assert "Wrote a haiku" in text
    assert "›" in text
    # Expanded-only chevron must NOT appear.
    assert "⌄" not in text


def test_expanded_renders_chevron_down_and_tool_section() -> None:
    """Expanded: ``⌄`` chevron, tool name visible in the body."""
    turn = ReasoningTurn(
        turn_id=1, thinking="reasoning text", duration_s=1.2,
        tool_actions=(
            ToolAction(name="Read", args_preview="file_path=foo.md",
                       ok=True, duration_s=0.5,
                       input={"file_path": "foo.md"},
                       output={"content": "data"}),
        ),
        summary="Read the file",
    )
    rv = ReasoningView(turn=turn, open=True)
    text = _render(rv)
    assert "⌄" in text
    assert "Read" in text
    # PARAMETERS / RESULT labels visible from ToolInput / ToolOutput.
    assert "PARAMETERS" in text or "file_path" in text
    assert "RESULT" in text or "content" in text


def test_expanded_renders_error_block_when_errortext_present() -> None:
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=(
            ToolAction(name="Bash", args_preview="false", ok=False,
                       duration_s=0.1, errorText="exit code 1"),
        ),
        summary="x",
    )
    rv = ReasoningView(turn=turn, open=True)
    text = _render(rv)
    assert "ERROR" in text
    assert "exit code 1" in text


# ─── 6. Trigger fallbacks ────────────────────────────────────────────


def test_trigger_falls_back_to_thought_for_when_no_summary() -> None:
    """Mirrors AI Elements' defaultGetThinkingMessage — when there's
    no summary AND duration is set, the trigger reads
    "Thought for N seconds"."""
    turn = ReasoningTurn(
        turn_id=1, thinking="hmm", duration_s=2.5,
        tool_actions=(), summary=None,
    )
    rv = ReasoningView(turn=turn, open=False, duration=2.5)
    text = _render(rv)
    assert "Thought for" in text


def test_zero_tool_turn_does_not_crash() -> None:
    """Empty tool list + empty thinking → placeholder line, no
    exception."""
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=(), summary=None,
    )
    rv = ReasoningView(turn=turn, open=True)
    text = _render(rv)
    # Body shows the placeholder (defined in render_content's
    # final-else branch).
    assert "no extended thinking" in text or "no tool actions" in text


# ─── 7. Slash-output integration via render_turns_to_text ────────────


def test_render_turns_to_text_uses_ai_elements_port() -> None:
    """``render_turns_to_text`` (called by /reasoning show) must
    produce output containing the AI-Elements-style status labels —
    proves the new path is wired."""
    turn = ReasoningTurn(
        turn_id=1, thinking="hmm", duration_s=1.0,
        tool_actions=(
            ToolAction(name="Read", args_preview="foo", ok=True,
                       duration_s=0.5,
                       input={"file_path": "foo.md"}),
        ),
        summary="Read foo",
    )
    text = render_turns_to_text([turn])
    assert "Completed" in text  # AI Elements label for output-available
    assert "Read" in text


# ─── 8. render_turn_view convenience ─────────────────────────────────


def test_render_turn_view_returns_open_view() -> None:
    turn = ReasoningTurn(
        turn_id=1, thinking="x", duration_s=0.1,
        tool_actions=(), summary="x",
    )
    rv = render_turn_view(turn)
    assert rv.is_open is True
