"""Tests for ``opencomputer.cli_ui.status_line``.

Covers per-segment formatters (token K-suffix, percentage rounding,
elapsed-time bucket transitions at 60s / 3600s, cost dollar-cents),
full-line snapshots for populated and cold-start runtimes, the ``-1m``
model-id variant, and ``NO_COLOR`` ANSI suppression.
"""

from __future__ import annotations

import time

import pytest

from opencomputer.cli_ui import status_line
from opencomputer.cli_ui.status_line import (
    BAR_EMPTY,
    BAR_FILL,
    BAR_WIDTH,
    DEFAULT_MAX_CONTEXT,
    EXTENDED_MAX_CONTEXT,
    PREFIX,
    SEPARATOR,
    format_cost,
    format_elapsed,
    format_tokens,
    max_context_for,
    percent_used,
    progress_bar,
    render_status_line,
)

# ─── helpers ────────────────────────────────────────────────────────────


class _FakeRuntime:
    """Bare-bones stand-in for a RuntimeContext.

    The renderer reads ``runtime.custom`` only — we don't need a real
    plugin_sdk RuntimeContext for these tests, and using one couples
    them to the SDK's frozen-dataclass quirks.
    """

    def __init__(self, custom: dict | None = None) -> None:
        self.custom = custom or {}


def _flatten(fragments: list[tuple[str, str]]) -> str:
    return "".join(t for _s, t in fragments)


# ─── format_tokens ──────────────────────────────────────────────────────


class TestFormatTokens:
    def test_zero(self) -> None:
        assert format_tokens(0) == "0"

    def test_below_one_k(self) -> None:
        assert format_tokens(999) == "999"

    def test_at_one_k(self) -> None:
        assert format_tokens(1_000) == "1K"

    def test_fractional_k(self) -> None:
        assert format_tokens(12_400) == "12.4K"

    def test_whole_two_hundred_k(self) -> None:
        # User's exact-format example: ``200K`` (no decimal).
        assert format_tokens(200_000) == "200K"

    def test_at_one_m(self) -> None:
        assert format_tokens(1_000_000) == "1M"

    def test_fractional_m(self) -> None:
        assert format_tokens(1_500_000) == "1.5M"

    def test_negative_collapses_to_zero(self) -> None:
        assert format_tokens(-5) == "0"

    def test_non_int_collapses_to_zero(self) -> None:
        assert format_tokens(None) == "0"  # type: ignore[arg-type]
        assert format_tokens("12k") == "0"  # type: ignore[arg-type]


# ─── format_cost ────────────────────────────────────────────────────────


class TestFormatCost:
    def test_none_returns_empty(self) -> None:
        # Empty string signals "omit the segment".
        assert format_cost(None) == ""

    def test_zero(self) -> None:
        assert format_cost(0.0) == "$0.00"

    def test_six_cents(self) -> None:
        # Matches the user's exact-format example.
        assert format_cost(0.06) == "$0.06"

    def test_rounds_to_cents(self) -> None:
        assert format_cost(0.0649) == "$0.06"
        assert format_cost(0.065) == "$0.07"  # banker's rounding via :func:`f-string`

    def test_negative_clamped_to_zero(self) -> None:
        assert format_cost(-0.5) == "$0.00"

    def test_non_numeric_returns_empty(self) -> None:
        assert format_cost("free") == ""  # type: ignore[arg-type]


# ─── format_elapsed ─────────────────────────────────────────────────────


class TestFormatElapsed:
    def test_zero(self) -> None:
        assert format_elapsed(0) == "0s"

    def test_seconds(self) -> None:
        assert format_elapsed(45) == "45s"

    def test_at_60_transitions_to_minutes(self) -> None:
        # Bucket boundary — 60s exactly is the first minute.
        assert format_elapsed(60) == "1m"

    def test_minutes(self) -> None:
        assert format_elapsed(15 * 60) == "15m"

    def test_just_under_an_hour(self) -> None:
        assert format_elapsed(3599) == "59m"

    def test_at_3600_transitions_to_hours(self) -> None:
        assert format_elapsed(3600) == "1h0m"

    def test_hours_minutes(self) -> None:
        assert format_elapsed(3600 + 23 * 60) == "1h23m"

    def test_negative_collapses(self) -> None:
        assert format_elapsed(-10) == "0s"

    def test_non_numeric_collapses(self) -> None:
        assert format_elapsed("forever") == "0s"  # type: ignore[arg-type]


# ─── percent_used + progress_bar ────────────────────────────────────────


class TestPercentUsed:
    def test_zero_total(self) -> None:
        assert percent_used(100, 0) == 0

    def test_zero_used(self) -> None:
        assert percent_used(0, 200_000) == 0

    def test_six_percent(self) -> None:
        # Matches the user's exact-format example: 12.4K / 200K = 6%.
        assert percent_used(12_400, 200_000) == 6

    def test_overflow_caps_at_100(self) -> None:
        assert percent_used(500_000, 200_000) == 100


