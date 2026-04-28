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
    }


def test_runtime_flags_none_custom_returns_nones():
    assert runtime_flags_from_custom(None) == {
        "reasoning_effort": None,
        "service_tier": None,
    }


def test_runtime_flags_extracts_strings():
    custom = {
        "reasoning_effort": "high",
        "service_tier": "priority",
        "yolo_session": True,  # unrelated key — should not appear
    }
    out = runtime_flags_from_custom(custom)
    assert out == {"reasoning_effort": "high", "service_tier": "priority"}


def test_runtime_flags_filters_non_string_values():
    custom = {"reasoning_effort": 42, "service_tier": True}
    out = runtime_flags_from_custom(custom)
    assert out == {"reasoning_effort": None, "service_tier": None}


# ---------- Anthropic translator ----------


@pytest.mark.parametrize("effort,expected_budget", [
    ("minimal", 1024),
    ("low", 2048),
    ("medium", 4096),
    ("high", 8192),
    ("xhigh", 16384),
])
def test_anthropic_reasoning_levels(effort, expected_budget):
    out = anthropic_kwargs_from_runtime(reasoning_effort=effort)
    assert out["thinking"] == {"type": "enabled", "budget_tokens": expected_budget}


def test_anthropic_reasoning_none_omits_thinking():
    out = anthropic_kwargs_from_runtime(reasoning_effort="none")
    assert "thinking" not in out


def test_anthropic_reasoning_unknown_omits_thinking():
    out = anthropic_kwargs_from_runtime(reasoning_effort="ultra")
    assert "thinking" not in out


def test_anthropic_no_args_returns_empty():
    assert anthropic_kwargs_from_runtime() == {}


def test_anthropic_service_tier_priority():
    out = anthropic_kwargs_from_runtime(service_tier="priority")
    assert out["service_tier"] == "priority"


def test_anthropic_service_tier_default_omits():
    out = anthropic_kwargs_from_runtime(service_tier="default")
    assert "service_tier" not in out


def test_anthropic_combined_flags():
    out = anthropic_kwargs_from_runtime(
        reasoning_effort="high", service_tier="priority"
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
    runtime.custom; the kwargs translator reads them back."""
    custom = {"reasoning_effort": "high", "service_tier": "priority"}
    flags = runtime_flags_from_custom(custom)
    api_kwargs = anthropic_kwargs_from_runtime(**flags)
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
    assert anthropic_kwargs_from_runtime(**flags) == {}
    assert openai_kwargs_from_runtime(**flags) == {}
