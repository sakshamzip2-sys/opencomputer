"""Wave 3 follow-up — dynamic context-window probe chain."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Per-test isolation: fresh cache + tmp profile home."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import context_window_probe as cwp

    cwp.reset_cache()
    yield
    cwp.reset_cache()


def test_cache_miss_returns_none():
    from opencomputer.agent.context_window_probe import _cache_get

    assert _cache_get("any", "no-such-model") is None


def test_cache_put_then_get_roundtrip():
    from opencomputer.agent.context_window_probe import _cache_get, _cache_put

    _cache_put("openrouter", "anthropic/claude-foo", 500_000)
    assert _cache_get("openrouter", "anthropic/claude-foo") == 500_000


def test_cache_persists_to_disk(tmp_path, monkeypatch):
    from opencomputer.agent import context_window_probe as cwp

    cwp.reset_cache()
    cwp._cache_put("openrouter", "model-a", 128_000)
    # Drop in-memory; reload from disk.
    cwp.reset_cache()
    assert cwp._cache_get("openrouter", "model-a") == 128_000


def test_cache_ttl_expires():
    from opencomputer.agent import context_window_probe as cwp

    cwp.reset_cache()
    cwp._cache_put("openrouter", "stale-model", 100_000)
    # Forcibly age the entry past TTL.
    cache = cwp._load_cache()
    cache[cwp._cache_key("openrouter", "stale-model")]["fetched_at"] = (
        time.time() - cwp.CACHE_TTL_SECONDS - 1.0
    )
    assert cwp._cache_get("openrouter", "stale-model") is None


# ─── OpenRouter probe ───────────────────────────────────────────────


def test_probe_openrouter_finds_model_by_full_id(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "anthropic/claude-opus-4-7", "context_length": 1_000_000},
                    {"id": "openai/gpt-4o", "context_length": 128_000},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    assert cwp._probe_openrouter("anthropic/claude-opus-4-7") == 1_000_000


def test_probe_openrouter_finds_model_by_suffix(monkeypatch):
    """A bare 'claude-opus-4-7' (no provider/) matches OR's namespaced id."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "anthropic/claude-opus-4-7", "context_length": 1_000_000},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    assert cwp._probe_openrouter("claude-opus-4-7") == 1_000_000


def test_probe_openrouter_unknown_returns_none(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "openai/gpt-4o", "context_length": 128_000}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    assert cwp._probe_openrouter("unknown-model") is None


