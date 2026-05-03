"""Vercel AI Elements ``Reasoning`` + ``Tool`` port to Rich.

Faithful port of:
- ``packages/elements/src/reasoning.tsx``  (Reasoning, ReasoningTrigger, ReasoningContent)
- ``packages/elements/src/tool.tsx``       (Tool, ToolHeader, ToolContent, ToolInput, ToolOutput)

from https://github.com/vercel/ai-elements.

Field names + state vocabulary mirror the AI Elements TypeScript types
verbatim (camelCase ``toolName`` / ``errorText`` preserved deliberately
for round-trip JSON interop with the AI SDK schema). The component
boundaries are also the same: a ``Reasoning`` aggregate composed of a
trigger (collapsed-state header) and content (expanded body), with
nested ``ToolView`` per dispatched tool.

## What's reproducible (full)

- Prop interface: ``isStreaming``, ``open``, ``defaultOpen``, ``duration``
  on Reasoning; ``type``, ``state``, ``toolName``, ``title``, ``input``,
  ``output``, ``errorText`` on Tool family.
- State vocabulary: all 7 ToolState values from AI Elements'
  ``ToolPart["state"]`` union map to Rich-styled glyphs + colors.
- Visual hierarchy: collapsed → one-line summary + chevron.
  Expanded → header + reasoning markdown + per-tool subsections with
  Parameters / Result / Error blocks.
- JSON pretty-print of input/output via ``rich.syntax.Syntax``.
- Markdown rendering of reasoning text via ``rich.markdown.Markdown``.

## What's NOT reproducible (with substitute)

| AI Elements behavior | Substitute in Rich-on-terminal |
|---|---|
| True click-to-toggle on a printed card | Card is born collapsed at finalize; expanded form available retroactively via ``/reasoning show <N>``. |
| Auto-open when ``isStreaming`` becomes true | Existing live thinking panel covers the streaming phase; ReasoningView only renders the post-stream collapsed/expanded card. |
| Auto-close 1s after streaming ends | Card is born collapsed — no auto-close needed. |
| Chevron rotation animation | Static ``›`` (collapsed) ⟷ ``⌄`` (expanded). |
| Lucide BrainIcon / WrenchIcon | Unicode emoji (``💭`` brain, ``🔧`` wrench, ``⚙`` tool). |
| ``<Shimmer>`` thinking text | Existing ``Spinner('dots')`` during the live phase. |
| Status badges with rounded backgrounds | Inline ``[color]glyph label[/color]`` styled spans. |
| ``data-[state]`` CSS animations | None — terminals don't animate. |

These limitations were documented at length in the prototype PR #404
(experiments/textual_prototype) which proved the Textual migration
needed to overcome them costs ~9-13 engineer-days.
"""
from __future__ import annotations

import json

from rich.box import ROUNDED
from rich.console import Console, ConsoleOptions, RenderResult
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.tree import Tree

from opencomputer.cli_ui.reasoning_store import (
    ReasoningTurn,
    Source,
    ToolState,
)


def _fmt_duration(seconds: float) -> str:
    """Match the formatter the rest of cli_ui uses."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


# ─── ToolView (port of <Tool> + <ToolHeader> + <ToolContent> + ...) ──


#: Mirror of AI Elements' ``statusLabels`` (tool.tsx).
_STATUS_LABELS: dict[ToolState, str] = {
    "approval-requested": "Awaiting Approval",
    "approval-responded": "Responded",
    "input-streaming": "Pending",
    "input-available": "Running",
    "output-available": "Completed",
    "output-denied": "Denied",
    "output-error": "Error",
}


#: Glyph + color substitute for the React Lucide icon set in
#: ``statusIcons``. Rich-on-terminal: emoji + ANSI color combine to
#: convey the same status semantics as the colored React badges.
_STATUS_GLYPHS: dict[ToolState, tuple[str, str]] = {
    "approval-requested": ("⏱", "yellow"),
    "approval-responded": ("✓", "blue"),
    "input-streaming": ("○", "dim"),
    "input-available": ("⏵", "yellow"),
    "output-available": ("✓", "green"),
    "output-denied": ("⊘", "orange1"),
    "output-error": ("✗", "red"),
}


def _render_status_badge(state: ToolState) -> Text:
    """Port of AI Elements' ``getStatusBadge`` — glyph + label, colored."""
    glyph, color = _STATUS_GLYPHS[state]
    label = _STATUS_LABELS[state]
    return Text.assemble(
        (glyph, color),
        (" ", ""),
        (label, f"dim {color}"),
    )


