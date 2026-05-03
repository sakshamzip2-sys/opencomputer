"""StreamingRenderer must render thinking chunks live in a panel above
the answer, and respect ``runtime.custom['show_reasoning']`` on finalize."""
from __future__ import annotations

import io

from rich.console import Console

from opencomputer.cli_ui.streaming import StreamingRenderer


def _make_renderer() -> tuple[StreamingRenderer, io.StringIO, Console]:
    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=True, record=True)
    r = StreamingRenderer(console)
    return r, buf, console


def test_on_thinking_chunk_appends_to_internal_buffer() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("step 1; ")
        r.on_thinking_chunk("step 2")
        # Internal buffer is private but accessible for white-box test.
        assert "".join(getattr(r, "_thinking_buffer", [])) == "step 1; step 2"


def test_on_thinking_chunk_records_started_at_on_first_chunk() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        assert getattr(r, "_thinking_started_at", 0.0) == 0.0
        r.on_thinking_chunk("hi")
        assert getattr(r, "_thinking_started_at", 0.0) > 0.0


def test_on_thinking_chunk_empty_string_is_noop() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("")
        assert getattr(r, "_thinking_buffer", []) == []


def test_render_includes_thinking_panel_when_buffer_non_empty() -> None:
    """White-box: _render() output must reference Thinking when buffer
    has chunks. Also verifies the panel renders FIRST in the Group
    (above the answer markdown)."""
    from rich.panel import Panel

    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("reasoning content")
        group = r._render()  # type: ignore[attr-defined]
        # Thinking panel must be the first renderable so it appears
        # above the answer markdown when both are present.
        assert isinstance(group.renderables[0], Panel)
        titles = [
            str(c.title)
            for c in group.renderables
            if isinstance(c, Panel) and c.title is not None
        ]
        assert any("Thinking" in t for t in titles), (
            f"expected a Thinking panel, got titles={titles}"
        )


def test_finalize_collapses_to_summary_when_show_reasoning_false() -> None:
    """When the runtime flag is OFF (default), finalize must print the
    one-line summary — NOT the full panel.

    Tests finalize in isolation (no preceding on_thinking_chunk live
    stream): Rich.Live with ``record=True`` captures every intermediate
    frame, which would conflate live-panel-output with finalize-output.
    The live behavior is covered by ``test_render_includes_thinking_panel_*``;
    this test owns the finalize behavior alone.
    """
    r, _buf, _con = _make_renderer()
    with r:
        r.finalize(
            reasoning="verbose internal reasoning details",
            iterations=1,
            in_tok=10,
            out_tok=2,
            elapsed_s=3.2,
            show_reasoning=False,
        )
    out = _con.export_text()
    # Detail body must NOT appear when collapsed.
    assert "verbose internal reasoning details" not in out
    # A summary line must appear.
    assert "Thought" in out or "💭" in out


def test_finalize_keeps_full_panel_when_show_reasoning_true() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.finalize(
            reasoning="verbose internal reasoning details",
            iterations=1,
            in_tok=10,
            out_tok=2,
            elapsed_s=3.2,
            show_reasoning=True,
        )
    out = _con.export_text()
    assert "verbose internal reasoning details" in out


def test_finalize_no_thinking_emits_nothing_about_reasoning() -> None:
    """If reasoning is None/empty, finalize must not print any thinking
    summary or panel — current callers rely on this."""
    r, _buf, _con = _make_renderer()
    with r:
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=5,
            out_tok=2,
            elapsed_s=0.4,
            show_reasoning=False,
        )
    out = _con.export_text()
    assert "Thought" not in out
    assert "💭" not in out


def test_chat_loop_passes_thinking_callback_to_step_once() -> None:
    """The CLI's ``_build_thinking_callback`` helper forwards each
    thinking-delta chunk to the underlying renderer hook."""
    from opencomputer.cli import _build_thinking_callback

    captured: list[str] = []
    cb = _build_thinking_callback(captured.append)
    cb("a")
    cb("b")
    assert captured == ["a", "b"]


# ─── Reasoning Dropdown v2 — unbounded tool history ─────────────────────


def test_renderer_records_unbounded_tool_history() -> None:
    """Tool-call panel evicts after 3 visible rows (_TOOL_PANEL_MAX_ROWS).
    The parallel _tool_history must keep ALL completed calls so the
    /reasoning show tree can render the full action sequence.
    """
    r, _buf, _con = _make_renderer()
    with r:
        for i in range(5):
            idx = r.on_tool_start(f"Tool{i}", f"arg{i}")
            r.on_tool_end(f"Tool{i}", idx, ok=(i % 2 == 0))

    history = r.tool_history()
    assert [a.name for a in history] == [f"Tool{i}" for i in range(5)]
    assert [a.ok for a in history] == [True, False, True, False, True]
    # Visible panel still capped at 3.
    assert len(r._tool_calls) == 3


# ─── Reasoning Dropdown v2 — push to ReasoningStore on finalize ─────────


def test_finalize_pushes_turn_into_reasoning_store() -> None:
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    store = ReasoningStore()
    renderer = StreamingRenderer(
        Console(file=io.StringIO()), reasoning_store=store
    )
    with renderer:
        renderer.on_thinking_chunk("Let me ")
        renderer.on_thinking_chunk("think...")
        idx = renderer.on_tool_start("Read", "foo.py")
        renderer.on_tool_end("Read", idx, ok=True)
        renderer.finalize(
            reasoning="Let me think...",
            iterations=1,
            in_tok=10,
            out_tok=20,
            elapsed_s=1.5,
            show_reasoning=False,
        )

    turn = store.get_latest()
    assert turn is not None
    assert turn.turn_id == 1
    assert turn.thinking == "Let me think..."
    assert turn.action_count == 1
    assert turn.tool_actions[0].name == "Read"


