"""End-to-end integration test for life-event "teeth" (Tasks 1–8).

This file drives the WHOLE feature flow continuously — exercising the real
Tasks 3/5/7 wiring with no live LLM and no real cron — so a regression in
any single piece (injection / cron scheduling / STOP-hook self-correction)
trips here even when the per-component unit tests still pass.

The flow under test:

1. A life-event pattern fires. A ``surfacing="hint"`` :class:`PatternFiring`
   lands on the global registry's queue (Task 1/2 — here seeded directly,
   mirroring ``test_life_event_injection``).
2. The next turn's prompt is assembled. ``LifeEventInjectionProvider.collect``
   (Task 3) drains the firing, emits a ``<life-event-hint>`` block carrying
   the hint text + a per-event tone directive, and (Task 5) schedules a
   one-shot follow-up cron via ``actions.schedule_followup`` — recording the
   ``cron_id``, ``verdict_pending=True`` and ``surfaced_turn`` in
   ``life_event_state.json``.
3. A LATER turn ends. ``on_stop_hook`` (Task 7) judges the user's reply:
   - a refuting reply → ``actions.cancel_followup`` deletes the cron and
     clears the state entry — the tooth is dropped;
   - a confirming reply → the cron + entry survive, only ``verdict_pending``
     flips off.

Isolation (mirrors ``test_life_event_actions`` / ``test_life_event_injection``):

- ``OPENCOMPUTER_HOME`` → a per-test ``tmp_path`` so ``life_event_state.json``
  writes never touch the real profile.
- ``actions.create_job`` / ``actions.remove_job`` are monkey-patched so NO
  real cron job is ever written to ``cron.db``. ``actions.py`` imports both
  names directly, so patching ``actions.<name>`` is the right seam.
- ``reset_global_registry_for_test()`` brackets each test so the registry
  singleton (and its queue) never leaks between tests.

Each scenario (surface / refute / confirm) gets its own fully-isolated test
function — fresh registry, fresh tmp_path, fresh monkeypatch — rather than
one shared-state flow, so a failure points at exactly one transition.
"""
from __future__ import annotations

import time

import pytest

from opencomputer.awareness.life_events import actions, state
from opencomputer.awareness.life_events.classifier import on_stop_hook
from opencomputer.awareness.life_events.injection import (
    LifeEventInjectionProvider,
)
from opencomputer.awareness.life_events.pattern import PatternFiring
from opencomputer.awareness.life_events.registry import (
    get_global_registry,
    reset_global_registry_for_test,
)
from plugin_sdk.core import Message
from plugin_sdk.hooks import HookContext, HookEvent
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext

# The turn the burnout hint surfaces on. The STOP hook must judge the reply
# on a STRICTLY LATER turn — so the surface step uses N and every judging
# step uses N + 1.
_SURFACE_TURN = 3


# ── fixtures: cron + profile isolation ───────────────────────────────────


@pytest.fixture
def fake_cron(monkeypatch):
    """Neutralise the cron backend for one test.

    Returns a ``(created, removed)`` pair of lists:

    - ``created`` accumulates the kwargs of every ``create_job`` call so the
      test can assert on the scheduled cron's shape;
    - ``removed`` accumulates the job ids passed to ``remove_job`` so the
      test can assert whether (and which) cron was cancelled.

    Each stubbed ``create_job`` returns a unique ``"id"`` so
    ``schedule_followup`` → ``mark_surfaced`` records a real ``cron_id``.
    ``actions.py`` imports ``create_job`` / ``remove_job`` directly, so the
    monkeypatch target is ``actions.<name>``.
    """
    created: list[dict] = []
    removed: list[str] = []

    def fake_create_job(**kwargs):
        created.append(kwargs)
        return {"id": f"cron-{len(created)}", "name": kwargs.get("name")}

    def fake_remove_job(job_id):
        removed.append(job_id)
        return True

    monkeypatch.setattr(actions, "create_job", fake_create_job)
    monkeypatch.setattr(actions, "remove_job", fake_remove_job)
    return created, removed


