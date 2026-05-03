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

    @property
    def action_count(self) -> int:
        return len(self.tool_actions)


class ReasoningStore:
    """Append-only ring buffer of :class:`ReasoningTurn`.

    Thread-safety: NOT thread-safe. The CLI chat loop is single-threaded
    so this is fine; if a future caller needs concurrent appends, wrap
    accesses with a lock.
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


def _fmt_duration(seconds: float) -> str:
    """Match streaming.py's duration formatter."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def render_turn_tree(turn: ReasoningTurn) -> Tree:
    """Render one ReasoningTurn as a Rich Tree for the console.

    Layout::

        💭 Turn #N · Thought for X.Xs · K actions
        ├── 🧠 Reasoning: <thinking text>
        ├── 🔧 Tool(args) ✓ 0.05s
        └── 🔧 Tool(args) ✗ 0.12s

    Empty thinking and empty tool-action lists each get a single
    placeholder child so users see the structure, not just a header.
    """
    s = "" if turn.action_count == 1 else "s"
    header = Text.assemble(
        ("💭 ", "dim cyan"),
        (f"Turn #{turn.turn_id}", "bold cyan"),
        ("  ·  ", "dim"),
        (f"Thought for {_fmt_duration(turn.duration_s)}", "dim cyan"),
        ("  ·  ", "dim"),
        (f"{turn.action_count} action{s}", "dim cyan"),
    )
    tree = Tree(header, guide_style="grey50")

    if turn.thinking:
        # Indent multi-line thinking under a single "Reasoning:" node so
        # the tree connectors stay clean.
        thinking_node = tree.add(Text.assemble(("🧠 Reasoning: ", "dim cyan")))
        for line in turn.thinking.splitlines() or [turn.thinking]:
            thinking_node.add(Text(line, style="dim"))
    else:
        tree.add(Text("(no extended thinking)", style="italic dim"))

    if turn.tool_actions:
        for action in turn.tool_actions:
            mark = "✓" if action.ok else "✗"
            mark_style = "green" if action.ok else "red"
            tree.add(
                Text.assemble(
                    ("🔧 ", "dim"),
                    (action.name, "bold"),
                    ("(", "dim"),
                    (action.args_preview, "dim"),
                    (") ", "dim"),
                    (mark, mark_style),
                    (f"  {_fmt_duration(action.duration_s)}", "dim"),
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