def test_finalize_skips_store_push_when_no_store_attached() -> None:
    """Backwards compat: existing callers that don't pass a store must
    keep working without crashing."""
    renderer = StreamingRenderer(Console(file=io.StringIO()))  # no store
    with renderer:
        renderer.finalize(
            reasoning="x",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    # No exception; nothing else to assert.


def test_finalize_records_turn_even_without_thinking() -> None:
    """Tool-only turns (no extended-thinking) must still be recorded
    so /reasoning show all shows them."""
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    store = ReasoningStore()
    renderer = StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store)
    with renderer:
        idx = renderer.on_tool_start("Bash", "ls")
        renderer.on_tool_end("Bash", idx, ok=True)
        renderer.finalize(
            reasoning=None,
            iterations=1,
            in_tok=5,
            out_tok=5,
            elapsed_s=0.5,
            show_reasoning=False,
        )
    turn = store.get_latest()
    assert turn is not None
    assert turn.thinking == ""
    assert turn.action_count == 1


def test_finalize_skips_empty_no_op_turn() -> None:
    """A turn with neither thinking nor tool calls is a no-op and
    should NOT pollute /reasoning show all with empty entries."""
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    store = ReasoningStore()
    renderer = StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store)
    with renderer:
        renderer.finalize(
            reasoning=None,
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    assert store.get_all() == []


# ─── Reasoning Dropdown v2 — collapsed-line format with turn id ─────────


def test_collapsed_line_includes_turn_id_and_action_count() -> None:
    import re

    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    renderer = StreamingRenderer(
        Console(file=out, force_terminal=False), reasoning_store=store
    )
    with renderer:
        renderer.on_thinking_chunk("hmm")
        idx1 = renderer.on_tool_start("Read", "a")
        renderer.on_tool_end("Read", idx1, ok=True)
        idx2 = renderer.on_tool_start("Edit", "b")
        renderer.on_tool_end("Edit", idx2, ok=True)
        renderer.finalize(
            reasoning="hmm",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    text = out.getvalue()
    assert re.search(r"turn #1", text), text
    assert re.search(r"2 actions", text), text
    assert "/reasoning show to expand" in text


def test_collapsed_line_omits_turn_id_when_store_missing() -> None:
    """Backwards compat: legacy callers without a store keep the old
    format without turn id."""
    out = io.StringIO()
    renderer = StreamingRenderer(Console(file=out, force_terminal=False))
    with renderer:
        renderer.on_thinking_chunk("hmm")
        renderer.finalize(
            reasoning="hmm",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    text = out.getvalue()
    assert "turn #" not in text
    assert "/reasoning show to expand" in text


def test_collapsed_line_singular_action_no_plural_s() -> None:
    """Cosmetic: '1 action' not '1 actions'."""
    import re

    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    renderer = StreamingRenderer(
        Console(file=out, force_terminal=False), reasoning_store=store
    )
    with renderer:
        renderer.on_thinking_chunk("x")
        idx = renderer.on_tool_start("Read", "a")
        renderer.on_tool_end("Read", idx, ok=True)
        renderer.finalize(
            reasoning="x",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    text = out.getvalue()
    assert re.search(r"\b1 action\b", text), text
    assert "1 actions" not in text


# ─── v2: Thinking History UI — collapsed-line summary ────────────────────


def test_finalize_collapsed_line_includes_summary_when_available() -> None:
    """When the inline summary thread completes within the 1.5s join,
    the collapsed line includes the summary as a bold lead-in."""
    from unittest.mock import patch

    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()

    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value="Wrote a haiku about sloths",
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("let me think about haikus")
            renderer.finalize(
                reasoning="let me think about haikus",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    assert "Wrote a haiku about sloths" in text
    t = store.get_by_id(1)
    assert t is not None and t.summary == "Wrote a haiku about sloths"


def test_finalize_collapsed_line_falls_back_when_summary_unavailable() -> None:
    """When generate_summary returns None (LLM failure), the collapsed
    line falls back to today's metadata-only format."""
    from unittest.mock import patch

    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value=None,
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("hmm")
            renderer.finalize(
                reasoning="hmm",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    assert "Thought for" in text
    assert "/reasoning show" in text
    t = store.get_by_id(1)
    assert t is not None and t.summary is None


def test_finalize_skips_summary_when_show_reasoning_true() -> None:
    """When show_reasoning=True (full panel mode), no summary call —
    the panel itself is the rich view."""
    from unittest.mock import patch

    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    call_count = {"n": 0}

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return None

    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        side_effect=_counting,
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("x")
            renderer.finalize(
                reasoning="x",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=True,
            )
    assert call_count["n"] == 0, "summary thread should NOT spawn when show_reasoning=True"


def test_finalize_no_summary_thread_when_no_store() -> None:
    """No store attached → no summary thread (nowhere to write the
    result back)."""
    from unittest.mock import patch

    out = io.StringIO()
    call_count = {"n": 0}

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return None

    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        side_effect=_counting,
    ):
        renderer = StreamingRenderer(Console(file=out, force_terminal=False))
        with renderer:
            renderer.on_thinking_chunk("x")
            renderer.finalize(
                reasoning="x",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=False,
            )
    assert call_count["n"] == 0
