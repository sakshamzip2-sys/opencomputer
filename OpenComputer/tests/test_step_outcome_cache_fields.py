"""StepOutcome carries cache_read_tokens and cache_write_tokens."""

from opencomputer.agent.step import StepOutcome
from plugin_sdk.core import Message, StopReason


def test_step_outcome_default_cache_zero():
    out = StepOutcome(
        stop_reason=StopReason.END_TURN,
        assistant_message=Message(role="assistant", content="ok"),
    )
    assert out.cache_read_tokens == 0
    assert out.cache_write_tokens == 0


def test_step_outcome_carries_cache_tokens():
    out = StepOutcome(
        stop_reason=StopReason.END_TURN,
        assistant_message=Message(role="assistant", content="ok"),
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=1234,
        cache_write_tokens=200,
    )
    assert out.cache_read_tokens == 1234
    assert out.cache_write_tokens == 200
