"""Unit tests for LifeEventInjectionProvider (life-event teeth, Task 3).

The provider is a :class:`DynamicInjectionProvider` that drains the
life-event registry's pending ``"hint"`` firings each turn and, for each
non-muted firing, injects a ``<life-event-hint>`` block carrying the
firing's ``hint_text`` plus a per-event tone directive.

Pinned behavior:
- a queued ``"hint"`` firing -> block contains the hint AND the directive
- a muted pattern's firing -> ``collect()`` returns ``None``
- an empty queue -> ``None``
- a firing whose ``pattern_id`` has no tone directive -> the hint still
  appears with no directive line
"""
from __future__ import annotations

import time

from opencomputer.awareness.life_events.injection import (
    LifeEventInjectionProvider,
)
from opencomputer.awareness.life_events.pattern import PatternFiring
from opencomputer.awareness.life_events.registry import (
    get_global_registry,
    reset_global_registry_for_test,
)
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from plugin_sdk.runtime_context import RuntimeContext


def _ctx() -> InjectionContext:
    """Minimal real InjectionContext — collect() ignores it, but the ABC
    requires the argument."""
    return InjectionContext(
        messages=(),
        runtime=RuntimeContext(),
        session_id="test",
        turn_index=1,
    )


def _firing(pattern_id: str, hint_text: str = "noticed something") -> PatternFiring:
    return PatternFiring(
        pattern_id=pattern_id,
        confidence=0.85,
        evidence_count=4,
        surfacing="hint",
        hint_text=hint_text,
        timestamp=time.time(),
    )


# ── provider identity ────────────────────────────────────────────────


def test_is_a_dynamic_injection_provider() -> None:
    assert isinstance(LifeEventInjectionProvider(), DynamicInjectionProvider)


def test_provider_id_is_life_event_hint() -> None:
    assert LifeEventInjectionProvider().provider_id == "life_event_hint"


def test_priority_is_60() -> None:
    assert LifeEventInjectionProvider().priority == 60


# ── empty queue ──────────────────────────────────────────────────────


async def test_empty_queue_returns_none() -> None:
    reset_global_registry_for_test()
    try:
        out = await LifeEventInjectionProvider().collect(_ctx())
        assert out is None
    finally:
        reset_global_registry_for_test()


# ── queued hint firing → block with hint + tone directive ────────────


async def test_queued_hint_firing_emits_block_with_hint_and_directive() -> None:
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        assert out.startswith("<life-event-hint>")
        assert out.endswith("</life-event-hint>")
        # Hint text is present.
        assert "your work rhythm shifted" in out
        # The matching per-event tone directive is present.
        assert "Respond gently and concisely; do not pile on tasks." in out
    finally:
        reset_global_registry_for_test()


async def test_exam_prep_firing_carries_its_directive() -> None:
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("exam_prep", "exams seem close"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        assert "exams seem close" in out
        assert (
            "Keep replies focused and low-friction; the user is time-pressured."
            in out
        )
    finally:
        reset_global_registry_for_test()


async def test_collect_drains_the_queue() -> None:
    """After collect() the firing must be drained — a second collect on the
    same turn-less registry yields None."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("travel", "you appear to be travelling"))

        provider = LifeEventInjectionProvider()
        first = await provider.collect(_ctx())
        second = await provider.collect(_ctx())

        assert first is not None
        assert second is None
    finally:
        reset_global_registry_for_test()


# ── muted pattern → None ─────────────────────────────────────────────


async def test_muted_pattern_firing_returns_none() -> None:
    """A firing queued before the user muted that pattern (mute-between-turns
    race) must be filtered out by collect()."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        # Queue the firing FIRST, then mute — simulates the race the
        # defensive is_muted() filter exists for.
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))
        reg.mute("burnout")

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is None
    finally:
        reset_global_registry_for_test()


async def test_muted_firing_dropped_unmuted_firing_kept() -> None:
    """With one muted + one non-muted firing queued, only the non-muted
    firing's hint surfaces."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "burnout hint text"))
        reg._queue.append(_firing("job_change", "job change hint text"))
        reg.mute("burnout")

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        assert "job change hint text" in out
        assert "burnout hint text" not in out
    finally:
        reset_global_registry_for_test()


# ── firing with no tone directive ────────────────────────────────────


async def test_firing_without_tone_directive_still_emits_hint() -> None:
    """A firing whose pattern_id has no entry in the tone table still gets
    its hint surfaced — just with no directive line."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("unmapped_pattern", "an unmapped hint"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        assert "an unmapped hint" in out
        # Only the hint line sits between the tags — no directive.
        body = out.removeprefix("<life-event-hint>\n").removesuffix(
            "\n</life-event-hint>"
        )
        assert body == "an unmapped hint"
    finally:
        reset_global_registry_for_test()


# ── multiple firings batched into one block ──────────────────────────


async def test_multiple_firings_batched_into_one_block() -> None:
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "burnout hint"))
        reg._queue.append(_firing("job_change", "job change hint"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        # Exactly one wrapping block.
        assert out.count("<life-event-hint>") == 1
        assert out.count("</life-event-hint>") == 1
        # Both hints + both directives present.
        assert "burnout hint" in out
        assert "Respond gently and concisely; do not pile on tasks." in out
        assert "job change hint" in out
        assert "Be encouraging and practical about the transition." in out
    finally:
        reset_global_registry_for_test()
