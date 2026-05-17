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


# ── dedup: a pattern that re-fires within one turn → ONE hint line ────


async def test_repeated_firings_of_same_pattern_dedup_to_one_hint() -> None:
    """``LifeEventPattern.accumulate`` has no post-fire reset — once a
    pattern crosses threshold it returns a firing on EVERY matching event,
    so the queue can hold multiple firings of the SAME pattern_id within one
    turn. ``collect()`` must dedup by pattern_id so the hint_text + tone
    directive appear exactly ONCE, not N times.
    """
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        # Two firings of the SAME pattern, as a re-firing pattern produces.
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider().collect(_ctx())

        assert out is not None
        # The burnout hint_text appears exactly once despite two firings.
        assert out.count("your work rhythm shifted") == 1
        # The tone directive likewise appears exactly once.
        assert out.count("Respond gently and concisely; do not pile on tasks.") == 1
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


# ── Task 4: surface-aware provider ───────────────────────────────────
#
# The provider gains a ``surface`` constructor argument. The CLI surface
# (where life-event teeth shipped first, unflagged) is always active;
# every other surface (gateway / wire / webui) is opt-in via the
# ``life_events.multi_surface_life_events`` feature flag. ``collect()``
# drains the registry queue BEFORE the surface gate so a flag-off
# long-running daemon still keeps the process-global queue bounded.


def _write_multi_surface_flag(home, *, enabled: bool) -> None:
    """Write ``feature_flags.json`` into ``home`` with the multi-surface
    life-event flag set to ``enabled``.

    The autouse fixture already points ``OPENCOMPUTER_HOME`` at ``tmp_path``;
    this drops a ``feature_flags.json`` sibling that
    :func:`FeatureFlags.read` picks up. Flag default is ``False``, so a
    flag-off test could simply skip this — writing it explicitly keeps the
    on/off pair symmetric and self-documenting.
    """
    import json
    from pathlib import Path

    path = Path(home) / "feature_flags.json"
    path.write_text(
        json.dumps({"life_events": {"multi_surface_life_events": enabled}})
    )


class _FakeRequestContext:
    """Minimal stand-in carrying just the fields ``_resolve_origin`` reads.

    The real :class:`plugin_sdk.runtime_context.RequestContext` is a frozen
    slotted dataclass; ``_resolve_origin`` only touches ``.channel`` and
    ``.user_id``, so a tiny attribute holder is enough and keeps the test
    free of the full constructor's required ``request_id``.
    """

    def __init__(self, channel, user_id) -> None:
        self.channel = channel
        self.user_id = user_id


def _activate_request_context(monkeypatch, ctx) -> None:
    """Make ``ctx`` the active RequestContext as ``_resolve_origin`` sees it.

    ``_resolve_origin`` reaches the active request through the process-wide
    plugin registry singleton: ``registry.shared_api.request_context``. This
    helper installs a real ``PluginAPI`` on ``registry.shared_api`` (built
    via ``PluginRegistry.api()``) and parks ``ctx`` on its private
    ``_request_context`` — the same slot ``PluginAPI.in_request`` writes —
    so the public ``request_context`` property returns it. ``monkeypatch``
    restores ``shared_api`` after the test.
    """
    from opencomputer.plugins.registry import registry as _plugin_registry

    api = _plugin_registry.api()
    api._request_context = ctx
    monkeypatch.setattr(_plugin_registry, "shared_api", api)


# ── 4a: surface constructor argument ─────────────────────────────────


def test_default_surface_is_cli() -> None:
    assert LifeEventInjectionProvider()._surface == "cli"


def test_surface_argument_is_stored() -> None:
    assert LifeEventInjectionProvider(surface="gateway")._surface == "gateway"


# ── 4d: CLI surface is always on (no regression vs #630) ─────────────


async def test_cli_surface_surfaces_hint_without_flag() -> None:
    """The CLI surface needs no flag — life-event teeth shipped CLI-only
    and unflagged. A queued firing surfaces its <life-event-hint> block."""
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider(surface="cli").collect(_ctx())

        assert out is not None
        assert out.startswith("<life-event-hint>")
        assert "your work rhythm shifted" in out
    finally:
        reset_global_registry_for_test()


# ── 4d: gateway surface gated by the flag ────────────────────────────


async def test_gateway_surface_flag_off_returns_none(tmp_path) -> None:
    """A non-CLI surface with the flag OFF surfaces nothing."""
    _write_multi_surface_flag(tmp_path, enabled=False)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider(surface="gateway").collect(_ctx())

        assert out is None
    finally:
        reset_global_registry_for_test()


