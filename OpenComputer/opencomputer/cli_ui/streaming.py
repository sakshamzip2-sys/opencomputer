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
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

from opencomputer.cli_ui.reasoning_store import ToolAction

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

    def __init__(
        self,
        console: Console,
        *,
        reasoning_store: ReasoningStore | None = None,
    ) -> None:
        self.console = console
        self._reasoning_store = reasoning_store
        self._buffer: list[str] = []
        # Live thinking buffer + first-chunk timestamp. Both are written
        # by on_thinking_chunk and read by _render / finalize. The
        # timestamp anchors the panel duration label and the collapsed
        # "Thought for X.Xs" summary at finalize.
        self._thinking_buffer: list[str] = []
        self._thinking_started_at: float = 0.0
        # Tool calls keyed by ``(name, idx)`` so concurrent dispatches
        # of the same tool don't collide. ``OrderedDict`` makes the
        # eviction order deterministic (FIFO).
        self._tool_calls: OrderedDict[tuple[str, int], _ToolCallRow] = (
            OrderedDict()
        )
        self._tool_call_seq = 0
        # Unbounded parallel history of completed tool calls. Used by
        # the reasoning tree (which needs the full sequence, not the
        # last-3 visible window).
        self._tool_history: list[ToolAction] = []
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
        """Append a thinking-delta chunk and live-refresh the panel.

        The panel renders ABOVE the answer markdown; the spinner / tool
        panel render below as before. First chunk anchors the duration
        timestamp used by both the live panel header and (at finalize)
        the collapsed summary.
        """
        if not text:
            return
        if not self._thinking_buffer:
            self._thinking_started_at = time.monotonic()
        self._thinking_buffer.append(text)
        self._refresh()

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
        """Mark a tool call as completed. Idempotent for the visible
        panel — late callbacks for evicted rows are silently dropped
        from the panel but ALWAYS captured in :attr:`_tool_history` for
        the reasoning tree.
        """
        row = self._tool_calls.get((name, idx))
        if row is not None:
            row.ended_at = time.monotonic()
            row.ok = ok
            duration = row.ended_at - row.started_at
            args_preview = row.args_preview
        else:
            # Row was evicted from the visible panel before the end
            # callback arrived. Synthesize a minimal record for the
            # history so the tree still shows it.
            duration = 0.0
            args_preview = ""
        self._tool_history.append(
            ToolAction(
                name=name,
                args_preview=args_preview,
                ok=ok,
                duration_s=duration,
            )
        )
        self._refresh()

    def tool_history(self) -> list[ToolAction]:
        """Return the full ordered list of completed tool calls this
        turn. Includes calls evicted from the visible 3-row panel.
        """
        return list(self._tool_history)

    # ─── finalize ──────────────────────────────────────────────────

    def finalize(
        self,
        *,
        reasoning: str | None,
        iterations: int,
        in_tok: int,
        out_tok: int,
        elapsed_s: float,
        show_reasoning: bool = False,
    ) -> None:
        """Stop the Live, render the final markdown + thinking panel /
        collapsed summary + token-rate footer. Caller MUST exit the
        context manager after calling this.

        ``show_reasoning`` (default ``False``, set by ``/reasoning show``
        via ``runtime.custom["show_reasoning"]``) controls whether the
        full thinking panel stays visible after streaming completes:

        * ``False`` (default) → panel collapses to a one-line
          ``💭 Thought for 3.2s — /reasoning show to expand`` summary.
          The live panel that streamed during the turn is replaced.
        * ``True`` → full panel rendered with the reasoning text.
        """
        # Stop Live first so subsequent console.print writes go to the
        # real terminal cleanly.
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

        # v2 (Thinking History UI): kick off async summary generation
        # as early as possible so the result can land in the collapsed
        # line below. The thread writes to a mutable cell so we can
        # read it after the brief join. No-op if no thinking text or
        # no store attached.
        _summary_cell: dict[str, str | None] = {"value": None}
        _summary_thread = None
        _thinking_for_summary = (reasoning or "").strip()
        if (
            self._reasoning_store is not None
            and _thinking_for_summary
            and not show_reasoning
        ):
            try:
                from opencomputer.agent.reasoning_summary import generate_summary

                def _run_summary() -> None:
                    _summary_cell["value"] = generate_summary(_thinking_for_summary)

                import threading as _threading
                _summary_thread = _threading.Thread(
                    target=_run_summary,
                    daemon=True,
                    name="reason-summary-inline",
                )
                _summary_thread.start()
            except Exception:  # noqa: BLE001 — never crash on summary spawn
                _summary_thread = None

        if reasoning and reasoning.strip():
            # Prefer the panel-anchored timestamp (set on the FIRST
            # thinking chunk) so the duration reflects how long the
            # model spent thinking, not the wall-clock since turn-start.
            # Falls back to the caller-provided elapsed_s if no live
            # thinking arrived this turn.
            thinking_elapsed = (
                (time.monotonic() - self._thinking_started_at)
                if self._thinking_started_at > 0.0
                else elapsed_s
            )
            if show_reasoning:
                self.console.print(
                    Panel(
                        Text(reasoning.strip(), style="dim"),
                        title=Text(
                            f"💭 Thinking ({_fmt_duration(thinking_elapsed)})",
                            style="dim cyan",
                        ),
                        border_style="grey50",
                        padding=(0, 1),
                    )
                )
            else:
                # Collapsed format. When a store is attached, prefix the
                # turn id + action count so users can refer to it
                # explicitly: "/reasoning show 5". When the v2 summary
                # thread completed in time (1.5s cap, Haiku is usually
                # well under that), lead with the summary so the line
                # reads like Image #7 ("Wrote a haiku · turn #5 · ...").
                if _summary_thread is not None:
                    _summary_thread.join(timeout=1.5)
                _summary_str = _summary_cell["value"]
                next_turn_id = (
                    self._reasoning_store.peek_next_id()
                    if self._reasoning_store is not None
                    else None
                )
                action_count = len(self._tool_history)
                meta_parts: list[str] = [
                    f"💭 Thought for {_fmt_duration(thinking_elapsed)}"
                ]
                if next_turn_id is not None:
                    meta_parts.append(f"turn #{next_turn_id}")
                if action_count > 0:
                    s = "" if action_count == 1 else "s"
                    meta_parts.append(f"{action_count} action{s}")
                meta = " · ".join(meta_parts)
                if _summary_str:
                    # v3 (Claude.ai parity, Image #10): just summary +
                    # chevron-right. No metadata clutter — the metadata
                    # moves to the expanded tree's header so the
                    # collapsed form reads like a section heading.
                    # Use Ctrl+X Ctrl+R or /reasoning show <N> to expand.
                    self.console.print(
                        f"[bold]{_summary_str}[/bold] [dim]›[/dim]"
                    )
                else:
                    # Fallback when summary is unavailable: today's
                    # metadata-only format with an explicit hint about
                    # how to see the full reasoning + tool actions.
                    self.console.print(
                        f"[dim cyan]{meta} — /reasoning show to expand[/dim cyan]"
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

        # Push captured state into the per-session store so the
        # /reasoning show command can re-render this turn later.
        # Skip turns that are pure no-ops (no thinking, no tools) — they
        # add noise to /reasoning show all without information value.
        if self._reasoning_store is not None:
            thinking_str = (reasoning or "").strip()
            if thinking_str or self._tool_history:
                thinking_elapsed_for_store = (
                    (time.monotonic() - self._thinking_started_at)
                    if self._thinking_started_at > 0.0
                    else elapsed_s
                )
                appended_turn = self._reasoning_store.append(
                    thinking=thinking_str,
                    duration_s=thinking_elapsed_for_store,
                    tool_actions=self._tool_history,
                )
                # If the inline summary thread already produced a
                # value, copy it into the store so /reasoning show <N>
                # picks it up. If not, kick off the daemon variant —
                # it will write back asynchronously.
                if _summary_cell["value"]:
                    self._reasoning_store.update_summary(
                        turn_id=appended_turn.turn_id,
                        summary=_summary_cell["value"],
                    )
                elif thinking_str and (
                    _summary_thread is None
                    or not _summary_thread.is_alive()
                    and _summary_cell["value"] is None
                ):
                    # Inline thread either wasn't started, or finished
                    # with None (LLM failure). Don't bother re-running.
                    pass
                elif thinking_str and _summary_thread is not None:
                    # Inline thread is still running past our 1.5s cap.
                    # Hand it off: when it completes, write the result
                    # to the store. The thread already targets
                    # _summary_cell, so wrap with a small helper.
                    import threading as _threading

                    def _store_when_done(
                        store=self._reasoning_store,
                        turn_id=appended_turn.turn_id,
                        cell=_summary_cell,
                        thread=_summary_thread,
                    ) -> None:
                        thread.join()
                        if cell["value"]:
                            store.update_summary(turn_id=turn_id, summary=cell["value"])

                    _threading.Thread(
                        target=_store_when_done,
                        daemon=True,
                        name="reason-summary-deferred-store",
                    ).start()

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
        """Compose the live thinking panel + streaming-text view +
        spinner + tool panel.

        Order matters: the thinking panel ALWAYS goes first when
        present, so users see the chain-of-thought stream above the
        answer markdown (matches every other LLM dropdown UX). Spinner
        visibility rule: show whenever the AI is still working —
        i.e. before the first text chunk OR while any tool is currently
        running.
        """
        renderables = []

        # Live thinking panel — first when present, so it stays above
        # the answer markdown and the tool panel.
        if self._thinking_buffer:
            thinking_text = "".join(self._thinking_buffer)
            elapsed = time.monotonic() - self._thinking_started_at
            renderables.append(
                Panel(
                    Text(thinking_text, style="dim"),
                    title=Text(
                        f"💭 Thinking ({_fmt_duration(elapsed)})",
                        style="dim cyan",
                    ),
                    border_style="grey50",
                    padding=(0, 1),
                )
            )

        # Detect whether the AI is "still working":
        #   - no text yet (haven't streamed first chunk)  → spinner
        #   - any tool currently running                  → spinner
        any_tool_running = any(
            row.ok is None for row in self._tool_calls.values()
        )
        show_spinner = (not self._buffer) or any_tool_running

        if self._buffer:
            text = "".join(self._buffer)
            # Provisionally close any unterminated code fence so Rich's
            # markdown renderer doesn't error mid-stream.
            if text.count("```") % 2 == 1:
                text = text + "\n```"
            renderables.append(Markdown(text, code_theme="ansi_dark"))

        if show_spinner:
            spinner_label = "Running tool…" if any_tool_running else "Thinking…"
            renderables.append(
                Spinner("dots", text=Text(spinner_label, style="dim"))
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
