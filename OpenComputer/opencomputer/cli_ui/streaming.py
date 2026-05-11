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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    from opencomputer.cli_ui.reasoning_store import ReasoningStore
    from opencomputer.cli_ui.sources import _HitLike

from opencomputer.agent.stream_retry import RetryStatus
from opencomputer.cli_ui.reasoning_store import ToolAction
from opencomputer.cli_ui.sources import (
    SourcesRegistry,
    render_sources_block,
    rewrite_inline_url_refs,
    strip_emitted_sources_block,
)

#: Module-level sentinel for "the active renderer right now". Set by
#: :meth:`StreamingRenderer.__enter__`; cleared by ``__exit__``. The
#: hook bridge in ``cli.py`` reads this on every PRE/POST_TOOL_USE
#: dispatch to decide whether to forward to a renderer or no-op.
_CURRENT: StreamingRenderer | None = None


def _skin_color(key: str, fallback: str) -> str:
    """Return a hex color from the active skin's ``colors`` dict.

    Hermes v2 D6 wiring (2026-05-09): renderers call this to consume
    the 22 Hermes color keys the skin engine ships
    (``banner_dim``, ``response_border``, ``ui_label``, etc.).

    Args:
        key: a key from the active skin's ``colors`` dict — e.g.
            ``"banner_dim"`` / ``"response_border"`` / ``"ui_label"``.
        fallback: the legacy hard-coded color used before skinning.
            Returned when no skin is applied or the key is missing.

    Returns:
        A Rich-compatible color string (hex literal preferred — Rich
        accepts both ``"#aabbcc"`` and named colors like ``"grey50"``).
    """
    try:
        from opencomputer.cli_ui.skin import current_spec
    except Exception:  # noqa: BLE001 — never break the render path
        return fallback
    try:
        spec = current_spec()
    except Exception:  # noqa: BLE001
        return fallback
    if spec is None:
        return fallback
    val = spec.colors.get(key)
    if not isinstance(val, str) or not val:
        return fallback
    return val


