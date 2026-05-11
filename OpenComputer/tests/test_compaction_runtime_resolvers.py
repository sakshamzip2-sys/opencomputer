"""Tests for the runtime-state resolvers exposed by ``compaction.py``.

Both helpers are the single source of truth shared by the TUI status
line (``opencomputer.cli_ui.status_line``) and the ``/context`` slash
command. Before this layer existed the two surfaces hand-typed their
own logic and drifted: the status bar was double-counting cumulative
input + output, and ``/context`` displayed ``98%`` for a compaction
threshold the engine actually fires at ``80%``. Centralising the
resolution kills both drifts at once.

Coverage focuses on the contract a slash / status caller needs:

* ``resolve_current_input_tokens(custom)`` — pick the value the bar
  should render. Prefers the most recent LLM call's reported
  ``input_tokens`` over cumulative session counts. Refuses to add
  output tokens (they re-enter as input next turn — would
  double-count). Adversarial inputs degrade to ``0``.

* ``resolve_effective_compaction_threshold_ratio(custom)`` — pick the
  trigger ratio to display. Honors runtime overrides written by the
  loop from ``config.yaml``; falls back to ``CompactionConfig`` 's
  default. Rejects out-of-range / corrupt values so the panel can
  never claim "compaction triggers at 9000%".
"""

from __future__ import annotations

from opencomputer.agent.compaction import (
    CompactionConfig,
    resolve_current_input_tokens,
    resolve_effective_compaction_threshold_ratio,
)

# ─── resolve_current_input_tokens ───────────────────────────────────────


def test_current_input_prefers_last_input_when_positive() -> None:
    """The bar reports CURRENT context size, not cumulative billing.

    A 10-turn session with ~30K-token turns accumulates
    ``session_tokens_in ≈ 300K`` because each turn re-sends the history.
    The actual current request is still ~30K. The bar must reflect the
    latter.
    """
    custom = {"last_input_tokens": 30_000, "session_tokens_in": 300_000}
    assert resolve_current_input_tokens(custom) == 30_000


def test_current_input_falls_back_to_session_in_when_last_input_zero() -> None:
    """Pre-first-response state: the loop hasn't recorded
    ``last_input_tokens`` yet but cumulative session counters may
    already include earlier tally writes (one-shot CLI mode etc.).
    Show something instead of an empty bar.
    """
    custom = {"last_input_tokens": 0, "session_tokens_in": 12_000}
    assert resolve_current_input_tokens(custom) == 12_000


def test_current_input_falls_back_to_session_in_when_last_input_missing() -> None:
    """Missing key behaves the same as zero — fall through to the
    cumulative counter. The bar renders ``0`` only when both signals
    are absent / zero (true cold start)."""
    custom = {"session_tokens_in": 7_500}
    assert resolve_current_input_tokens(custom) == 7_500


def test_current_input_ignores_session_tokens_out() -> None:
    """Output tokens become input on the next turn — they're already
    counted in ``last_input_tokens`` then. Summing them in would
    double-count what's already in the input budget."""
    custom = {
        "last_input_tokens": 30_000,
        "session_tokens_in": 300_000,
        "session_tokens_out": 50_000,
    }
    assert resolve_current_input_tokens(custom) == 30_000


def test_current_input_returns_zero_on_empty_dict() -> None:
    assert resolve_current_input_tokens({}) == 0


def test_current_input_handles_none_custom() -> None:
    """Adversarial: callers occasionally pass ``None`` instead of an
    empty dict (status-line cold-start path). Must not crash."""
    assert resolve_current_input_tokens(None) == 0  # type: ignore[arg-type]


def test_current_input_handles_string_value() -> None:
    """A buggy plugin stomped a string onto the key — fall back,
    don't raise."""
    custom = {"last_input_tokens": "garbage", "session_tokens_in": 5_000}
    assert resolve_current_input_tokens(custom) == 5_000


def test_current_input_handles_none_value() -> None:
    custom = {"last_input_tokens": None, "session_tokens_in": 5_000}
    assert resolve_current_input_tokens(custom) == 5_000


def test_current_input_handles_negative_value() -> None:
    """A buggy provider could report negative input_tokens (shouldn't,
    but defenses are cheap). Treat negative as zero so we fall through
    to ``session_tokens_in``."""
    custom = {"last_input_tokens": -50, "session_tokens_in": 5_000}
    assert resolve_current_input_tokens(custom) == 5_000


def test_current_input_handles_negative_session_in_as_zero() -> None:
    """Same defensive logic on the fallback path — never return < 0."""
    custom = {"session_tokens_in": -100}
    assert resolve_current_input_tokens(custom) == 0


