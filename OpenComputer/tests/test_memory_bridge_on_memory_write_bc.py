"""Tier-B 2026-05-10: BC + rich-signal forwarding for ``on_memory_write``.

Pre-Tier-B, ``MemoryBridge._on_memory_write_event`` always called
``provider.on_memory_write(action, target, content_size)`` — the new
``compaction_delta`` and ``dropped_paragraphs`` fields on
``MemoryWriteEvent`` (M2 of PR #588) were silently dropped at the bridge
boundary.

Tier-B uses ``inspect.signature`` to detect what the override accepts and
forwards the matching subset:

* Legacy 3-kwarg overrides (the existing test mocks here exemplify this)
  keep working with no signature change required.
* Overrides with the post-Tier-B kwargs receive the rich signal.
* Overrides with ``**kwargs`` get everything the bridge knows about.
* A one-time INFO log fires per provider lifetime when the override is
  legacy-shaped, so operators see a nudge to upgrade without log spam.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.memory_bridge import (
    _ON_MEMORY_WRITE_KWARGS,
    MemoryBridge,
    _accepted_on_memory_write_kwargs,
    _reset_on_memory_write_cache,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    """Each test starts with empty kwarg + log-once caches."""
    _reset_on_memory_write_cache()
    yield
    _reset_on_memory_write_cache()


def _make_event(**overrides: Any):
    """Construct a MemoryWriteEvent with sensible defaults."""
    from plugin_sdk.ingestion import MemoryWriteEvent

    base: dict[str, Any] = {
        "session_id": None,
        "source": "agent_memory",
        "action": "append",
        "target": "MEMORY.md",
        "content_size": 100,
        "compaction_delta": 0,
        "dropped_paragraphs": 0,
    }
    base.update(overrides)
    return MemoryWriteEvent(**base)


def _make_bridge_with_provider(provider: Any) -> MemoryBridge:
    """Construct a MemoryBridge whose context returns the given provider.

    ``MemoryBridge._provider`` is a read-only property that reads
    ``self._ctx.provider`` — set the underlying ctx attribute and the
    property returns it.
    """
    ctx = MagicMock()
    ctx.provider = provider
    ctx._failure_state = {"consecutive_failures": 0, "disabled": False}
    return MemoryBridge(ctx=ctx)


# ─── kwarg-detection unit tests ─────────────────────────────────────


class TestAcceptedKwargs:
    """The detection function decides what each provider's override gets."""

    def test_legacy_three_kwarg_signature(self) -> None:
        class Legacy:
            provider_id = "legacy"

            async def on_memory_write(self, *, action, target, content_size):
                pass

        accepted = _accepted_on_memory_write_kwargs(Legacy())
        assert accepted == frozenset({"action", "target", "content_size"})

    def test_rich_five_kwarg_signature(self) -> None:
        class Rich:
            provider_id = "rich"

            async def on_memory_write(
                self, *, action, target, content_size,
                compaction_delta=0, dropped_paragraphs=0,
            ):
                pass

        accepted = _accepted_on_memory_write_kwargs(Rich())
        assert accepted == _ON_MEMORY_WRITE_KWARGS

    def test_var_kwargs_signature_gets_everything(self) -> None:
        class Wild:
            provider_id = "wild"

            async def on_memory_write(self, **kwargs):
                pass

        accepted = _accepted_on_memory_write_kwargs(Wild())
        assert accepted == _ON_MEMORY_WRITE_KWARGS

    def test_partial_signature_only_accepts_declared_kwargs(self) -> None:
        # Mid-migration provider only adds compaction_delta, not
        # dropped_paragraphs. Bridge forwards exactly what's declared.
        class Partial:
            provider_id = "partial"

            async def on_memory_write(
                self, *, action, target, content_size, compaction_delta=0,
            ):
                pass

        accepted = _accepted_on_memory_write_kwargs(Partial())
        assert accepted == frozenset(
            {"action", "target", "content_size", "compaction_delta"}
        )

    def test_result_is_cached_per_instance(self) -> None:
        class Counter:
            provider_id = "counter"
            sig_lookups = 0

            async def on_memory_write(self, *, action, target, content_size):
                pass

        p = Counter()
        a = _accepted_on_memory_write_kwargs(p)
        b = _accepted_on_memory_write_kwargs(p)
        assert a is b  # frozenset interned on first call, same object on second

    def test_provider_with_no_handler_returns_empty(self) -> None:
        class NoHandler:
            provider_id = "nohandler"

        assert _accepted_on_memory_write_kwargs(NoHandler()) == frozenset()


