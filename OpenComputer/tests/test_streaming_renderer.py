"""Grok-style streaming renderer (Round 5).

Mocks Rich's Live so tests don't depend on a TTY. The renderer's
contract is observable via:
- the module-level ``current_renderer()`` sentinel
- the captured Console output (use ``Console(record=True)``)
- the internal buffer / tool-call ordered dict (white-box for state
  transitions; black-box console output for rendering correctness)
"""
from __future__ import annotations

from rich.console import Console


def _make_console() -> Console:
    """Recording console with a fixed width so output is deterministic."""
    return Console(record=True, width=120, force_terminal=True)


def test_current_renderer_is_none_outside_context() -> None:
    from opencomputer.cli_ui import current_renderer

    assert current_renderer() is None


def test_enter_exit_sets_and_clears_current_renderer() -> None:
    from opencomputer.cli_ui import StreamingRenderer, current_renderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        assert current_renderer() is r
    assert current_renderer() is None


def test_buffer_accumulates_chunks() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_chunk("Hello, ")
        r.on_chunk("world!")
        assert "".join(r._buffer) == "Hello, world!"


def test_finalize_emits_thinking_panel_when_reasoning_present() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_chunk("The answer is 42.")
        r.finalize(
            reasoning="I weighed several options and decided 42.",
            iterations=1,
            in_tok=10,
            out_tok=5,
            elapsed_s=0.5,
            # New default is collapsed-summary; pass True to keep the
            # full panel visible (this test asserts the panel renders).
            show_reasoning=True,
        )
    output = console.export_text()
    assert "Thinking" in output
    assert "42" in output  # reasoning text appears


def test_finalize_skips_thinking_panel_when_reasoning_empty() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_chunk("Done.")
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=10,
            out_tok=2,
            elapsed_s=0.2,
        )
    output = console.export_text()
    assert "💭" not in output  # no thinking emoji panel
    assert "Done." in output


def test_finalize_emits_token_rate_in_footer() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_chunk("hi")
        # 100 tokens in 1.0s = 100 tok/s
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=50,
            out_tok=100,
            elapsed_s=1.0,
        )
    output = console.export_text()
    assert "100 tok/s" in output
    assert "1.0s" in output


def test_tool_panel_tracks_running_calls() -> None:
    """on_tool_start adds a row; on_tool_end flips status."""
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        idx = r.on_tool_start("Bash", "ls /tmp")
        assert ("Bash", idx) in r._tool_calls
        assert r._tool_calls[("Bash", idx)].ok is None  # running
        r.on_tool_end("Bash", idx, ok=True)
        assert r._tool_calls[("Bash", idx)].ok is True


def test_tool_panel_caps_at_three_rows() -> None:
    """Older calls scroll off so the panel stays compact."""
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        for i in range(5):
            r.on_tool_start("Read", f"file-{i}")
        # Only the last 3 stay visible.
        assert len(r._tool_calls) == 3


def test_tool_panel_concurrent_same_name_distinct_rows() -> None:
    """Two concurrent Bash calls each get their own row keyed by idx."""
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        i1 = r.on_tool_start("Bash", "ls /a")
        i2 = r.on_tool_start("Bash", "ls /b")
        assert i1 != i2
        assert len([k for k in r._tool_calls if k[0] == "Bash"]) == 2


def test_unmatched_code_fence_is_provisionally_closed() -> None:
    """Mid-stream code blocks shouldn't break Rich's Markdown renderer.

    We watch for an odd number of ``` runs in the buffer; render adds a
    closing fence so the parser never sees a half-open code block.
    """
    from opencomputer.cli_ui.streaming import StreamingRenderer

    console = _make_console()
    r = StreamingRenderer(console)
    r._buffer = ["Here's some code:\n```python\nprint('hi')"]
    out = r._render()
    # Smoke: the call returns without raising even with an unclosed fence
    assert out is not None


def test_truncate_args_preview_handles_narrow_console() -> None:
    """Args preview must fit on one line even on tiny terminals."""
    from opencomputer.cli_ui.streaming import _truncate_args_preview

    long = "x" * 500
    short = _truncate_args_preview(long, console_width=40)
    assert len(short) <= 40
    assert short.endswith("…")


def test_truncate_args_preview_strips_newlines() -> None:
    from opencomputer.cli_ui.streaming import _truncate_args_preview

    out = _truncate_args_preview("foo\nbar\nbaz", console_width=120)
    assert "\n" not in out
    assert out == "foo bar baz"


def test_fmt_duration_human_readable() -> None:
    from opencomputer.cli_ui.streaming import _fmt_duration

    assert _fmt_duration(0.4) == "0.4s"
    assert _fmt_duration(2.4) == "2.4s"
    assert _fmt_duration(72) == "1m12s"


def test_finalize_zero_elapsed_does_not_divide_by_zero() -> None:
    """If somehow elapsed is 0, token rate is 0 (not crash)."""
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.finalize(
            reasoning=None, iterations=0, in_tok=0, out_tok=0, elapsed_s=0.0
        )
    output = console.export_text()
    assert "0 tok/s" in output


def test_on_tool_end_for_unknown_idx_is_noop() -> None:
    """Late completion callback for an evicted row shouldn't crash."""
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_tool_end("Bash", idx=999, ok=True)  # never started
        # Nothing crashes; no row appears.
        assert len(r._tool_calls) == 0


def test_thinking_chunk_does_not_pollute_answer_buffer() -> None:
    """Live-thinking chunks land in ``_thinking_buffer``, never ``_buffer``.

    The two streams are separate so a re-render only touches the side
    that changed, and so finalize can branch on show_reasoning without
    losing the answer text.
    """
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.on_thinking_chunk("internal monologue")
        assert r._buffer == [], "thinking should not pollute the answer buffer"
        assert r._thinking_buffer == ["internal monologue"]