async def test_gateway_surface_flag_off_still_drains_the_queue(tmp_path) -> None:
    """drain-then-gate: a gated-off surface STILL drains the registry queue.

    Draining before the gate keeps the process-global queue bounded on a
    long-running flag-off gateway daemon. After a gated collect() the queue
    must be empty — the firing was consumed, just not surfaced."""
    _write_multi_surface_flag(tmp_path, enabled=False)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider(surface="gateway").collect(_ctx())

        assert out is None
        # The firing was drained despite the gate — queue is now empty.
        assert reg._queue == []
        assert reg.drain_pending() == []
    finally:
        reset_global_registry_for_test()


async def test_gateway_surface_flag_on_surfaces_hint(tmp_path) -> None:
    """A non-CLI surface with the flag ON surfaces the hint block."""
    _write_multi_surface_flag(tmp_path, enabled=True)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        out = await LifeEventInjectionProvider(surface="gateway").collect(_ctx())

        assert out is not None
        assert out.startswith("<life-event-hint>")
        assert "your work rhythm shifted" in out
    finally:
        reset_global_registry_for_test()


# ── 4d: wire / webui surfaces gated identically ──────────────────────


async def test_wire_surface_flag_off_returns_none(tmp_path) -> None:
    _write_multi_surface_flag(tmp_path, enabled=False)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("travel", "you appear to be travelling"))

        out = await LifeEventInjectionProvider(surface="wire").collect(_ctx())

        assert out is None
    finally:
        reset_global_registry_for_test()


async def test_wire_surface_flag_on_surfaces_hint(tmp_path) -> None:
    _write_multi_surface_flag(tmp_path, enabled=True)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("travel", "you appear to be travelling"))

        out = await LifeEventInjectionProvider(surface="wire").collect(_ctx())

        assert out is not None
        assert "you appear to be travelling" in out
    finally:
        reset_global_registry_for_test()


async def test_webui_surface_flag_off_returns_none(tmp_path) -> None:
    _write_multi_surface_flag(tmp_path, enabled=False)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("job_change", "something shifted at work"))

        out = await LifeEventInjectionProvider(surface="webui").collect(_ctx())

        assert out is None
    finally:
        reset_global_registry_for_test()


async def test_webui_surface_flag_on_surfaces_hint(tmp_path) -> None:
    _write_multi_surface_flag(tmp_path, enabled=True)
    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("job_change", "something shifted at work"))

        out = await LifeEventInjectionProvider(surface="webui").collect(_ctx())

        assert out is not None
        assert "something shifted at work" in out
    finally:
        reset_global_registry_for_test()


# ── 4c: _resolve_origin reads the active RequestContext ──────────────


def test_resolve_origin_none_when_no_request_context() -> None:
    """CLI / wire / webui do not enter a request scope — _resolve_origin
    returns None when no RequestContext is active."""
    from opencomputer.awareness.life_events.injection import _resolve_origin

    assert _resolve_origin() is None


def test_resolve_origin_returns_channel_coords_when_active(monkeypatch) -> None:
    """With a RequestContext carrying channel + user_id active on the
    gateway path, _resolve_origin returns the platform/chat_id mapping."""
    from opencomputer.awareness.life_events.injection import _resolve_origin

    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel="telegram", user_id="chat-42")
    )

    assert _resolve_origin() == {"platform": "telegram", "chat_id": "chat-42"}


def test_resolve_origin_none_when_channel_missing(monkeypatch) -> None:
    """A RequestContext with user_id but no channel is not a usable origin."""
    from opencomputer.awareness.life_events.injection import _resolve_origin

    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel=None, user_id="chat-42")
    )

    assert _resolve_origin() is None


def test_resolve_origin_none_when_user_id_missing(monkeypatch) -> None:
    """A RequestContext with channel but no user_id is not a usable origin."""
    from opencomputer.awareness.life_events.injection import _resolve_origin

    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel="discord", user_id=None)
    )

    assert _resolve_origin() is None


# ── 4d: collect() threads the resolved origin into schedule_followup ──


async def test_collect_passes_resolved_origin_to_schedule_followup(
    monkeypatch, tmp_path
) -> None:
    """When a RequestContext is active, collect() builds an origin from it
    and passes it as the ``origin`` kwarg of schedule_followup."""
    _write_multi_surface_flag(tmp_path, enabled=True)
    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel="telegram", user_id="chat-7")
    )

    captured: dict = {}

    def fake_schedule_followup(_firing_arg, *, origin=None, surfaced_turn=0):
        captured["origin"] = origin
        captured["surfaced_turn"] = surfaced_turn

    from opencomputer.awareness.life_events import injection as inj_mod

    monkeypatch.setattr(inj_mod.actions, "schedule_followup", fake_schedule_followup)

    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        await LifeEventInjectionProvider(surface="gateway").collect(_ctx())

        assert captured["origin"] == {"platform": "telegram", "chat_id": "chat-7"}
    finally:
        reset_global_registry_for_test()


