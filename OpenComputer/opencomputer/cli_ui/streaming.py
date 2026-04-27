"""Grok-style streaming renderer for ``opencomputer chat``.

Wraps Rich's ``Live`` to give the terminal four upgrades over the
prior plain-text streaming:

1. **Spinner** before the first chunk arrives so the user never stares
   at a blank prompt.
2. **Live markdown rendering** — code blocks get syntax highlighting
   in real time. Re-renders rate-limited at 4 fps so even chunky
   responses don't strobe the terminal.
3. **Tool-call status panel** — displays the last 3 tool dispatches
   beneath the streaming text, with elapsed time and ✓/✗ status.
4. **Thinking block** — when the provider returns extended-thinking
   reasoning, render it in a dim panel ABOVE the answer at finalize.

Designed to fail silently on non-TTY terminals. Caller is expected
to gate creation of the renderer on ``sys.stdout.isatty()``; passing
non-TTY into the context manager still works (Rich.Live degrades
gracefully) but the experience is plain-text, no live updates.

Hook bridge: the module exposes :func:`current_renderer` so a single
``HookEngine`` registration in ``cli.py`` can dispatch tool-start /
tool-end events to whatever renderer is currently active. The
sentinel is set in ``__enter__`` and cleared in ``__exit__`` so
nested or interleaved chat turns can't cross-talk.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    pass

#: Module-level sentinel for "the active renderer right now". Set by
#: :meth:`StreamingRenderer.__enter__`; cleared by ``__exit__``. The
#: hook bridge in ``cli.py`` reads this on every PRE/POST_TOOL_USE
#: dispatch to decide whether to forward to a renderer or no-op.
_CURRENT: StreamingRenderer | None = None


def current_renderer() -> StreamingRenderer | None:
    """Return the currently-active renderer, or None.

    Used by the cli.py hook subscriber. Tests can monkey-patch the
    module global directly for isolation.
    """
    return _CURRENT


#: How many tool-call rows stay visible at once. Older calls scroll
#: off so the panel doesn't crowd a long stream.
_TOOL_PANEL_MAX_ROWS = 3


@dataclass
class _ToolCallRow:
    """One row in the tool-status panel."""

    name: str
    args_preview: str
    started_at: float
    ended_at: float | None = None
    ok: bool | None = None  # None = running


class StreamingRenderer:
    """Owns a Rich.Live for one chat turn.

    Lifecycle::

        with StreamingRenderer(console) as r:
            r.start_thinking()                        # spinner visible
            for chunk in stream:
                r.on_chunk(chunk)                     # spinner → markdown
            # tools dispatched via hooks fire on_tool_start / on_tool_end
            r.finalize(reasoning="...", iterations=2,
                       in_tok=13909, out_tok=42, elapsed=1.4)

    The same instance is reusable across turns (call ``reset()``) but
    we currently create one per turn for simplicity.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._buffer: list[str] = []
        # Tool calls keyed by ``(name, idx)`` so concurrent dispatches
        # of the same tool don't collide. ``OrderedDict`` makes the
        # eviction order deterministic (FIFO).
        self._tool_calls: OrderedDict[tuple[str, int], _ToolCallRow] = (
            OrderedDict()
        )
        self._tool_call_seq = 0
        self._live: Live | None = None
        self._stream_started = False
        self._turn_started_at = 0.0
        # Compact assistant header is shown once when the first chunk
        # arrives, so the dim "oc ›" prefix doesn't repaint every frame.
        self._header_shown = False

    # ─── lifecycle ────────────────────────────────────────────────

    def __enter__(self) -> StreamingRenderer:
        global _CURRENT
        _CURRENT = self
        self._turn_started_at = time.monotonic()
        # Live is started lazily — see start_thinking. Keeps the
        # constructor cheap and lets callers pre-build the renderer.
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global _CURRENT
        _CURRENT = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001 — never raise from cleanup
                pass
            self._live = None

    # ─── pre-stream spinner ────────────────────────────────────────

    def start_thinking(self) -> None:
        """Show the spinner. Call once at the top of the turn."""
        spinner = Spinner("dots", text=Text("Thinking…", style="dim"))
        self._live = Live(
            spinner,
            console=self.console,
            refresh_per_second=4,
            transient=True,
        )
        self._live.start()

    # ─── stream callbacks ──────────────────────────────────────────

    def on_chunk(self, text: str) -> None:
        """Append a text chunk and refresh the live view."""
        if not text:
            return
        if not self._stream_started:
            self._stream_started = True
            self._header_shown = True
        self._buffer.append(text)
        self._refresh()

    def on_thinking_chunk(self, text: str) -> None:
        """Hook for live thinking streaming (deferred — currently a no-op).

        Anthropic's SDK exposes thinking chunks separately from
        assistant chunks. For v1 we render the thinking PANEL at
        finalize from the persisted ``ProviderResponse.reasoning``
        field. Live thinking-stream is a follow-up if the post-hoc
        render feels insufficient.
        """
        # Intentional no-op for v1. Reserved hook for the follow-up.
        return

    def on_tool_start(self, name: str, args_preview: str) -> int:
        """Register the start of a tool call. Returns an opaque id the
        caller passes back to :meth:`on_tool_end` so concurrent calls
        of the same tool don't collide."""
        self._tool_call_seq += 1
        idx = self._tool_call_seq
        self._tool_calls[(name, idx)] = _ToolCallRow(
            name=name,
            args_preview=_truncate_args_preview(args_preview, self.console.width),
            started_at=time.monotonic(),
        )
        # Keep only the last N rows visible. Pop oldest from the front
        # of the OrderedDict.
        while len(self._tool_calls) > _TOOL_PANEL_MAX_ROWS:
            self._tool_calls.popitem(last=False)
        self._refresh()
        return idx

    def on_tool_end(self, name: str, idx: int, ok: bool) -> None:
        """Mark a tool call as completed. Idempotent — late callbacks
        for evicted rows are silently dropped."""
        row = self._tool_calls.get((name, idx))
        if row is None:
            return
        row.ended_at = time.monotonic()
        row.ok = ok
        self._refresh()

    # ─── finalize ──────────────────────────────────────────────────

    def finalize(
        self,
        *,
        reasoning: str | None,
        iterations: int,
        in_tok: int,
        out_tok: int,
        elapsed_s: float,
    ) -> None:
        """Stop the Live, render the final markdown + thinking panel
        + token-rate footer. Caller MUST exit the context manager
        after calling this."""
        # Stop Live first so subsequent console.print writes go to the
        # real terminal cleanly.
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

        # Thinking panel above the answer (post-hoc — see class docstring).
        if reasoning and reasoning.strip():
            self.console.print(
                Panel(
                    Text(reasoning.strip(), style="dim"),
                    title=Text(
                        f"💭 Thinking ({_fmt_duration(elapsed_s)})",
                        style="dim cyan",
                    ),
                    border_style="grey50",
                    padding=(0, 1),
                )
            )

        # Final answer as Markdown — re-rendered from the full buffer
        # so code blocks get proper syntax highlighting. ``code_theme=
        # "ansi_dark"`` uses the terminal's own ANSI colors instead of
        # Pygments' monokai (which has a hardcoded dark background that
        # shows up as a black band on dark terminals).
        if self._buffer:
            content = "".join(self._buffer)
            if self._header_shown:
                self.console.print("[bold magenta]oc ›[/bold magenta]")
            self.console.print(Markdown(content, code_theme="ansi_dark"))

        # Token-rate footer.
        rate = (out_tok / elapsed_s) if elapsed_s > 0 else 0.0
        self.console.print(
            f"[dim]({iterations} iterations · "
            f"{in_tok} in / {out_tok} out · "
            f"{rate:.0f} tok/s · "
            f"{_fmt_duration(elapsed_s)})[/dim]\n"
        )

    # ─── internals ─────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Push the current buffer + tool panel to Rich.Live."""
        if self._live is None:
            return
        try:
            self._live.update(self._render(), refresh=True)
        except Exception:  # noqa: BLE001 — never break the chat loop on a render hiccup
            pass

    def _render(self) -> Group:
        """Compose the streaming-text view + tool-status panel."""
        renderables = []

        # The streaming text. Use Markdown when the buffer has content;
        # otherwise leave a dim placeholder so Live always has something
        # to draw.
        if self._buffer:
            text = "".join(self._buffer)
            # Provisionally close any unterminated code fence so Rich's
            # markdown renderer doesn't error mid-stream.
            if text.count("```") % 2 == 1:
                text = text + "\n```"
            renderables.append(Markdown(text, code_theme="ansi_dark"))
        else:
            renderables.append(
                Spinner("dots", text=Text("Thinking…", style="dim"))
            )

        # Tool-status panel below the stream — only when at least one
        # tool has started this turn.
        if self._tool_calls:
            renderables.append(self._render_tool_panel())

        return Group(*renderables)

    def _render_tool_panel(self) -> Panel:
        """Stack the current tool-call rows into a dim panel."""
        rows: list[Text] = []
        for (_name, _idx), row in self._tool_calls.items():
            rows.append(_render_tool_row(row))
        return Panel(
            Group(*rows),
            title=Text("tools", style="dim"),
            border_style="grey39",
            padding=(0, 1),
        )


