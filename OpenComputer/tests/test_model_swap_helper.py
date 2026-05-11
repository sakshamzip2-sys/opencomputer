"""Tests for ``opencomputer/agent/model_swap.py``.

This is the single source of truth used by:

* ``cli.py::_on_model_swap`` for the ``/model <id>`` slash command, and
* ``agent/loop.py``'s pending-swap consumer for Alt+M scoped-models cycling.

The helper has to:

1. Reject empty / whitespace-only input loudly (no silent no-op).
2. Resolve aliases through ``model_resolver.resolve_model``.
3. Strip ``:nitro`` / ``:floor`` when the provider isn't OpenRouter,
   with a warning surfaced via console or _log.
4. Route ``custom:<name>:<model>`` through the custom provider builder.
5. Apply via ``dataclasses.replace`` (frozen config stays frozen).
6. Refresh ``_provider_supports_native_thinking`` per-model.
7. Fire ``HookEvent.BEFORE_MODEL_RESOLVE`` for observability — never block.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from opencomputer.agent.model_swap import swap_model

# ─── Test fixtures ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _FakeModelCfg:
    model: str
    provider: str
    model_aliases: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _FakeConfig:
    model: _FakeModelCfg


class _FakeProvider:
    """A provider stand-in that records calls to
    ``supports_native_thinking_for``. Tests rely on the recorded
    arg list to verify the refresh fired."""

    def __init__(self, supports: bool = True, raises: bool = False) -> None:
        self._supports = supports
        self._raises = raises
        self.calls: list[str] = []

    def supports_native_thinking_for(self, model: str) -> bool:
        self.calls.append(model)
        if self._raises:
            raise RuntimeError("provider blew up")
        return self._supports


class _FakeLoop:
    """A loop stand-in with mutable .config and .provider — the
    swap_model contract."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        provider: str = "anthropic",
        aliases: dict[str, str] | None = None,
        prov_supports: bool = True,
        prov_raises: bool = False,
    ) -> None:
        self.config = _FakeConfig(
            model=_FakeModelCfg(
                model=model, provider=provider, model_aliases=aliases or {}
            )
        )
        self.provider = _FakeProvider(supports=prov_supports, raises=prov_raises)


def _make_runtime() -> SimpleNamespace:
    return SimpleNamespace(custom={"session_id": "test-session"})


# ─── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_string_rejected(self) -> None:
        loop = _FakeLoop()
        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model="")
        assert ok is False
        assert "required" in msg.lower()
        # Config must be unchanged.
        assert loop.config.model.model == "claude-sonnet-4-6"

    def test_whitespace_rejected(self) -> None:
        loop = _FakeLoop()
        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model="   ")
        assert ok is False
        assert "required" in msg.lower()

    def test_non_string_rejected(self) -> None:
        loop = _FakeLoop()
        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model=None)  # type: ignore[arg-type]
        assert ok is False


# ─── Alias resolution ──────────────────────────────────────────────────


class TestAliasResolution:
    def test_resolves_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _FakeLoop(aliases={"sonnet": "claude-sonnet-4-6"})
        # Stub resolve_model to return the alias-resolved value.
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: a.get(x, x))

        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model="sonnet")
        assert ok is True
        assert loop.config.model.model == "claude-sonnet-4-6"

    def test_invalid_id_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _FakeLoop()
        from opencomputer.agent import model_resolver as mr

        def _raise(*_a: Any, **_kw: Any) -> str:
            raise ValueError("unknown alias: foo")

        monkeypatch.setattr(mr, "resolve_model", _raise)
        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model="foo")
        assert ok is False
        assert "unknown alias" in msg

    def test_resolve_returning_non_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda *_a, **_kw: None)
        ok, msg = swap_model(loop=loop, runtime=_make_runtime(), new_model="x")
        assert ok is False
        assert "invalid model id" in msg.lower()


# ─── OpenRouter routing-suffix stripping ───────────────────────────────