@pytest.fixture(autouse=True)
def isolate_profile_and_registry(tmp_path, monkeypatch):
    """Profile-isolate and registry-isolate every test in this file.

    ``OPENCOMPUTER_HOME`` → ``tmp_path`` keeps ``life_event_state.json``
    writes in a temp dir. ``reset_global_registry_for_test`` brackets the
    test so the process-wide registry singleton (and its firing queue)
    starts and ends clean — no firing leaks into the next test.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    reset_global_registry_for_test()
    yield
    reset_global_registry_for_test()


# ── helpers ──────────────────────────────────────────────────────────────


def _burnout_firing(hint_text: str = "your work rhythm looked like it shifted") -> PatternFiring:
    """A surfacing=\"hint\" Burnout firing — what the registry queues on a fire."""
    return PatternFiring(
        pattern_id="burnout",
        confidence=0.85,
        evidence_count=4,
        surfacing="hint",
        hint_text=hint_text,
        timestamp=time.time(),
    )


def _injection_ctx(turn_index: int) -> InjectionContext:
    """A real InjectionContext for a given turn — what the InjectionEngine
    passes to ``collect`` while assembling turn ``turn_index``'s prompt."""
    return InjectionContext(
        messages=(),
        runtime=RuntimeContext(),
        session_id="e2e-session",
        turn_index=turn_index,
    )


def _stop_ctx(*, turn_index: int, last_user_text: str) -> HookContext:
    """A STOP HookContext whose message history ends with a user reply.

    Mirrors the real loop: ``messages`` is the conversation so far and the
    final ``role == "user"`` message is the reply ``on_stop_hook`` judges.
    """
    return HookContext(
        event=HookEvent.STOP,
        session_id="e2e-session",
        messages=[
            Message(role="user", content="(the surfacing-turn message)"),
            Message(role="assistant", content="(hint-influenced reply)"),
            Message(role="user", content=last_user_text),
        ],
        turn_index=turn_index,
    )


async def _surface_burnout_hint(hint_text: str) -> str:
    """Drive the surface step: seed a Burnout firing, run ``collect``.

    Returns the ``<life-event-hint>`` block ``collect`` produced. After this
    call ``life_event_state.json`` holds a verdict-pending ``burnout`` entry
    with a ``cron_id`` and ``surfaced_turn == _SURFACE_TURN``.
    """
    registry = get_global_registry()
    registry._queue.append(_burnout_firing(hint_text))
    block = await LifeEventInjectionProvider().collect(_injection_ctx(_SURFACE_TURN))
    assert block is not None, "collect() must surface the queued burnout firing"
    return block


# ── Scenario 1: a firing surfaces a hint AND schedules its follow-up cron ─


async def test_firing_surfaces_hint_block_and_schedules_followup(fake_cron):
    """A queued Burnout firing → a <life-event-hint> block + a follow-up cron.

    This is Tasks 3 + 5 wired together: ``collect`` both emits the prompt
    block (hint text + tone directive) and schedules the proactive check-in
    cron, recording ``cron_id`` / ``verdict_pending`` / ``surfaced_turn`` in
    ``life_event_state.json``.
    """
    created, _removed = fake_cron
    hint = "your work rhythm looked like it shifted"

    block = await _surface_burnout_hint(hint)

    # --- The injected prompt block. -------------------------------------
    assert block.startswith("<life-event-hint>")
    assert block.endswith("</life-event-hint>")
    # The firing's own hint text is surfaced verbatim.
    assert hint in block
    # The burnout per-event tone directive rides along.
    assert "Respond gently and concisely; do not pile on tasks." in block

    # --- The follow-up cron was scheduled (Task 5). ---------------------
    assert len(created) == 1, "surfacing the hint must schedule exactly one cron"
    cron_kwargs = created[0]
    # A one-shot cron 3 days out (burnout's _FOLLOWUP_DELAY_DAYS) carrying
    # the gentle check-in prompt.
    assert cron_kwargs["schedule"] == "3d"
    assert cron_kwargs["prompt"] == actions._CHECKIN_PROMPT["burnout"]
    # collect() cannot see the active channel — origin is None, so the cron
    # is created WITHOUT channel targeting (documented v1 limitation).
    assert cron_kwargs.get("origin_platform") is None
    assert cron_kwargs.get("notify") != "origin"

    # --- The state record (Task 5 → state.mark_surfaced). ---------------
    entry = state.load_state()["burnout"]
    assert entry["cron_id"], "a follow-up cron_id must be recorded"
    assert entry["verdict_pending"] is True, "surfacing makes the next reply verdict-pending"
    assert entry["surfaced_turn"] == _SURFACE_TURN, (
        "the surfacing turn must be recorded for the STOP-hook turn check"
    )


