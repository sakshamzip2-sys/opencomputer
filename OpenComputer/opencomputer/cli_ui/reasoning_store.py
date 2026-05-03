"""Per-session in-memory store of finalized reasoning turns.

Captured at :meth:`StreamingRenderer.finalize`, queried by the
``/reasoning show`` slash command. Survives across chat turns within a
single CLI session; not persisted to disk.

Capped to the last ``max_turns`` to keep memory bounded for long
sessions. Eviction is FIFO (oldest first); evicted turns return ``None``
from :meth:`get_by_id`.
"""
from __future__ import annotations

import io
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

_DEFAULT_MAX_TURNS = 50


#: Mirror of Vercel AI Elements ``ToolPart["state"]`` union (see
#: vercel/ai-elements packages/elements/src/tool.tsx). Values kept
#: identical so a future cross-runtime tool-status bridge stays
#: interoperable. Rich-on-terminal can't reproduce the React badge
#: animation but the state vocabulary IS reproducible.
ToolState = Literal[
    "approval-requested",
    "approval-responded",
    "input-streaming",
    "input-available",
    "output-available",
    "output-denied",
    "output-error",
]


@dataclass(frozen=True)
class ToolCall:
    """AI-Elements-shaped projection of a tool dispatch.

    Field names mirror Vercel AI Elements' ``ToolUIPart`` /
    ``DynamicToolUIPart`` verbatim (camelCase ``toolName`` / ``errorText``
    preserved deliberately for round-trip JSON interop with the AI SDK
    schema). Constructed by :meth:`ReasoningTurn.tool_calls` from the
    existing :class:`ToolAction` records — no new instrumentation
    needed for back-compat.
    """

    type: str                       # noqa: A003 — mirror "tool-<name>"
    toolName: str                   # noqa: N815 — AI Elements field name
    state: ToolState
    input: dict | None              # noqa: A003 — mirror AI Elements
    output: dict | str | None
    errorText: str | None           # noqa: N815 — AI Elements field
    started_at: float
    ended_at: float | None
    description: str | None = None  # PR #390 — AI-generated per-action summary


@dataclass(frozen=True)
class Source:
    """Mirror of Vercel AI Elements ``Source`` anchor props
    (sources.tsx): ``href``, ``title``. Plus optional metadata
    Claude.ai exposes but AI Elements' minimal port doesn't:
    ``tool`` (which OC tool produced this source) and ``snippet``
    (short excerpt). Extracted at render time from the existing
    WebSearch / WebFetch ToolAction outputs — no new tool
    instrumentation needed.
    """

    href: str                       # the URL
    title: str                      # display label
    tool: str | None = None         # e.g. "WebSearch", "WebFetch"
    snippet: str | None = None      # short excerpt, if available


@dataclass(frozen=True)
class TimelineStep:
    """Aggregate event log entry for one turn. Drives ReasoningView's
    expanded-state body. ``parent_id`` lets a tool nest under an
    iteration; today most are flat (no parent)."""

    id: int
    kind: Literal["iteration", "tool", "reasoning", "web_search"]
    label: str
    detail: str | None
    status: str                     # ToolState | "completed" | "in_progress"
    started_at: float
    ended_at: float | None
    parent_id: int | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolAction:
    """One tool dispatch within a turn. Immutable record.

    Original record kept verbatim for back-compat with existing
    ``render_turn_tree`` + tests + per-action description pipeline
    (PR #390). New optional fields (``started_at``, ``ended_at``,
    ``input``, ``output``, ``errorText``) all default to None so
    existing callers don't break; they're populated by the streaming
    renderer's enriched ``on_tool_start`` / ``on_tool_end`` kwargs.
    """

    name: str
    args_preview: str
    ok: bool
    duration_s: float
    description: str | None = None
    """LLM-generated one-line description of what the tool action did
    in plain English (e.g. ``"Wrote a haiku in foo.md"`` instead of
    ``"Edit(file_path=foo.md, content=...)"``). Set asynchronously by
    a daemon thread after :meth:`ReasoningStore.append`. ``None`` when
    the description call hasn't completed yet, was empty, or failed —
    the renderer falls back to the args_preview in that case."""

    # AI-Elements-port fields (additive, all optional for back-compat):
    started_at: float | None = None
    ended_at: float | None = None
    input: dict | None = None        # noqa: A003 — mirror AI Elements
    output: dict | str | None = None
    errorText: str | None = None     # noqa: N815 — AI Elements field


