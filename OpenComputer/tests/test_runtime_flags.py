"""Tests for runtime.custom → provider-API-kwargs translators."""
import pytest

from opencomputer.agent.runtime_flags import (
    anthropic_kwargs_from_runtime,
    openai_kwargs_from_runtime,
    runtime_flags_from_custom,
)

# ---------- runtime_flags_from_custom ----------


def test_runtime_flags_empty_custom_returns_nones():
    assert runtime_flags_from_custom({}) == {
        "reasoning_effort": None,
        "service_tier": None,
        "anthropic_skills": None,
    }


def test_runtime_flags_none_custom_returns_nones():
    assert runtime_flags_from_custom(None) == {
        "reasoning_effort": None,
        "service_tier": None,
        "anthropic_skills": None,
    }


def test_runtime_flags_extracts_strings():
    custom = {
        "reasoning_effort": "high",
        "service_tier": "priority",
        "yolo_session": True,  # unrelated key — should not appear
    }
    out = runtime_flags_from_custom(custom)
    assert out == {
        "reasoning_effort": "high",
        "service_tier": "priority",
        "anthropic_skills": None,
    }


def test_runtime_flags_filters_non_string_values():
    custom = {"reasoning_effort": 42, "service_tier": True}
    out = runtime_flags_from_custom(custom)
    assert out == {
        "reasoning_effort": None,
        "service_tier": None,
        "anthropic_skills": None,
    }


def test_runtime_flags_extracts_anthropic_skills():
    """SP4 follow-up: forward anthropic_skills list into runtime_extras."""
    custom = {"anthropic_skills": ["pdf", "pptx"]}
    out = runtime_flags_from_custom(custom)
    assert out["anthropic_skills"] == ["pdf", "pptx"]


def test_runtime_flags_anthropic_skills_strips_whitespace_and_drops_empty():
    """Whitespace stripped per item; empty/whitespace-only items dropped."""
    custom = {"anthropic_skills": [" pdf ", "", "  ", "pptx"]}
    out = runtime_flags_from_custom(custom)
    assert out["anthropic_skills"] == ["pdf", "pptx"]


def test_runtime_flags_anthropic_skills_rejects_non_list():
    """Plain string instead of list is rejected (returns None)."""
    custom = {"anthropic_skills": "pdf"}
    out = runtime_flags_from_custom(custom)
    assert out["anthropic_skills"] is None


def test_runtime_flags_anthropic_skills_rejects_list_with_non_strings():
    """List with non-string items is rejected as a whole (returns None)."""
    custom = {"anthropic_skills": ["pdf", 42, None]}
    out = runtime_flags_from_custom(custom)
    assert out["anthropic_skills"] is None


# ---------- Anthropic translator ----------


@pytest.mark.parametrize("effort,expected_budget", [
    ("minimal", 1024),
    ("low", 2048),
    ("medium", 4096),
    ("high", 8192),
    ("xhigh", 16384),
])
def test_anthropic_reasoning_levels(effort, expected_budget):
    out = anthropic_kwargs_from_runtime(model="claude-opus-4-5", reasoning_effort=effort)
    assert out["thinking"] == {"type": "enabled", "budget_tokens": expected_budget}


def test_anthropic_reasoning_none_omits_thinking():
    out = anthropic_kwargs_from_runtime(model="claude-opus-4-5", reasoning_effort="none")
    assert "thinking" not in out


def test_anthropic_reasoning_unknown_omits_thinking():
    out = anthropic_kwargs_from_runtime(model="claude-opus-4-5", reasoning_effort="ultra")
    assert "thinking" not in out


def test_anthropic_no_args_returns_empty():
    assert anthropic_kwargs_from_runtime(model="claude-opus-4-5") == {}


def test_anthropic_service_tier_priority():
    out = anthropic_kwargs_from_runtime(model="claude-opus-4-5", service_tier="priority")
    assert out["service_tier"] == "priority"


def test_anthropic_service_tier_default_omits():
    out = anthropic_kwargs_from_runtime(model="claude-opus-4-5", service_tier="default")
    assert "service_tier" not in out


def test_anthropic_combined_flags():
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-5", reasoning_effort="high", service_tier="priority"
    )
    assert out["thinking"]["budget_tokens"] == 8192
    assert out["service_tier"] == "priority"


# ---------- OpenAI translator ----------


