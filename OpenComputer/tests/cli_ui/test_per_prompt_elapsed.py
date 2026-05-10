"""Tests for cli_ui.per_prompt_elapsed."""

from opencomputer.cli_ui.per_prompt_elapsed import PromptClock


def test_starts_blank() -> None:
    clock = PromptClock(_now=lambda: 0.0)
    assert clock.render() == ""


def test_running_renders_live() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 12.4
    assert clock.render() == "⏱ 12s"


def test_frozen_after_stop() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 32.0
    clock.stop()
    rendered = clock.render()
    assert rendered.startswith("⏲ 32s")
    assert "/" in rendered  # separator between prompt and total


def test_total_session_time_includes_after_stop() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 32.0
    clock.stop()
    t[0] = 100.0  # session continues; render after stop
    rendered = clock.render()
    assert "32s" in rendered
    assert "1m 40s" in rendered  # total = 100s


def test_reset_drops_state() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 5.0
    clock.reset()
    assert clock.render() == ""


def test_minute_threshold_format() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 125.0
    assert clock.render() == "⏱ 2m 5s"


def test_double_stop_idempotent() -> None:
    t = [0.0]
    clock = PromptClock(_now=lambda: t[0])
    clock.start()
    t[0] = 10.0
    clock.stop()
    first = clock.render()
    t[0] = 50.0
    clock.stop()  # should not advance frozen value
    second = clock.render()
    # First component (prompt elapsed) must remain at 10s; total grows.
    assert second.split(" / ")[0] == first.split(" / ")[0]
