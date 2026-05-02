"""Context-window detection must be model-agnostic and conservative.

The previous fuzzy-match logic ``model.startswith(key.split("-")[0])``
silently mis-routed any unlisted model whose family prefix matched a
listed entry. Most damagingly: GPT-4o (128k actual) inherited gpt-5.4's
400k window and would never trigger compaction before hitting the API's
real context limit.

These tests pin down the correct behaviour:
  1. Listed models return their declared windows.
  2. Unlisted models fall through to a CONSERVATIVE default
     (smaller than reality), so we compact too early instead of too late.
  3. Family prefixes match conservatively — adding a new ``gpt-5.4`` entry
     must NOT accidentally inflate gpt-4o's window.
"""

from opencomputer.agent.compaction import (
    DEFAULT_CONTEXT_WINDOWS,
    context_window_for,
)


# ─── Anthropic family (200k) ─────────────────────────────────────────


def test_listed_anthropic_4x_models():
    assert context_window_for("claude-opus-4-7") == 200_000
    assert context_window_for("claude-sonnet-4-6") == 200_000
    assert context_window_for("claude-haiku-4-5") == 200_000


def test_anthropic_3_5_sonnet():
    """Claude 3.5 Sonnet has 200k context."""
    assert context_window_for("claude-3-5-sonnet-latest") == 200_000


def test_anthropic_3_5_haiku():
    """Claude 3.5 Haiku has 200k context."""
    assert context_window_for("claude-3-5-haiku-latest") == 200_000


# ─── OpenAI family ────────────────────────────────────────────────────


def test_gpt_4o_does_not_inherit_gpt_5_4_window():
    """The headline bug fix: gpt-4o is 128k, NOT 400k. The old fuzzy
    match would have answered 400k via ``startswith('gpt')`` — which
    means compaction never fires before the API rejects the request."""
    w = context_window_for("gpt-4o")
    assert w <= 128_000, (
        f"gpt-4o must report <=128k context; got {w}. "
        "If this is reporting 400k, the fuzzy match has regressed."
    )


def test_gpt_4_turbo():
    assert context_window_for("gpt-4-turbo") <= 128_000


def test_gpt_3_5_turbo_small_window():
    """gpt-3.5-turbo has 16k. We must NOT claim 400k."""
    w = context_window_for("gpt-3.5-turbo")
    assert w <= 16_385, f"gpt-3.5-turbo must report <=16k; got {w}"


def test_o1_o3_listed():
    """o1 / o3 reasoning models have 200k context."""
    assert context_window_for("o1-preview") <= 200_000
    assert context_window_for("o3-mini") <= 200_000


# ─── Other major providers ────────────────────────────────────────────


def test_gemini_2_pro_listed():
    """Gemini 2.0 Pro has 2M context — not handled by the old dict
    (silently fell back to 200k, causing 10x more compactions than
    needed)."""
    assert context_window_for("gemini-2.0-pro") >= 1_000_000


def test_gemini_1_5_pro_listed():
    assert context_window_for("gemini-1.5-pro") >= 1_000_000


def test_deepseek_chat_listed():
    """DeepSeek Chat has 64k context, not 200k. Wrong window means
    compaction never fires before the API rejects."""
    w = context_window_for("deepseek-chat")
    assert w <= 64_000, f"deepseek-chat must report <=64k; got {w}"


def test_deepseek_reasoner_listed():
    w = context_window_for("deepseek-reasoner")
    assert w <= 64_000


# ─── Conservative default for unknown models ─────────────────────────


def test_unknown_model_returns_conservative_default():
    """An entirely unknown model name must NOT be optimistic. Compact
    too early (small wasted aux-LLM call) rather than too late
    (failed conversation)."""
    w = context_window_for("totally-unknown-2099")
    assert w <= 128_000, (
        f"unknown models must use a conservative default <=128k; got {w}. "
        "Optimistic defaults break compaction by claiming windows we don't have."
    )


def test_default_is_conservative():
    """Pin the default itself — it should not be 200k+."""
    assert DEFAULT_CONTEXT_WINDOWS["_default"] <= 128_000


# ─── No fuzzy-match cross-contamination ──────────────────────────────


def test_unlisted_claude_falls_back_safely():
    """A future Claude variant we don't know about must NOT silently
    inherit the wrong family's window via prefix match."""
    w = context_window_for("claude-future-model-2030")
    # OK to inherit the Claude family default (since they all share 200k
    # right now), but we must use a real claude-family rule, not the
    # buggy startswith-on-first-token fuzzy match.
    assert w <= 200_000


def test_no_false_positive_from_short_prefix():
    """The old fuzzy match did ``model.startswith(key.split('-')[0])``
    so an entry ``gpt-5.4`` produced prefix ``gpt`` which matched
    every gpt-* model. This must NOT happen."""
    # If we add a fictional model-family-only entry, an unrelated model
    # that happens to share its first hyphen-token shouldn't inherit it.
    # Direct lookup must dominate.
    explicit = context_window_for("gpt-3.5-turbo")
    # Even with gpt-5.4 in the dict, gpt-3.5-turbo must not inherit 400k.
    assert explicit < DEFAULT_CONTEXT_WINDOWS.get("gpt-5.4", 9_999_999_999)
