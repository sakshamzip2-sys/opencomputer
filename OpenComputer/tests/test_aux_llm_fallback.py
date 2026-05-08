"""T68 — auxiliary task fallback chain.

When the configured provider's aux call fails (transient: rate limit /
5xx / connection), walk the user's ``fallback_providers`` chain before
raising. Mirrors the chat-loop fallback contract — same transient/fatal
classification, same per-turn scoping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.aux_llm import complete_text


class _StubProvider:
    """Provider stub whose ``complete`` follows a scripted sequence."""

    def __init__(self, name: str, *, raise_on_calls: int = 0, reply: str = "ok") -> None:
        self.name = name
        self.raise_on_calls = raise_on_calls
        self.reply = reply
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        if self.calls <= self.raise_on_calls:
            raise RuntimeError("rate limit exceeded")  # transient
        from plugin_sdk.core import Message
        from plugin_sdk.provider_contract import ProviderResponse, Usage

        return ProviderResponse(
            message=Message(role="assistant", content=self.reply),
            usage=Usage(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


@pytest.mark.asyncio
async def test_aux_falls_back_on_transient_failure(monkeypatch):
    """Primary fails on a transient error → fallback provider is used."""
    primary = _StubProvider("p1", raise_on_calls=1)
    backup = _StubProvider("p2", reply="from-backup")

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_provider", lambda: primary
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_default_model", lambda: "model-x"
    )

    fake_cfg = MagicMock()
    fake_cfg.model.name = "model-x"
    fake_cfg.fallback_providers = (
        MagicMock(provider="p2", model="model-y", base_url=None, key_env=None),
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.default_config", lambda: fake_cfg
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_fallback_provider",
        lambda fp: backup,
    )

    result = await complete_text(messages=[{"role": "user", "content": "hi"}])
    assert result == "from-backup"
    assert primary.calls == 1
    assert backup.calls == 1


@pytest.mark.asyncio
async def test_aux_does_not_retry_on_fatal_error(monkeypatch):
    """Non-transient errors (auth, bad request) short-circuit immediately."""
    primary = MagicMock()
    primary.complete = AsyncMock(side_effect=RuntimeError("invalid api key"))

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_provider", lambda: primary
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_default_model", lambda: "model-x"
    )

    fake_cfg = MagicMock()
    fake_cfg.fallback_providers = ()
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.default_config", lambda: fake_cfg
    )

    with pytest.raises(RuntimeError, match="invalid api key"):
        await complete_text(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_aux_no_fallback_chain_passes_through(monkeypatch):
    """Empty fallback chain → exact same behavior as before."""
    primary = _StubProvider("p1", reply="primary-only")
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_provider", lambda: primary
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_default_model", lambda: "model-x"
    )

    fake_cfg = MagicMock()
    fake_cfg.fallback_providers = ()
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.default_config", lambda: fake_cfg
    )

    result = await complete_text(messages=[{"role": "user", "content": "hi"}])
    assert result == "primary-only"
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_aux_chain_exhausted_raises_last_error(monkeypatch):
    """All providers fail transiently → re-raise the LAST error."""
    primary = _StubProvider("p1", raise_on_calls=10)
    backup = _StubProvider("p2", raise_on_calls=10)

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_provider", lambda: primary
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_default_model", lambda: "model-x"
    )
    fake_cfg = MagicMock()
    fake_cfg.fallback_providers = (
        MagicMock(provider="p2", model="model-y", base_url=None, key_env=None),
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.default_config", lambda: fake_cfg
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_fallback_provider", lambda fp: backup
    )

    with pytest.raises(RuntimeError, match="rate limit"):
        await complete_text(messages=[{"role": "user", "content": "hi"}])
    assert primary.calls == 1
    assert backup.calls == 1
