"""Tests for opencomputer.evolution.reward — RewardFunction Protocol and RuleBasedRewardFunction.

All tests are pure unit tests; no I/O, no mocks needed.
Trajectories are built with the helpers from trajectory.py (new_record, new_event, with_event).
"""

from __future__ import annotations

import dataclasses

import pytest

from opencomputer.evolution.reward import RewardFunction, RuleBasedRewardFunction
from opencomputer.evolution.trajectory import new_event, new_record, with_event

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SESSION = "sess-test"


def _tool_event(outcome: str, tool_name: str = "Read") -> object:
    return new_event(
        session_id=_SESSION,
        action_type="tool_call",
        tool_name=tool_name,
        outcome=outcome,
    )


def _user_event(text_starts_with: str | None = None) -> object:
    metadata = {}
    if text_starts_with is not None:
        metadata["text_starts_with"] = text_starts_with
    return new_event(
        session_id=_SESSION,
        action_type="user_reply",
        outcome="success",
        metadata=metadata,
    )


def _finished_record(*, completion_flag: bool = False, events=()):
    """Return a record with ended_at set."""
    rec = new_record(_SESSION)
    rec = dataclasses.replace(rec, ended_at=1_700_001_000.0, completion_flag=completion_flag)
    for ev in events:
        rec = with_event(rec, ev)
    return rec


# ---------------------------------------------------------------------------
# 1. Protocol runtime-checkable
# ---------------------------------------------------------------------------


def test_reward_function_protocol_runtime_checkable() -> None:
    """RuleBasedRewardFunction() is an instance of the runtime_checkable RewardFunction Protocol."""
    fn = RuleBasedRewardFunction()
    assert isinstance(fn, RewardFunction)


# ---------------------------------------------------------------------------
# 2. Default weights sum to one
# ---------------------------------------------------------------------------


def test_default_weights_sum_to_one() -> None:
    """RuleBasedRewardFunction() constructs without error (weights default to 0.5+0.3+0.2=1.0)."""
    fn = RuleBasedRewardFunction()
    assert fn.weight_tool_success == 0.5
    assert fn.weight_user_confirmed == 0.3
    assert fn.weight_task_completed == 0.2


# ---------------------------------------------------------------------------
# 3. Invalid weights rejected
# ---------------------------------------------------------------------------


def test_invalid_weights_rejected() -> None:
    """Weights that don't sum to 1.0 raise ValueError."""
    with pytest.raises(ValueError, match="1.0"):
        RuleBasedRewardFunction(
            weight_tool_success=0.9,
            weight_user_confirmed=0.05,
            weight_task_completed=0.0,
        )


# ---------------------------------------------------------------------------
# 4. In-flight record returns None
# ---------------------------------------------------------------------------


def test_in_flight_returns_none() -> None:
    """Record with ended_at=None yields None (reward undefined for in-flight sessions)."""
    rec = new_record(_SESSION)  # ended_at is None by default
    fn = RuleBasedRewardFunction()
    assert fn.score(rec) is None


# ---------------------------------------------------------------------------
# 5. Empty record returns 0.0
# ---------------------------------------------------------------------------


def test_empty_record_returns_zero() -> None:
    """Record with ended_at set but no events yields 0.0."""
    rec = _finished_record()
    fn = RuleBasedRewardFunction()
    assert fn.score(rec) == 0.0


# ---------------------------------------------------------------------------
# 6. All tool calls succeed → full tool signal
# ---------------------------------------------------------------------------


def test_all_tool_calls_succeed_full_signal() -> None:
    """3 successful tool_calls, no user_reply, no completion_flag → 0.5."""
    events = [_tool_event("success"), _tool_event("success"), _tool_event("success")]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 7. All tool calls fail → zero signal
# ---------------------------------------------------------------------------


def test_all_tool_calls_fail_zero_signal() -> None:
    """3 failed tool_calls, no user_reply → 0.0."""
    events = [_tool_event("failure"), _tool_event("failure"), _tool_event("failure")]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 8. Mixed tool outcomes
# ---------------------------------------------------------------------------


def test_mixed_tool_outcomes() -> None:
    """2 success + 2 failure → tool_success_rate=0.5 → contributes 0.25 → total 0.25."""
    events = [
        _tool_event("success"),
        _tool_event("success"),
        _tool_event("failure"),
        _tool_event("failure"),
    ]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    assert score == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 9. Other outcome values count as not-success
# ---------------------------------------------------------------------------