def test_current_input_treats_bool_as_invalid() -> None:
    """``bool`` is an ``int`` subclass — ``True`` would otherwise be
    treated as ``1`` token. A bool in either field signals an upstream
    bug; reject it and fall through to the next signal."""
    custom_true = {"last_input_tokens": True, "session_tokens_in": 5_000}
    assert resolve_current_input_tokens(custom_true) == 5_000

    custom_false = {"last_input_tokens": False, "session_tokens_in": 5_000}
    assert resolve_current_input_tokens(custom_false) == 5_000


def test_current_input_handles_float() -> None:
    """Some providers report token counts as floats. Coerce to int."""
    custom = {"last_input_tokens": 12_345.6}
    assert resolve_current_input_tokens(custom) == 12_345


def test_current_input_handles_nan_float() -> None:
    """A NaN must not propagate — fall back to next signal."""
    custom = {"last_input_tokens": float("nan"), "session_tokens_in": 9_000}
    assert resolve_current_input_tokens(custom) == 9_000


def test_current_input_handles_inf_float() -> None:
    """Infinity is not a token count. Fall back."""
    custom = {"last_input_tokens": float("inf"), "session_tokens_in": 9_000}
    assert resolve_current_input_tokens(custom) == 9_000


def test_current_input_handles_numeric_string() -> None:
    """Yaml round-trips can serialise an int as ``'12000'``. Tolerate it."""
    custom = {"last_input_tokens": "12000"}
    assert resolve_current_input_tokens(custom) == 12_000


def test_current_input_handles_list_value() -> None:
    """Adversarial: list / dict / set — must not raise."""
    custom = {"last_input_tokens": [1, 2, 3], "session_tokens_in": 9_000}
    assert resolve_current_input_tokens(custom) == 9_000


# ─── resolve_effective_compaction_threshold_ratio ───────────────────────


def test_threshold_default_matches_compaction_config() -> None:
    """Unconfigured installs see ``CompactionConfig`` 's default — never
    a hand-typed constant that drifts. This single-source-of-truth is
    THE point of the helper.
    """
    assert (
        resolve_effective_compaction_threshold_ratio({})
        == CompactionConfig().threshold_ratio
    )


def test_threshold_default_is_80_percent() -> None:
    """Anchor on the documented value so a default change is intentional.

    If ``CompactionConfig.threshold_ratio`` ever changes, update this
    test and the deep-dive doc together — a silent change would
    invalidate user-facing rendering.
    """
    assert resolve_effective_compaction_threshold_ratio({}) == 0.8


def test_threshold_honors_runtime_override() -> None:
    """User customised ``loop.compaction.threshold_ratio: 0.6`` in
    config.yaml → loop populates ``runtime.custom`` → ``/context`` shows
    the customised value, not the default."""
    custom = {"compaction_threshold_ratio": 0.6}
    assert resolve_effective_compaction_threshold_ratio(custom) == 0.6


def test_threshold_accepts_one_inclusive() -> None:
    """``1.0`` means "fire at 100% of window" — degenerate but valid."""
    custom = {"compaction_threshold_ratio": 1.0}
    assert resolve_effective_compaction_threshold_ratio(custom) == 1.0


def test_threshold_accepts_int_one() -> None:
    """YAML may load ``1`` as Python ``int``."""
    custom = {"compaction_threshold_ratio": 1}
    assert resolve_effective_compaction_threshold_ratio(custom) == 1.0


def test_threshold_rejects_zero() -> None:
    """Zero disables compaction entirely — that's a config bug. Fall
    back to default rather than render ``0%`` (which would imply
    immediate compaction)."""
    custom = {"compaction_threshold_ratio": 0.0}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_out_of_range_high() -> None:
    """A corrupt config can't show "compaction triggers at 9000%"."""
    custom = {"compaction_threshold_ratio": 99.0}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_negative() -> None:
    custom = {"compaction_threshold_ratio": -0.1}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_string() -> None:
    custom = {"compaction_threshold_ratio": "80%"}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_bool() -> None:
    """``True`` is int ``1`` — would otherwise pass the range check.
    A bool in a float field signals an upstream config-loading bug."""
    custom = {"compaction_threshold_ratio": True}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_nan() -> None:
    custom = {"compaction_threshold_ratio": float("nan")}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_rejects_none() -> None:
    custom = {"compaction_threshold_ratio": None}
    assert (
        resolve_effective_compaction_threshold_ratio(custom)
        == CompactionConfig().threshold_ratio
    )


def test_threshold_handles_none_custom() -> None:
    """Defensive: caller may pass ``None`` instead of an empty dict."""
    assert (
        resolve_effective_compaction_threshold_ratio(None)  # type: ignore[arg-type]
        == CompactionConfig().threshold_ratio
    )