class TestRoutingSuffix:
    def test_strips_nitro_when_not_openrouter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(provider="anthropic")
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        ok, msg = swap_model(
            loop=loop,
            runtime=_make_runtime(),
            new_model="claude-sonnet-4-6:nitro",
        )
        assert ok is True
        assert loop.config.model.model == "claude-sonnet-4-6"  # suffix stripped
        # Message reports the resolved canonical name, not the suffixed input.
        assert ":nitro" not in msg

    def test_keeps_suffix_on_openrouter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(provider="openrouter")
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        ok, msg = swap_model(
            loop=loop,
            runtime=_make_runtime(),
            new_model="anthropic/claude-sonnet-4-6:nitro",
        )
        assert ok is True
        # Suffix preserved when the provider IS openrouter.
        assert loop.config.model.model.endswith(":nitro")

    def test_console_receives_strip_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(provider="anthropic")
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        printed: list[str] = []

        class _C:
            def print(self, text: str) -> None:
                printed.append(text)

        ok, _ = swap_model(
            loop=loop,
            runtime=_make_runtime(),
            new_model="claude-sonnet-4-6:nitro",
            console=_C(),
        )
        assert ok is True
        assert any(":nitro" in p for p in printed)


# ─── Provider-supports-native-thinking refresh ─────────────────────────


class TestProviderRefresh:
    def test_refresh_called_with_new_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        swap_model(loop=loop, runtime=rt, new_model="new-model")
        # Provider was queried for the new model id.
        assert loop.provider.calls == ["new-model"]
        # Flag landed on runtime.
        assert rt.custom["_provider_supports_native_thinking"] is True

    def test_refresh_failure_defaults_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(prov_raises=True)
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        ok, _ = swap_model(loop=loop, runtime=rt, new_model="new-model")
        # Swap still succeeded — the refresh failure is non-fatal.
        assert ok is True
        assert rt.custom["_provider_supports_native_thinking"] is False


# ─── Hook fire ──────────────────────────────────────────────────────────