def test_other_outcomes_count_as_not_success() -> None:
    """1 success + 1 blocked_by_hook + 1 user_cancelled → tool_success_rate=1/3."""
    events = [
        _tool_event("success"),
        _tool_event("blocked_by_hook"),
        _tool_event("user_cancelled"),
    ]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    expected = 0.5 * (1 / 3)  # ~0.1667
    assert score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 10. User confirmed — negative cue
# ---------------------------------------------------------------------------


def test_user_confirmed_negative_cue() -> None:
    """Last user_reply with text_starts_with='stop please' → user_confirmed=0.0."""
    events = [_user_event("stop please")]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    # tool_success=0.0 (no tools), user_confirmed=0.0, task_completed=0.0
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 11. User confirmed — positive (no negative cue) → contributes 0.3
# ---------------------------------------------------------------------------


def test_user_confirmed_positive_when_no_negative_cue() -> None:
    """Last user_reply with text_starts_with='thanks great' → user_confirmed=1.0 → 0.3."""
    events = [_user_event("thanks great")]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    # tool_success=0.0, user_confirmed=1.0, task_completed=0.0
    assert score == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 12. User confirmed — neutral when text_starts_with missing
# ---------------------------------------------------------------------------


def test_user_confirmed_neutral_when_text_missing() -> None:
    """Last user_reply has no text_starts_with in metadata → user_confirmed=0.5 → 0.15."""
    events = [_user_event(None)]  # no text_starts_with
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    # tool_success=0.0, user_confirmed=0.5, task_completed=0.0
    assert score == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# 13. user_confirmed picks the LAST user_reply
# ---------------------------------------------------------------------------


def test_user_confirmed_picks_last_user_reply() -> None:
    """Only the last user_reply's text_starts_with is checked.

    Setup: first two have positive text ('thanks', 'great'), last has 'no...' → result 0.0.
    """
    events = [
        _user_event("thanks for that"),
        _user_event("great work"),
        _user_event("no that is wrong"),
    ]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    # user_confirmed=0.0 (last reply starts with 'no')
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 14. task_completed flag contributes 0.2
# ---------------------------------------------------------------------------


def test_task_completed_flag_contributes() -> None:
    """completion_flag=True with no events → tool_success=0, user_confirmed=0, task_completed=0.2."""
    rec = _finished_record(completion_flag=True)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    assert score == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# 15. Combined perfect score
# ---------------------------------------------------------------------------


def test_combined_perfect_score() -> None:
    """1 tool_call success + 1 user_reply with 'great' + completion_flag=True → 1.0."""
    events = [
        _tool_event("success"),
        _user_event("great job"),
    ]
    rec = _finished_record(completion_flag=True, events=events)
    fn = RuleBasedRewardFunction()
    score = fn.score(rec)
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 16. Score clamped to [0.0, 1.0]
# ---------------------------------------------------------------------------


def test_combined_score_clamped_to_unit_interval() -> None:
    """Score never exceeds 1.0 or drops below 0.0 across various inputs."""
    fn = RuleBasedRewardFunction()

    samples = [
        _finished_record(),
        _finished_record(completion_flag=True),
        _finished_record(events=[_tool_event("success")]),
        _finished_record(events=[_tool_event("failure"), _user_event("stop")]),
        _finished_record(
            completion_flag=True,
            events=[_tool_event("success"), _user_event("great")],
        ),
    ]

    for rec in samples:
        s = fn.score(rec)
        assert s is not None
        assert 0.0 <= s <= 1.0, f"Score {s} out of [0, 1] for record {rec}"


# ---------------------------------------------------------------------------
# 17. Each negative cue is recognized (case-insensitive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "no thanks",
        "No thanks",
        "NO THANKS",
        "stop doing that",
        "Stop it",
        "STOP",
        "wrong answer",
        "Wrong",
        "undo that",
        "Undo please",
        "revert the change",
        "Revert",
        "cancel this",
        "Cancel everything",
    ],
)
def test_each_negative_cue_is_recognized(text: str) -> None:
    """All negative-cue prefixes (case-insensitive) yield user_confirmed=0.0."""
    events = [_user_event(text)]
    rec = _finished_record(events=events)
    fn = RuleBasedRewardFunction()
    # With no tools and no completion flag, score equals user_confirmed * 0.3.
    # If user_confirmed is 0.0 the total must be 0.0.
    score = fn.score(rec)
    assert score == pytest.approx(0.0), (
        f"Expected negative cue '{text}' to yield 0.0, got {score}"
    )
