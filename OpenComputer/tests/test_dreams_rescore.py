"""Tests for ``opencomputer.agent.dreams_rescore`` — Gap 3 from
``self-evolution-gaps-deep-dive.md``.

Coverage:
- Parser: empty, single, multi-entry, tools-prefix, no-date, malformed,
  embedded arrows in answer, embedded newlines, truncation.
- Rescorer: happy path, error per-entry, threshold gating, callback
  exception isolation, clamp on out-of-range scores.
- Promotion-line render: only candidates surface, format roundtrip.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from opencomputer.agent.dreams_rescore import (
    DreamEntry,
    RescoreOutcome,
    parse_dreams_md,
    render_promotion_candidates,
    rescore_entries,
)

# ── parser ──────────────────────────────────────────────────────────


def test_parse_empty_string_returns_empty_list() -> None:
    assert parse_dreams_md("") == []


def test_parse_whitespace_only_returns_empty_list() -> None:
    assert parse_dreams_md("\n\n   \n\t\n") == []


def test_parse_single_entry_with_date_and_qa() -> None:
    content = "- 2026-05-10: Q: hello → A: hi"
    entries = parse_dreams_md(content)
    assert len(entries) == 1
    e = entries[0]
    assert e.date == "2026-05-10"
    assert e.tools == ()
    assert e.question == "hello"
    assert e.answer == "hi"
    assert "hello" in e.raw_text


def test_parse_entry_with_tools_prefix() -> None:
    content = "- 2026-05-10: [tools: Bash] Q: ls / → A: bin etc"
    entries = parse_dreams_md(content)
    assert len(entries) == 1
    assert entries[0].tools == ("Bash",)


def test_parse_entry_with_multiple_tools() -> None:
    content = "- 2026-05-10: [tools: Bash, Read, Edit] Q: x → A: y"
    entries = parse_dreams_md(content)
    assert entries[0].tools == ("Bash", "Read", "Edit")


def test_parse_multi_entry_separated_by_blank_lines() -> None:
    content = (
        "- 2026-05-10: Q: hello → A: hi\n"
        "\n"
        "- 2026-05-10: Q: bye → A: cya\n"
        "\n"
        "- 2026-05-11: Q: yo → A: hey"
    )
    entries = parse_dreams_md(content)
    assert len(entries) == 3
    assert [e.question for e in entries] == ["hello", "bye", "yo"]


def test_parse_handles_embedded_arrows_in_answer() -> None:
    """The QA regex must not split on a stray → inside the answer text."""
    content = "- 2026-05-10: Q: tell me → A: A → B → C is the chain"
    entries = parse_dreams_md(content)
    assert len(entries) == 1
    # The regex is non-greedy on Q-body so it picks the FIRST →. The
    # answer contains the remaining arrows verbatim.
    assert entries[0].answer.endswith("is the chain")


def test_parse_skips_block_with_no_qa_and_no_date(caplog) -> None:
    """A noisy line with neither date prefix nor Q:/A: markers is dropped
    with a WARN log — load-bearing for the "loud failure" rule."""
    content = "this is just garbage text\n\n- 2026-05-10: Q: x → A: y"
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.dreams_rescore"):
        entries = parse_dreams_md(content)
    assert len(entries) == 1
    assert entries[0].question == "x"
    assert any("skipping block" in r.message.lower() for r in caplog.records)


def test_parse_max_entries_truncates() -> None:
    content = "\n\n".join(
        f"- 2026-05-{10 + i:02d}: Q: q{i} → A: a{i}" for i in range(20)
    )
    entries = parse_dreams_md(content, max_entries=5)
    assert len(entries) == 5
    assert [e.question for e in entries] == [f"q{i}" for i in range(5)]


def test_parse_handles_no_date_but_real_qa() -> None:
    """Some pre-2026 entries may lack the date prefix. Still parseable."""
    content = "- Q: legacy entry → A: still works"
    entries = parse_dreams_md(content)
    assert len(entries) == 1
    assert entries[0].date == ""
    assert entries[0].question == "legacy entry"


def test_parse_real_dreams_md_sample() -> None:
    """Smoke test against a fragment of real-machine DREAMS.md content."""
    content = (
        "- 2026-05-10: [tools: AppleScriptRun] Q: Pause it again → A: Paused.\n"
        "\n"
        "- 2026-05-10: Q: Hello → A: done\n"
        "\n"
        "- 2026-05-10: [tools: Bash] Q: can i use oc webui → A: Yes - and someone already did the work to make it possible."
    )
    entries = parse_dreams_md(content)
    assert len(entries) == 3
    assert entries[0].tools == ("AppleScriptRun",)
    assert entries[1].tools == ()
    assert entries[2].tools == ("Bash",)
    assert entries[1].question == "Hello"


# ── rescorer ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescore_happy_path_clamps_and_flags_promotion() -> None:
    entries = [
        DreamEntry(raw_text="t1", date="2026-05-10", tools=(), question="q1", answer="a1"),
        DreamEntry(raw_text="t2", date="2026-05-10", tools=(), question="q2", answer="a2"),
    ]
    scores = iter([0.9, 0.3])

    async def fake_score(_: str) -> float:
        return next(scores)

    outcomes = await rescore_entries(
        entries, score_fn=fake_score, promote_threshold=0.75
    )
    assert len(outcomes) == 2
    assert outcomes[0].new_score == 0.9
    assert outcomes[0].promoted_candidate is True
    assert outcomes[1].new_score == 0.3
    assert outcomes[1].promoted_candidate is False


@pytest.mark.asyncio
async def test_rescore_clamps_out_of_range_score() -> None:
    """Provider returning 1.7 or -0.4 must be clamped to [0, 1]."""
    entries = [DreamEntry(raw_text="t", date="", tools=(), question="q", answer="a")]
    high_score = iter([1.7])

    async def too_high(_: str) -> float:
        return next(high_score)

    out = await rescore_entries(entries, score_fn=too_high)
    assert out[0].new_score == 1.0

    low_score = iter([-0.4])

    async def too_low(_: str) -> float:
        return next(low_score)

    out2 = await rescore_entries(entries, score_fn=too_low)
    assert out2[0].new_score == 0.0


@pytest.mark.asyncio
async def test_rescore_records_per_entry_error_and_continues(caplog) -> None:
    """One provider blip must not abort the rescore — the failing entry
    is recorded with ``error=...`` and the next entry rescores normally.
    """
    entries = [
        DreamEntry(raw_text="t1", date="", tools=(), question="q1", answer="a1"),
        DreamEntry(raw_text="t2", date="", tools=(), question="q2", answer="a2"),
    ]
    state = {"count": 0}

    async def flaky(text: str) -> float:
        state["count"] += 1
        if state["count"] == 1:
            raise RuntimeError("provider boom")
        return 0.42

    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.dreams_rescore"):
        outcomes = await rescore_entries(entries, score_fn=flaky)

    assert outcomes[0].error == "RuntimeError: provider boom"
    assert outcomes[0].new_score == 0.0
    assert outcomes[0].promoted_candidate is False
    assert outcomes[1].error is None
    assert outcomes[1].new_score == 0.42


@pytest.mark.asyncio
async def test_rescore_progress_callback_exception_does_not_abort() -> None:
    """A buggy progress callback must not break the rescore loop."""
    entries = [
        DreamEntry(raw_text="t1", date="", tools=(), question="q1", answer="a1"),
    ]

    async def good_score(_: str) -> float:
        return 0.5

    def bad_progress(_i: int, _t: int) -> None:
        raise ValueError("buggy callback")

    out = await rescore_entries(entries, score_fn=good_score, on_progress=bad_progress)
    assert len(out) == 1
    assert out[0].new_score == 0.5


@pytest.mark.asyncio
async def test_rescore_with_no_qa_does_not_promote_even_at_high_score() -> None:
    """Entry with empty Q or A must NOT be marked as a promotion
    candidate even if it scores high — there's nothing to write to
    MEMORY.md."""
    entries = [
        DreamEntry(raw_text="t", date="", tools=(), question="", answer=""),
    ]

    async def high(_: str) -> float:
        return 0.99

    out = await rescore_entries(entries, score_fn=high, promote_threshold=0.5)
    assert out[0].new_score == 0.99
    assert out[0].promoted_candidate is False


# ── promotion-line render ───────────────────────────────────────────


def test_render_promotion_candidates_only_promotes_flagged() -> None:
    e1 = DreamEntry(raw_text="r1", date="2026-05-10", tools=(), question="q1", answer="a1")
    e2 = DreamEntry(raw_text="r2", date="2026-05-11", tools=(), question="q2", answer="a2")
    outcomes = [
        RescoreOutcome(entry=e1, new_score=0.9, promoted_candidate=True),
        RescoreOutcome(entry=e2, new_score=0.4, promoted_candidate=False),
    ]
    lines = render_promotion_candidates(outcomes)
    assert len(lines) == 1
    assert lines[0] == "- 2026-05-10: Q: q1 → A: a1"


def test_render_promotion_candidates_handles_missing_date() -> None:
    e = DreamEntry(raw_text="r", date="", tools=(), question="q", answer="a")
    outcomes = [RescoreOutcome(entry=e, new_score=0.9, promoted_candidate=True)]
    lines = render_promotion_candidates(outcomes)
    assert lines == ["- Q: q → A: a"]


def test_render_promotion_candidates_empty_input() -> None:
    assert render_promotion_candidates([]) == []


def test_rescore_outcome_display_question_truncates() -> None:
    long_q = "x" * 200
    e = DreamEntry(raw_text="r", date="", tools=(), question=long_q, answer="a")
    o = RescoreOutcome(entry=e, new_score=0.5)
    assert o.display_question == "x" * 60 + "…"


def test_rescore_outcome_display_question_no_question() -> None:
    e = DreamEntry(raw_text="r", date="", tools=(), question="", answer="a")
    o = RescoreOutcome(entry=e, new_score=0.5)
    assert o.display_question == "(no Q)"


@pytest.mark.asyncio
async def test_rescore_passes_raw_text_not_question_only() -> None:
    """Privacy contract: the score function receives the SAME ``raw_text``
    the original dreaming-v2 score gate would have seen — not the parsed
    Q/A separately. This keeps the rescore behaviorally comparable to
    the gate it's diagnosing."""
    captured: list[str] = []

    async def capturing_score(text: str) -> float:
        captured.append(text)
        return 0.5

    e = DreamEntry(
        raw_text="this is the exact original block content",
        date="2026-05-10",
        tools=("Bash",),
        question="q",
        answer="a",
    )
    await rescore_entries([e], score_fn=capturing_score)
    assert captured == ["this is the exact original block content"]


# ── CLI integration ─────────────────────────────────────────────────


def test_cli_dream_v2_rescore_no_dreams_md_exits_cleanly(
    monkeypatch, tmp_path
) -> None:
    """When DREAMS.md is absent, the CLI must print a friendly message and
    exit 0 — not crash with FileNotFoundError."""
    from typer.testing import CliRunner

    from opencomputer.cli_memory import memory_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(memory_app, ["dream-v2-rescore"])
    assert result.exit_code == 0
    assert "No DREAMS.md found" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_dream_v2_rescore_empty_dreams_md_exits_cleanly(
    monkeypatch, tmp_path
) -> None:
    """Empty DREAMS.md → "Parsed 0 entries" → exit 0."""
    from typer.testing import CliRunner

    from opencomputer.cli_memory import memory_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    dreams = tmp_path / "DREAMS.md"
    dreams.write_text("")
    runner = CliRunner()
    result = runner.invoke(memory_app, ["dream-v2-rescore"])
    assert result.exit_code == 0
    assert "Parsed 0 entries" in result.stdout or "No DREAMS" in result.stdout