async def test_collect_passes_none_origin_when_no_request_context(
    monkeypatch,
) -> None:
    """On the CLI surface (no request scope) collect() passes origin=None to
    schedule_followup — the check-in cron is still created, just untargeted."""
    captured: dict = {}

    def fake_schedule_followup(_firing_arg, *, origin=None, surfaced_turn=0):
        captured["origin"] = origin

    from opencomputer.awareness.life_events import injection as inj_mod

    monkeypatch.setattr(inj_mod.actions, "schedule_followup", fake_schedule_followup)

    reset_global_registry_for_test()
    try:
        reg = get_global_registry()
        reg._queue.append(_firing("burnout", "your work rhythm shifted"))

        await LifeEventInjectionProvider(surface="cli").collect(_ctx())

        assert captured["origin"] is None
    finally:
        reset_global_registry_for_test()


# ── 4e: register_life_event_injection_provider helper ────────────────


def test_register_helper_registers_provider_with_surface() -> None:
    """register_life_event_injection_provider(surface=...) installs a
    provider carrying that surface on the injection engine."""
    from opencomputer.agent.injection import engine
    from opencomputer.awareness.life_events.injection import (
        register_life_event_injection_provider,
    )

    try:
        register_life_event_injection_provider(surface="gateway")

        provider = engine._providers.get("life_event_hint")
        assert isinstance(provider, LifeEventInjectionProvider)
        assert provider._surface == "gateway"
    finally:
        engine.unregister("life_event_hint")


def test_register_helper_is_idempotent() -> None:
    """Calling the helper twice does not double-register — unregister runs
    first, so the engine holds exactly one life_event_hint provider."""
    from opencomputer.agent.injection import engine
    from opencomputer.awareness.life_events.injection import (
        register_life_event_injection_provider,
    )

    try:
        register_life_event_injection_provider(surface="cli")
        register_life_event_injection_provider(surface="wire")

        provider = engine._providers.get("life_event_hint")
        assert isinstance(provider, LifeEventInjectionProvider)
        # The second call won — surface is the latest registration's.
        assert provider._surface == "wire"
    finally:
        engine.unregister("life_event_hint")


# ── 4f: each surface's setup path registers the provider ─────────────
#
# Multi-surface life-event teeth: CLI, wire, gateway and webui each call
# ``register_life_event_injection_provider`` during their boot, carrying
# their own surface label. These tests drive the *real* surface setup
# function far enough to hit that registration, then assert the live
# injection engine holds a ``life_event_hint`` provider with the right
# ``_surface``. The registration genuinely runs — nothing here asserts
# source text.


class _StopAfterRegistrationError(Exception):
    """Sentinel raised by a mock placed on the call *immediately after*
    a surface's life-event registration, so the surface's setup unwinds
    the moment registration has run (avoids booting a forever-loop)."""


@pytest.fixture
def _clean_life_event_provider():
    """Ensure the injection engine has no stale ``life_event_hint``
    provider before the test and is left clean afterwards."""
    from opencomputer.agent.injection import engine

    engine.unregister("life_event_hint")
    try:
        yield engine
    finally:
        engine.unregister("life_event_hint")


def test_cli_chat_surface_registers_life_event_provider(
    _clean_life_event_provider, monkeypatch
) -> None:
    """``_run_chat_session`` (the ``oc chat`` REPL) registers the
    life-event provider with ``surface="cli"``."""
    from unittest.mock import MagicMock

    from opencomputer import cli

    engine = _clean_life_event_provider

    # Neutralise every setup call between session start and the
    # registration line; ``AgentLoop`` (the statement right after the
    # registration) raises the sentinel so the REPL never boots.
    for name in (
        "_configure_logging_once",
        "_check_provider_key",
        "_register_builtin_tools",
        "_discover_plugins",
        "_apply_model_overrides",
        "_discover_and_register_agents",
        "_register_settings_hooks",
        "_seed_chat_status_metadata",
        "_apply_personality_skin_at_startup",
        "_has_any_provider_configured",
    ):
        monkeypatch.setattr(cli, name, MagicMock(return_value=True))

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "stub"
    monkeypatch.setattr(cli, "load_config", MagicMock(return_value=fake_cfg))

    provider = MagicMock()
    provider.supports_native_thinking_for.return_value = False
    monkeypatch.setattr(cli, "_resolve_provider", MagicMock(return_value=provider))

    def _stop(*_a, **_k):
        raise _StopAfterRegistrationError

    monkeypatch.setattr(cli, "AgentLoop", _stop)

    with pytest.raises(_StopAfterRegistrationError):
        cli._run_chat_session(resume="", plan=False, no_compact=False)

    prov = engine._providers.get("life_event_hint")
    assert isinstance(prov, LifeEventInjectionProvider)
    assert prov._surface == "cli"