class TestProgressBar:
    def test_zero_used_all_empty(self) -> None:
        assert progress_bar(0, 200_000) == BAR_EMPTY * BAR_WIDTH

    def test_full_all_filled(self) -> None:
        assert progress_bar(200_000, 200_000) == BAR_FILL * BAR_WIDTH

    def test_six_percent_zero_filled_cells(self) -> None:
        # 6% of 10 cells floors to 0 — Claude Code's actual behavior at
        # this percentage; user's example bar is rendered identically.
        assert progress_bar(12_400, 200_000) == BAR_EMPTY * BAR_WIDTH

    def test_sixty_percent(self) -> None:
        assert progress_bar(120_000, 200_000) == BAR_FILL * 6 + BAR_EMPTY * 4

    def test_zero_total_all_empty(self) -> None:
        assert progress_bar(100, 0) == BAR_EMPTY * BAR_WIDTH

    def test_overflow_all_filled(self) -> None:
        assert progress_bar(500_000, 200_000) == BAR_FILL * BAR_WIDTH


# ─── max_context_for ────────────────────────────────────────────────────


class TestMaxContextFor:
    def test_known_claude(self) -> None:
        # Wave 3 (2026-05-08) — Opus 4.6/4.7 ship 1M by default.
        assert max_context_for("claude-opus-4-7") == 1_000_000

    def test_known_gpt_4o(self) -> None:
        assert max_context_for("gpt-4o") == 128_000

    def test_one_m_suffix_overrides_table(self) -> None:
        # The compaction table doesn't carry a million-token alias;
        # ``-1m`` substring is the canonical signal.
        assert max_context_for("claude-sonnet-4-6-1m") == EXTENDED_MAX_CONTEXT

    def test_bracket_one_m_alias(self) -> None:
        # Hermes-style ``[1m]`` suffix in some configs.
        assert max_context_for("claude-sonnet-4-6[1m]") == EXTENDED_MAX_CONTEXT

    def test_unknown_falls_through_to_compaction_default(self) -> None:
        # Compaction returns its conservative ``_default`` (64k) — that
        # IS the right answer here; we mirror the compactor exactly.
        assert max_context_for("totally-unknown-model") == 64_000

    def test_empty_string_returns_default(self) -> None:
        assert max_context_for("") == DEFAULT_MAX_CONTEXT

    def test_non_string_returns_default(self) -> None:
        assert max_context_for(None) == DEFAULT_MAX_CONTEXT  # type: ignore[arg-type]


# ─── render_status_line — full-line snapshots ───────────────────────────


class TestRenderStatusLine:
    def test_populated_runtime_matches_user_format(self, monkeypatch) -> None:
        # Pin the elapsed clock so the snapshot is deterministic. We
        # patch the indirection layer (`_now_monotonic`) instead of
        # `time.monotonic` globally to keep test isolation.
        anchor = 1000.0
        monkeypatch.setattr(status_line, "_now_monotonic", lambda: anchor + 900)

        rt = _FakeRuntime({
            "model_id": "claude-opus-4-7",
            "session_tokens_in": 8_000,
            "session_tokens_out": 4_400,
            "session_cost_usd": 0.06,
            "session_started_at": anchor,
        })
        text = _flatten(render_status_line(rt))
        # Exact format the user requested. Leading + trailing pad are
        # part of the rendered line; the visible "core" is the segment
        # in between.
        # Wave 3 (2026-05-08) — Opus 4.6/4.7 default to 1M context;
        # 12.4K / 1M = 1% (rounded down from 1.24%).
        assert (
            text
            == " ⚕ claude-opus-4-7 │ 12.4K/1M │ [░░░░░░░░░░] 1% │ $0.06 │ 15m "
        )

    def test_cold_start_snapshot(self) -> None:
        rt = _FakeRuntime({})
        text = _flatten(render_status_line(rt))
        # No model id, no tokens, no cost, no start time.
        # Cost segment is omitted (None → ""); elapsed shows "0s" so the
        # field's existence is visible from the first frame. (unknown) hits
        # DEFAULT_MAX_CONTEXT (200K) because no model id is set.
        assert text == " ⚕ (unknown) │ 0/200K │ [░░░░░░░░░░] 0% │ 0s "

    def test_one_m_model_uses_extended_context(self, monkeypatch) -> None:
        monkeypatch.setattr(status_line, "_now_monotonic", lambda: 100.0)
        rt = _FakeRuntime({
            "model_id": "claude-sonnet-4-6-1m",
            "session_tokens_in": 50_000,
            "session_tokens_out": 50_000,
            "session_cost_usd": 1.23,
            "session_started_at": 100.0,
        })
        text = _flatten(render_status_line(rt))
        # 100K / 1M = 10% — bar shows one filled cell.
        assert "100K/1M" in text
        assert "10%" in text
        assert BAR_FILL in text  # non-zero fill
        assert "$1.23" in text

    def test_runtime_none_renders_cold_start(self) -> None:
        # Defensive — test fixtures sometimes pass None.
        text = _flatten(render_status_line(None))
        assert text.startswith(" ⚕ ")
        # No model_id when runtime is None → falls through to
        # DEFAULT_MAX_CONTEXT (200K) since the override layers and
        # static table both need a real model id to resolve.
        assert "0/200K" in text
        assert "0%" in text

    def test_missing_cost_omits_segment(self, monkeypatch) -> None:
        monkeypatch.setattr(status_line, "_now_monotonic", lambda: 100.0)
        rt = _FakeRuntime({
            "model_id": "claude-opus-4-7",
            "session_tokens_in": 1_000,
            "session_tokens_out": 0,
            # session_cost_usd missing → segment dropped
            "session_started_at": 100.0,
        })
        text = _flatten(render_status_line(rt))
        assert "$" not in text  # no cost segment

    def test_corrupt_runtime_custom_does_not_crash(self) -> None:
        # Another component stomped a non-int onto the counter — render
        # must not crash; it falls back to zero for that field.
        rt = _FakeRuntime({
            "model_id": "claude-opus-4-7",
            "session_tokens_in": "garbage",
            "session_tokens_out": 4.5,  # float, not int
            "session_cost_usd": "free",
            "session_started_at": "nowish",
        })
        text = _flatten(render_status_line(rt))
        # Tokens in: bad → 0; tokens out: float (not int) → 0; cost: bad
        # → omitted; started_at: bad → 0s.
        # Wave 3 (2026-05-08) — Opus 4.7 = 1M context.
        assert "0/1M" in text
        assert "$" not in text
        assert "0s" in text

    def test_separator_uses_unicode_pipe(self) -> None:
        # Guard against accidental ASCII-pipe regressions — the user
        # explicitly called out U+2502.
        rt = _FakeRuntime({"model_id": "claude-opus-4-7"})
        text = _flatten(render_status_line(rt))
        assert SEPARATOR == " │ "
        assert " │ " in text
        assert " | " not in text  # no ASCII pipe


