"""v1.1 plan-3 M9.3 — auto-mode block-budget pin.

Pins the contract:

* 3 consecutive `BLOCK` decisions → budget tripped.
* 20 total `BLOCK` decisions across the session → budget tripped (even
  with allows interleaved).
* `ALLOW` / `ASK` reset the consecutive-block counter; total counter
  monotonically increases.
* `/auto on` (after a pause) calls :func:`reset_block_budget` and
  clears both counters + the paused flag.
* :func:`is_paused` returns True iff the budget has tripped and not
  been reset.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.tool_call_classifier import (
    _BLOCK_BUDGETS,
    CONSECUTIVE_BLOCK_LIMIT,
    TOTAL_BLOCK_LIMIT,
    ClassifierDecision,
    Decision,
    get_block_budget,
    is_paused,
    record_classifier_decision,
    reset_block_budget,
)


@pytest.fixture(autouse=True)
def _isolate_budget_dict() -> None:
    """Each test starts with an empty per-process budget dict."""
    _BLOCK_BUDGETS.clear()
    yield
    _BLOCK_BUDGETS.clear()


def _block(reason: str = "destructive") -> ClassifierDecision:
    return ClassifierDecision(decision=Decision.BLOCK, rationale=reason)


def _allow() -> ClassifierDecision:
    return ClassifierDecision(decision=Decision.ALLOW, rationale="safe")


def _ask() -> ClassifierDecision:
    return ClassifierDecision(decision=Decision.ASK, rationale="ambiguous")


# ─── consecutive-block budget ────────────────────────────────────────────


def test_three_consecutive_blocks_trips_budget() -> None:
    sid = "session-A"
    assert record_classifier_decision(sid, _block()) is False
    assert record_classifier_decision(sid, _block()) is False
    assert record_classifier_decision(sid, _block()) is True  # 3rd trips
    assert is_paused(sid)


def test_two_blocks_then_allow_resets_consecutive() -> None:
    sid = "session-B"
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _allow())  # resets consecutive
    # Now another 2 blocks shouldn't trip (only 2 in a row again)
    assert record_classifier_decision(sid, _block()) is False
    assert record_classifier_decision(sid, _block()) is False
    assert not is_paused(sid)


def test_ask_also_resets_consecutive_counter() -> None:
    sid = "session-C"
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _ask())
    assert record_classifier_decision(sid, _block()) is False
    assert not is_paused(sid)


# ─── total-block budget ──────────────────────────────────────────────────


def test_twenty_total_blocks_trips_even_with_allows_interleaved() -> None:
    sid = "session-D"
    # Pattern: B, A, B, A, ... — never trips consecutive but does total.
    tripped = False
    for i in range(40):  # 20 blocks + 20 allows
        decision = _block() if i % 2 == 0 else _allow()
        result = record_classifier_decision(sid, decision)
        if result:
            tripped = True
            break
    assert tripped
    assert is_paused(sid)
    assert get_block_budget(sid).total_blocks == TOTAL_BLOCK_LIMIT


def test_total_block_limit_is_twenty() -> None:
    """Pin the magic number — if changed, this test must be updated explicitly."""
    assert TOTAL_BLOCK_LIMIT == 20


def test_consecutive_block_limit_is_three() -> None:
    """Pin the magic number — if changed, this test must be updated explicitly."""
    assert CONSECUTIVE_BLOCK_LIMIT == 3


# ─── reset semantics ─────────────────────────────────────────────────────


def test_reset_clears_counters_and_paused_flag() -> None:
    sid = "session-E"
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _block())
    record_classifier_decision(sid, _block())
    assert is_paused(sid)

    reset_block_budget(sid)

    assert not is_paused(sid)
    assert sid not in _BLOCK_BUDGETS


def test_reset_safe_when_session_unknown() -> None:
    """Resetting a session that never had a budget is a no-op, not an error."""
    reset_block_budget("never-existed")  # shouldn't raise


def test_paused_at_only_set_once_per_pause() -> None:
    """After tripping, additional blocks don't bump paused_at."""
    sid = "session-F"
    for _ in range(CONSECUTIVE_BLOCK_LIMIT):
        record_classifier_decision(sid, _block())
    paused_ts_initial = get_block_budget(sid).paused_at
    assert paused_ts_initial is not None

    # More blocks
    for _ in range(5):
        record_classifier_decision(sid, _block())

    paused_ts_after = get_block_budget(sid).paused_at
    assert paused_ts_after == paused_ts_initial


# ─── /auto on resets via slash command ──────────────────────────────────


def test_auto_on_clears_paused_session_via_runtime() -> None:
    """When `/auto on` runs and runtime.custom['m9_3_paused_session']
    is set, the slash command resets the budget for that session."""
    from opencomputer.agent.slash_commands_impl.auto_cmd import AutoCommand
    from plugin_sdk.runtime_context import RuntimeContext

    sid = "session-G"
    # Trip the budget
    for _ in range(CONSECUTIVE_BLOCK_LIMIT):
        record_classifier_decision(sid, _block())
    assert is_paused(sid)

    runtime = RuntimeContext()
    runtime.custom["m9_3_paused_session"] = sid

    cmd = AutoCommand()
    asyncio.new_event_loop().run_until_complete(cmd.execute("on", runtime))

    assert not is_paused(sid)
    assert "m9_3_paused_session" not in runtime.custom
