"""Tests for ``opencomputer/cli_ui/summary_cards.py``.

PI's ``branch-summary-message.ts`` and ``compaction-summary-message.ts``
render structured chat cards when a session forks or a compaction
completes. We don't have PI's TUI component system, so the OC port
renders ASCII / Unicode box-drawing cards that work in any terminal.

The cards are returned as plain strings — callers (``/branch`` slash,
the agent loop's compaction emission point) embed them in the chat
output. This keeps the SlashCommandResult contract unchanged.
"""

from __future__ import annotations

from opencomputer.cli_ui.summary_cards import (
    render_branch_card,
    render_compaction_card,
)


class TestBranchCard:
    def test_renders_title_and_id(self) -> None:
        card = render_branch_card(
            new_session_id="abc123def456",
            title="try-different-approach",
            messages_copied=42,
        )
        assert "try-different-approach" in card
        # 8-char id prefix is enough for human identification.
        assert "abc123de" in card
        assert "42" in card

    def test_includes_resume_hint(self) -> None:
        card = render_branch_card(
            new_session_id="abc123",
            title="fork",
            messages_copied=0,
        )
        assert "oc chat --resume" in card
        assert "abc123" in card

    def test_uses_box_drawing(self) -> None:
        card = render_branch_card(
            new_session_id="x" * 32,
            title="fork",
            messages_copied=1,
        )
        # A "card" should be visually distinct — at least one Unicode
        # box-drawing character so it stands out in the chat scroll.
        box_chars = set("┌┐└┘─│├┤┬┴┼╭╮╯╰")
        assert any(c in card for c in box_chars), card

    def test_empty_title_safe(self) -> None:
        # A bare /branch with no title shouldn't crash.
        card = render_branch_card(
            new_session_id="abc",
            title="",
            messages_copied=0,
        )
        assert "abc" in card


class TestCompactionCard:
    def test_renders_before_after_counts(self) -> None:
        card = render_compaction_card(
            messages_before=120,
            messages_after=45,
            tokens_before=50_000,
            tokens_after=12_000,
            reason="auto",
        )
        assert "120" in card
        assert "45" in card
        # Token savings should be visible — either as numbers or "%".
        assert "%" in card or "saved" in card.lower()

    def test_uses_box_drawing(self) -> None:
        card = render_compaction_card(
            messages_before=100,
            messages_after=20,
            tokens_before=1000,
            tokens_after=200,
            reason="manual",
        )
        box_chars = set("┌┐└┘─│├┤┬┴┼╭╮╯╰")
        assert any(c in card for c in box_chars), card

    def test_no_savings_no_division_error(self) -> None:
        # Edge case: zero-before. Card must not crash.
        card = render_compaction_card(
            messages_before=0,
            messages_after=0,
            tokens_before=0,
            tokens_after=0,
            reason="auto",
        )
        # Whatever the card says is fine as long as it's a string.
        assert isinstance(card, str)
        assert len(card) > 0

    def test_increase_handled(self) -> None:
        # Pathological: compaction grew tokens (shouldn't happen, but
        # the card must not blow up on negative savings).
        card = render_compaction_card(
            messages_before=10,
            messages_after=10,
            tokens_before=100,
            tokens_after=200,
            reason="aux-summary",
        )
        assert isinstance(card, str)
        assert "200" in card or "100" in card

    def test_token_row_omitted_when_none(self) -> None:
        # Honest no-data path — pass None for token counts and the
        # row vanishes rather than showing a misleading "0 → 0".
        card = render_compaction_card(
            messages_before=10,
            messages_after=5,
            tokens_before=None,
            tokens_after=None,
            reason="manual",
        )
        # Message row should still be there.
        assert "messages: 10 → 5" in card
        # Token row should NOT be there.
        assert "tokens:" not in card

    def test_token_row_present_when_provided(self) -> None:
        card = render_compaction_card(
            messages_before=10,
            messages_after=5,
            tokens_before=500,
            tokens_after=100,
            reason="auto",
        )
        assert "tokens:" in card

    def test_token_row_omitted_when_only_one_side_provided(self) -> None:
        # Defensive: passing one side as None and the other as int is
        # asymmetric — better to omit than to render half a row.
        card = render_compaction_card(
            messages_before=10,
            messages_after=5,
            tokens_before=500,
            tokens_after=None,
            reason="auto",
        )
        assert "tokens:" not in card
