"""Tests for :class:`opencomputer.inference.engine.BehavioralInferenceEngine`."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from opencomputer.inference.engine import BehavioralInferenceEngine
from opencomputer.inference.storage import MotifStore
from plugin_sdk.inference import Motif, MotifKind
from plugin_sdk.ingestion import SignalEvent, ToolCallEvent


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Bus swap+restore — preserves cross-file singleton invariant."""
    from opencomputer.ingestion import bus as bus_module
    from opencomputer.ingestion.bus import reset_default_bus

    saved = bus_module.default_bus
    reset_default_bus()
    yield
    bus_module.default_bus = saved


# Test fakes ──────────────────────────────────────────────────────────


class _StubExtractor:
    """Returns one fixed motif per non-empty batch. Tracks call count."""

    name: ClassVar[str] = "stub"
    kind: ClassVar[MotifKind] = "temporal"

    def __init__(self) -> None:
        self.calls: int = 0
        self.last_batch_len: int = 0

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        self.calls += 1
        self.last_batch_len = len(events)
        if not events:
            return []
        return [
            Motif(
                kind="temporal",
                confidence=0.5,
                support=len(events),
                summary=f"stub-batch-{self.calls}",
                payload={"batch_len": len(events)},
            )
        ]


class _BrokenExtractor:
    """Always raises — used to exercise per-extractor isolation."""

    name: ClassVar[str] = "broken"
    kind: ClassVar[MotifKind] = "temporal"

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        raise RuntimeError("boom")


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return _dt.datetime(
        year, month, day, hour, minute, 0, tzinfo=_dt.UTC
    ).timestamp()


# Tests ───────────────────────────────────────────────────────────────


def test_engine_attaches_and_buffers_events(tmp_path: Path) -> None:
    """attach_to_bus subscribes; published events end up in the buffer."""
    from opencomputer.ingestion.bus import default_bus

    store = MotifStore(db_path=tmp_path / "m.sqlite")
    stub = _StubExtractor()
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[stub],
        batch_size=1000,
        batch_seconds=1e9,
    )
    engine.attach_to_bus()
    try:
        for i in range(5):
            default_bus.publish(
                ToolCallEvent(
                    tool_name="Read", timestamp=_ts(2026, 1, 5, 9) + i
                )
            )
        assert engine.buffer_size == 5
        assert engine.attached is True
        # No flush yet — batch_size is large.
        assert stub.calls == 0
    finally:
        engine.detach()


def test_engine_flushes_on_batch_size(tmp_path: Path) -> None:
    """Reaching batch_size triggers an automatic flush."""
    from opencomputer.ingestion.bus import default_bus

    store = MotifStore(db_path=tmp_path / "m.sqlite")
    stub = _StubExtractor()
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[stub],
        batch_size=3,
        batch_seconds=1e9,
    )
    engine.attach_to_bus()
    try:
        for i in range(3):
            default_bus.publish(
                ToolCallEvent(
                    tool_name="Read", timestamp=_ts(2026, 1, 5, 9) + i
                )
            )
        assert stub.calls == 1
        assert stub.last_batch_len == 3
        assert engine.buffer_size == 0
        assert store.count() == 1
    finally:
        engine.detach()


def test_engine_flushes_on_time_window(tmp_path: Path) -> None:
    """Elapsed batch_seconds triggers an automatic flush.

    We avoid actually sleeping by manipulating the engine's last_flush
    timestamp directly.
    """
    from opencomputer.ingestion.bus import default_bus

    store = MotifStore(db_path=tmp_path / "m.sqlite")
    stub = _StubExtractor()
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[stub],
        batch_size=1000,
        batch_seconds=0.01,
    )
    engine.attach_to_bus()
    try:
        # Force the time threshold by backdating the last flush.
        import time as _time

        engine._last_flush_at = _time.monotonic() - 1.0  # noqa: SLF001
        default_bus.publish(
            ToolCallEvent(tool_name="Read", timestamp=_ts(2026, 1, 5, 9))
        )
        assert stub.calls == 1
    finally:
        engine.detach()


def test_engine_extractors_isolated(tmp_path: Path) -> None:
    """A broken extractor logs but does not stop the others."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    stub = _StubExtractor()
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[_BrokenExtractor(), stub],
        batch_size=1,
        batch_seconds=1e9,
    )
    # Skip the bus — flush directly.
    engine._buffer.append(  # noqa: SLF001 — test access
        ToolCallEvent(tool_name="Read", timestamp=_ts(2026, 1, 5, 9))
    )
    n = engine.flush_now()
    # stub returned 1 motif; broken extractor returned 0; total = 1.
    assert n == 1
    assert stub.calls == 1
    assert store.count() == 1


def test_engine_detach_stops_buffering(tmp_path: Path) -> None:
    """After detach(), new publishes do NOT enter the buffer."""
    from opencomputer.ingestion.bus import default_bus

    store = MotifStore(db_path=tmp_path / "m.sqlite")
    stub = _StubExtractor()
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[stub],
        batch_size=1000,
        batch_seconds=1e9,
    )
    engine.attach_to_bus()
    default_bus.publish(
        ToolCallEvent(tool_name="Read", timestamp=_ts(2026, 1, 5, 9))
    )
    assert engine.buffer_size == 1
    engine.detach()
    assert engine.attached is False
    default_bus.publish(
        ToolCallEvent(tool_name="Read", timestamp=_ts(2026, 1, 5, 9, 1))
    )
    # Still 1 — detach stopped buffering.
    assert engine.buffer_size == 1


def test_engine_flush_now_with_empty_buffer_returns_zero(tmp_path: Path) -> None:
    """flush_now on an empty buffer is a fast-path no-op."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    engine = BehavioralInferenceEngine(
        store=store,
        extractors=[_StubExtractor()],
    )
    assert engine.flush_now() == 0


def test_engine_uses_default_extractors_when_none_provided(tmp_path: Path) -> None:
    """Constructing without extractors gives us the three production ones."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    engine = BehavioralInferenceEngine(store=store)
    names = {
        getattr(e, "name", type(e).__name__)
        for e in engine._extractors  # noqa: SLF001 — test introspection
    }
    assert names == {"temporal", "transition", "implicit_goal"}