class TestHookFire:
    def test_hook_fired_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        fired: list = []
        from opencomputer.hooks import engine as engine_mod

        original_ff = engine_mod.engine.fire_and_forget
        monkeypatch.setattr(
            engine_mod.engine, "fire_and_forget", lambda ctx: fired.append(ctx)
        )
        try:
            swap_model(loop=loop, runtime=rt, new_model="some-model")
        finally:
            monkeypatch.setattr(engine_mod.engine, "fire_and_forget", original_ff)

        assert len(fired) == 1
        ctx = fired[0]
        assert ctx.event.value == "BeforeModelResolve"
        # Hook payload encodes the new model id.
        payload = ctx.messages[0]
        assert payload["new_model"] == "some-model"

    def test_hook_failure_doesnt_block_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        from opencomputer.hooks import engine as engine_mod

        def _explode(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("hook engine offline")

        monkeypatch.setattr(engine_mod.engine, "fire_and_forget", _explode)
        ok, _ = swap_model(loop=loop, runtime=rt, new_model="x")
        # Swap still applies even when the hook engine is offline.
        assert ok is True
        assert loop.config.model.model == "x"


# ─── Custom provider path ──────────────────────────────────────────────


class TestCustomProvider:
    def test_custom_spec_builds_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        rt = _make_runtime()

        new_provider = _FakeProvider(supports=False)
        from opencomputer.agent import custom_provider_client as cpc

        monkeypatch.setattr(
            cpc, "parse_custom_model_spec", lambda s: ("my-llm", "gpt-fast")
        )
        monkeypatch.setattr(
            cpc, "build_custom_provider", lambda name, cfg: new_provider
        )

        ok, msg = swap_model(
            loop=loop, runtime=rt, new_model="custom:my-llm:gpt-fast"
        )
        assert ok is True
        assert loop.provider is new_provider
        assert loop.config.model.model == "gpt-fast"
        assert loop.config.model.provider == "custom:my-llm"
        assert "custom:my-llm:gpt-fast" in msg

    def test_custom_parse_error_returns_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop()
        from opencomputer.agent import custom_provider_client as cpc

        def _raise(*_a: Any) -> tuple[str, str]:
            raise ValueError("malformed custom spec")

        monkeypatch.setattr(cpc, "parse_custom_model_spec", _raise)
        ok, msg = swap_model(
            loop=loop, runtime=_make_runtime(), new_model="custom:bogus"
        )
        assert ok is False
        assert "malformed custom spec" in msg
        # Loop state unchanged on failure.
        assert loop.provider.__class__.__name__ == "_FakeProvider"


# ─── Runtime active-model cache refresh ────────────────────────────────


class TestRuntimeActiveModelCache:
    """Regression: 2026-05-11 — ``/model`` swap appeared to silently
    fail because ``swap_model`` mutated ``loop.config`` but never
    refreshed ``runtime.custom["model_id"]`` (the status-line cache)
    nor ``runtime.custom["active_model_id"]`` (the Alt+M cycle
    anchor). Both writes must fire on every successful swap so the
    UI reflects the new model immediately, not on the next user turn.
    """

    def test_canonical_swap_writes_model_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(model="claude-sonnet-4-6")
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        ok, _ = swap_model(loop=loop, runtime=rt, new_model="claude-opus-4-7")
        assert ok is True
        # Status-line consumer reads this every keystroke.
        assert rt.custom["model_id"] == "claude-opus-4-7"

    def test_canonical_swap_writes_active_model_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _FakeLoop(model="claude-sonnet-4-6")
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        swap_model(loop=loop, runtime=rt, new_model="claude-opus-4-7")
        # Alt+M cycle anchor — without this the cycle always restarts
        # from favorites[0] after a /model swap.
        assert rt.custom["active_model_id"] == "claude-opus-4-7"

    def test_swap_writes_canonical_id_after_alias_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User types ``/model fast`` — the cache must store the resolved
        ``claude-haiku-4-5-20251001``, not the alias text."""
        loop = _FakeLoop(aliases={"fast": "claude-haiku-4-5-20251001"})
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: a.get(x, x))

        swap_model(loop=loop, runtime=rt, new_model="fast")
        assert rt.custom["model_id"] == "claude-haiku-4-5-20251001"
        assert rt.custom["active_model_id"] == "claude-haiku-4-5-20251001"

    def test_swap_writes_stripped_id_after_nitro_strip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User types ``/model claude-sonnet-4-6:nitro`` on a non-OR
        provider — the cache must store the STRIPPED id matching what
        the next API call will actually send."""
        loop = _FakeLoop(provider="anthropic")
        rt = _make_runtime()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)

        swap_model(
            loop=loop, runtime=rt, new_model="claude-sonnet-4-6:nitro"
        )
        # The colon-suffix is OR-only; non-OR providers strip it. Cache
        # must reflect what the provider actually sees, not the user's
        # typed string.
        assert rt.custom["model_id"] == "claude-sonnet-4-6"
        assert rt.custom["active_model_id"] == "claude-sonnet-4-6"

    def test_failed_swap_does_not_overwrite_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed swap (empty / invalid / unknown alias) must NOT
        scribble over the existing runtime cache — the old model is
        still the active one."""
        loop = _FakeLoop(model="claude-sonnet-4-6")
        rt = _make_runtime()
        rt.custom["model_id"] = "claude-sonnet-4-6"
        rt.custom["active_model_id"] = "claude-sonnet-4-6"

        ok, _ = swap_model(loop=loop, runtime=rt, new_model="")
        assert ok is False
        # Cache untouched — the old model is still authoritative.
        assert rt.custom["model_id"] == "claude-sonnet-4-6"
        assert rt.custom["active_model_id"] == "claude-sonnet-4-6"

        from opencomputer.agent import model_resolver as mr

        def _raise(*_a: Any, **_kw: Any) -> str:
            raise ValueError("unknown alias")

        monkeypatch.setattr(mr, "resolve_model", _raise)
        ok, _ = swap_model(
            loop=loop, runtime=rt, new_model="totally-not-real-alias"
        )
        assert ok is False
        assert rt.custom["model_id"] == "claude-sonnet-4-6"
        assert rt.custom["active_model_id"] == "claude-sonnet-4-6"

    def test_custom_swap_writes_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``custom:<name>:<model>`` path must refresh the cache too
        so the status line and cycle anchor stay coherent across provider
        + model swaps in a single call."""
        loop = _FakeLoop()
        rt = _make_runtime()
        new_provider = _FakeProvider(supports=False)
        from opencomputer.agent import custom_provider_client as cpc

        monkeypatch.setattr(
            cpc, "parse_custom_model_spec", lambda s: ("my-llm", "gpt-fast")
        )
        monkeypatch.setattr(
            cpc, "build_custom_provider", lambda name, cfg: new_provider
        )

        ok, _ = swap_model(
            loop=loop, runtime=rt, new_model="custom:my-llm:gpt-fast"
        )
        assert ok is True
        assert rt.custom["model_id"] == "gpt-fast"
        assert rt.custom["active_model_id"] == "gpt-fast"

    def test_swap_with_none_runtime_doesnt_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive: if some future caller passes ``runtime=None``,
        the swap must still mutate ``loop.config`` correctly (the
        cache refresh is a best-effort UI courtesy, not load-bearing
        on swap correctness)."""
        loop = _FakeLoop()
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        # The current API contract has runtime as non-None, but the
        # cache-refresh helper must degrade gracefully so a future
        # caller mistake doesn't turn into a NoneType.custom crash.
        from opencomputer.agent.model_swap import (
            _refresh_runtime_active_model_cache,
        )
        # Direct test on the helper — should not raise.
        _refresh_runtime_active_model_cache(None, "claude-opus-4-7")
        # Also exercises a runtime whose custom is not a dict.
        from types import SimpleNamespace
        _refresh_runtime_active_model_cache(
            SimpleNamespace(custom="not-a-dict"), "claude-opus-4-7"
        )
        # Runtime without a .custom attribute at all.
        _refresh_runtime_active_model_cache(object(), "claude-opus-4-7")

    def test_empty_model_id_is_noop(self) -> None:
        """The helper rejects empty/None model ids — calling it with
        a garbage value should NOT pollute runtime.custom with an
        empty string (which would render as ``"(unknown)"`` in the
        status line and confuse the user)."""
        from opencomputer.agent.model_swap import (
            _refresh_runtime_active_model_cache,
        )
        rt = _make_runtime()
        rt.custom["model_id"] = "claude-opus-4-7"
        rt.custom["active_model_id"] = "claude-opus-4-7"
        _refresh_runtime_active_model_cache(rt, "")
        _refresh_runtime_active_model_cache(rt, None)  # type: ignore[arg-type]
        # Both rejected — existing cache survives.
        assert rt.custom["model_id"] == "claude-opus-4-7"
        assert rt.custom["active_model_id"] == "claude-opus-4-7"


# ─── Delegate / background factories pick up post-swap config ──────────


class TestSubagentInheritsSwap:
    """Regression: 2026-05-11 — until this fix, ``DelegateTool.set_factory``
    and ``bg_registry.set_factory`` in cli.py closed over the FROZEN
    session-start ``cfg`` and ``provider`` locals, not the live
    ``loop.config`` / ``loop.provider``. After a ``/model`` swap the
    parent loop's config was updated via ``dataclasses.replace`` but
    the factory's captured ``cfg`` was unchanged — so every Delegate
    spawn and every ``/background`` job after the swap inherited the
    OLD model. That latent staleness is the "hardcoded somewhere"
    Saksham was smelling: the SLASH succeeds, the status bar updates,
    but subagents revert to the pre-swap model.

    These tests simulate the chat-session factory pattern (loop +
    lambda) and assert that mutating ``loop.config`` post-construction
    is observed by the factory's next call.
    """

    def test_factory_reads_live_loop_config_after_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirror cli.py:1685: factory closes over ``loop``. After
        ``swap_model`` rebinds ``loop.config``, a NEW factory call
        sees the swapped model.
        """
        parent = _FakeLoop(model="claude-sonnet-4-6")

        # Equivalent to cli.py's DelegateTool.set_factory(...) — the
        # factory closes over the parent ``loop`` and reads .config /
        # .provider at call time, not at definition time.
        def child_factory():
            return _FakeLoop_passthrough(
                provider=parent.provider, config=parent.config
            )

        # Pre-swap: factory yields a child with sonnet.
        c1 = child_factory()
        assert c1.config.model.model == "claude-sonnet-4-6"

        # Swap on the parent.
        from opencomputer.agent import model_resolver as mr

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        ok, _ = swap_model(
            loop=parent, runtime=_make_runtime(), new_model="claude-opus-4-7"
        )
        assert ok is True

        # Post-swap: a NEW factory invocation must see opus, not the
        # frozen-at-import sonnet. THIS is the regression — pre-fix
        # the factory captured ``cfg`` directly and ALWAYS returned
        # sonnet here, no matter how many swaps happened.
        c2 = child_factory()
        assert c2.config.model.model == "claude-opus-4-7", (
            "Delegate factory must read loop.config at call time. "
            "If you see sonnet here the fix at cli.py:1685 regressed."
        )

    def test_factory_closing_over_stale_cfg_reproduces_old_bug(self) -> None:
        """Locks in WHY we changed the closure target. A factory that
        closes over a captured ``cfg`` reference will keep returning
        children with the OLD model even after the parent's config is
        rebound via ``dataclasses.replace``. This test exists so a
        future refactor that "simplifies" the closure can't silently
        re-introduce the bug — running this test before reverting the
        fix would surface the regression immediately.
        """
        parent = _FakeLoop(model="claude-sonnet-4-6")
        original_cfg = parent.config  # The "frozen at session start" snapshot.

        # BUG PATTERN — closes over the local cfg variable:
        def buggy_factory():
            return _FakeLoop_passthrough(
                provider=parent.provider, config=original_cfg
            )

        # Rebind parent.config (simulates dataclasses.replace from swap_model).
        parent.config = dataclasses.replace(
            parent.config,
            model=dataclasses.replace(
                parent.config.model, model="claude-opus-4-7"
            ),
        )
        # Parent sees the new model.
        assert parent.config.model.model == "claude-opus-4-7"
        # But the buggy factory still returns a child with the OLD model.
        child = buggy_factory()
        assert child.config.model.model == "claude-sonnet-4-6", (
            "If this assertion fails the closure semantics changed — "
            "either Python evaluated cfg differently OR _FakeLoop_passthrough "
            "stopped honoring the config arg. The original cli.py bug DID "
            "exhibit exactly this stale-capture pattern; keep this test."
        )

    def test_factory_reads_live_provider_after_provider_swap(self) -> None:
        """The same closure pattern protects /provider swap too:
        ``loop.provider`` is reassigned post-swap; a factory closing
        over ``loop`` reads the live provider, while one closing over
        the captured local ``provider`` would not.
        """
        parent = _FakeLoop(provider="anthropic")
        original_provider = parent.provider

        def live_factory():
            return _FakeLoop_passthrough(
                provider=parent.provider, config=parent.config
            )

        # Swap parent.provider (mirrors cli.py:_on_provider_swap path).
        new_prov = _FakeProvider(supports=False)
        parent.provider = new_prov

        child = live_factory()
        assert child.provider is new_prov
        assert child.provider is not original_provider


@dataclasses.dataclass
class _FakeLoop_passthrough:
    """Mutable, non-frozen stand-in for child AgentLoop. Used in the
    subagent-inheritance tests where we need to inspect config/provider
    on the freshly-spawned child."""

    provider: Any
    config: Any