# ── Scenario 2: a refuting reply on a later turn cancels the cron ────────


async def test_refuting_reply_on_later_turn_cancels_followup_cron(fake_cron):
    """Full flow: surface at turn N, then a refuting reply at turn N+1 →
    the follow-up cron is cancelled and the state entry is dropped.

    Turn N+1 is STRICTLY LATER than the recorded ``surfaced_turn`` (N), so
    the surfacing-turn skip does NOT apply — ``on_stop_hook`` judges the
    reply. ``"I'm totally fine, not stressed"`` is a clear refutation, so
    ``cancel_followup`` deletes the cron and clears the whole tooth.
    """
    created, removed = fake_cron

    # Surface the hint (turn N) — schedules the cron, records the entry.
    await _surface_burnout_hint("your work rhythm looked like it shifted")
    scheduled_cron_id = state.load_state()["burnout"]["cron_id"]
    assert len(created) == 1

    # The user's reply on turn N+1 clearly refutes burnout.
    await on_stop_hook(
        _stop_ctx(turn_index=_SURFACE_TURN + 1, last_user_text="I'm totally fine, not stressed")
    )

    # The follow-up cron was cancelled — its recorded id was passed to
    # remove_job.
    assert removed == [scheduled_cron_id], (
        "a refuting reply must cancel the scheduled follow-up cron"
    )
    # The whole tooth is dropped — no burnout entry survives.
    assert "burnout" not in state.load_state(), (
        "a refuting reply must clear the burnout state entry entirely"
    )


# ── Scenario 3: a confirming reply on a later turn keeps the cron ────────


async def test_confirming_reply_on_later_turn_keeps_followup_cron(fake_cron):
    """Full flow: surface at turn N, then a confirming reply at turn N+1 →
    the follow-up cron is KEPT; only ``verdict_pending`` flips off.

    A separate, fully-isolated test (fresh registry/tmp/monkeypatch via the
    autouse fixture + the per-test ``fake_cron``) — never sharing state with
    the refuting scenario. ``"yeah I'm really burnt out"`` confirms the
    rough patch, so the gentle check-in must still fire: the cron and the
    entry survive, the reply is simply no longer verdict-pending.
    """
    created, removed = fake_cron

    # Surface the hint (turn N).
    await _surface_burnout_hint("your work rhythm looked like it shifted")
    scheduled_cron_id = state.load_state()["burnout"]["cron_id"]
    assert len(created) == 1

    # The user's reply on turn N+1 confirms the rough patch.
    await on_stop_hook(
        _stop_ctx(turn_index=_SURFACE_TURN + 1, last_user_text="yeah I'm really burnt out")
    )

    # A confirming reply must NOT cancel the cron.
    assert removed == [], "a confirming reply must NOT delete the follow-up cron"
    # The entry survives, its cron_id retained — the check-in still fires.
    entry = state.load_state().get("burnout")
    assert entry is not None, "the burnout entry must survive a confirming reply"
    assert entry["cron_id"] == scheduled_cron_id, "the cron_id must be retained"
    # The reply has been judged — it is no longer verdict-pending.
    assert entry["verdict_pending"] is False, (
        "a judged (confirming) reply must clear verdict_pending"
    )


