"""M1.3 — opt-in aux-LLM response cache.

Pins the contract added on 2026-05-09 wiring AgentCache into
``opencomputer.agent.aux_llm.complete_text`` via the ``use_cache=True``
kwarg. The plan's original premise (wrap a v2 LLM-backed reviewer
that doesn't exist yet) was reframed: the production callsite that
benefits is ``security.smart_mode.assess_command_risk``, where
temperature=0.0 + a fixed system prompt make the LLM verdict
deterministic per (command, capability_id, scope).
"""

from __future__ import annotations

from typing import Any

import pytest

from opencomputer.agent.agent_cache import (
    DEFAULT_AUX_RESPONSE_CACHE_MAX,
    aux_response_signature,
)
from opencomputer.agent.aux_llm import (
    aux_cache_stats,
    clear_aux_response_cache,
    complete_text,
)

# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Each test starts with an empty cache + zeroed stats."""
    clear_aux_response_cache()
    yield
    clear_aux_response_cache()


class _FakeProvider:
    """Stand-in for an Anthropic / OpenAI provider plugin.

    Counts every ``complete()`` call so tests can assert cache hits
    didn't silently invoke the upstream provider.
    """

    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    async def complete(self, **kwargs: Any) -> Any:
        self.call_count += 1
        self.last_kwargs = kwargs

        class _Msg:
            content = self.response

        class _Resp:
            message = _Msg()

        return _Resp()


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    """Replace the aux_llm provider resolver + cost recorder with stubs."""
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._resolve_provider", lambda: provider
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm._record_aux_cost",
        lambda *a, **k: None,
    )

    # default_config().model.provider drives the cache-key provider name
    class _ModelCfg:
        provider = "test-provider"

    class _Cfg:
        model = _ModelCfg()
        fallback_providers: tuple = ()

    monkeypatch.setattr("opencomputer.agent.aux_llm.default_config", lambda: _Cfg())


# ─── opt-in cache contract ───────────────────────────────────────────────


class TestUseCacheOptIn:
    @pytest.mark.asyncio
    async def test_use_cache_false_default_always_calls_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("hello")
        _patch_provider(monkeypatch, provider)

        msgs = [{"role": "user", "content": "say hi"}]
        await complete_text(
            messages=msgs, system="be terse", max_tokens=32, model="test-model"
        )
        await complete_text(
            messages=msgs, system="be terse", max_tokens=32, model="test-model"
        )

        assert provider.call_count == 2  # no caching
        stats = aux_cache_stats()
        assert stats == {"hits": 0, "misses": 0}

    @pytest.mark.asyncio
    async def test_use_cache_true_second_call_hits_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("yes")
        _patch_provider(monkeypatch, provider)

        msgs = [{"role": "user", "content": "is 2+2=4"}]
        out1 = await complete_text(
            messages=msgs,
            system="judge",
            max_tokens=32,
            model="test-model",
            use_cache=True,
        )
        out2 = await complete_text(
            messages=msgs,
            system="judge",
            max_tokens=32,
            model="test-model",
            use_cache=True,
        )

        assert out1 == out2 == "yes"
        assert provider.call_count == 1
        stats = aux_cache_stats()
        assert stats == {"hits": 1, "misses": 1}

    @pytest.mark.asyncio
    async def test_different_messages_yield_different_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("response")
        _patch_provider(monkeypatch, provider)

        await complete_text(
            messages=[{"role": "user", "content": "A"}],
            model="test-model",
            use_cache=True,
        )
        await complete_text(
            messages=[{"role": "user", "content": "B"}],
            model="test-model",
            use_cache=True,
        )

        assert provider.call_count == 2  # different keys, both miss
        stats = aux_cache_stats()
        assert stats == {"hits": 0, "misses": 2}

    @pytest.mark.asyncio
    async def test_different_system_yields_different_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("response")
        _patch_provider(monkeypatch, provider)

        msgs = [{"role": "user", "content": "X"}]
        await complete_text(
            messages=msgs, system="A", model="test-model", use_cache=True
        )
        await complete_text(
            messages=msgs, system="B", model="test-model", use_cache=True
        )

        assert provider.call_count == 2

    @pytest.mark.asyncio
    async def test_different_temperature_yields_different_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("response")
        _patch_provider(monkeypatch, provider)

        msgs = [{"role": "user", "content": "X"}]
        await complete_text(
            messages=msgs, temperature=0.0, model="test-model", use_cache=True
        )
        await complete_text(
            messages=msgs, temperature=0.7, model="test-model", use_cache=True
        )

        assert provider.call_count == 2

    @pytest.mark.asyncio
    async def test_different_max_tokens_yields_different_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("response")
        _patch_provider(monkeypatch, provider)

        msgs = [{"role": "user", "content": "X"}]
        await complete_text(
            messages=msgs, max_tokens=128, model="test-model", use_cache=True
        )
        await complete_text(
            messages=msgs, max_tokens=256, model="test-model", use_cache=True
        )

        assert provider.call_count == 2


