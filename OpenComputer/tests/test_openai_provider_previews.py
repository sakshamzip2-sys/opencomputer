"""Regression-lock the OpenAI provider's LLMCallEvent preview population.

Anthropic provider populates input_preview/output_preview from the last
user message and response_text. The OpenAI provider must do the same so
Langfuse evaluators (LLM-as-a-judge) have non-empty input/output on
OpenAI traces. See design spec §13.1 + §17 at
docs/superpowers/specs/2026-05-06-llm-judge-prompts-design.md.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


def _load_openai_provider():
    """Load the OpenAI provider as an isolated module.

    Mirrors the synthetic-unique-name pattern documented in
    OpenComputer/CLAUDE.md §7.1 (plugin module-cache collisions).
    """
    repo_root = Path(__file__).resolve().parents[1]
    plugin_path = repo_root / "extensions" / "openai-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider_previews", plugin_path
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_openai_provider_previews"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_usage(input_tokens: int = 10, output_tokens: int = 5):
    """Construct a minimal Usage stand-in (only the fields the emitter reads)."""
    return type(
        "Usage",
        (),
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": 0,
        },
    )()


def test_emit_llm_event_populates_previews_from_messages_and_response_text() -> None:
    """When messages + response_text are passed, LLMCallEvent has non-None previews."""
    from opencomputer.inference.observability import LLMCallEvent
    from plugin_sdk.core import Message

    provider_mod = _load_openai_provider()
    captured: list[LLMCallEvent] = []
    instance = provider_mod.OpenAIProvider.__new__(provider_mod.OpenAIProvider)

    messages = [Message(role="user", content="what's the capital of France?")]

    with patch.object(provider_mod, "record_llm_call", side_effect=lambda e: captured.append(e)):
        instance._emit_llm_event(
            model="gpt-4o-mini",
            usage=_make_usage(),
            t0=0.0,
            t1=0.5,
            site="agent_loop",
            messages=messages,
            response_text="Paris.",
        )

    assert len(captured) == 1
    event = captured[0]
    assert event.input_preview == "what's the capital of France?"
    assert event.output_preview == "Paris."


def test_emit_llm_event_caps_previews_at_1500_chars() -> None:
    """Both previews must be capped at 1500 chars to bound JSONL log size."""
    from opencomputer.inference.observability import LLMCallEvent
    from plugin_sdk.core import Message

    provider_mod = _load_openai_provider()
    captured: list[LLMCallEvent] = []
    instance = provider_mod.OpenAIProvider.__new__(provider_mod.OpenAIProvider)

    long_input = "x" * 5000
    long_output = "y" * 5000
    messages = [Message(role="user", content=long_input)]

    with patch.object(provider_mod, "record_llm_call", side_effect=lambda e: captured.append(e)):
        instance._emit_llm_event(
            model="gpt-4o-mini",
            usage=_make_usage(),
            t0=0.0,
            t1=0.5,
            site="agent_loop",
            messages=messages,
            response_text=long_output,
        )

    assert captured[0].input_preview == "x" * 1500
    assert captured[0].output_preview == "y" * 1500


def test_emit_llm_event_no_messages_yields_none_previews() -> None:
    """Backwards-compat: omitting messages/response_text leaves previews None."""
    from opencomputer.inference.observability import LLMCallEvent

    provider_mod = _load_openai_provider()
    captured: list[LLMCallEvent] = []
    instance = provider_mod.OpenAIProvider.__new__(provider_mod.OpenAIProvider)

    with patch.object(provider_mod, "record_llm_call", side_effect=lambda e: captured.append(e)):
        instance._emit_llm_event(
            model="gpt-4o-mini",
            usage=_make_usage(),
            t0=0.0,
            t1=0.5,
            site="agent_loop",
        )

    assert captured[0].input_preview is None
    assert captured[0].output_preview is None


def test_emit_llm_event_uses_last_user_message_when_history_present() -> None:
    """Multi-turn history → preview is the LAST user message, not the first."""
    from opencomputer.inference.observability import LLMCallEvent
    from plugin_sdk.core import Message

    provider_mod = _load_openai_provider()
    captured: list[LLMCallEvent] = []
    instance = provider_mod.OpenAIProvider.__new__(provider_mod.OpenAIProvider)

    messages = [
        Message(role="user", content="first ask"),
        Message(role="assistant", content="first reply"),
        Message(role="user", content="second ask — this is the one"),
    ]

    with patch.object(provider_mod, "record_llm_call", side_effect=lambda e: captured.append(e)):
        instance._emit_llm_event(
            model="gpt-4o-mini",
            usage=_make_usage(),
            t0=0.0,
            t1=0.5,
            site="agent_loop",
            messages=messages,
            response_text="ack",
        )

    assert captured[0].input_preview == "second ask — this is the one"