@pytest.mark.parametrize("effort,expected", [
    ("minimal", "minimal"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "high"),  # capped at high
])
def test_openai_reasoning_levels(effort, expected):
    out = openai_kwargs_from_runtime(reasoning_effort=effort)
    assert out["reasoning_effort"] == expected


def test_openai_reasoning_none_omits():
    out = openai_kwargs_from_runtime(reasoning_effort="none")
    assert "reasoning_effort" not in out


def test_openai_reasoning_unknown_omits():
    out = openai_kwargs_from_runtime(reasoning_effort="ultra")
    assert "reasoning_effort" not in out


def test_openai_service_tier_priority():
    out = openai_kwargs_from_runtime(service_tier="priority")
    assert out["service_tier"] == "priority"


def test_openai_service_tier_default_omits():
    out = openai_kwargs_from_runtime(service_tier="default")
    assert "service_tier" not in out


def test_openai_no_args_returns_empty():
    assert openai_kwargs_from_runtime() == {}


# ---------- end-to-end: extract from runtime.custom and translate ----------


def test_full_pipeline_anthropic():
    """The whole point: /reasoning + /fast slash commands write to
    runtime.custom; the kwargs translator reads them back.

    Uses an Opus 4.5 model so we exercise the legacy enabled+budget_tokens
    shape — the adaptive shape is covered by separate tests below.
    """
    custom = {"reasoning_effort": "high", "service_tier": "priority"}
    flags = runtime_flags_from_custom(custom)
    api_kwargs = anthropic_kwargs_from_runtime(model="claude-opus-4-5", **flags)
    assert api_kwargs == {
        "thinking": {"type": "enabled", "budget_tokens": 8192},
        "service_tier": "priority",
    }


def test_full_pipeline_openai():
    custom = {"reasoning_effort": "medium", "service_tier": "priority"}
    flags = runtime_flags_from_custom(custom)
    api_kwargs = openai_kwargs_from_runtime(**flags)
    assert api_kwargs == {
        "reasoning_effort": "medium",
        "service_tier": "priority",
    }


def test_full_pipeline_no_flags():
    """Default state — no /reasoning, no /fast — produces empty kwargs."""
    flags = runtime_flags_from_custom({})
    assert anthropic_kwargs_from_runtime(model="claude-opus-4-5", **flags) == {}
    assert openai_kwargs_from_runtime(**flags) == {}


# ─── Adaptive-thinking migration tests (2026-05-02) ────────────────


def test_anthropic_kwargs_adaptive_branch_for_opus_4_7():
    """Opus 4.7 must get adaptive thinking + output_config.effort."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="high",
    )
    assert out["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert out["output_config"] == {"effort": "high"}


def test_anthropic_kwargs_adaptive_branch_xhigh():
    """xhigh effort passes through unchanged on adaptive models."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="xhigh",
    )
    assert out["output_config"] == {"effort": "xhigh"}


def test_anthropic_kwargs_adaptive_minimal_collapses_to_low():
    """Internal 'minimal' has no Anthropic equivalent; collapse to 'low'."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="minimal",
    )
    assert out["output_config"] == {"effort": "low"}


def test_anthropic_kwargs_legacy_branch_preserves_budget_tokens():
    """Opus 4.5 keeps enabled+budget_tokens — adaptive not supported."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-5",
        reasoning_effort="high",
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    # No output_config on legacy branch — Opus 4.5 supports effort but
    # the effort+legacy combo is deferred to a follow-up PR.
    assert "output_config" not in out


def test_anthropic_kwargs_none_emits_nothing_on_both_branches():
    """reasoning_effort='none' emits no thinking kwargs on either branch."""
    for model in ["claude-opus-4-7", "claude-opus-4-5"]:
        out = anthropic_kwargs_from_runtime(
            model=model,
            reasoning_effort="none",
        )
        assert "thinking" not in out
        assert "output_config" not in out


def test_anthropic_kwargs_unknown_effort_falls_back_to_high_on_adaptive():
    """Unknown internal effort name falls back to API default 'high' on adaptive."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="ultra-mega",
    )
    assert out["output_config"] == {"effort": "high"}


def test_anthropic_kwargs_service_tier_works_on_adaptive():
    """service_tier='priority' still passes through on adaptive branch."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        service_tier="priority",
    )
    assert out["service_tier"] == "priority"
