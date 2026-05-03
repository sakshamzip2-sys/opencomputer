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

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

_DEFAULT_MAX_TURNS = 50


@dataclass(frozen=True)
class ToolAction:
    """One tool dispatch within a turn. Immutable record."""

    name: str
    args_preview: str
    ok: bool
    duration_s: float


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
            if chip:
                # File action: action name on top, file chip indented.
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
    """
    buf = io.StringIO()
    console = Console(file=buf, color_system=None, width=120, no_color=True)
    for t in turns:
        console.print(render_turn_tree(t))
    return buf.getvalue().rstrip()


__all__ = [
    "ReasoningStore",
    "ReasoningTurn",
    "ToolAction",
    "render_turn_tree",
    "render_turns_to_text",
]
