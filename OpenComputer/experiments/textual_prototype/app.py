"""Standalone Textual app hosting one CollapsibleThinkingCard.

Run with:
    python -m experiments.textual_prototype.app

A human runs this to manually verify the toggle works in a real
terminal. The pytest smoke test in ``test_widget.py`` covers the
non-interactive path. This file is the interactive counterpart —
useful for taking a screenshot for the README.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header

from experiments.textual_prototype.widget import CollapsibleThinkingCard


_SAMPLE_SUMMARY = "Wrote a haiku about sloths"
_SAMPLE_TREE = """\
└── Reasoning turn 1
    ├── Read: poems.txt
    ├── Edit: drafts/haiku.txt
    └── Write: drafts/haiku.txt
"""


class ThinkingDemoApp(App):
    """Single-card demo. Card is auto-focused so Enter routes to it."""

    CSS = """
    CollapsibleThinkingCard {
        border: round $primary;
        padding: 0 2;
        width: auto;
        height: auto;
        margin: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield CollapsibleThinkingCard(
                summary=_SAMPLE_SUMMARY, tree_text=_SAMPLE_TREE
            )
        yield Footer()

    def on_mount(self) -> None:
        # Focus the card so Enter goes to its binding rather than the
        # Footer or App-level default.
        card = self.query_one(CollapsibleThinkingCard)
        card.focus()


if __name__ == "__main__":
    ThinkingDemoApp().run()
