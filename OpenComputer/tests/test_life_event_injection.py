"""Unit tests for LifeEventInjectionProvider (life-event teeth, Tasks 3 & 5).

The provider is a :class:`DynamicInjectionProvider` that drains the
life-event registry's pending ``"hint"`` firings each turn and, for each
non-muted firing, injects a ``<life-event-hint>`` block carrying the
firing's ``hint_text`` plus a per-event tone directive. Task 5 extends
``collect()`` so that surfacing a firing also schedules its follow-up cron
via :func:`actions.schedule_followup`.

Pinned behavior:
- a queued ``"hint"`` firing -> block contains the hint AND the directive
- a muted pattern's firing -> ``collect()`` returns ``None``
- an empty queue -> ``None``
- a firing whose ``pattern_id`` has no tone directive -> the hint still
  appears with no directive line
- surfacing a firing schedules its follow-up cron (state gets ``cron_id``
  + ``verdict_pending``); a cron failure is fail-open — the block still
  returns.
"""
from __future__ import annotations

import time

import pytest

from opencomputer.awareness.life_events import actions, state
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


@pytest.fixture(autouse=True)
def _isolate_cron_and_profile(tmp_path, monkeypatch):
    """Make every test in this file cron-free and profile-isolated.

    Task 5 wires ``collect()`` to :func:`actions.schedule_followup`, which
    creates a REAL cron (``create_job``) and writes ``life_event_state.json``
    to the active profile. This autouse fixture neutralises both for the
    whole file:

    - ``OPENCOMPUTER_HOME`` -> ``tmp_path`` so state writes land in a temp
      dir, never the real user profile (mirrors ``test_life_event_state``).
    - ``actions.create_job`` / ``actions.remove_job`` are stubbed so no real
      cron job is ever written to ``cron.db`` (mirrors the monkeypatch
      targets in ``test_life_event_actions`` — ``actions.py`` imports both
      names directly, so patching ``actions.<name>`` is the right seam).

    Each stubbed ``create_job`` returns a unique id so a per-pattern
    ``cron_id`` is recorded by ``schedule_followup`` -> ``mark_surfaced``.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    created: list[dict] = []

    def fake_create_job(**kwargs):
        created.append(kwargs)
        return {"id": f"cron-{len(created)}", "name": kwargs.get("name")}

    monkeypatch.setattr(actions, "create_job", fake_create_job)
    monkeypatch.setattr(actions, "remove_job", lambda _job_id: True)
    return created


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


# ── Task 5: surfacing a firing schedules its follow-up cron ──────────


async def test_collect_schedules_followup_cron_for_surfaced_firing() -> None:
    """After collect() surfaces a firing, life_event_state has a cron_id for
    that pattern and verdict_pending is True (schedule_followup ran)."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None  # the hint block still surfaces
        entry = state.load_state().get("burnout")
        assert entry is not None, "schedule_followup must have recorded state"
        assert entry["cron_id"], "a follow-up cron_id must be recorded"
        assert entry["verdict_pending"] is True
    finally:
        reset_global_registry_for_test()


async def test_collect_schedules_a_cron_per_surfaced_firing() -> None:
    """Each non-muted surfaced firing gets its own follow-up cron."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "burnout hint"))
        reg._queue.append(_firing("travel", "travel hint"))

        await LifeEventInjectionProvider().collect(_ctx())

        loaded = state.load_state()
        assert loaded.get("burnout", {}).get("cron_id")
        assert loaded.get("travel", {}).get("cron_id")
    finally:
        reset_global_registry_for_test()


async def test_collect_does_not_schedule_cron_for_muted_firing() -> None:
    """A muted firing is dropped before cron scheduling — no state entry."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "burnout hint"))
        reg.mute("burnout")

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is None
        assert "burnout" not in state.load_state()
    finally:
        reset_global_registry_for_test()


async def test_collect_is_fail_open_when_cron_scheduling_raises(
    monkeypatch, caplog
) -> None:
    """A cron failure must NOT break prompt assembly: collect() still returns
    the <life-event-hint> block, and a WARNING is logged."""
    import logging

    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        def boom(*_args, **_kwargs):
            raise RuntimeError("cron backend down")

        # Patch schedule_followup as the injection module sees it.
        from opencomputer.awareness.life_events import injection as inj_mod

        monkeypatch.setattr(inj_mod.actions, "schedule_followup", boom)

        with caplog.at_level(logging.WARNING):
            out = await LifeEventInjectionProvider().collect(_ctx())

        # The block is still returned despite the cron failure (fail-open).
        assert out is not None
        assert out.startswith("<life-event-hint>")
        assert "your work rhythm shifted" in out
        # A WARNING was logged for the failed scheduling.
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a WARNING must be logged when cron scheduling fails"
        )
    finally:
        reset_global_registry_for_test()