# ─── LRU eviction ────────────────────────────────────────────────────────


class TestLRUEviction:
    @pytest.mark.asyncio
    async def test_lru_eviction_at_max_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("filler")
        _patch_provider(monkeypatch, provider)

        # Fill the cache past capacity by varying message content
        from opencomputer.agent.aux_llm import _AUX_RESPONSE_CACHE

        # Use a smaller maxsize for this test — temporarily resize
        original_max = _AUX_RESPONSE_CACHE.max_size
        _AUX_RESPONSE_CACHE.max_size = 4
        try:
            for i in range(6):
                await complete_text(
                    messages=[{"role": "user", "content": f"prompt-{i}"}],
                    model="test-model",
                    use_cache=True,
                )
            # Should hold at most 4 entries (LRU evicted oldest 2)
            assert len(_AUX_RESPONSE_CACHE) == 4

            # Re-asking for prompt-0 (evicted) → cache miss + new provider call
            calls_before = provider.call_count
            await complete_text(
                messages=[{"role": "user", "content": "prompt-0"}],
                model="test-model",
                use_cache=True,
            )
            assert provider.call_count == calls_before + 1
        finally:
            _AUX_RESPONSE_CACHE.max_size = original_max


# ─── signature shape ─────────────────────────────────────────────────────


class TestAuxResponseSignature:
    def test_identical_inputs_yield_identical_keys(self) -> None:
        msgs = [{"role": "user", "content": "X"}]
        k1 = aux_response_signature(
            provider_name="anthropic",
            model="claude-opus-4-7",
            system="sys",
            messages=msgs,
            max_tokens=128,
            temperature=0.0,
        )
        k2 = aux_response_signature(
            provider_name="anthropic",
            model="claude-opus-4-7",
            system="sys",
            messages=msgs,
            max_tokens=128,
            temperature=0.0,
        )
        assert k1 == k2

    def test_message_order_matters(self) -> None:
        k1 = aux_response_signature(
            provider_name="x",
            model="m",
            system="",
            messages=[
                {"role": "user", "content": "A"},
                {"role": "user", "content": "B"},
            ],
            max_tokens=64,
            temperature=0.0,
        )
        k2 = aux_response_signature(
            provider_name="x",
            model="m",
            system="",
            messages=[
                {"role": "user", "content": "B"},
                {"role": "user", "content": "A"},
            ],
            max_tokens=64,
            temperature=0.0,
        )
        assert k1 != k2  # transcripts in different orders are different cache entries

    def test_default_max_size_constant(self) -> None:
        # Pin the constant so a future bump is intentional
        assert DEFAULT_AUX_RESPONSE_CACHE_MAX == 256


# ─── stats + clear ───────────────────────────────────────────────────────


class TestStatsAndClear:
    @pytest.mark.asyncio
    async def test_clear_resets_stats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider("ok")
        _patch_provider(monkeypatch, provider)

        await complete_text(
            messages=[{"role": "user", "content": "X"}],
            model="test-model",
            use_cache=True,
        )
        await complete_text(
            messages=[{"role": "user", "content": "X"}],
            model="test-model",
            use_cache=True,
        )
        assert aux_cache_stats() == {"hits": 1, "misses": 1}

        clear_aux_response_cache()
        assert aux_cache_stats() == {"hits": 0, "misses": 0}

        # After clear, the same prompt is a miss again
        await complete_text(
            messages=[{"role": "user", "content": "X"}],
            model="test-model",
            use_cache=True,
        )
        assert aux_cache_stats()["misses"] == 1

    def test_aux_cache_stats_returns_fresh_dict(self) -> None:
        snap1 = aux_cache_stats()
        snap1["hits"] = 9999
        snap2 = aux_cache_stats()
        assert snap2["hits"] == 0  # mutating the returned dict didn't leak


# ─── smart_mode integration: opt-in is wired ────────────────────────────


class TestSmartModeUsesCache:
    """Verify the smart_mode wiring actually passes use_cache=True.

    Reads the source file rather than mocking — pinning the wiring
    with a static check is more robust than re-running the LLM call.
    """

    def test_smart_mode_passes_use_cache_true(self) -> None:
        from pathlib import Path

        text = Path(
            "opencomputer/security/smart_mode.py"
        ).read_text()
        assert "use_cache=True" in text, (
            "regression: smart_mode.assess_command_risk lost the "
            "use_cache=True opt-in. Re-add per M1.3 (2026-05-09)."
        )