# ─── one-time legacy-signature log ──────────────────────────────────


class TestLegacySignatureLogOnce:
    """Operators see exactly one info-level nudge per legacy provider."""

    def test_legacy_provider_logs_once_on_first_lookup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="opencomputer.agent.memory_bridge")

        class Legacy:
            provider_id = "legacy-1"

            async def on_memory_write(self, *, action, target, content_size):
                pass

        p = Legacy()
        _accepted_on_memory_write_kwargs(p)
        _accepted_on_memory_write_kwargs(p)  # second call must NOT re-log
        _accepted_on_memory_write_kwargs(p)

        legacy_lines = [
            r.getMessage() for r in caplog.records
            if "legacy signature" in r.getMessage()
        ]
        assert len(legacy_lines) == 1
        assert "legacy-1" in legacy_lines[0]
        assert "compaction_delta" in legacy_lines[0]
        assert "dropped_paragraphs" in legacy_lines[0]

    def test_rich_provider_does_not_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="opencomputer.agent.memory_bridge")

        class Rich:
            provider_id = "rich-1"

            async def on_memory_write(
                self, *, action, target, content_size,
                compaction_delta=0, dropped_paragraphs=0,
            ):
                pass

        _accepted_on_memory_write_kwargs(Rich())
        legacy_lines = [
            r for r in caplog.records if "legacy signature" in r.getMessage()
        ]
        assert legacy_lines == []


# ─── end-to-end forwarding through MemoryBridge ─────────────────────


class TestEndToEndForwarding:
    """The bridge actually projects + forwards the right kwarg subset
    when the bus publishes a MemoryWriteEvent."""

    def test_legacy_provider_receives_only_three_kwargs(self) -> None:
        captured: dict[str, Any] = {}

        class Legacy:
            provider_id = "legacy-e2e"

            async def on_memory_write(self, *, action, target, content_size):
                captured["action"] = action
                captured["target"] = target
                captured["content_size"] = content_size

        bridge = _make_bridge_with_provider(Legacy())
        ev = _make_event(content_size=3480, compaction_delta=520, dropped_paragraphs=2)
        # The bridge calls ``asyncio.run(coro)`` when no loop is running
        # (this sync test is one such caller), so the call blocks until
        # the provider's coroutine completes — no manual pump needed.
        bridge._on_memory_write_event(ev)

        assert captured == {
            "action": "append",
            "target": "MEMORY.md",
            "content_size": 3480,
        }

    def test_rich_provider_receives_full_kwarg_set(self) -> None:
        captured: dict[str, Any] = {}

        class Rich:
            provider_id = "rich-e2e"

            async def on_memory_write(
                self, *, action, target, content_size,
                compaction_delta=0, dropped_paragraphs=0,
            ):
                captured.update({
                    "action": action,
                    "target": target,
                    "content_size": content_size,
                    "compaction_delta": compaction_delta,
                    "dropped_paragraphs": dropped_paragraphs,
                })

        bridge = _make_bridge_with_provider(Rich())
        ev = _make_event(content_size=3480, compaction_delta=520, dropped_paragraphs=2)
        bridge._on_memory_write_event(ev)

        assert captured["compaction_delta"] == 520
        assert captured["dropped_paragraphs"] == 2

    def test_var_kwargs_provider_receives_everything(self) -> None:
        captured: dict[str, Any] = {}

        class Wild:
            provider_id = "wild-e2e"

            async def on_memory_write(self, **kwargs):
                captured.update(kwargs)

        bridge = _make_bridge_with_provider(Wild())
        ev = _make_event(content_size=999, compaction_delta=42, dropped_paragraphs=1)
        bridge._on_memory_write_event(ev)

        assert set(captured.keys()) == _ON_MEMORY_WRITE_KWARGS
        assert captured["dropped_paragraphs"] == 1

    def test_provider_raise_does_not_propagate(self) -> None:
        # Existing exception-isolation contract — bridge catches everything.
        class Bomb:
            provider_id = "bomb"

            async def on_memory_write(self, *, action, target, content_size):
                raise RuntimeError("boom")

        bridge = _make_bridge_with_provider(Bomb())
        # Bridge must swallow the exception (sync part); coroutine fault is
        # raised when we await it. The handler's responsibility is the sync
        # call — it's already wrapped in try/except.
        bridge._on_memory_write_event(_make_event())
        # Nothing to assert — just confirming no exception escapes the
        # synchronous handler invocation. If it did, the test would error.
