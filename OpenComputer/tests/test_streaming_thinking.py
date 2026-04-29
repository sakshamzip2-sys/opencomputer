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
