"""Smoke test for the CollapsibleThinkingCard widget.

Uses Textual's ``App.run_test()`` + ``Pilot`` harness to drive the
widget without a real terminal. The contract:

1. Initial state is *collapsed* — the summary text is visible, the
   tree is NOT visible.
2. Pressing Enter expands — the tree becomes visible.
3. Pressing Enter again collapses — back to summary-only.
4. The widget exposes a ``state`` property the test can assert on
   (so we don't depend on querying child node visibility, which is
   Textual-internal).
"""
from __future__ import annotations

import pytest

# Skip the whole file gracefully if textual isn't installed (this is an
# experiment — the main suite shouldn't fail when textual is absent).
textual = pytest.importorskip("textual")
from textual.app import App, ComposeResult  # noqa: E402

from experiments.textual_prototype.widget import (  # noqa: E402
    CollapsibleThinkingCard,
)


class _ProbeApp(App):
    """Single-widget host for the test."""

    def __init__(self, summary: str, tree_text: str) -> None:
        super().__init__()
        self._summary = summary
        self._tree_text = tree_text
        self.card: CollapsibleThinkingCard | None = None

    def compose(self) -> ComposeResult:
        self.card = CollapsibleThinkingCard(
            summary=self._summary, tree_text=self._tree_text
        )
        yield self.card


@pytest.mark.asyncio
async def test_card_starts_collapsed() -> None:
    app = _ProbeApp(summary="Wrote a haiku", tree_text="L1\nL2\nL3")
    async with app.run_test():
        assert app.card is not None
        assert app.card.state == "collapsed"


@pytest.mark.asyncio
async def test_enter_toggles_to_expanded() -> None:
    app = _ProbeApp(summary="Wrote a haiku", tree_text="L1\nL2\nL3")
    async with app.run_test() as pilot:
        assert app.card is not None
        app.set_focus(app.card)
        await pilot.press("enter")
        assert app.card.state == "expanded"


@pytest.mark.asyncio
async def test_enter_again_collapses() -> None:
    app = _ProbeApp(summary="Wrote a haiku", tree_text="L1\nL2\nL3")
    async with app.run_test() as pilot:
        assert app.card is not None
        app.set_focus(app.card)
        await pilot.press("enter")
        await pilot.press("enter")
        assert app.card.state == "collapsed"