# ── Scenario 4: the surfacing turn's own STOP is skipped ─────────────────


async def test_stop_hook_on_surfacing_turn_does_not_judge_the_reply(fake_cron):
    """The STOP firing on the surfacing turn itself must NOT judge anything.

    The user's turn-N message predates the hint-influenced reply, so even a
    refuting phrase in it must not cancel the cron. ``on_stop_hook`` runs
    with ``turn_index == surfaced_turn`` and must leave the cron + entry
    untouched and still verdict-pending — the real reply lands on turn N+1.
    This guards the turn-index timing that the whole self-correction relies
    on.
    """
    created, removed = fake_cron

    await _surface_burnout_hint("your work rhythm looked like it shifted")
    assert len(created) == 1

    # STOP on the SAME turn the hint surfaced on — and the seeded message
    # even refutes — yet nothing must be judged.
    await on_stop_hook(
        _stop_ctx(turn_index=_SURFACE_TURN, last_user_text="I'm totally fine, not stressed")
    )

    assert removed == [], "the surfacing turn's own STOP must NOT cancel the cron"
    entry = state.load_state().get("burnout")
    assert entry is not None, "the entry must survive the surfacing turn's STOP"
    assert entry["verdict_pending"] is True, (
        "still verdict-pending — the real reply (turn N+1) has not been seen yet"
    )


# ── Scenarios 5–7: multi-surface teeth, WIRED end-to-end ─────────────────
#
# Tasks 4 + 6 extended life-event teeth past the CLI. These scenarios drive
# the WHOLE wired path — ``register_life_event_injection_provider(surface)``
# installs a surface-labelled provider on the *real* injection engine; the
# provider is then pulled back off ``engine._providers`` (exactly the object
# the InjectionEngine would call mid-prompt) and its ``collect()`` is run.
# So registration → feature flag → registry drain → provider → cron all
# fire for real; a regression anywhere on that chain trips here.
#
# On the gateway surface the proactive check-in is *origin-targeted*: the
# gateway parks a ``RequestContext`` (channel + user_id) on the shared
# ``PluginAPI``; ``collect`` reads it and threads a ``{platform, chat_id}``
# origin into the cron. These scenarios install that context the same way
# the gateway dispatch does (via the plugin registry's ``shared_api``) and
# assert the scheduled cron carries the origin.


def _write_multi_surface_flag(home, *, enabled: bool) -> None:
    """Write ``feature_flags.json`` into ``home`` with the multi-surface
    life-event flag set to ``enabled``.

    The autouse fixture already points ``OPENCOMPUTER_HOME`` at the test's
    ``tmp_path``; this drops the ``feature_flags.json`` sibling that
    :func:`FeatureFlags.read` (and thus the provider's surface gate) picks
    up. Mirrors the helper of the same name in ``test_life_event_injection``.
    """
    import json
    from pathlib import Path

    (Path(home) / "feature_flags.json").write_text(
        json.dumps({"life_events": {"multi_surface_life_events": enabled}})
    )


class _FakeRequestContext:
    """Minimal stand-in carrying just the fields ``_resolve_origin`` reads.

    The real :class:`plugin_sdk.runtime_context.RequestContext` is a frozen
    slotted dataclass with a required ``request_id``; ``_resolve_origin``
    only touches ``.channel`` and ``.user_id``, so a tiny attribute holder
    is enough. Mirrors the helper of the same name in
    ``test_life_event_injection``.
    """

    def __init__(self, channel, user_id) -> None:
        self.channel = channel
        self.user_id = user_id


