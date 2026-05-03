"""CollapsibleThinkingCard — Textual prototype widget.

A single-keystroke-toggle widget that renders the thinking-history
card. Two states:

- ``collapsed``: summary text + chevron, single line.
- ``expanded``: full tree text below the summary.

Pressing Enter toggles. Designed to be focusable so the parent App
can ``set_focus(card)`` and the keypress reaches the widget's bindings.

This is a feasibility probe — it intentionally does NOT match the
production card's full styling (rounded panel, color, etc). The
question is "does the toggle work?", not "does it look pixel-perfect?".
"""
from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class CollapsibleThinkingCard(Widget, can_focus=True):
    """A toggle-able card. ``state`` is a reactive Literal so the
    Textual reactive system re-composes when it changes."""

    BINDINGS = [
        Binding("enter", "toggle", "expand/collapse", show=True),
    ]

    # ``init=False`` so ``watch_state`` doesn't fire during __init__ (before
    # ``compose()`` has mounted the children — the watcher's query_one
    # would raise NoMatches). The watcher fires only on subsequent
    # transitions from compose-time onward.
    state: reactive[Literal["collapsed", "expanded"]] = reactive(
        "collapsed", init=False
    )

    def __init__(self, *, summary: str, tree_text: str) -> None:
        super().__init__()
        self._summary = summary
        self._tree_text = tree_text

    def compose(self) -> ComposeResult:
        # Vertical so the tree can sit underneath the summary on expand.
        with Vertical():
            yield Static(self._render_summary(), id="summary-line")
            yield Static(self._tree_text, id="tree-body")

    def on_mount(self) -> None:
        # Hide the body initially — collapsed is the default.
        self.query_one("#tree-body", Static).display = False

    def watch_state(self, _old: str, new: str) -> None:
        """Reactive watcher — toggles the body's display attribute."""
        body = self.query_one("#tree-body", Static)
        body.display = (new == "expanded")
        # Update the summary line to flip the chevron orientation.
        summary = self.query_one("#summary-line", Static)
        summary.update(self._render_summary())

    def action_toggle(self) -> None:
        """Bound to Enter (see BINDINGS)."""
        self.state = "expanded" if self.state == "collapsed" else "collapsed"

    def _render_summary(self) -> str:
        chevron = "v" if self.state == "expanded" else ">"
        return f"{self._summary}  {chevron}"
