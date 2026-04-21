"""Phase 3 tests: OpenAI provider plugin + plugin-registry provider resolution."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch


def _import_openai_provider():
    """Load the openai-provider's provider module with a unique name.

    Direct-path load so we don't collide with other plugins that also have
    a top-level 'provider' module.
    """
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "openai-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "openai_provider_test_only", provider_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["openai_provider_test_only"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── OpenAI provider ────────────────────────────────────────────


def test_openai_provider_requires_api_key() -> None:
    """Without OPENAI_API_KEY, constructor raises."""
    import pytest

    mod = _import_openai_provider()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        with pytest.raises(RuntimeError, match="API key not set"):
            mod.OpenAIProvider()


def test_openai_provider_base_url_from_env() -> None:
    """Verify OPENAI_BASE_URL env var reaches the SDK client."""
    mod = _import_openai_provider()
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://custom.example/v1"},
        clear=False,
    ):
        p = mod.OpenAIProvider()
        assert "custom.example" in str(p.client.base_url)


def test_openai_message_conversion_roundtrip() -> None:
    """Convert Message list → OpenAI format (structural check, no HTTP)."""
    mod = _import_openai_provider()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
        p = mod.OpenAIProvider()

    from plugin_sdk.core import Message, ToolCall

    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="Read", arguments={"file_path": "/x"})],
        ),
        Message(role="tool", content="contents", tool_call_id="t1"),
    ]
    out = p._to_openai_messages(messages, system="be helpful")
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"
    assert out[2]["role"] == "assistant"
    assert "tool_calls" in out[2]
    assert out[3]["role"] == "tool"
    assert out[3]["tool_call_id"] == "t1"


# ─── CLI provider resolution ────────────────────────────────────


def test_resolve_provider_from_plugin_registry() -> None:
    """When a plugin registers a provider class, _resolve_provider picks it up."""
    from opencomputer.cli import _resolve_provider
    from opencomputer.plugins.registry import registry as plugin_registry

    class FakeProvider:
        name = "fake"

        def __init__(self):
            self.created = True

    plugin_registry.providers["fake"] = FakeProvider
    try:
        p = _resolve_provider("fake")
        assert isinstance(p, FakeProvider)
        assert p.created
    finally:
        plugin_registry.providers.pop("fake", None)


def test_resolve_provider_unknown_raises() -> None:
    """If no plugin matches, raise a helpful error."""
    import pytest

    from opencomputer.cli import _resolve_provider

    with pytest.raises(RuntimeError, match="Provider 'no-such-provider' is not available"):
        _resolve_provider("no-such-provider")


def test_resolve_provider_unknown_anthropic_now_errors() -> None:
    """After Phase 3.1 there's NO in-tree fallback — anthropic must be loaded as a plugin."""
    import pytest

    from opencomputer.cli import _resolve_provider
    from opencomputer.plugins.registry import registry as plugin_registry

    plugin_registry.providers.pop("anthropic", None)
    with pytest.raises(RuntimeError, match="Provider 'anthropic' is not available"):
        _resolve_provider("anthropic")


# ─── Plugin manifest discovery ──────────────────────────────────


def test_openai_plugin_manifest_discoverable() -> None:
    """The openai-provider plugin manifest should be discoverable."""
    from opencomputer.plugins.discovery import discover

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    candidates = discover([ext_dir])
    ids = [c.manifest.id for c in candidates]
    assert "openai-provider" in ids
    oai = next(c for c in candidates if c.manifest.id == "openai-provider")
    assert oai.manifest.kind == "provider"
    assert oai.manifest.entry == "plugin"