@dataclass(frozen=True)
class ReasoningTurn:
    """A finalized chat turn's reasoning + tool-action trail."""

    turn_id: int
    thinking: str
    duration_s: float
    tool_actions: tuple[ToolAction, ...] = field(default_factory=tuple)
    summary: str | None = None
    """LLM-generated one-line description of this turn's intent
    (e.g. "Wrote a haiku about sloths"). Set asynchronously after
    :meth:`ReasoningStore.append` by a daemon thread (see
    :mod:`opencomputer.agent.reasoning_summary`); may remain ``None``
    if the summary call timed out, was empty, or failed."""

    @property
    def action_count(self) -> int:
        return len(self.tool_actions)

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """AI-Elements-shaped projection of :attr:`tool_actions`.

        Built on demand from the existing ToolAction records; no
        persistence change. Maps OC's ``ok: bool`` / errorText absence
        onto AI Elements' ``state`` union:
        - ``ok=True`` → ``output-available``
        - ``ok=False`` (no errorText) → ``output-error``
        - ``ok=False`` AND ``errorText`` set → ``output-error``

        ``type`` follows AI Elements' ``tool-<name>`` convention so the
        wire shape round-trips with the AI SDK if a future bridge
        forwards these to a web client.
        """
        out: list[ToolCall] = []
        for a in self.tool_actions:
            if a.errorText is not None:
                state: ToolState = "output-error"
            elif a.ok:
                state = "output-available"
            else:
                state = "output-error"
            started = a.started_at if a.started_at is not None else 0.0
            ended = (
                a.ended_at
                if a.ended_at is not None
                else (started + a.duration_s if started else None)
            )
            out.append(
                ToolCall(
                    type=f"tool-{a.name}",
                    toolName=a.name,
                    state=state,
                    input=a.input,
                    output=a.output,
                    errorText=a.errorText,
                    started_at=started,
                    ended_at=ended,
                    description=a.description,
                )
            )
        return tuple(out)

    @property
    def sources(self) -> tuple[Source, ...]:
        """Web sources surfaced this turn. Extracted from the existing
        WebSearch / WebFetch :class:`ToolAction` outputs — no new tool
        instrumentation needed.

        - **WebSearch**: parses the markdown listing the tool emits
          (pattern: ``N. **Title**\\n   url``).
        - **WebFetch**: uses ``input["url"]`` as href, with the tool
          name as the title placeholder (no structured title is
          available from a raw page fetch).

        De-duplicated by href, preserving first-seen order so the
        rendered list matches the chronological tool-call order.
        """
        import re

        seen: dict[str, Source] = {}
        # WebSearch markdown pattern: 'N. **Title**\n   url'
        # Capture title (group 1) and url (group 2); be lenient on
        # whitespace + ordinal digits.
        ws_re = re.compile(
            r"^\s*\d+\.\s+\*\*(?P<title>[^*]+)\*\*\s*\n\s+(?P<url>https?://\S+)",
            re.MULTILINE,
        )

        for action in self.tool_actions:
            tool = action.name
            output_text = ""
            if isinstance(action.output, str):
                output_text = action.output
            elif isinstance(action.output, dict):
                # Tools may eventually emit structured output; check the
                # common 'content' key as a string fallback.
                content = action.output.get("content")
                if isinstance(content, str):
                    output_text = content

            if tool == "WebSearch" and output_text:
                for m in ws_re.finditer(output_text):
                    href = m.group("url").rstrip(".,);")
                    title = m.group("title").strip()
                    if href and href not in seen:
                        seen[href] = Source(
                            href=href, title=title, tool=tool
                        )
            elif tool == "WebFetch":
                href = (action.input or {}).get("url") if action.input else None
                if href and href not in seen:
                    # No structured title available from a raw fetch.
                    # Use the URL's host+path as a placeholder title.
                    from urllib.parse import urlparse
                    parsed = urlparse(href)
                    title = parsed.netloc + parsed.path
                    seen[href] = Source(
                        href=href,
                        title=title or href,
                        tool=tool,
                    )

        return tuple(seen.values())

    @property
    def timeline(self) -> tuple[TimelineStep, ...]:
        """Aggregate event log built on demand. Today's emission is
        flat: one ``reasoning`` step (if thinking text exists) plus one
        ``tool`` step per tool action. ``parent_id`` is None for all
        v1 entries; iteration-nesting is reserved for a follow-on once
        the agent loop emits per-iteration markers.
        """
        steps: list[TimelineStep] = []
        next_id = 1

        if self.thinking:
            steps.append(
                TimelineStep(
                    id=next_id,
                    kind="reasoning",
                    label="Reasoning",
                    detail=None,    # body lives in turn.thinking
                    status="completed",
                    started_at=0.0,  # turn-relative; absolute lost on store push
                    ended_at=self.duration_s,
                    parent_id=None,
                    metadata={"chars": len(self.thinking)},
                )
            )
            next_id += 1

        for tc in self.tool_calls:
            steps.append(
                TimelineStep(
                    id=next_id,
                    kind="tool",
                    label=tc.toolName,
                    detail=tc.description or None,
                    status=tc.state,
                    started_at=tc.started_at,
                    ended_at=tc.ended_at,
                    parent_id=None,
                    metadata={"type": tc.type},
                )
            )
            next_id += 1
        return tuple(steps)