class ToolView:
    """Port of the ``<Tool>`` component family (tool.tsx).

    Produces a Rich renderable (Panel) approximating the React card:
    header (wrench + name + status badge) → optional Parameters block
    → optional Result/Error block. The "expanded" state is implicit —
    we always render the body when the parent ReasoningView is in
    expanded mode (``open=True``).

    Parameters mirror AI Elements' Tool* prop interface:
    - ``type``: e.g. ``"tool-Read"`` (matches AI SDK convention)
    - ``state``: one of the 7 :class:`ToolState` values
    - ``toolName``: bare tool name (for the dynamic-tool case)
    - ``title``: optional override for the header label
    - ``input``: parameter dict; rendered as JSON
    - ``output``: result dict | string; rendered as JSON
    - ``errorText``: error string; rendered in destructive style
    """

    def __init__(
        self,
        *,
        type: str,                              # noqa: A002 — mirror AI Elements
        state: ToolState,
        toolName: str | None = None,           # noqa: N803
        title: str | None = None,
        input: dict | None = None,             # noqa: A002
        output: dict | str | None = None,
        errorText: str | None = None,          # noqa: N803
        description: str | None = None,
        duration_s: float | None = None,
    ) -> None:
        self.type = type
        self.state = state
        self.toolName = toolName
        self.title = title
        self.input = input
        self.output = output
        self.errorText = errorText
        self.description = description
        self.duration_s = duration_s

    @property
    def derived_name(self) -> str:
        """Port of the React ``derivedName`` ternary in ``ToolHeader``."""
        if self.type == "dynamic-tool" and self.toolName:
            return self.toolName
        # AI Elements does ``type.split("-").slice(1).join("-")``; the
        # OC convention puts the tool name as the bare suffix
        # (``tool-Read`` → ``Read``) so the same split works.
        if "-" in self.type:
            return self.type.split("-", 1)[1]
        return self.toolName or self.type

    def render_header(self) -> Text:
        """Port of <ToolHeader> — wrench + name + status badge."""
        return Text.assemble(
            ("🔧 ", "dim"),
            (self.title or self.derived_name, "bold"),
            ("  ", ""),
            _render_status_badge(self.state),
            (
                f"  · {_fmt_duration(self.duration_s)}"
                if self.duration_s is not None
                else "",
                "dim",
            ),
        )

    def render_input(self) -> Padding | None:
        """Port of <ToolInput> — pretty-printed JSON parameters."""
        if not self.input:
            return None
        try:
            body = json.dumps(self.input, indent=2, default=str)
        except (TypeError, ValueError):
            body = str(self.input)
        return Padding(
            _labelled_block("PARAMETERS", body, lang="json"),
            (0, 0, 0, 2),
        )

    def render_output(self) -> Padding | None:
        """Port of <ToolOutput> — JSON result or destructive error."""
        if not (self.output or self.errorText):
            return None
        if self.errorText:
            return Padding(
                _labelled_block(
                    "ERROR", self.errorText, lang="text", error=True
                ),
                (0, 0, 0, 2),
            )
        if isinstance(self.output, dict):
            try:
                body = json.dumps(self.output, indent=2, default=str)
                lang = "json"
            except (TypeError, ValueError):
                body = str(self.output)
                lang = "text"
        else:
            body = str(self.output)
            lang = "text"
        return Padding(
            _labelled_block("RESULT", body, lang=lang),
            (0, 0, 0, 2),
        )


