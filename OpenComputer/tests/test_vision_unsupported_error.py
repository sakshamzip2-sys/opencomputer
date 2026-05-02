"""Tests for the VisionUnsupportedError + BaseProvider.complete_vision default.

Mirrors the existing BatchUnsupportedError pattern. Most providers don't
support multimodal image input — the default raises a specific error type
so callers can catch it and surface a clean 'vision not supported on
<name>' message rather than crashing with a cryptic LLM-API HTTP error.
"""
from __future__ import annotations

import pytest

from plugin_sdk import (
    BaseProvider,
    Message,
    StreamEvent,
    VisionUnsupportedError,
)


class _StubProvider(BaseProvider):
    """Minimal stub — implements the abstracts but no vision override."""

    name = "stub"
    default_model = "stub-1"

    async def complete(self, **_kw):  # type: ignore[override]
        raise NotImplementedError

    async def stream_complete(self, **_kw):  # type: ignore[override]
        raise NotImplementedError
        yield  # pragma: no cover — async-generator typing


def test_vision_unsupported_error_is_not_implemented_subclass():
    """The error subclasses NotImplementedError so callers can match
    either the specific or general type. Same pattern as BatchUnsupportedError.
    """
    assert issubclass(VisionUnsupportedError, NotImplementedError)


def test_default_complete_vision_raises_with_provider_name():
    """The default raise_for_status-style behaviour: provider name in
    the message so the caller can surface 'vision not supported on stub'.
    """
    import asyncio

    p = _StubProvider()
    with pytest.raises(VisionUnsupportedError, match="stub"):
        asyncio.run(
            p.complete_vision(
                model="stub-1",
                image_base64="data",
                mime_type="image/png",
                prompt="describe",
            )
        )


def test_provider_can_override_complete_vision_to_succeed():
    """Sanity: a provider that implements complete_vision can return
    text. (Anthropic / OpenAI override; this test mirrors that path.)
    """
    import asyncio

    class _VisionStub(BaseProvider):
        name = "vision-stub"
        default_model = "vs-1"

        async def complete(self, **_kw):  # type: ignore[override]
            raise NotImplementedError

        async def stream_complete(self, **_kw):  # type: ignore[override]
            raise NotImplementedError
            yield  # pragma: no cover

        async def complete_vision(
            self,
            *,
            model: str,
            image_base64: str,
            mime_type: str,
            prompt: str,
            max_tokens: int = 1024,
        ) -> str:
            return f"saw {len(image_base64)} bytes of {mime_type}"

    p = _VisionStub()
    result = asyncio.run(
        p.complete_vision(
            model="vs-1",
            image_base64="abcd",
            mime_type="image/jpeg",
            prompt="describe",
        )
    )
    assert result == "saw 4 bytes of image/jpeg"


def test_vision_unsupported_error_is_exported_from_plugin_sdk():
    """Public API surface — callers in opencomputer/* import from plugin_sdk."""
    from plugin_sdk import VisionUnsupportedError as Exported
    assert Exported is VisionUnsupportedError


def test_anthropic_provider_overrides_complete_vision():
    """Anthropic's chat-completion provider implements vision —
    overrides the default-raises behavior so VisionAnalyzeTool can call
    it on this provider successfully.
    """
    pytest.importorskip("anthropic")
    from extensions.anthropic_provider.provider import AnthropicProvider

    # The override is present on the class (not inherited from BaseProvider)
    assert "complete_vision" in AnthropicProvider.__dict__


def test_openai_provider_overrides_complete_vision():
    """OpenAI's chat-completion provider implements vision via the
    image_url multimodal shape (gpt-4o, gpt-5.4 vision-capable models).
    """
    from extensions.openai_provider.provider import OpenAIProvider

    assert "complete_vision" in OpenAIProvider.__dict__


def test_openrouter_inherits_vision_from_openai():
    """OpenRouter is a thin OpenAIProvider subclass — class-attribute
    lookup finds the override on the parent so OpenRouter inherits
    vision support automatically.

    Loaded via importlib because openrouter_provider isn't in the
    conftest alias list (consistent with how
    ``tests/test_openrouter_provider.py`` already imports it).
    """
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    or_path = repo_root / "extensions" / "openrouter-provider" / "provider.py"
    if not or_path.exists():
        pytest.skip("openrouter-provider/provider.py not present")
    spec = importlib.util.spec_from_file_location(
        "_or_provider_test", str(or_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    OpenRouterProvider = mod.OpenRouterProvider

    # OpenRouter doesn't override; uses OpenAI's
    assert "complete_vision" not in OpenRouterProvider.__dict__
    # But MRO lookup finds it
    assert OpenRouterProvider.complete_vision is not BaseProvider.complete_vision


def test_ollama_groq_default_to_vision_unsupported():
    """Providers that don't override raise the default
    VisionUnsupportedError — confirms the gating works the right way
    around (no false positives).
    """
    import asyncio

    from extensions.groq_provider.provider import GroqProvider
    from extensions.ollama_provider.provider import OllamaProvider

    for cls in (OllamaProvider, GroqProvider):
        # No override — falls through to BaseProvider default
        assert "complete_vision" not in cls.__dict__
        if cls is GroqProvider:
            # Groq requires a key; skip the actual call
            continue
        p = cls()
        with pytest.raises(VisionUnsupportedError):
            asyncio.run(
                p.complete_vision(
                    model="x",
                    image_base64="data",
                    mime_type="image/png",
                    prompt="describe",
                )
            )
