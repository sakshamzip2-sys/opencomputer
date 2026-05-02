"""Tests for Anthropic Skills-via-API helpers (SP4)."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_skills_via_api_provider", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime(custom: dict | None = None):
    """Build a SimpleNamespace mimicking RuntimeContext shape."""
    return SimpleNamespace(custom=custom or {})


# ─── _resolve_anthropic_skills ────────────────────────────────


def test_resolve_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == []
    assert module._resolve_anthropic_skills(None) == []


def test_resolve_reads_runtime_custom(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": ["pdf", "pptx"]})
    assert module._resolve_anthropic_skills(runtime) == ["pdf", "pptx"]


def test_resolve_reads_env_when_runtime_unset(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", "pdf,xlsx")
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == ["pdf", "xlsx"]
    # None runtime path also reads env
    assert module._resolve_anthropic_skills(None) == ["pdf", "xlsx"]


def test_resolve_runtime_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", "pdf,xlsx")
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": ["docx"]})
    assert module._resolve_anthropic_skills(runtime) == ["docx"]


def test_resolve_warns_on_bad_type(monkeypatch, caplog):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": "pdf"})  # str, should be list
    with caplog.at_level(logging.WARNING):
        result = module._resolve_anthropic_skills(runtime)
    assert result == []
    assert any(
        "bad type" in r.message.lower() or "list" in r.message.lower()
        for r in caplog.records
    )


def test_resolve_strips_whitespace_and_drops_empty(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", " pdf , , xlsx ,  ")
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == ["pdf", "xlsx"]


# ─── _build_skills_container ──────────────────────────────────


def test_build_skills_container_shape():
    module = _load_provider_module()
    container = module._build_skills_container(["pdf", "pptx"])
    assert container == {
        "skills": [
            {"type": "anthropic", "skill_id": "pdf", "version": "latest"},
            {"type": "anthropic", "skill_id": "pptx", "version": "latest"},
        ]
    }


# ─── _augment_kwargs_for_skills ───────────────────────────────


def test_augment_noop_for_empty_skills():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7", "messages": []}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=[])
    assert out == {"model": "claude-opus-4-7", "messages": []}


def test_augment_adds_beta_headers():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7"}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    betas = out["extra_headers"]["anthropic-beta"].split(",")
    assert "code-execution-2025-08-25" in betas
    assert "skills-2025-10-02" in betas
    assert "files-api-2025-04-14" in betas


def test_augment_preserves_existing_betas():
    module = _load_provider_module()
    kwargs = {
        "model": "claude-opus-4-7",
        "extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"},
    }
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    betas = out["extra_headers"]["anthropic-beta"].split(",")
    assert "prompt-caching-2024-07-31" in betas
    assert "skills-2025-10-02" in betas


def test_augment_adds_container():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7"}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf", "xlsx"])
    assert out["container"]["skills"][0]["skill_id"] == "pdf"
    assert out["container"]["skills"][1]["skill_id"] == "xlsx"


def test_augment_adds_code_execution_tool():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7", "tools": []}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    tool_types = [t.get("type") for t in out["tools"]]
    assert "code_execution_20250825" in tool_types


def test_augment_no_duplicate_tool_when_already_present():
    module = _load_provider_module()
    existing_tool = {"type": "code_execution_20250825", "name": "code_execution"}
    kwargs = {"model": "claude-opus-4-7", "tools": [existing_tool]}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    code_exec_count = sum(
        1 for t in out["tools"] if t.get("type") == "code_execution_20250825"
    )
    assert code_exec_count == 1


# ─── Integration: complete() wire-up ──────────────────────────


def _load_provider_via_conftest_alias():
    """Use the conftest-registered ``extensions.anthropic_provider.provider``
    alias so pydantic's forward-ref resolution sees the proper module
    namespace (Literal, etc.). Falls back to the manual loader if the
    alias hasn't been wired up.
    """
    import importlib
    try:
        return importlib.import_module("extensions.anthropic_provider.provider")
    except ModuleNotFoundError:
        return _load_provider_module()


def test_provider_complete_calls_augment_when_skills_set(monkeypatch):
    """Integration: provider.complete() must augment kwargs when skills set.

    The runtime_extras dict is the only flag-carrier the provider sees
    from the agent loop, so we set ``OPENCOMPUTER_ANTHROPIC_SKILLS`` env
    var (always-on resolution path) and assert the SDK ``messages.create``
    call received the container, beta headers, and code_execution tool.
    """
    import asyncio
    from unittest.mock import MagicMock

    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", "pdf")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    module = _load_provider_via_conftest_alias()

    captured_kwargs: dict = {}

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text="ok")]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    fake_response.model = "claude-opus-4-7"
    fake_response.id = "msg_test"

    async def fake_create(**kw):
        captured_kwargs.update(kw)
        return fake_response

    Provider = module.AnthropicProvider
    provider = Provider(api_key="sk-test")

    fake_client = MagicMock()
    fake_client.messages.create = fake_create
    # Patch both the cached client and the per-key builder so any code
    # path we land on uses our fake.
    provider.client = fake_client
    monkeypatch.setattr(
        provider, "_build_client_for_key", lambda _key: fake_client, raising=False
    )

    from plugin_sdk.core import Message

    messages = [Message(role="user", content="hi")]

    try:
        asyncio.run(
            provider.complete(
                model="claude-opus-4-7",
                messages=messages,
                max_tokens=10,
            )
        )
    except Exception:
        # Downstream parsing of the mocked response may fail; we only
        # care about the kwargs that hit the SDK.
        pass

    assert "container" in captured_kwargs, (
        f"container missing from captured kwargs: keys={list(captured_kwargs)}"
    )
    assert captured_kwargs["container"]["skills"][0]["skill_id"] == "pdf"
    tool_types = [t.get("type") for t in captured_kwargs.get("tools") or []]
    assert "code_execution_20250825" in tool_types
    extra_headers = captured_kwargs.get("extra_headers") or {}
    betas = extra_headers.get("anthropic-beta", "").split(",")
    assert "skills-2025-10-02" in betas


def test_provider_complete_no_change_when_skills_unset(monkeypatch):
    """Without the env var or runtime flag, today's behavior is preserved.

    The augmenter is a no-op for empty skill_ids, so kwargs should be
    free of any container / code_execution_20250825 tool.
    """
    import asyncio
    from unittest.mock import MagicMock

    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    module = _load_provider_via_conftest_alias()

    captured_kwargs: dict = {}

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text="ok")]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    fake_response.model = "claude-opus-4-7"
    fake_response.id = "msg_test"

    async def fake_create(**kw):
        captured_kwargs.update(kw)
        return fake_response

    Provider = module.AnthropicProvider
    provider = Provider(api_key="sk-test")

    fake_client = MagicMock()
    fake_client.messages.create = fake_create
    provider.client = fake_client
    monkeypatch.setattr(
        provider, "_build_client_for_key", lambda _key: fake_client, raising=False
    )

    from plugin_sdk.core import Message

    messages = [Message(role="user", content="hi")]

    try:
        asyncio.run(
            provider.complete(
                model="claude-opus-4-7",
                messages=messages,
                max_tokens=10,
            )
        )
    except Exception:
        pass

    assert "container" not in captured_kwargs
    tool_types = [t.get("type") for t in captured_kwargs.get("tools") or []]
    assert "code_execution_20250825" not in tool_types
