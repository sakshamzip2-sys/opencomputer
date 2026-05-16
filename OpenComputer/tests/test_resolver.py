"""Tests for the per-tool-call sandbox backend resolver — Milestone 2 (T2.4).

Covers ``opencomputer.sandbox.resolver.resolve_backend``: every branch of
the decision matrix from the module docstring —

1. ``sandbox_preference == "skip"`` → ``None``.
2. sandboxing disabled globally → ``None`` for an ordinary tool; the
   ``required`` tool's claim is honored.
3. a tool ``sandbox_backend_hint`` that is available → use the hint.
4. otherwise → the user's configured default backend.

plus the ``fallback`` policy (T2.9): a ``required`` tool with no
reachable backend raises under ``error`` and runs un-sandboxed (warning)
under ``local``.

The resolver resolves backends by name via
``opencomputer.sandbox.runner._named_strategy``; the tests monkeypatch
that boundary with stub strategies so they are deterministic and need no
real Docker / E2B / bwrap.
"""

from __future__ import annotations

import logging

import pytest

from opencomputer.agent.config import Config
from opencomputer.sandbox import resolver as resolver_mod
from opencomputer.sandbox.policy import SandboxPolicy, SandboxScopeContext
from opencomputer.sandbox.resolver import (
    SANDBOX_FALLBACK_ERROR,
    SANDBOX_FALLBACK_LOCAL,
    fallback_policy,
    resolve_backend,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.sandbox import SandboxStrategy, SandboxUnavailable
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── stub tools + backends ─────────────────────────────────────────────


class _StubTool(BaseTool):
    """Minimal concrete ``BaseTool`` — the resolver only reads its class fields."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="Stub", description="stub", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
        return ToolResult(tool_call_id=call.id, content="ok")


class _DefaultTool(_StubTool):
    """A tool with the default preference — no opt-in, no hint."""


class _SkipTool(_StubTool):
    sandbox_preference = "skip"


class _RequiredTool(_StubTool):
    sandbox_preference = "required"


class _HintTool(_StubTool):
    sandbox_backend_hint = "e2b"


class _RequiredHintTool(_StubTool):
    sandbox_preference = "required"
    sandbox_backend_hint = "e2b"


class _StubBackend(SandboxStrategy):
    """A stub :class:`SandboxStrategy` whose availability is controllable."""

    def __init__(self, name: str, *, available: bool = True) -> None:
        self.name = name
        self._available = available

    def is_available(self) -> bool:
        return self._available

    async def run(self, argv, *, config, stdin=None, cwd=None):  # pragma: no cover
        raise NotImplementedError

    def explain(self, argv, *, config):  # pragma: no cover
        return list(argv)


def _patch_named_strategy(
    monkeypatch: pytest.MonkeyPatch, backends: dict[str, _StubBackend]
) -> None:
    """Patch ``_named_strategy`` to resolve from ``backends`` (else raise).

    Mirrors the real ``runner._named_strategy`` contract: an unknown
    name OR a known-but-unavailable backend raises ``SandboxUnavailable``.
    """

    def _fake(name: str) -> SandboxStrategy:
        backend = backends.get(name)
        if backend is None:
            raise SandboxUnavailable(f"unknown sandbox strategy {name!r}")
        if not backend.is_available():
            raise SandboxUnavailable(f"sandbox strategy {name!r} not available")
        return backend

    monkeypatch.setattr(resolver_mod, "_named_strategy", _fake)


def _config(backend: str | None = None, fallback: str = "error") -> Config:
    """Build a ``Config`` whose ``sandbox`` policy carries backend + fallback.

    M2 (2026-05-16): the backend choice + fallback policy live on the M1
    :class:`~opencomputer.sandbox.policy.SandboxPolicy` — the ``sandbox:``
    config block — so the spec's ``sandbox.backend`` / ``sandbox.fallback``
    key names are literally true.
    """
    return Config(sandbox=SandboxPolicy(backend=backend, fallback=fallback))


# ─── branch 1: a "skip" tool is never sandboxed ────────────────────────


def test_skip_tool_returns_none_even_with_backend_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_named_strategy(monkeypatch, {"e2b": _StubBackend("e2b")})
    # Backend IS configured — but the tool opted out, so still None.
    assert resolve_backend(_SkipTool(), _config(backend="e2b")) is None


def test_skip_tool_returns_none_with_no_config() -> None:
    assert resolve_backend(_SkipTool(), _config()) is None


def test_skip_beats_a_backend_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """``skip`` short-circuits before the hint branch is even consulted."""

    class _SkipWithHint(_StubTool):
        sandbox_preference = "skip"
        sandbox_backend_hint = "e2b"

    _patch_named_strategy(monkeypatch, {"e2b": _StubBackend("e2b")})
    assert resolve_backend(_SkipWithHint(), _config(backend="docker")) is None


# ─── branch 2: sandboxing disabled globally ────────────────────────────


def test_disabled_globally_ordinary_tool_returns_none() -> None:
    """No backend configured + default tool → None. The no-op path."""
    assert resolve_backend(_DefaultTool(), _config()) is None


def test_disabled_globally_none_config_object_returns_none() -> None:
    """A bare ``Config`` (default ``sandbox`` policy) is the no-op path."""
    assert resolve_backend(_DefaultTool(), Config()) is None


def test_disabled_globally_with_none_config_returns_none() -> None:
    """``config=None`` is treated as sandboxing-disabled."""
    assert resolve_backend(_DefaultTool(), None) is None


def test_disabled_globally_empty_backend_string_is_unset() -> None:
    """An empty / whitespace ``backend`` string counts as not configured."""
    assert resolve_backend(_DefaultTool(), _config(backend="   ")) is None


def test_disabled_globally_required_tool_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``required`` tool's claim is honored even with no default backend.

    With no backend configured and the default ``error`` fallback, the
    resolver raises rather than silently running un-sandboxed.
    """
    _patch_named_strategy(monkeypatch, {})
    with pytest.raises(SandboxUnavailable, match="required"):
        resolve_backend(_RequiredTool(), _config())


def test_disabled_globally_required_tool_uses_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``required`` tool with an available hint resolves to the hint.

    Even though sandboxing is "disabled globally" (no default backend),
    the ``required`` claim drives past branch 2 and the hint is used.
    """
    e2b = _StubBackend("e2b", available=True)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    assert resolve_backend(_RequiredHintTool(), _config()) is e2b


# ─── branch 3: a tool backend hint that is available ───────────────────


def test_available_hint_is_used_over_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2b = _StubBackend("e2b", available=True)
    docker = _StubBackend("docker", available=True)
    _patch_named_strategy(monkeypatch, {"e2b": e2b, "docker": docker})
    # Default is docker, but the tool hints e2b and e2b is available.
    assert resolve_backend(_HintTool(), _config(backend="docker")) is e2b


def test_unavailable_hint_falls_back_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2b = _StubBackend("e2b", available=False)
    docker = _StubBackend("docker", available=True)
    _patch_named_strategy(monkeypatch, {"e2b": e2b, "docker": docker})
    # Hinted e2b is unavailable → fall through to the configured docker.
    assert resolve_backend(_HintTool(), _config(backend="docker")) is docker


def test_unknown_hint_falls_back_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hint naming a backend that does not exist falls back, not crashes."""

    class _BadHintTool(_StubTool):
        sandbox_backend_hint = "no-such-backend"

    docker = _StubBackend("docker", available=True)
    _patch_named_strategy(monkeypatch, {"docker": docker})
    assert resolve_backend(_BadHintTool(), _config(backend="docker")) is docker


def test_hint_used_when_it_is_also_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2b = _StubBackend("e2b", available=True)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    assert resolve_backend(_HintTool(), _config(backend="e2b")) is e2b


# ─── branch 4: the user's configured default backend ──────────────────


def test_configured_default_used_when_no_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _StubBackend("docker", available=True)
    _patch_named_strategy(monkeypatch, {"docker": docker})
    assert resolve_backend(_DefaultTool(), _config(backend="docker")) is docker


def test_configured_default_ordinary_tool_unreachable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary tool whose configured default is unreachable → None.

    A missing optional backend must not break a tool that does not
    *require* a sandbox; it simply runs un-sandboxed.
    """
    e2b = _StubBackend("e2b", available=False)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    assert resolve_backend(_DefaultTool(), _config(backend="e2b")) is None


def test_required_tool_with_available_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2b = _StubBackend("e2b", available=True)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    assert resolve_backend(_RequiredTool(), _config(backend="e2b")) is e2b


# ─── fallback policy (T2.9) ────────────────────────────────────────────


def test_required_tool_unreachable_default_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``required`` tool + unreachable default + ``error`` policy → raise."""
    e2b = _StubBackend("e2b", available=False)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    with pytest.raises(SandboxUnavailable, match="error"):
        resolve_backend(_RequiredTool(), _config(backend="e2b", fallback="error"))


def test_required_tool_unreachable_default_local_runs_host(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``required`` tool + unreachable default + ``local`` policy → None + WARN."""
    e2b = _StubBackend("e2b", available=False)
    _patch_named_strategy(monkeypatch, {"e2b": e2b})
    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.resolver"):
        result = resolve_backend(
            _RequiredTool(), _config(backend="e2b", fallback="local")
        )
    assert result is None
    assert any("running on the HOST" in r.message for r in caplog.records)


def test_required_tool_no_default_local_runs_host(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``required`` tool + no backend configured + ``local`` → None + WARN."""
    _patch_named_strategy(monkeypatch, {})
    with caplog.at_level(logging.WARNING, logger="opencomputer.sandbox.resolver"):
        result = resolve_backend(_RequiredTool(), _config(fallback="local"))
    assert result is None
    assert any("running on the HOST" in r.message for r in caplog.records)


def test_fallback_policy_defaults_to_error() -> None:
    assert fallback_policy(_config()) == SANDBOX_FALLBACK_ERROR
    assert fallback_policy(None) == SANDBOX_FALLBACK_ERROR
    assert fallback_policy(Config()) == SANDBOX_FALLBACK_ERROR


def test_fallback_policy_local_when_configured() -> None:
    assert fallback_policy(_config(fallback="local")) == SANDBOX_FALLBACK_LOCAL


def test_fallback_policy_unknown_value_treated_as_error() -> None:
    """An unrecognised fallback value fails safe to ``error``.

    ``SandboxPolicy.__post_init__`` rejects a bad ``fallback`` at
    construction — but ``fallback_policy`` is also a defensive read
    layer: even a policy object corrupted in memory past validation
    (forced here via ``object.__setattr__`` on the frozen dataclass)
    must still resolve to ``error``, never silently to host fallback.
    """
    cfg = Config(sandbox=SandboxPolicy(backend="e2b"))
    # Force an out-of-set value the way a corrupted in-memory object
    # (one that somehow bypassed __post_init__ validation) would look.
    object.__setattr__(cfg.sandbox, "fallback", "bogus")
    assert fallback_policy(cfg) == SANDBOX_FALLBACK_ERROR


# ─── ctx argument is accepted (reserved, currently unused) ─────────────


def test_resolver_accepts_scope_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reserved ``ctx`` argument is accepted without affecting the result."""
    docker = _StubBackend("docker", available=True)
    _patch_named_strategy(monkeypatch, {"docker": docker})
    ctx = SandboxScopeContext(session_id="s1", agent_id="a1")
    assert resolve_backend(_DefaultTool(), _config(backend="docker"), ctx) is docker


def test_resolver_ctx_defaults_to_none() -> None:
    """``ctx`` is optional — omitting it is the same as ``None``."""
    assert resolve_backend(_DefaultTool(), _config()) is None