def _activate_request_context(monkeypatch, ctx) -> None:
    """Make ``ctx`` the active RequestContext as ``_resolve_origin`` sees it.

    ``_resolve_origin`` reaches the active request through the process-wide
    plugin registry singleton — ``registry.shared_api.request_context`` —
    which on the gateway path is the same ``PluginAPI`` the dispatch wrapped
    in ``in_request(ctx)``. This installs a real ``PluginAPI`` (built via
    ``PluginRegistry.api()``) on ``registry.shared_api`` and parks ``ctx`` on
    its private ``_request_context`` slot — the slot ``PluginAPI.in_request``
    writes — so the public ``request_context`` property returns it.
    ``monkeypatch`` restores ``shared_api`` afterwards. Mirrors the helper of
    the same name in ``test_life_event_injection``.
    """
    from opencomputer.plugins.registry import registry as _plugin_registry

    api = _plugin_registry.api()
    api._request_context = ctx
    monkeypatch.setattr(_plugin_registry, "shared_api", api)


@pytest.fixture
def _clean_life_event_provider():
    """Bracket a test so the real injection engine has no stale
    ``life_event_hint`` provider before it and is left clean afterwards.

    These scenarios register the provider on the *process-wide* injection
    engine via ``register_life_event_injection_provider``; this fixture is
    the symmetric cleanup so registration never leaks into another test.
    """
    from opencomputer.agent.injection import engine

    engine.unregister("life_event_hint")
    try:
        yield engine
    finally:
        engine.unregister("life_event_hint")


async def test_gateway_surface_flag_on_surfaces_hint_and_origin_targeted_cron(
    fake_cron, _clean_life_event_provider, monkeypatch, tmp_path
):
    """WIRED: gateway registration + flag ON + active RequestContext →
    a <life-event-hint> block AND an origin-targeted check-in cron.

    Drives the full multi-surface path end to end:

    1. ``register_life_event_injection_provider("gateway")`` installs the
       provider on the real injection engine.
    2. The multi-surface feature flag is ON in the test profile.
    3. A gateway ``RequestContext`` (channel ``telegram`` + user ``chat-42``)
       is active — exactly as the gateway dispatch parks it.
    4. A burnout firing is fed into the global registry.
    5. The provider pulled BACK off ``engine._providers`` (the object the
       InjectionEngine itself would invoke) runs ``collect()``.

    The hint block must surface (flag ON unlocks the non-CLI surface) and
    the scheduled cron must carry the origin derived from the request
    context — ``notify="origin"`` + ``origin_platform``/``origin_chat_id``.
    """
    from opencomputer.awareness.life_events.injection import (
        LifeEventInjectionProvider,
        register_life_event_injection_provider,
    )

    created, _removed = fake_cron
    engine = _clean_life_event_provider
    _write_multi_surface_flag(tmp_path, enabled=True)
    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel="telegram", user_id="chat-42")
    )

    # 1. The gateway surface's boot-time registration.
    register_life_event_injection_provider(surface="gateway")
    provider = engine._providers.get("life_event_hint")
    assert isinstance(provider, LifeEventInjectionProvider), (
        "the gateway registration must install the provider on the engine"
    )
    assert provider._surface == "gateway"

    # 2. A life event fires onto the global registry queue.
    hint = "your work rhythm looked like it shifted"
    get_global_registry()._queue.append(_burnout_firing(hint))

    # 3. The InjectionEngine assembles the turn — run the registered provider.
    block = await provider.collect(_injection_ctx(_SURFACE_TURN))

    # --- The hint surfaced: flag ON unlocks the gateway surface. ---------
    assert block is not None, "flag ON → the gateway surface must surface the hint"
    assert block.startswith("<life-event-hint>")
    assert block.endswith("</life-event-hint>")
    assert hint in block
    assert "Respond gently and concisely; do not pile on tasks." in block

    # --- The check-in cron is origin-targeted (gateway path). ------------
    assert len(created) == 1, "surfacing the hint must schedule exactly one cron"
    cron_kwargs = created[0]
    assert cron_kwargs["schedule"] == "3d"
    assert cron_kwargs["prompt"] == actions._CHECKIN_PROMPT["burnout"]
    # The origin was derived from the active RequestContext and threaded in.
    assert cron_kwargs["notify"] == "origin", (
        "the gateway check-in cron must request origin delivery"
    )
    assert cron_kwargs["origin_platform"] == "telegram"
    assert cron_kwargs["origin_chat_id"] == "chat-42"

    # --- State recorded the surfaced firing. -----------------------------
    entry = state.load_state()["burnout"]
    assert entry["cron_id"], "a follow-up cron_id must be recorded"
    assert entry["verdict_pending"] is True
    assert entry["surfaced_turn"] == _SURFACE_TURN