def _skin_spinner_text(*, phase: Literal["waiting", "thinking"]) -> str:
    """Build the spinner text using the active skin's face/verb cycles.

    Hermes v2 D5 wiring (2026-05-09): the YAML data + accessors shipped
    in PR #515 are now actually consumed by the streaming renderer.

    Args:
        phase: ``"waiting"`` for the pre-first-byte network wait
            (uses ``spinner.waiting_faces``); ``"thinking"`` once
            reasoning content has begun streaming
            (uses ``spinner.thinking_faces``).

    Returns:
        ``"<face> <verb>…"`` when the active skin defines faces, or
        the legacy literal ``"Thinking…"`` as a no-skin fallback.

    Defensive: import-failures and empty cycles fall back to the legacy
    string so the spinner never breaks even if skin loading goes
    sideways. Picking the *first* face (rather than rotating) keeps
    Rich's Live happy — the dots glyph already animates; rotating the
    face would require a custom Spinner.
    """
    try:
        from opencomputer.cli_ui.skin import (
            current_spinner_thinking_faces,
            current_spinner_verbs,
            current_spinner_waiting_faces,
        )
    except Exception:  # noqa: BLE001 — never break the render path
        return "Thinking…"
    try:
        faces = (
            current_spinner_waiting_faces()
            if phase == "waiting"
            else current_spinner_thinking_faces()
        )
        verbs = current_spinner_verbs()
    except Exception:  # noqa: BLE001 — fail to legacy text
        return "Thinking…"
    if not faces:
        # No face cycle configured — fall back to the legacy verb-only
        # spinner so user-authored skins that omit faces still get the
        # text they configured.
        verb = verbs[0] if verbs else "thinking"
        return f"{verb.capitalize()}…"
    face = faces[0]
    verb = verbs[0] if verbs else "thinking"
    return f"{face} {verb}…"


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
        # AI-Elements port: side-table for the full ``input`` dict + the
        # absolute ``started_at`` ts captured at on_tool_start, attached
        # to the eventual ToolAction at on_tool_end. Kept off
        # _ToolCallRow because that class is a UI-panel concern (3-row
        # window); this is a per-history-entry concern (unbounded).
        self._pending_inputs: dict[
            tuple[str, int], tuple[dict | None, float]
        ] = {}
        self._live: Live | None = None
        self._stream_started = False
        self._turn_started_at = 0.0
        # Compact assistant header is shown once when the first chunk
        # arrives, so the dim "oc ›" prefix doesn't repaint every frame.
        self._header_shown = False
        # Per-turn source accumulator. Fed by tool callbacks (WebSearch
        # etc. push their hits here via add_search_sources) and by the
        # prose rewrite pass in finalize. Renders as the Sources block
        # between markdown body and footer when non-empty.
        self._sources = SourcesRegistry()
        # 2026-05-11 — pre-first-byte retry status surface (driven by
        # opencomputer.agent.stream_retry via the retry_callback chain).
        # When non-None, a transient line renders just above the spinner
        # like "Anthropic overloaded — retry 2/4 in 1.3s". Cleared when
        # the next attempt starts emitting tokens (on_chunk /
        # on_thinking_chunk) or at finalize.
        self._retry_status: RetryStatus | None = None
        self._retry_status_started_at: float = 0.0

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
        """Show the spinner. Call once at the top of the turn.

        Spinner text is skin-aware: during the pre-first-chunk wait
        (network round-trip), the active skin's
        ``spinner.waiting_faces`` cycle is used (Hermes v2 D5 wiring,
        2026-05-09). Once first reasoning content arrives, ``_render``
        switches to ``spinner.thinking_faces``.
        """
        spinner = Spinner(
            "dots",
            text=Text(_skin_spinner_text(phase="waiting"), style="dim"),
        )
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
        # First real content from the (possibly retried) attempt — clear
        # any lingering pre-first-byte retry status so the panel doesn't
        # stick around once we're successfully streaming.
        if self._retry_status is not None:
            self._retry_status = None
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
        # Thinking deltas count as "the attempt is now producing output";
        # clear the pre-first-byte retry status now that we're past it.
        if self._retry_status is not None:
            self._retry_status = None
        self._thinking_buffer.append(text)
        self._refresh()

    def on_retry_status(self, status: RetryStatus) -> None:
        """Display a transient pre-first-byte retry banner.

        Called by :func:`opencomputer.agent.stream_retry.stream_with_retry`
        between attempts (and on exhaustion) so the user can see *why*
        the response is delayed instead of staring at a frozen spinner.

        Best-effort: any rendering error logs at DEBUG and the agent
        loop continues — the retry happens regardless of whether the UI
        managed to display this notice.
        """
        try:
            self._retry_status = status
            self._retry_status_started_at = time.monotonic()
            self._refresh()
        except Exception as exc:  # noqa: BLE001 — UI bridge mustn't wedge retry
            import logging

            logging.getLogger(__name__).debug(
                "on_retry_status failed: %s", exc
            )

    def on_tool_start(
        self,
        name: str,
        args_preview: str,
        *,
        input: dict | None = None,  # noqa: A002 — mirror AI Elements
    ) -> int:
        """Register the start of a tool call. Returns an opaque id the
        caller passes back to :meth:`on_tool_end` so concurrent calls
        of the same tool don't collide.

        ``input`` (optional, AI-Elements naming preserved) is the full
        parameter dict for the call — when provided, it's stored on
        the eventual :class:`ToolAction` for the AI-Elements-style
        expanded view (ReasoningView). Back-compat default ``None``
        preserves the existing behaviour for callers that only have the
        truncated ``args_preview`` string.
        """
        self._tool_call_seq += 1
        idx = self._tool_call_seq
        started_at = time.monotonic()
        self._tool_calls[(name, idx)] = _ToolCallRow(
            name=name,
            args_preview=_truncate_args_preview(args_preview, self.console.width),
            started_at=started_at,
        )
        # Stash the full input dict + start ts in a side map keyed by
        # (name, idx) so on_tool_end can attach them to the ToolAction
        # without growing _ToolCallRow (which is a UI panel concern).
        self._pending_inputs[(name, idx)] = (input, started_at)
        # Keep only the last N rows visible. Pop oldest from the front
        # of the OrderedDict.
        while len(self._tool_calls) > _TOOL_PANEL_MAX_ROWS:
            self._tool_calls.popitem(last=False)
        self._refresh()
        return idx

    def on_tool_end(
        self,
        name: str,
        idx: int,
        ok: bool,
        *,
        output: dict | str | None = None,
        errorText: str | None = None,  # noqa: N803 — mirror AI Elements
    ) -> None:
        """Mark a tool call as completed. Idempotent for the visible
        panel — late callbacks for evicted rows are silently dropped
        from the panel but ALWAYS captured in :attr:`_tool_history` for
        the reasoning tree.

        ``output`` and ``errorText`` (optional, AI-Elements naming
        preserved) populate the AI-Elements-shaped ToolCall projection
        on :class:`ReasoningTurn`. Back-compat defaults ``None`` keep
        existing callers green; when only the legacy ``ok`` bool is
        passed, the projection sets state to ``output-available`` /
        ``output-error`` accordingly with no body.
        """
        row = self._tool_calls.get((name, idx))
        ended_at = time.monotonic()
        if row is not None:
            row.ended_at = ended_at
            row.ok = ok
            duration = row.ended_at - row.started_at
            args_preview = row.args_preview
        else:
            # Row was evicted from the visible panel before the end
            # callback arrived. Synthesize a minimal record for the
            # history so the tree still shows it.
            duration = 0.0
            args_preview = ""
        # Pull the matched start metadata if we recorded it.
        input_dict, started_at = self._pending_inputs.pop(
            (name, idx), (None, None)
        )
        self._tool_history.append(
            ToolAction(
                name=name,
                args_preview=args_preview,
                ok=ok,
                duration_s=duration,
                started_at=started_at,
                ended_at=ended_at,
                input=input_dict,
                output=output,
                errorText=errorText,
            )
        )
        self._refresh()

    def tool_history(self) -> list[ToolAction]:
        """Return the full ordered list of completed tool calls this
        turn. Includes calls evicted from the visible 3-row panel.
        """
        return list(self._tool_history)

    # ─── sources bridge ────────────────────────────────────────────

    def add_search_sources(self, hits: Iterable[_HitLike]) -> None:
        """Push search-tool hits into the per-turn source registry.

        Called by ``WebSearchTool`` (and any other search-style tool)
        after a successful query. Title + snippet come from the search
        backend response — no extra fetch needed during render. Empty
        iterable is a safe no-op; duplicate URLs are deduped by the
        registry.
        """
        try:
            self._sources.add_search_hits(hits)
        except Exception as exc:  # noqa: BLE001 — UI bridge must never
            # crash the tool call. Worst case: source goes unrendered.
            import logging
            logging.getLogger(__name__).debug(
                "add_search_sources failed: %s", exc
            )

    def emit_compaction_card(
        self,
        *,
        messages_before: int,
        messages_after: int,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
        reason: str = "auto",
    ) -> None:
        """Render an in-chat summary card when a compaction completes.

        Called by ``AgentLoop`` immediately after a successful
        ``CompactionResult.did_compact``. Best-effort — never raises;
        a render failure logs at ``DEBUG`` and the agent continues.
        Token counts are optional; the card omits the token row when
        either side is ``None`` (honest "we don't know" path).
        """
        try:
            from opencomputer.cli_ui.summary_cards import render_compaction_card

            card = render_compaction_card(
                messages_before=messages_before,
                messages_after=messages_after,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                reason=reason,
            )
            self.console.print(card)
        except Exception as exc:  # noqa: BLE001 — UI bridge must never crash the loop
            import logging
            logging.getLogger(__name__).debug(
                "emit_compaction_card failed: %s", exc
            )

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
        assistant_response: str | None = None,
    ) -> None:
        """Stop the Live, render the thinking-history line + final
        markdown + token-rate footer. Caller MUST exit the context
        manager after calling this.

        Print rules:

        * ``show_reasoning=True`` AND ``reasoning`` non-empty →
          full thinking panel above the answer.
        * Else, if EITHER thinking OR tool actions occurred → a
          collapsed thinking-history line (with AI summary if Haiku
          completed in time, else metadata fallback).
        * No-op turns (no thinking AND no tools) → nothing printed.

        ``assistant_response`` (optional) — the model's final response
        text. Used as a fallback summary input when ``reasoning`` is
        empty (tool-only turns) so users still get a Claude.ai-style
        summary line for tool-driven exchanges (e.g. weather query →
        WebFetch → "Reported NYC weather").
        """
        # Clear any lingering pre-first-byte retry status so it doesn't
        # leak into the post-turn summary. Whether the retry succeeded
        # or exhausted, the banner has done its job by now.
        self._retry_status = None
        # ─── Decide what this turn contained ──────────────────────────
        thinking_str = (reasoning or "").strip()
        has_thinking = bool(thinking_str)
        has_tools = bool(self._tool_history)
        # Match the store-push gate (line further below) — same condition
        # for visible cue and for storage. Avoids the regression where
        # tool-only turns were captured silently with no user indicator.
        has_anything = has_thinking or has_tools

        # Summary input: prefer thinking text (richer signal); fall back
        # to assistant response so tool-only turns still get a summary.
        _summary_input = thinking_str or (assistant_response or "").strip()

        # Kick off async summary generation early. Skip when:
        #  - no store (nowhere to write back),
        #  - no input (nothing to summarize),
        #  - show_reasoning=True (full panel mode is already the rich view).
        _summary_cell: dict[str, str | None] = {"value": None}
        _summary_thread = None
        if (
            self._reasoning_store is not None
            and _summary_input
            and not show_reasoning
            and has_anything
        ):
            try:
                from opencomputer.agent.reasoning_summary import generate_summary

                def _run_summary() -> None:
                    _summary_cell["value"] = generate_summary(_summary_input)

                import threading as _threading
                _summary_thread = _threading.Thread(
                    target=_run_summary,
                    daemon=True,
                    name="reason-summary-inline",
                )
                _summary_thread.start()
            except Exception:  # noqa: BLE001 — never crash on summary spawn
                _summary_thread = None

        # ─── Wait for summary BEFORE stopping Live ──────────────────────
        # Flicker fix (user-reported, 2026-05-03): previously the call
        # sequence was {stop Live} → {wait ~1.5s for summary} → {print
        # final content}. With transient=True, the Live region was
        # erased on stop, leaving the user staring at empty space for
        # ~1.5s before the final state appeared. Now we keep the Live
        # region (showing the streaming state the user already saw)
        # alive during the summary wait — only stopping right before
        # we have the final content ready to print. The visible gap
        # shrinks from ~1.5s to ~30ms (imperceptible).
        if _summary_thread is not None and not show_reasoning:
            _summary_thread.join(timeout=1.5)
        # NOW stop Live so subsequent console.print writes go to the
        # real terminal cleanly.
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

        # ─── Print thinking section ──────────────────────────────────
        if has_anything:
            # Prefer the panel-anchored timestamp (set on the FIRST
            # thinking chunk). Falls back to the caller-provided
            # elapsed_s when there were no thinking deltas this turn.
            thinking_elapsed = (
                (time.monotonic() - self._thinking_started_at)
                if self._thinking_started_at > 0.0
                else elapsed_s
            )
            if show_reasoning and has_thinking:
                # Full panel — only when there's actual thinking text.
                self.console.print(
                    Panel(
                        Text(thinking_str, style="dim"),
                        title=Text(
                            f"💭 Thinking ({_fmt_duration(thinking_elapsed)})",
                            style="dim cyan",
                        ),
                        border_style="grey50",
                        padding=(0, 1),
                    )
                )
            else:
                # Collapsed format. Summary thread already joined
                # above (before Live.stop) so the result is ready to
                # read immediately — no extra wait here.
                _summary_str = _summary_cell["value"]
                next_turn_id = (
                    self._reasoning_store.peek_next_id()
                    if self._reasoning_store is not None
                    else None
                )
                action_count = len(self._tool_history)

                # AI Elements port (PR — feat/ai-elements-reasoning-port):
                # build a transient ReasoningTurn-shaped record from the
                # in-flight state and render via ReasoningView.
                # This replaces the inline v6 Panel block (PR #395) but
                # produces the same visual at default ``open=False``
                # (the summary trigger as a content-width rounded card).
                # The full expanded form is reachable retroactively via
                # ``/reasoning show`` once the turn is in the store.
                from opencomputer.cli_ui.reasoning_store import ReasoningTurn
                from opencomputer.cli_ui.reasoning_view import ReasoningView

                # When the summarizer hasn't produced text, fall back to
                # the same metadata-cell synthesis the v6 inline block
                # did — so the trigger is never blank.
                _trigger_summary = _summary_str
                if not _trigger_summary:
                    meta_parts: list[str] = []
                    if has_thinking:
                        meta_parts.append(
                            f"💭 Thought for {_fmt_duration(thinking_elapsed)}"
                        )
                    elif has_tools:
                        s = "" if action_count == 1 else "s"
                        meta_parts.append(f"🔧 Used {action_count} tool{s}")
                    if next_turn_id is not None:
                        meta_parts.append(f"turn #{next_turn_id}")
                    if has_thinking and action_count > 0:
                        s = "" if action_count == 1 else "s"
                        meta_parts.append(f"{action_count} action{s}")
                    _trigger_summary = " · ".join(meta_parts) or None

                _preview_turn = ReasoningTurn(
                    turn_id=next_turn_id or 0,
                    thinking=thinking_str,
                    duration_s=thinking_elapsed,
                    tool_actions=tuple(self._tool_history),
                    summary=_trigger_summary,
                )
                self.console.print(
                    ReasoningView(
                        turn=_preview_turn,
                        isStreaming=False,   # finalize fires post-stream
                        open=False,          # collapsed trigger card
                    )
                )

        # Final answer as Markdown — re-rendered from the full buffer
        # so code blocks get proper syntax highlighting. ``code_theme=
        # "ansi_dark"`` uses the terminal's own ANSI colors instead of
        # Pygments' monokai (which has a hardcoded dark background that
        # shows up as a black band on dark terminals).
        #
        # Two source-related passes happen here, in order:
        #   1. Strip the model's ad-hoc "Sources:\n  • <url>" trailer
        #      from the prose (it'll be replaced by the structured
        #      Sources block below). URLs the strip surfaces are
        #      registered into the registry so they still render.
        #   2. Rewrite ``(https://...)`` parentheticals to ``[N]``
        #      references. Auto-registers new URLs as a side effect.
        # The token-rate footer is printed AFTER, untouched.
        if self._buffer:
            content = "".join(self._buffer)
            content, stripped_urls = strip_emitted_sources_block(content)
            for url in stripped_urls:
                self._sources.add_url(url)
            content = rewrite_inline_url_refs(content, self._sources)
            # Hermes-CLI parity A2: strip rendered `**bold**` / `*italic*`
            # markup so terminals without bold/italic don't print literal
            # asterisks. Code blocks / lists / tables preserved.
            # Disable via `OPENCOMPUTER_NO_MD_STRIP=1` for raw markdown.
            import os as _os

            from opencomputer.cli_ui.markdown_strip import strip_for_terminal

            if not _os.environ.get("OPENCOMPUTER_NO_MD_STRIP"):
                content = strip_for_terminal(content)
            if self._header_shown:
                self.console.print("[bold magenta]oc ›[/bold magenta]")
            self.console.print(Markdown(content, code_theme="ansi_dark"))

        # Sources block — only renders when the registry has entries.
        # Sits between the answer markdown and the token-rate footer
        # (the footer is intentionally the last thing printed).
        render_sources_block(self.console, self._sources.sources())

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
        # Same gate as the visible cue above (has_anything) — keeps
        # storage and display in sync.
        if self._reasoning_store is not None and has_anything:
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
            # v4 (Claude.ai parity): kick off plain-English per-action
            # descriptions in the background. They replace the generic
            # tool-name + args display in the expanded /reasoning show
            # tree with a description like "Wrote a haiku in foo.md".
            # Fire-and-forget — daemon thread updates the store as
            # each Haiku call lands; the next prompt is never delayed.
            if self._tool_history:
                try:
                    from opencomputer.agent.reasoning_summary import (
                        maybe_describe_tool_actions,
                    )
                    maybe_describe_tool_actions(
                        store=self._reasoning_store,
                        turn_id=appended_turn.turn_id,
                        actions=self._tool_history,
                    )
                except Exception:  # noqa: BLE001 — never crash on description spawn
                    pass
            # If the inline summary thread already produced a value,
            # copy it into the store so /reasoning show <N> picks it
            # up. If the thread is still running past the 1.5s cap,
            # hand off to a deferred-storage thread.
            if _summary_cell["value"]:
                self._reasoning_store.update_summary(
                    turn_id=appended_turn.turn_id,
                    summary=_summary_cell["value"],
                )
            elif _summary_input and (
                _summary_thread is None
                or not _summary_thread.is_alive()
                and _summary_cell["value"] is None
            ):
                # Inline thread either wasn't started, or finished
                # with None (LLM failure). Don't bother re-running.
                pass
            elif _summary_input and _summary_thread is not None:
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
                    # Hermes v2 D6 wiring (2026-05-09): use the active
                    # skin's banner_dim color via Rich Theme. Falls back
                    # to grey50 when no skin is applied (Theme inherits
                    # from default), matching pre-skin behavior.
                    border_style=_skin_color("banner_dim", "grey50"),
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
            if any_tool_running:
                spinner_label = "Running tool…"
            else:
                # Hermes v2 D5 wiring (2026-05-09): once first reasoning
                # content has arrived, switch to thinking_faces. Before
                # the first chunk we'd be in pre-stream waiting (handled
                # by start_thinking); _render runs after at least one
                # chunk so we're past that boundary.
                spinner_label = _skin_spinner_text(phase="thinking")
            renderables.append(
                Spinner("dots", text=Text(spinner_label, style="dim"))
            )

        # 2026-05-11 — pre-first-byte retry banner. Renders just below
        # the spinner so the eye doesn't have to jump; cleared when the
        # next attempt successfully streams (on_chunk /
        # on_thinking_chunk) or at finalize.
        if self._retry_status is not None:
            renderables.append(_render_retry_panel(self._retry_status))

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
            # Hermes v2 D6 wiring (2026-05-09): tool-panel border uses
            # the active skin's banner_dim color so different skins
            # tint the tool panel consistently with the rest of the UI.
            border_style=_skin_color("banner_dim", "grey39"),
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