def test_wire_surface_registers_life_event_provider(
    _clean_life_event_provider, monkeypatch
) -> None:
    """The ``oc wire`` command registers the life-event provider with
    ``surface="wire"``."""
    from unittest.mock import MagicMock

    from opencomputer import cli

    engine = _clean_life_event_provider

    for name in (
        "_configure_logging_once",
        "_check_provider_key",
        "_register_builtin_tools",
        "_discover_plugins",
        "_apply_model_overrides",
        "_discover_and_register_agents",
        "_register_settings_hooks",
    ):
        monkeypatch.setattr(cli, name, MagicMock(return_value=True))

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "stub"
    monkeypatch.setattr(cli, "load_config", MagicMock(return_value=fake_cfg))
    monkeypatch.setattr(cli, "_resolve_provider", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(cli, "AgentLoop", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "opencomputer.cli_hints.maybe_print_docker_toggle_hint", MagicMock()
    )

    # ``DelegateTool.set_factory`` is the statement right after the
    # wire surface's life-event registration — raise there to unwind
    # before the WebSocket server boots.
    def _stop(*_a, **_k):
        raise _StopAfterRegistrationError

    monkeypatch.setattr(cli.DelegateTool, "set_factory", _stop)

    with pytest.raises(_StopAfterRegistrationError):
        cli.wire(host="127.0.0.1", port=18789, detach=False)

    prov = engine._providers.get("life_event_hint")
    assert isinstance(prov, LifeEventInjectionProvider)
    assert prov._surface == "wire"


def test_gateway_foreground_registers_life_event_provider(
    _clean_life_event_provider, monkeypatch
) -> None:
    """``_run_foreground`` (bare ``oc gateway``) registers the life-event
    provider with ``surface="gateway"``."""
    from unittest.mock import MagicMock

    from opencomputer import cli, cli_gateway

    engine = _clean_life_event_provider

    # ``_run_foreground`` imports these helpers from ``opencomputer.cli``
    # at call time — patch them on that module.
    for name in (
        "_configure_logging_once",
        "_check_provider_key",
        "_register_builtin_tools",
        "_discover_plugins",
        "_apply_model_overrides",
        "_discover_and_register_agents",
        "_register_settings_hooks",
        "_resolve_provider",
    ):
        monkeypatch.setattr(cli, name, MagicMock(return_value=True))
    monkeypatch.setattr(cli, "_discover_plugins", MagicMock(return_value=0))

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "stub"
    fake_cfg.mcp.session_scoped = False
    monkeypatch.setattr(
        "opencomputer.agent.config_store.load_config",
        MagicMock(return_value=fake_cfg),
    )
    monkeypatch.setattr(
        "opencomputer.agent.loop.AgentLoop", MagicMock(return_value=MagicMock())
    )

    # ``MCPManager(...)`` is the first statement after the gateway
    # surface's life-event registration — raise there.
    def _stop(*_a, **_k):
        raise _StopAfterRegistrationError

    monkeypatch.setattr("opencomputer.mcp.client.MCPManager", _stop)
    # ``DelegateTool.set_factory`` runs before the registration; it only
    # stores a factory closure, so it is inert and left unpatched.

    with pytest.raises(_StopAfterRegistrationError):
        cli_gateway._run_foreground()

    prov = engine._providers.get("life_event_hint")
    assert isinstance(prov, LifeEventInjectionProvider)
    assert prov._surface == "gateway"


async def test_webui_completion_registers_life_event_provider(
    _clean_life_event_provider, monkeypatch
) -> None:
    """``_run_agent_completion`` (the ``oc webui`` / ``oc workspace``
    OpenAI-compat route) registers the life-event provider with
    ``surface="webui"``."""
    from unittest.mock import AsyncMock, MagicMock

    from opencomputer.dashboard.routes import openai_compat

    engine = _clean_life_event_provider

    fake_loop = MagicMock()
    fake_loop.config.model.model = "stub-model"
    fake_loop.run_conversation = AsyncMock(
        return_value=MagicMock(final_message=MagicMock(content="ok")),
    )
    monkeypatch.setattr(
        "opencomputer.gateway.agent_loop_factory.build_agent_loop_for_profile",
        MagicMock(return_value=fake_loop),
    )

    await openai_compat._run_agent_completion(
        user_message="hello",
        history=[],
        system_prompt=None,
        model="stub-model",
        oc_session_id=None,
    )

    prov = engine._providers.get("life_event_hint")
    assert isinstance(prov, LifeEventInjectionProvider)
    assert prov._surface == "webui"