class ReasoningStore:
    """Append-only ring buffer of :class:`ReasoningTurn`.

    Thread-safety: the CLI chat loop is single-threaded for appends.
    :meth:`update_summary` may be called from a daemon thread, but it
    targets a specific turn_id (different slot than the next append)
    and uses :func:`dataclasses.replace` for atomic-replace semantics.
    """

    def __init__(self, max_turns: int = _DEFAULT_MAX_TURNS) -> None:
        self._turns: deque[ReasoningTurn] = deque(maxlen=max_turns)
        self._next_id = 1

    def append(
        self,
        *,
        thinking: str,
        duration_s: float,
        tool_actions: list[ToolAction] | tuple[ToolAction, ...],
    ) -> ReasoningTurn:
        turn = ReasoningTurn(
            turn_id=self._next_id,
            thinking=thinking,
            duration_s=duration_s,
            tool_actions=tuple(tool_actions),
        )
        self._next_id += 1
        self._turns.append(turn)
        return turn

    def get_latest(self) -> ReasoningTurn | None:
        return self._turns[-1] if self._turns else None

    def get_by_id(self, turn_id: int) -> ReasoningTurn | None:
        for t in self._turns:
            if t.turn_id == turn_id:
                return t
        return None

    def get_all(self) -> list[ReasoningTurn]:
        return list(self._turns)

    def peek_next_id(self) -> int:
        """Return the id the next :meth:`append` will assign.

        Lets the renderer print the turn id in the collapsed line BEFORE
        the push happens (the push is the last step of finalize).
        """
        return self._next_id

    def update_summary(self, *, turn_id: int, summary: str) -> None:
        """Set the summary on a previously-appended turn.

        Called from a background daemon thread (see
        :mod:`opencomputer.agent.reasoning_summary`); safe because the
        frozen dataclass is replaced wholesale via
        :func:`dataclasses.replace` (atomic swap, no in-place mutation).

        Unknown ``turn_id`` is a silent no-op — the turn may have been
        evicted by the time the summary call returned (slow LLM + chatty
        session past the 50-turn cap).
        """
        from dataclasses import replace
        for i, t in enumerate(self._turns):
            if t.turn_id == turn_id:
                self._turns[i] = replace(t, summary=summary)
                return

    def update_tool_description(
        self, *, turn_id: int, action_idx: int, description: str
    ) -> None:
        """Set the description on a specific :class:`ToolAction` within
        a previously-appended turn.

        Called from a background daemon thread that runs Haiku
        per-action; safe because both the ToolAction and the enclosing
        ReasoningTurn are replaced wholesale via
        :func:`dataclasses.replace`. Unknown ``turn_id`` or out-of-range
        ``action_idx`` is a silent no-op (turn evicted, or action list
        changed since the description thread was spawned).
        """
        from dataclasses import replace
        for i, t in enumerate(self._turns):
            if t.turn_id == turn_id:
                if not (0 <= action_idx < len(t.tool_actions)):
                    return
                old_action = t.tool_actions[action_idx]
                new_action = replace(old_action, description=description)
                new_actions = (
                    t.tool_actions[:action_idx]
                    + (new_action,)
                    + t.tool_actions[action_idx + 1:]
                )
                self._turns[i] = replace(t, tool_actions=new_actions)
                return