async def test_gateway_surface_flag_off_surfaces_nothing(
    fake_cron, _clean_life_event_provider, monkeypatch, tmp_path
):
    """WIRED: gateway registration + flag OFF → collect() returns None,
    no hint, no cron.

    Same wiring as the flag-ON scenario — ``register_life_event_injection_provider
    ("gateway")``, an active gateway ``RequestContext``, a queued burnout
    firing — but the ``multi_surface_life_events`` flag is OFF. The gateway
    surface is opt-in, so the registered provider's ``collect()`` must return
    ``None``: no ``<life-event-hint>`` block, and (the firing never reaching
    the surface) no check-in cron scheduled. This is the gate that keeps
    multi-surface teeth dark until a profile opts in.
    """
    from opencomputer.awareness.life_events.injection import (
        register_life_event_injection_provider,
    )

    created, removed = fake_cron
    engine = _clean_life_event_provider
    _write_multi_surface_flag(tmp_path, enabled=False)
    _activate_request_context(
        monkeypatch, _FakeRequestContext(channel="telegram", user_id="chat-42")
    )

    register_life_event_injection_provider(surface="gateway")
    provider = engine._providers.get("life_event_hint")

    hint = "your work rhythm looked like it shifted"
    get_global_registry()._queue.append(_burnout_firing(hint))

    block = await provider.collect(_injection_ctx(_SURFACE_TURN))

    # Flag OFF → the gateway surface is gated: nothing surfaces.
    assert block is None, "flag OFF → the gateway surface must surface no hint"
    # No cron was scheduled — the firing never reached cron scheduling.
    assert created == [], "a gated-off surface must schedule no check-in cron"
    assert removed == []
    # And no state entry was recorded for the gated firing.
    assert "burnout" not in state.load_state(), (
        "a gated-off firing must record no life_event_state entry"
    )


async def test_cli_surface_unaffected_by_multi_surface_flag(
    fake_cron, _clean_life_event_provider, tmp_path
):
    """WIRED: the CLI surface is always on — the flag does not gate it.

    With the ``multi_surface_life_events`` flag explicitly OFF,
    ``register_life_event_injection_provider("cli")`` + the registered
    provider's ``collect()`` must STILL surface the hint: life-event teeth
    shipped CLI-only and unflagged in #630, and the multi-surface flag only
    gates the non-CLI surfaces. This pins that the new gating is additive —
    it never regresses the original CLI behaviour.
    """
    from opencomputer.awareness.life_events.injection import (
        LifeEventInjectionProvider,
        register_life_event_injection_provider,
    )

    created, _removed = fake_cron
    engine = _clean_life_event_provider
    # Flag OFF — and yet the CLI surface must still surface the hint.
    _write_multi_surface_flag(tmp_path, enabled=False)

    register_life_event_injection_provider(surface="cli")
    provider = engine._providers.get("life_event_hint")
    assert isinstance(provider, LifeEventInjectionProvider)
    assert provider._surface == "cli"

    hint = "your work rhythm looked like it shifted"
    get_global_registry()._queue.append(_burnout_firing(hint))

    block = await provider.collect(_injection_ctx(_SURFACE_TURN))

    # CLI is always on — the flag-OFF state does NOT gate it.
    assert block is not None, "the CLI surface must surface the hint regardless of the flag"
    assert block.startswith("<life-event-hint>")
    assert hint in block
    # The follow-up cron is still scheduled — just untargeted (no
    # RequestContext on the CLI path).
    assert len(created) == 1
    assert created[0].get("notify") != "origin"
    assert created[0].get("origin_platform") is None
