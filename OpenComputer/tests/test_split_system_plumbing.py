"""V2 — split-system plumbing helpers + back-compat shims.

Closes the TDD-audit gaps:
- ``_maybe_split_system_kwargs`` (loop.py) had no unit test
- Anthropic provider back-compat shim (``system=`` → ``base_system``) had no test
- Memory-bridge / channel-prompt content reaching ``injected_system`` was uncovered
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from opencomputer.agent.loop import (
    _PROVIDER_SIG_CACHE,
    _PROVIDER_SIG_CACHE_MAX,
    _maybe_split_system_kwargs,
)

# ─── _maybe_split_system_kwargs ──────────────────────────────────────


class _LegacyProvider:
    """A provider whose .complete() does NOT know about base_system."""

    async def complete(
        self,
        *,
        model: str,
        messages: list,
        system: str = "",
        tools: list | None = None,
        max_tokens: int = 4096,
    ):
        return None


class _NewProvider:
    """A provider whose .complete() accepts the new kwargs."""

    async def complete(
        self,
        *,
        model: str,
        messages: list,
        system: str = "",
        base_system: str = "",
        injected_system: str = "",
        session_id: str | None = None,
        tools: list | None = None,
        max_tokens: int = 4096,
    ):
        return None


class _KwargsProvider:
    """A provider that uses **kwargs to absorb everything."""

    async def complete(self, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _clear_sig_cache():
    """Each test starts with a clean sig cache so caching effects are
    deterministic. The cache is shared module-level state."""
    _PROVIDER_SIG_CACHE.clear()
    yield
    _PROVIDER_SIG_CACHE.clear()


def test_legacy_provider_filters_new_kwargs_out():
    """Legacy provider doesn't accept base_system/injected_system/session_id."""
    p = _LegacyProvider()
    out = _maybe_split_system_kwargs(
        p.complete,
        base_system="b",
        injected_system="i",
        session_id="s",
    )
    assert out == {}, f"expected empty dict (legacy), got {out}"


def test_new_provider_forwards_all_three_kwargs():
    p = _NewProvider()
    out = _maybe_split_system_kwargs(
        p.complete,
        base_system="b",
        injected_system="i",
        session_id="s",
    )
    assert out == {"base_system": "b", "injected_system": "i", "session_id": "s"}


def test_kwargs_provider_treated_as_accepting_all_new_kwargs():
    """A method using **kwargs should receive all the new kwargs."""
    p = _KwargsProvider()
    out = _maybe_split_system_kwargs(
        p.complete,
        base_system="b",
        injected_system="i",
        session_id="s",
    )
    assert "base_system" in out
    assert "injected_system" in out
    assert "session_id" in out


def test_signature_cache_is_hit_on_second_call():
    """Second call with same provider class+method shouldn't re-introspect."""
    p1 = _NewProvider()
    p2 = _NewProvider()  # different instance, same class

    _maybe_split_system_kwargs(p1.complete, base_system="b", injected_system="i", session_id="s")
    cache_size_after_first = len(_PROVIDER_SIG_CACHE)

    _maybe_split_system_kwargs(p2.complete, base_system="b", injected_system="i", session_id="s")
    cache_size_after_second = len(_PROVIDER_SIG_CACHE)

    # Class+method key is stable, so 2 different instances of the same
    # class should map to ONE cache entry. (BLOCKER 3 fix: id(method)
    # would have caused 2 separate entries here.)
    assert cache_size_after_first == 1
    assert cache_size_after_second == 1


def test_signature_cache_bounded():
    """Cache should not grow unboundedly."""
    # Generate ``cap + 5`` unique provider classes so we can exceed the cap
    # without relying on the test-file class set.
    for i in range(_PROVIDER_SIG_CACHE_MAX + 5):
        ProviderCls = type(
            f"P{i}",
            (),
            {"complete": _NewProvider.complete},
        )
        p = ProviderCls()
        _maybe_split_system_kwargs(
            p.complete, base_system="b", injected_system="i", session_id="s",
        )
    assert len(_PROVIDER_SIG_CACHE) <= _PROVIDER_SIG_CACHE_MAX


def test_inspect_signature_failure_returns_empty_kwargs():
    """A method whose signature can't be introspected falls back gracefully."""

    class _OpaqueProvider:
        # Re-defined as a builtin-like callable that confuses inspect
        complete = object()  # not actually a method

    p = _OpaqueProvider()
    # Calling inspect.signature on `object()` raises TypeError
    out = _maybe_split_system_kwargs(
        p.complete,
        base_system="b",
        injected_system="i",
        session_id="s",
    )
    assert out == {}


# ─── Anthropic provider back-compat shim ─────────────────────────────