def _fmt_duration(seconds: float) -> str:
    """Match streaming.py's duration formatter."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


_FILE_TOOLS = frozenset({"Edit", "Write", "Read", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "BashTool"})


def _action_icon(tool_name: str) -> str:
    """Map a tool name to a semantic icon for the expanded tree."""
    if tool_name in _FILE_TOOLS:
        return "📄"
    if tool_name in _SHELL_TOOLS:
        return "⚙️"
    return "🔧"


def _extract_path_chip(action: ToolAction) -> str | None:
    """Extract a single file path from a file-tool's args_preview for
    the chip display. Best-effort; returns None when no clean path is
    extractable.

    Args previews look like ``"file_path=/tmp/foo.md, content=..."`` or
    ``"path=foo.md"`` — pluck the value of the path-ish key.
    """
    if action.name not in _FILE_TOOLS:
        return None
    preview = action.args_preview or ""
    for key in ("file_path", "path", "notebook_path"):
        marker = f"{key}="
        if marker in preview:
            tail = preview.split(marker, 1)[1]
            value = tail.split(",", 1)[0].strip().strip('"').strip("'")
            return value or None
    return None


def render_turn_tree(turn: ReasoningTurn) -> Tree:
    """Render one ReasoningTurn as a Rich Tree matching Claude.ai's web
    UX (Image #9 — expanded form):

        Wrote a haiku about sloths ⌄                  (header)
        ├── 🧠 The user wants me to think about...    (reasoning text)
        ├── 📄 Edit                                    (file action)
        │       foo.md                                 (path chip)
        ├── ⚙️ Bash · ls -la                          (shell action)
        └── ⊘ Done · 3 actions in 1.8s                (footer)

    When ``turn.summary`` is None (Haiku slow / failed), the header
    falls back to today's metadata-bold layout so users always see
    the turn id + duration + action count.

    Empty-thinking and empty-tool-action turns each get a single
    placeholder child so the structure is visible even when sparse.
    """
    s = "" if turn.action_count == 1 else "s"

    if turn.summary:
        # Header is just the summary + chevron-down (expanded). The
        # collapsed form (printed by streaming.py.finalize) uses ›
        # (chevron-right). Together they read like a section heading.
        header = Text.assemble((turn.summary, "bold"), ("  ⌄", "dim"))
    else:
        # No summary → today's metadata-bold header so users still get
        # the turn id + duration + action count.
        header = Text.assemble(
            ("💭 ", "dim cyan"),
            (f"Turn #{turn.turn_id}", "bold cyan"),
            ("  ·  ", "dim"),
            (f"Thought for {_fmt_duration(turn.duration_s)}", "dim cyan"),
            ("  ·  ", "dim"),
            (f"{turn.action_count} action{s}", "dim cyan"),
        )

    tree = Tree(header, guide_style="grey50")

    # Reasoning text node — semantic icon (brain = "the AI was thinking").
    if turn.thinking:
        lines = turn.thinking.splitlines() or [turn.thinking]
        thinking_node = tree.add(
            Text.assemble(("🧠 ", "dim"), (lines[0], "dim"))
        )
        for line in lines[1:]:
            thinking_node.add(Text(line, style="dim"))
    else:
        tree.add(Text("(no extended thinking)", style="italic dim"))

    # Tool actions — semantic icons + optional path chips.
    if turn.tool_actions:
        for action in turn.tool_actions:
            mark = "✓" if action.ok else "✗"
            mark_style = "green" if action.ok else "red"
            icon = _action_icon(action.name)
            chip = _extract_path_chip(action)
            if action.description:
                # v4 Claude.ai parity: AI-generated description leads
                # the row instead of generic tool name + args. Action
                # name + chip (if applicable) shown as subordinates.
                desc_node = tree.add(
                    Text.assemble(
                        (f"{icon} ", "dim"),
                        (action.description, "bold"),
                        ("  ", ""),
                        (mark, mark_style),
                        (f"  {_fmt_duration(action.duration_s)}", "dim"),
                    )
                )
                if chip:
                    desc_node.add(Text(chip, style="italic dim"))
            elif chip:
                # File action without description (description thread
                # not done): show tool name + chip below.
                action_node = tree.add(
                    Text.assemble(
                        (f"{icon} ", "dim"),
                        (action.name, "bold"),
                        ("  ", ""),
                        (mark, mark_style),
                        (f"  {_fmt_duration(action.duration_s)}", "dim"),
                    )
                )
                action_node.add(Text(chip, style="italic dim"))
            else:
                # Shell or other: show args inline (truncated).
                args_brief = (action.args_preview or "").strip()
                if len(args_brief) > 60:
                    args_brief = args_brief[:57] + "..."
                tree.add(
                    Text.assemble(
                        (f"{icon} ", "dim"),
                        (action.name, "bold"),
                        ((f"  ·  {args_brief}" if args_brief else ""), "dim"),
                        ("  ", ""),
                        (mark, mark_style),
                        (f"  {_fmt_duration(action.duration_s)}", "dim"),
                    )
                )
        # Done footer with totals — Claude.ai parity (Image #9).
        total_dur = sum(a.duration_s for a in turn.tool_actions)
        tree.add(
            Text.assemble(
                ("⊘ ", "dim green"),
                ("Done", "bold green"),
                (
                    f"  ·  {turn.action_count} action{s}"
                    f" in {_fmt_duration(total_dur)}",
                    "dim",
                ),
            )
        )
    else:
        tree.add(Text("(no tool actions)", style="italic dim"))

    return tree


def render_turns_to_text(turns: list[ReasoningTurn]) -> str:
    """Render one or more turns as plain text suitable for
    ``SlashCommandResult.output``.

    color_system=None suppresses ANSI escape sequences (which render as
    garbage when the dispatcher routes the output as message content via
    ``opencomputer/agent/loop.py``). Unicode tree connectors (``├──``,
    ``└──``) Rich draws by default are preserved.

    Uses the AI Elements port (:class:`ReasoningView` in
    ``opencomputer.cli_ui.reasoning_view``) at ``open=True`` so the
    expanded view ships the AI-Elements-shaped Tool sections with
    Parameters / Result blocks under each tool call. Falls back to the
    legacy :func:`render_turn_tree` when the import is unavailable
    (defensive — currently always available, but keeps the boundary
    explicit in case the view module changes shape).
    """
    buf = io.StringIO()
    console = Console(file=buf, color_system=None, width=120, no_color=True)
    try:
        from opencomputer.cli_ui.reasoning_view import ReasoningView
        for t in turns:
            console.print(ReasoningView(turn=t, open=True))
    except ImportError:
        for t in turns:
            console.print(render_turn_tree(t))
    return buf.getvalue().rstrip()


__all__ = [
    "ReasoningStore",
    "ReasoningTurn",
    "Source",
    "TimelineStep",
    "ToolAction",
    "ToolCall",
    "ToolState",
    "render_turn_tree",
    "render_turns_to_text",
]
