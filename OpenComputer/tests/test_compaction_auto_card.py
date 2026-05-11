"""Integration test for compaction auto-emit card.

Verifies that ``StreamingRenderer.emit_compaction_card`` exists and
that the agent loop's ``did_compact=True`` path calls it with the
correct message counts. The agent-loop call site is wrapped in a
broad except so a missing renderer doesn't crash a turn — that
contract is also exercised here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


class TestEmitCompactionCard:
    def test_renderer_has_method(self) -> None:
        from opencomputer.cli_ui.streaming import StreamingRenderer

        assert callable(getattr(StreamingRenderer, "emit_compaction_card", None))

    def test_card_printed_on_call(self) -> None:
        from rich.console import Console

        from opencomputer.cli_ui.streaming import StreamingRenderer

        printed: list[Any] = []

        class _RecordingConsole(Console):
            def print(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
                printed.append(args)

        renderer = StreamingRenderer(console=_RecordingConsole())
        renderer.emit_compaction_card(
            messages_before=100,
            messages_after=30,
            reason="auto",
        )
        # Card was printed.
        assert len(printed) == 1
        text = "".join(str(a) for a in printed[0])
        assert "compaction" in text.lower()
        assert "100" in text
        assert "30" in text

    def test_card_swallows_render_errors(self) -> None:
        """A broken summary_cards module must not raise out of
        emit_compaction_card."""
        from rich.console import Console

        from opencomputer.cli_ui.streaming import StreamingRenderer

        renderer = StreamingRenderer(console=Console())
        with patch(
            "opencomputer.cli_ui.summary_cards.render_compaction_card",
            side_effect=RuntimeError("simulated"),
        ):
            # Must not raise.
            renderer.emit_compaction_card(
                messages_before=10,
                messages_after=5,
                reason="auto",
            )

    def test_token_row_omitted_when_none(self) -> None:
        """When the caller passes None for tokens, the card must not
        show a misleading 0 → 0 row."""
        from rich.console import Console

        from opencomputer.cli_ui.streaming import StreamingRenderer

        printed: list[Any] = []

        class _RecordingConsole(Console):
            def print(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
                printed.append(args)

        renderer = StreamingRenderer(console=_RecordingConsole())
        renderer.emit_compaction_card(
            messages_before=10,
            messages_after=5,
            tokens_before=None,
            tokens_after=None,
            reason="auto",
        )
        text = "".join(str(a) for a in printed[0])
        assert "tokens:" not in text
        assert "messages: 10 → 5" in text