def _load_anth_provider():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_back_compat_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_anthropic_back_compat_system_only(monkeypatch):
    """Legacy callers passing ``system=`` (no ``base_system``) get the same
    cached behavior as if they'd passed ``base_system=``.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    mod = _load_anth_provider()
    provider = mod.AnthropicProvider()

    # Legacy form: only ``system=`` (no base_system / injected_system)
    sys_for_sdk_legacy, _msgs, _tools = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 20000}],
        system="frozen base",
        model="claude-opus-4-7",
    )
    # New form: explicit base_system=
    sys_for_sdk_new, _msgs2, _tools2 = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 20000}],
        base_system="frozen base",
        model="claude-opus-4-7",
    )
    assert sys_for_sdk_legacy == sys_for_sdk_new


def test_anthropic_back_compat_system_and_base_keeps_base(monkeypatch):
    """When BOTH system= and base_system= are passed, base_system wins
    (it's the more specific kwarg)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    mod = _load_anth_provider()
    provider = mod.AnthropicProvider()

    sys_for_sdk, _msgs, _tools = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 20000}],
        system="legacy",
        base_system="modern",
        model="claude-opus-4-7",
    )
    # base_system = "modern" should win
    if isinstance(sys_for_sdk, list):
        assert sys_for_sdk[0]["text"] == "modern"
    else:
        assert sys_for_sdk == "modern"


# ─── BLOCKER 2 regression-lock: empty base + injection-only ──────────


def test_empty_base_with_injection_does_not_mark_volatile(monkeypatch):  # noqa: N802
    """Audit BLOCKER 2: when only injection is present, marking it
    burns a 25%-surcharge cache write with zero hit potential. The fix
    skips the marker AND passes the injection via the SDK ``system=``
    list-content path with NO cache_control entry.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    mod = _load_anth_provider()
    provider = mod.AnthropicProvider()

    sys_for_sdk, _msgs, _tools = provider._apply_cache_control(
        [{"role": "user", "content": "x" * 50000}],
        base_system="",
        injected_system="this is a per-turn injection",
        model="claude-opus-4-7",
    )
    # Should be a list (the SDK accepts list[content] for system=).
    assert isinstance(sys_for_sdk, list)
    assert len(sys_for_sdk) == 1
    # The block has the injection text...
    assert sys_for_sdk[0]["text"] == "this is a per-turn injection"
    # ...but NO cache_control marker (the regression we're locking).
    assert "cache_control" not in sys_for_sdk[0]


# ─── Agent-loop integration: injected_volatile carries memory + channel content ─


@pytest.mark.asyncio
async def test_agent_loop_passes_full_volatile_to_split_provider(monkeypatch):
    """Audit BLOCKER 1: agent loop must include prefetched memory +
    channel prompt + channel skill bodies in ``injected_system``, not
    just the engine-compose result. Without this fix, Anthropic users
    would silently lose channel content.
    """
    import asyncio
    from dataclasses import replace as _dc_replace

    from opencomputer.agent.loop import AgentLoop

    captured: list[dict] = []

    class _Capture:
        name = "capture"
        default_model = "capture-1"

        def __init__(self):
            from plugin_sdk import ProviderCapabilities

            self._caps = ProviderCapabilities()

        @property
        def capabilities(self):
            return self._caps

        def supports_native_thinking_for(self, model):
            return False

        async def complete(
            self,
            *,
            model,
            messages,
            system="",
            base_system="",
            injected_system="",
            session_id=None,
            tools=None,
            max_tokens=4096,
            temperature=1.0,
            stream=False,
            runtime_extras=None,
            response_schema=None,
            site="agent_loop",
        ):
            captured.append(
                {
                    "system": system,
                    "base_system": base_system,
                    "injected_system": injected_system,
                    "session_id": session_id,
                }
            )
            from plugin_sdk import Message, ProviderResponse, StopReason, Usage

            return ProviderResponse(
                message=Message(role="assistant", content="ok"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=10, output_tokens=10),
            )

        async def stream_complete(self, **kw):
            return None

    from opencomputer.agent.config import Config

    cfg = Config()
    # Use replace to avoid frozen-dataclass issues on cfg.model
    cfg = _dc_replace(cfg, model=_dc_replace(cfg.model, model="capture-1"))

    provider = _Capture()
    loop = AgentLoop(provider=provider, config=cfg)

    # Inject a channel prompt via runtime.custom — what Hermes channels do.
    loop._runtime.custom["channel_prompt"] = "channel-specific instruction"

    await loop.run_conversation("hi", session_id="test-session-X")

    assert captured, "provider was never called"
    call = captured[0]

    # Channel prompt should be in injected_system (NOT silently dropped).
    assert "channel-specific instruction" in call["injected_system"], (
        f"channel prompt missing from injected_system: {call['injected_system']!r}"
    )
    # session_id should be plumbed through (the agent loop assigns
    # ``sid`` from the run_conversation arg, which should equal ours).
    assert call["session_id"] == "test-session-X"