def test_probe_openrouter_network_failure_returns_none(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    def _fail(*a, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", _fail)
    assert cwp._probe_openrouter("anything") is None


def test_probe_openrouter_non_200_returns_none(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 503

        def json(self):
            return {}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    assert cwp._probe_openrouter("anything") is None


# ─── Ollama probe ──────────────────────────────────────────────────


def test_probe_ollama_reads_model_info_context_length(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "model_info": {"qwen3.context_length": 32_768},
                "parameters": "",
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    assert cwp._probe_ollama("qwen3:27b") == 32_768


def test_probe_ollama_reads_num_ctx_from_parameters(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "model_info": {},
                "parameters": "stop \"<|im_end|>\"\nnum_ctx 65536\ntemperature 0.7",
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    assert cwp._probe_ollama("qwen3:27b") == 65_536


def test_probe_ollama_offline_returns_none(monkeypatch):
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    def _fail(*a, **kw):
        raise httpx.ConnectError("no Ollama running")

    monkeypatch.setattr(httpx, "post", _fail)
    assert cwp._probe_ollama("any-model") is None


# ─── Anthropic probe ───────────────────────────────────────────────


def test_probe_anthropic_only_runs_for_claude_models(monkeypatch):
    """Non-claude model id short-circuits before any HTTP call."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sentinel = {"called": False}

    def _spy(*a, **kw):
        sentinel["called"] = True
        return None

    monkeypatch.setattr(httpx, "get", _spy)
    assert cwp._probe_anthropic("gpt-4o") is None
    assert sentinel["called"] is False


def test_probe_anthropic_no_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from opencomputer.agent import context_window_probe as cwp

    assert cwp._probe_anthropic("claude-opus-4-7") is None


def test_probe_anthropic_picks_up_max_input_tokens_when_present(monkeypatch):
    """Future-proof: if Anthropic adds the field, we use it."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "claude-opus-4-7", "max_input_tokens": 1_000_000},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    assert cwp._probe_anthropic("claude-opus-4-7") == 1_000_000


# ─── orchestrator + cache integration ───────────────────────────────


def test_probe_context_window_short_circuits_on_cache_hit(monkeypatch):
    """Cache hit returns immediately, no HTTP calls."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    cwp._cache_put("any", "cached-model", 99_999)
    sentinel = {"calls": 0}

    def _spy(*a, **kw):
        sentinel["calls"] += 1
        raise AssertionError("should not be called when cache hit")

    monkeypatch.setattr(httpx, "get", _spy)
    monkeypatch.setattr(httpx, "post", _spy)
    assert cwp.probe_context_window("cached-model") == 99_999
    assert sentinel["calls"] == 0


def test_probe_context_window_walks_chain_and_caches(monkeypatch):
    """All probes return None except OpenRouter; result is cached."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    or_called = {"n": 0}

    class _ORResp:
        status_code = 200

        def json(self):
            return {
                "data": [{"id": "anthropic/walk-test", "context_length": 200_000}]
            }

    def _get(*a, **kw):
        or_called["n"] += 1
        return _ORResp()

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("no ollama")),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert cwp.probe_context_window("anthropic/walk-test") == 200_000
    # Second call: cache hit; no additional HTTP calls.
    assert cwp.probe_context_window("anthropic/walk-test") == 200_000
    assert or_called["n"] == 1


def test_probe_context_window_all_miss_returns_none(monkeypatch):
    """Every source misses → None, no fallback to static defaults here
    (caller is responsible for the next layer)."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp

    class _Empty:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Empty())
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Empty())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cwp.probe_context_window("totally-fake-vendor/totally-fake-model") is None


# ─── compaction integration ─────────────────────────────────────────


def test_context_window_with_overrides_uses_probe(monkeypatch):
    """Resolver picks up the probed value when neither override layer matches."""
    from opencomputer.agent import context_window_probe as cwp
    from opencomputer.agent.compaction import context_window_with_overrides

    cwp._cache_put("any", "fresh-model", 333_333)
    assert context_window_with_overrides("fresh-model") == 333_333


def test_context_window_with_overrides_explicit_wins_over_probe(monkeypatch):
    """User override wins even if probe has a different value."""
    from opencomputer.agent import context_window_probe as cwp
    from opencomputer.agent.compaction import context_window_with_overrides

    cwp._cache_put("any", "dual-model", 200_000)
    result = context_window_with_overrides(
        "dual-model",
        model_context_overrides={"dual-model": 999_999},
    )
    assert result == 999_999


def test_context_window_with_overrides_enable_probe_false_skips(monkeypatch):
    """enable_probe=False (hot path) skips the probe layer entirely."""
    import httpx

    from opencomputer.agent import context_window_probe as cwp
    from opencomputer.agent.compaction import context_window_with_overrides

    cwp.reset_cache()  # ensure no cached value
    sentinel = {"called": False}

    def _spy(*a, **kw):
        sentinel["called"] = True
        return None

    monkeypatch.setattr(httpx, "get", _spy)
    monkeypatch.setattr(httpx, "post", _spy)
    # No override, no custom_provider, probe disabled → static table.
    result = context_window_with_overrides(
        "claude-opus-4-7",
        enable_probe=False,
    )
    assert result == 1_000_000  # from the static DEFAULT_CONTEXT_WINDOWS
    assert sentinel["called"] is False


def test_status_line_max_context_for_uses_static_path(monkeypatch):
    """The hot-path render passes enable_probe=False — never calls httpx."""
    import httpx

    from opencomputer.cli_ui.status_line import max_context_for

    sentinel = {"called": False}

    def _spy(*a, **kw):
        sentinel["called"] = True
        return None

    monkeypatch.setattr(httpx, "get", _spy)
    monkeypatch.setattr(httpx, "post", _spy)
    assert max_context_for("claude-opus-4-7") == 1_000_000
    assert sentinel["called"] is False