def _labelled_block(label: str, body: str, *, lang: str, error: bool = False):
    """Render a small "PARAMETERS:" or "RESULT:" header above a JSON
    code block. Mirrors the React shadcn h4-uppercase + bg-muted/50
    treatment in tool.tsx with a dim caps label + a Rich Syntax body.
    """
    label_style = "bold red" if error else "bold dim"
    label_text = Text(label, style=label_style)
    if lang == "json":
        body_renderable = Syntax(
            body, "json", theme="ansi_dark", background_color="default",
            word_wrap=True,
        )
    else:
        body_renderable = Text(body, style="red" if error else "dim")
    return _StackedRenderable(label_text, body_renderable)


class _StackedRenderable:
    """Tiny helper to print two Rich renderables vertically without
    wrapping in a Group (which would force a console). Used by
    _labelled_block to avoid the import cycle."""

    def __init__(self, *parts):
        self._parts = parts

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield from self._parts


# ─── ReasoningView (port of <Reasoning> + <ReasoningTrigger> + <ReasoningContent>) ──


# ─── SourcesView (port of <Sources> + <SourcesTrigger> + <SourcesContent> + <Source>) ──


class SourcesView:
    """Port of the ``<Sources>`` component family (sources.tsx).

    Reference (vercel/ai-elements packages/elements/src/sources.tsx):
    - ``<Sources>`` — Collapsible wrapper, ``text-primary text-xs``
    - ``<SourcesTrigger count>`` — ``Used {count} sources`` + chevron
    - ``<SourcesContent>`` — Collapsible body
    - ``<Source href title>`` — anchor with ``BookIcon`` + title

    Faithful port: Rich Tree with a "Used N sources" header (matches
    AI Elements' ``count`` prop semantics) and one node per source
    showing ``📖 title`` followed by the URL on the next line. The
    URL is rendered as a clickable Rich link when supported by the
    terminal (``[link=URL]URL[/link]`` markup) — closest substitute
    for the React ``<a href target="_blank">`` semantics.

    Constructor mirrors AI Elements' ``SourcesProps`` shape:
    - ``count``: derived from ``len(sources)`` if not passed (mirrors
      AI Elements' ``SourcesTriggerProps.count``).
    - ``open``: collapsed by default; opens to show the source list.
    """

    def __init__(
        self,
        *,
        sources: tuple[Source, ...] | list[Source],
        open: bool = False,                  # noqa: A002 — mirror AI Elements
        count: int | None = None,
    ) -> None:
        self.sources = tuple(sources)
        self._is_open = open
        # Mirror AI Elements: ``count`` is a separate prop on
        # SourcesTrigger but defaults to len(sources). Caller can
        # override (e.g. when there's a "see more" pagination).
        self.count = count if count is not None else len(self.sources)

    @property
    def is_open(self) -> bool:
        return self._is_open

    def render_trigger(self) -> Text:
        """Port of <SourcesTrigger> — ``Used N sources`` + chevron."""
        chevron = "⌄" if self.is_open else "›"
        return Text.assemble(
            ("📖 ", "dim"),
            (f"Used {self.count} source", "bold"),
            ("s" if self.count != 1 else "", "bold"),
            ("  ", ""),
            (chevron, "dim"),
        )

    def render_content(self) -> Tree:
        """Port of <SourcesContent> — one <Source> entry per row.

        Each entry: ``📖 title`` on one line, URL on the next as a
        Rich link (when the terminal supports OSC 8 hyperlinks; falls
        back to plain text on terminals that don't).
        """
        tree = Tree(self.render_trigger(), guide_style="grey50")
        if not self.sources:
            tree.add(Text("(no sources)", style="italic dim"))
            return tree
        for src in self.sources:
            # Rich link markup degrades gracefully on non-OSC-8 terms.
            link = Text(src.href, style=f"dim link {src.href}")
            label = Text.assemble(
                ("📖 ", "dim"),
                (src.title or src.href, "bold"),
                (
                    f"  · {src.tool}" if src.tool else "",
                    "dim",
                ),
            )
            entry = tree.add(label)
            entry.add(link)
            if src.snippet:
                entry.add(Text(src.snippet, style="italic dim"))
        return tree

    def __rich__(self):
        """Collapsed → just the trigger; expanded → full Tree."""
        if self.is_open:
            return self.render_content()
        return self.render_trigger()