# ─── module helpers ────────────────────────────────────────────────


def _truncate_args_preview(s: str, console_width: int) -> str:
    """Cap args preview so wrapped lines don't break the panel layout.

    Aims for one line: console width minus space for icon, name, dots,
    and timing column. ``console_width`` may be 0 in pytest captures;
    fall back to 80.
    """
    width = console_width or 80
    cap = max(20, width - 40)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _fmt_duration(seconds: float) -> str:
    """Format seconds like Grok: ``0.4s`` / ``2.4s`` / ``1m12s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _render_tool_row(row: _ToolCallRow) -> Text:
    """One line in the tool-status panel."""
    if row.ok is None:
        icon = Text("🔧", style="yellow")
        elapsed = time.monotonic() - row.started_at
        timing = f"({_fmt_duration(elapsed)} running)"
    elif row.ok:
        icon = Text("✓ ", style="green")
        elapsed = (row.ended_at or row.started_at) - row.started_at
        timing = _fmt_duration(elapsed)
    else:
        icon = Text("✗ ", style="red")
        elapsed = (row.ended_at or row.started_at) - row.started_at
        timing = f"{_fmt_duration(elapsed)} failed"

    line = Text()
    line.append(icon)
    line.append(f" {row.name:<12} ", style="cyan")
    line.append(row.args_preview, style="white")
    line.append(f"  {timing}", style="dim")
    return line


__all__ = [
    "StreamingRenderer",
    "current_renderer",
]