def test_cli_token_tally_sync_updates_status_line_bar() -> None:
    from opencomputer import cli

    rt = _FakeRuntime({
        "model_id": "claude-opus-4-7",
        "session_tokens_in": 0,
        "session_tokens_out": 0,
    })

    cli._sync_runtime_token_tally(rt, {"in": 9_000, "out": 3_000})

    text = _flatten(render_status_line(rt))
    assert "12K/1M" in text
    assert "1%" in text


# ─── NO_COLOR honor ────────────────────────────────────────────────────


class TestNoColor:
    def test_styles_suppressed_when_no_color_set(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        rt = _FakeRuntime({"model_id": "claude-opus-4-7"})
        frags = render_status_line(rt)
        # Every fragment's style string must be empty under NO_COLOR.
        styles = [s for s, _ in frags]
        assert all(s == "" for s in styles), styles

    def test_styles_present_without_no_color(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        rt = _FakeRuntime({"model_id": "claude-opus-4-7"})
        frags = render_status_line(rt)
        # At least the prefix + bar segments carry non-empty style hints.
        styles = [s for s, _ in frags]
        assert any(s for s in styles), "expected at least one styled fragment"


# ─── module surface guard ──────────────────────────────────────────────


def test_module_constants_match_user_spec() -> None:
    """Lock the U+xxxx code points the user explicitly required.

    A drive-by refactor that swaps these for ASCII would break the
    Claude-Code-style visual without any other test failing — keep them
    pinned to their unicode origins.
    """
    assert PREFIX == "⚕ "  # caduceus + space
    assert SEPARATOR == " │ "  # box-drawings light vertical
    assert BAR_FILL == "█"  # full block
    assert BAR_EMPTY == "░"  # light shade
    assert BAR_WIDTH == 10


# ─── elapsed-clock indirection respects monotonic ──────────────────────


def test_now_monotonic_uses_time_monotonic() -> None:
    # Sanity: the indirection isn't cached at import time.
    a = status_line._now_monotonic()
    b = status_line._now_monotonic()
    # Two near-instantaneous calls should be ordered, allowing for the
    # rare equal-tick case on coarse clocks.
    assert b >= a
    assert isinstance(a, float)


def test_render_uses_default_clock_when_started_at_present() -> None:
    """Without monkeypatching, render must still produce a numeric s/m
    bucket — guards against an accidental ``time.monotonic`` import drift.
    """
    rt = _FakeRuntime({
        "model_id": "claude-opus-4-7",
        "session_started_at": time.monotonic() - 5,
    })
    text = _flatten(render_status_line(rt))
    # Either "5s" or "6s" depending on test scheduling — both pass the
    # bucket check (sub-60s).
    assert any(token in text for token in ("4s", "5s", "6s"))
