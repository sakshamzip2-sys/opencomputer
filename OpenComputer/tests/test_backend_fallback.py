"""Tests for the sandbox backend fallback policy — Milestone 2 (T2.9).

``sandbox.fallback`` (a field on the M1
:class:`~opencomputer.sandbox.policy.SandboxPolicy` — the ``sandbox:``
config block) governs what happens when the chosen sandbox backend is
unreachable:

* ``"error"`` (default) — fail loud. OC never silently downgrades
  containment.
* ``"local"`` — run on the host, with a WARNING logged to the audit
  path.

The resolver (:func:`opencomputer.sandbox.resolver.resolve_backend`)
applies this policy when a ``sandbox_preference='required'`` tool's
backend cannot be resolved. These tests pin both halves: ``error``
raises :class:`~plugin_sdk.SandboxUnavailable`, ``local`` returns
``None`` (run on host) and emits the WARNING.
"""

from __future__ import annotations

import logging

import pytest

from opencomputer.agent.config import Config
from opencomputer.sandbox import resolver as resolver_mod
from opencomputer.sandbox.policy import SandboxPolicy
from opencomputer.sandbox.resolver import (
    SANDBOX_FALLBACK_ERROR,
    SANDBOX_FALLBACK_LOCAL,
    fallback_policy,
    resolve_backend,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.sandbox import SandboxStrategy, SandboxUnavailable
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_RESOLVER_LOGGER = "opencomputer.sandbox.resolver"

# ─── stubs ─────────────────────────────────────────────────────────────


class _RequiredTool(BaseTool):
    """A tool that MUST run sandboxed — drives the fallback policy."""

    sandbox_preference = "required"

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="Required", description="required", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
        return ToolResult(tool_call_id=call.id, content="ok")


class _OrdinaryTool(BaseTool):
    """A tool with the default preference — only *prefers* a sandbox."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="Ordinary", description="ordinary", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
        return ToolResult(tool_call_id=call.id, content="ok")


class _UnreachableBackend(SandboxStrategy):
    """A configured backend that reports itself unavailable on this host."""

    name = "e2b"

    def is_available(self) -> bool:
        return False

    async def run(self, argv, *, config, stdin=None, cwd=None):  # pragma: no cover
        raise NotImplementedError

    def explain(self, argv, *, config):  # pragma: no cover
        return list(argv)


def _patch_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_named_strategy`` return a backend that is never available."""

    def _fake(name: str) -> SandboxStrategy:
        if name == "e2b":
            # A known backend whose is_available() is False — the real
            # _named_strategy raises SandboxUnavailable for that case.
            raise SandboxUnavailable(f"sandbox strategy {name!r} not available")
        raise SandboxUnavailable(f"unknown sandbox strategy {name!r}")

    monkeypatch.setattr(resolver_mod, "_named_strategy", _fake)


def _cfg(backend: str | None, fallback: str) -> Config:
    return Config(sandbox=SandboxPolicy(backend=backend, fallback=fallback))


# ─── fallback default is "error" ───────────────────────────────────────


def test_fallback_defaults_to_error() -> None:
    """A fresh ``SandboxPolicy`` fails loud, never downgrades."""
    assert SandboxPolicy().fallback == "error"
    assert fallback_policy(Config()) == SANDBOX_FALLBACK_ERROR


# ─── error policy: raise loud ──────────────────────────────────────────


def test_error_policy_raises_when_backend_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``required`` tool + unreachable backend + ``error`` → SandboxUnavailable."""
    _patch_unreachable(monkeypatch)
    with pytest.raises(SandboxUnavailable) as exc_info:
        resolve_backend(_RequiredTool(), _cfg("e2b", "error"))
    # The message names the failure loudly so the operator can fix it.
    msg = str(exc_info.value)
    assert "required" in msg
    assert "fallback='error'" in msg


def test_error_policy_raises_when_no_backend_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``required`` tool + nothing configured + ``error`` → raise."""
    _patch_unreachable(monkeypatch)
    with pytest.raises(SandboxUnavailable, match="required"):
        resolve_backend(_RequiredTool(), _cfg(None, "error"))


def test_error_policy_does_not_emit_a_downgrade_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``error`` path raises — it must NOT log a 'running on HOST' line."""
    _patch_unreachable(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        with pytest.raises(SandboxUnavailable):
            resolve_backend(_RequiredTool(), _cfg("e2b", "error"))
    assert not any("running on the HOST" in r.message for r in caplog.records)


# ─── local policy: run on host with a WARNING ──────────────────────────


def test_local_policy_runs_on_host_when_backend_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``required`` tool + unreachable backend + ``local`` → None (host)."""
    _patch_unreachable(monkeypatch)
    result = resolve_backend(_RequiredTool(), _cfg("e2b", "local"))
    assert result is None


def test_local_policy_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``local`` downgrade is never silent — it logs a WARNING."""
    _patch_unreachable(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        resolve_backend(_RequiredTool(), _cfg("e2b", "local"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("running on the HOST" in r.message for r in warnings)
    # The warning names the unreachable backend so the operator knows
    # which one fell back.
    assert any("'e2b'" in r.message for r in warnings)


def test_local_policy_warns_when_no_backend_configured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``required`` tool + nothing configured + ``local`` → None + WARN."""
    _patch_unreachable(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_backend(_RequiredTool(), _cfg(None, "local"))
    assert result is None
    assert any("running on the HOST" in r.message for r in caplog.records)


# ─── the fallback policy only bites a "required" tool ──────────────────


def test_ordinary_tool_unreachable_backend_returns_none_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary tool never triggers the fallback policy — it just runs.

    The fallback policy (raise vs host) governs ``required`` tools. An
    ordinary tool whose configured backend is unreachable simply runs
    un-sandboxed regardless of the ``error`` / ``local`` setting.
    """
    _patch_unreachable(monkeypatch)
    # error policy — still no raise for an ordinary tool.
    assert resolve_backend(_OrdinaryTool(), _cfg("e2b", "error")) is None
    # local policy — same.
    assert resolve_backend(_OrdinaryTool(), _cfg("e2b", "local")) is None


def test_ordinary_tool_unreachable_backend_error_policy_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ordinary tool's silent un-sandbox is NOT a 'downgrade' warning.

    The 'running on the HOST' warning is reserved for a ``required``
    tool that was forced onto the host; an ordinary tool was never
    promised a sandbox, so no warning fires.
    """
    _patch_unreachable(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        resolve_backend(_OrdinaryTool(), _cfg("e2b", "error"))
    assert not any("running on the HOST" in r.message for r in caplog.records)


# ─── fallback_policy() helper round-trip ───────────────────────────────


def test_fallback_policy_reads_local() -> None:
    assert fallback_policy(_cfg("e2b", "local")) == SANDBOX_FALLBACK_LOCAL


def test_fallback_policy_reads_error() -> None:
    assert fallback_policy(_cfg("e2b", "error")) == SANDBOX_FALLBACK_ERROR


def test_fallback_policy_none_config_is_error() -> None:
    """A missing config fails safe to ``error`` — never to host fallback."""
    assert fallback_policy(None) == SANDBOX_FALLBACK_ERROR