_RETRY_KIND_HUMAN: dict[str, str] = {
    "overloaded": "upstream overloaded",
    "service_unavailable": "service unavailable",
    "bad_gateway": "bad gateway",
    "gateway_timeout": "gateway timeout",
    "internal_error": "upstream 500",
    "connection": "connection failure",
    "timeout": "timeout",
    "tls": "TLS error",
    "transient": "transient error",
}


def _render_retry_panel(status: RetryStatus) -> Panel:
    """Compact yellow-bordered panel showing pre-first-byte retry state.

    Renders one of:

    * ``upstream overloaded — retry 2/4 in 1.3s``
    * ``upstream overloaded — exhausted after 4 attempts``

    Bordered with the active skin's ``response_border`` color when
    available so different terminal skins keep the visual tone
    consistent.
    """
    label = _RETRY_KIND_HUMAN.get(status.error_kind, status.error_kind)
    if status.exhausted:
        body = Text(
            f"{label} — exhausted after {status.max_attempts} attempt"
            f"{'s' if status.max_attempts != 1 else ''}",
            style="yellow",
        )
        title_text = "retry exhausted"
    else:
        body = Text()
        body.append(label, style="yellow")
        body.append(" — retry ", style="dim")
        body.append(
            f"{status.next_attempt}/{status.max_attempts}",
            style="bold yellow",
        )
        body.append(" in ", style="dim")
        body.append(f"{status.delay_seconds:.1f}s", style="bold yellow")
        title_text = "retry"
    if status.error_message:
        body.append("\n", style="dim")
        body.append(status.error_message, style="dim")
    return Panel(
        body,
        title=Text(title_text, style="dim yellow"),
        border_style=_skin_color("response_border", "yellow"),
        padding=(0, 1),
    )


__all__ = [
    "StreamingRenderer",
    "current_renderer",
]
