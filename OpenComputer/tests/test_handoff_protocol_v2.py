"""Tests for opencomputer.agent.handoff.protocol_v2."""
from __future__ import annotations

import pytest

from opencomputer.agent.handoff.models import HandoffWarranted
from opencomputer.agent.handoff.protocol_v2 import (
    MAX_BODY_CHARS,
    PROTOCOL_VERSION,
    parse_handoff_response,
    render_handoff_prompt,
)


class TestRenderHandoffPrompt:
    def test_minimal_valid_inputs(self) -> None:
        prompt = render_handoff_prompt(
            source_profile="default",
            target_profile="stocks",
            recent_user_messages=["what's NVDA doing"],
            recent_assistant_messages=["NVDA is up 2%"],
        )
        assert PROTOCOL_VERSION == "handoff-v2"
        assert "Universal Handoff Protocol v2.0" in prompt.system
        assert "'default'" in prompt.system
        assert "'stocks'" in prompt.system
        assert "HANDOFF_NOT_WARRANTED:" in prompt.system
        assert "Step 0" in prompt.system
        assert "what's NVDA doing" in prompt.user
        assert "NVDA is up 2%" in prompt.user
        # R12 — handoff is data not authority
        assert "DATA" in prompt.system
        # R10 — portable
        assert "portable" in prompt.system or "Portable" in prompt.system

    def test_empty_source_raises(self) -> None:
        with pytest.raises(ValueError, match="source_profile"):
            render_handoff_prompt(
                source_profile="  ", target_profile="stocks",
                recent_user_messages=("x",), recent_assistant_messages=(),
            )

    def test_empty_target_raises(self) -> None:
        with pytest.raises(ValueError, match="target_profile"):
            render_handoff_prompt(
                source_profile="default", target_profile="",
                recent_user_messages=("x",), recent_assistant_messages=(),
            )

    def test_identical_profiles_raise(self) -> None:
        with pytest.raises(ValueError, match="identical"):
            render_handoff_prompt(
                source_profile="x", target_profile="x",
                recent_user_messages=("a",), recent_assistant_messages=(),
            )

    def test_negative_max_turns_raises(self) -> None:
        with pytest.raises(ValueError, match="max_turns"):
            render_handoff_prompt(
                source_profile="a", target_profile="b",
                recent_user_messages=("a",), recent_assistant_messages=(),
                max_turns=0,
            )

    def test_non_string_message_is_skipped(self) -> None:
        # Adversarial input — mixed types
        prompt = render_handoff_prompt(
            source_profile="a", target_profile="b",
            recent_user_messages=["valid", None, 42, "also valid"],  # type: ignore[list-item]
            recent_assistant_messages=("ok",),
        )
        assert "valid" in prompt.user
        assert "also valid" in prompt.user
        assert "None" not in prompt.user.split("---")[1]

    def test_huge_message_truncated_per_message(self) -> None:
        # 10K-char message — should land truncated, not in full
        huge = "X" * 10_000
        prompt = render_handoff_prompt(
            source_profile="a", target_profile="b",
            recent_user_messages=(huge,),
            recent_assistant_messages=("ok",),
        )
        # The whole 10K must NOT round-trip; the truncation marker fires
        assert "truncated" in prompt.user
        # The first 4000 chars do round-trip
        assert "X" * 100 in prompt.user

    def test_empty_history_yields_marker(self) -> None:
        prompt = render_handoff_prompt(
            source_profile="a", target_profile="b",
            recent_user_messages=(),
            recent_assistant_messages=(),
        )
        assert "(no prior messages)" in prompt.user

    def test_max_turns_clamps_tail(self) -> None:
        users = tuple(f"u{i}" for i in range(20))
        prompt = render_handoff_prompt(
            source_profile="a", target_profile="b",
            recent_user_messages=users,
            recent_assistant_messages=tuple(f"a{i}" for i in range(20)),
            max_turns=3,
        )
        # Last 3 user messages must be present
        assert "u19" in prompt.user
        assert "u17" in prompt.user
        # Earlier ones must NOT be present
        assert "u0\n" not in prompt.user
        assert "u1\n" not in prompt.user


class TestParseHandoffResponse:
    def test_empty_response_returns_trivial(self) -> None:
        out = parse_handoff_response("")
        assert out.warranted == HandoffWarranted.NO_TRIVIAL
        assert out.body == ""

    def test_none_response_returns_trivial(self) -> None:
        out = parse_handoff_response(None)  # type: ignore[arg-type]
        assert out.warranted == HandoffWarranted.NO_TRIVIAL

    def test_not_warranted_trivial_reason(self) -> None:
        out = parse_handoff_response(
            "HANDOFF_NOT_WARRANTED: single Q&A, trivial chat",
        )
        assert out.warranted == HandoffWarranted.NO_TRIVIAL
        assert "trivial" in out.reason

    def test_not_warranted_empty_reason(self) -> None:
        out = parse_handoff_response(
            "HANDOFF_NOT_WARRANTED: no user messages found",
        )
        assert out.warranted == HandoffWarranted.NO_EMPTY

    def test_not_warranted_completed_reason(self) -> None:
        out = parse_handoff_response(
            "HANDOFF_NOT_WARRANTED: task was completed in this session",
        )
        assert out.warranted == HandoffWarranted.NO_COMPLETED

    def test_normal_body_returns_yes(self) -> None:
        body = (
            "**Collaboration:** Saksham is a Mac user asking about NVDA. "
            "**State:** NVDA up 2%. "
            "**Next:** Continue with options analysis."
        )
        out = parse_handoff_response(body)
        assert out.warranted == HandoffWarranted.YES
        assert "NVDA" in out.body

    def test_body_over_max_truncated_at_paragraph(self) -> None:
        # Build a body where the para break sits before the limit
        para1 = "A" * (MAX_BODY_CHARS - 200)
        para2 = "B" * 500
        body = f"{para1}\n\n{para2}"
        out = parse_handoff_response(body)
        assert out.warranted == HandoffWarranted.YES
        assert len(out.body) <= MAX_BODY_CHARS + 50  # marker overhead
        assert "truncated" in out.body
        # Truncation happens at the paragraph boundary
        assert not out.body.endswith("B" * 100)

    def test_body_with_no_paragraph_breaks_hard_truncated(self) -> None:
        body = "Z" * (MAX_BODY_CHARS + 1000)
        out = parse_handoff_response(body)
        assert out.warranted == HandoffWarranted.YES
        assert "truncated" in out.body

    def test_reason_truncated_at_max(self) -> None:
        # 1000-char reason — should clamp
        out = parse_handoff_response(
            "HANDOFF_NOT_WARRANTED: " + "x" * 1000,
        )
        assert out.warranted == HandoffWarranted.NO_TRIVIAL  # default bucket
        assert len(out.reason) <= 200

    def test_not_warranted_prefix_with_extra_lines_ignored(self) -> None:
        # Model emits the sentinel but also continues — we trust the sentinel
        out = parse_handoff_response(
            "HANDOFF_NOT_WARRANTED: nothing substantive\nbut here's some content anyway"
        )
        assert out.warranted == HandoffWarranted.NO_TRIVIAL
        assert out.reason == "nothing substantive"

    def test_whitespace_around_response_stripped(self) -> None:
        out = parse_handoff_response("   \n\nactual content here\n\n   ")
        assert out.warranted == HandoffWarranted.YES
        assert out.body.strip() == "actual content here"