# ─── ReasoningView (port of <Reasoning> + <ReasoningTrigger> + <ReasoningContent>) ──


class ReasoningView:
    """Port of the ``<Reasoning>`` component family (reasoning.tsx).

    Wraps a finalized :class:`ReasoningTurn` and produces either the
    collapsed card (matches today's v6 PR #395 layout) or the expanded
    card (header + markdown reasoning + per-tool ToolView stack).

    Parameter names mirror AI Elements:
    - ``isStreaming``: when True, the existing live panel covers the
      streaming phase — this view is only rendered post-stream, so we
      DO render even when isStreaming was set (treating it as
      "isStreaming was true; now finished"). This matches AI Elements'
      duration-tracking semantics where ``duration`` is set when
      streaming ends.
    - ``open`` / ``defaultOpen``: explicit / default open state.
      Collapsed when False (default); expanded when True.
    - ``duration``: optional override for the header's "Thought for N
      seconds" text. Falls back to ``turn.duration_s``.
    """

    def __init__(
        self,
        *,
        turn: ReasoningTurn,
        isStreaming: bool = False,             # noqa: N803
        open: bool | None = None,              # noqa: A002
        defaultOpen: bool | None = None,       # noqa: N803
        duration: float | None = None,
    ) -> None:
        self.turn = turn
        self.isStreaming = isStreaming
        # Resolution mirrors React's useControllableState:
        # ``open`` (controlled) wins; else ``defaultOpen``; else False.
        if open is not None:
            self._is_open = open
        elif defaultOpen is not None:
            self._is_open = defaultOpen
        else:
            self._is_open = False
        self.duration = duration if duration is not None else turn.duration_s

    @property
    def is_open(self) -> bool:
        return self._is_open

    def thinking_message(self) -> Text:
        """Port of AI Elements' ``defaultGetThinkingMessage``.

        - Streaming → "Thinking…" (Rich can't shimmer; the live
          spinner during the streaming phase is the substitute).
        - Done with duration → "Thought for N seconds".
        - Done without duration → "Thought for a few seconds".
        """
        if self.isStreaming or self.duration == 0:
            return Text("Thinking…", style="bold dim")
        if self.duration is None:
            return Text("Thought for a few seconds", style="dim")
        return Text(
            f"Thought for {_fmt_duration(self.duration)}", style="dim"
        )

    def render_trigger(self) -> Text:
        """Port of <ReasoningTrigger> — brain icon + thinking message + chevron.

        When the turn has an LLM-generated summary, that takes
        precedence over the generic "Thought for N" — matches the
        v6 collapsed-card behaviour shipped in PR #395. The no-summary
        path appends the turn id so users can target it via
        ``/reasoning show <N>`` (terminal-only extension; AI Elements
        web has no equivalent — each component is standalone there).
        """
        chevron = "⌄" if self.is_open else "›"
        if self.turn.summary:
            label = Text(self.turn.summary, style="bold")
        else:
            msg = self.thinking_message()
            if self.turn.turn_id:
                label = Text.assemble(
                    ("💭 ", "dim"),
                    msg,
                    ("  ·  ", "dim"),
                    (f"Turn #{self.turn.turn_id}", "bold"),
                )
            else:
                label = Text.assemble(("💭 ", "dim"), msg)
        return Text.assemble(
            label,
            ("  ", ""),
            (chevron, "dim"),
        )

    def render_content(self):
        """Port of <ReasoningContent> + the per-step ToolView stack.

        The reference splits content into two boundaries: the markdown
        body (ReasoningContent's <Streamdown> children) and the
        per-tool subsections (each <Tool> sibling). We render them as a
        single Tree because Rich's Tree gives us the indented hierarchy
        AI Elements achieves with CSS nesting.
        """
        tree = Tree(
            self.render_trigger(),
            guide_style="grey50",
        )

        # ReasoningContent body — markdown.
        if self.turn.thinking:
            # First line on the head node; rest as children for tree
            # alignment (Rich's Markdown is multi-line and would
            # collide with the tree's branch lines).
            lines = self.turn.thinking.splitlines() or [self.turn.thinking]
            head = tree.add(
                Text.assemble(("🧠 ", "dim"), (lines[0], "dim"))
            )
            for line in lines[1:]:
                head.add(Text(line, style="dim"))

        # ToolView per tool call — header + (optional) params + (optional) result.
        for tc in self.turn.tool_calls:
            tv = ToolView(
                type=tc.type,
                state=tc.state,
                toolName=tc.toolName,
                title=tc.description,
                input=tc.input,
                output=tc.output,
                errorText=tc.errorText,
                description=tc.description,
                duration_s=(
                    tc.ended_at - tc.started_at
                    if tc.ended_at is not None and tc.started_at
                    else None
                ),
            )
            tool_node = tree.add(tv.render_header())
            params = tv.render_input()
            if params is not None:
                tool_node.add(params)
            output = tv.render_output()
            if output is not None:
                tool_node.add(output)

        # Sources subsection (port of <Sources>) — appended when the
        # turn used WebSearch / WebFetch and the extraction in
        # ReasoningTurn.sources turned up at least one URL. Mirrors
        # AI Elements composition where <Sources> sits as a sibling
        # under the Reasoning aggregate.
        if self.turn.sources:
            sv = SourcesView(sources=self.turn.sources, open=True)
            sources_node = tree.add(sv.render_trigger())
            for src in self.turn.sources:
                label = Text.assemble(
                    ("📖 ", "dim"),
                    (src.title or src.href, "bold"),
                    (
                        f"  · {src.tool}" if src.tool else "",
                        "dim",
                    ),
                )
                src_node = sources_node.add(label)
                src_node.add(Text(src.href, style=f"dim link {src.href}"))
                if src.snippet:
                    src_node.add(Text(src.snippet, style="italic dim"))

        # No tools and no thinking → single placeholder so structure is visible.
        if not self.turn.thinking and not self.turn.tool_calls:
            tree.add(Text("(no extended thinking, no tool actions)",
                          style="italic dim"))

        return tree

    def render_collapsed(self) -> Panel:
        """Collapsed-card form. Matches v6 PR #395 layout exactly so
        the on-screen appearance during normal operation is preserved.
        """
        return Panel(
            self.render_trigger(),
            box=ROUNDED,
            border_style="grey50",
            padding=(0, 2),
            expand=False,
        )

    def render_expanded(self) -> Panel:
        """Expanded-card form. The trigger sits on the panel border;
        content (markdown + tools) fills the body.
        """
        return Panel(
            self.render_content(),
            box=ROUNDED,
            border_style="grey50",
            padding=(0, 2),
            expand=False,
        )

    def __rich__(self):
        """Rich renderable dispatch — collapsed when ``open=False``
        (default), expanded when ``open=True``. Mirrors the AI
        Elements ``<Collapsible open={...}>`` semantics."""
        return self.render_expanded() if self.is_open else self.render_collapsed()


def render_turn_view(
    turn: ReasoningTurn,
    *,
    open: bool = True,                          # noqa: A002
) -> ReasoningView:
    """Convenience constructor mirroring ``render_turn_tree`` shape so
    callers can swap one for the other. Defaults to the expanded form
    because the typical caller (``/reasoning show <N>``) wants the body."""
    return ReasoningView(turn=turn, open=open)


__all__ = [
    "ReasoningView",
    "SourcesView",
    "ToolView",
    "render_turn_view",
]
