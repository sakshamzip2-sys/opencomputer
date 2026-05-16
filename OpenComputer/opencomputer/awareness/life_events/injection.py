"""LifeEventInjectionProvider — per-turn life-event hint + tone injection.

The life-event registry observes the F2 SignalEvent bus and queues
``surfacing="hint"`` :class:`PatternFiring` objects (job change, exam
prep, burnout, travel — see ``registry.py``). This provider is the chat
surfacer for that queue: at the start of every turn it drains the
pending firings and, for each non-muted firing, contributes a
``<life-event-hint>`` block to the system prompt carrying the firing's
``hint_text`` plus a per-event tone directive.

Why injection instead of mutating the system prompt directly?

- The base system prompt is FROZEN per session for prefix-cache hits.
  Mutating it mid-session would invalidate the cache.
- The InjectionEngine adds text AFTER the cached system prompt every
  turn; it's the canonical surface for per-turn cross-cutting context.

This mirrors :class:`opencomputer.agent.path_rules_injection.PathGlobRulesProvider`,
the other ``priority = 60`` per-turn provider.

Surfacing a hint also schedules its proactive follow-up: for each firing
:meth:`collect` surfaces, it calls
:func:`opencomputer.awareness.life_events.actions.schedule_followup`, which
creates a one-shot "gentle check-in" cron N days out and records the
``cron_id`` in ``life_event_state.json``. That call is fail-open — a cron
backend failure must never break prompt assembly, so it is wrapped in a
``try`` and ``collect`` still returns the ``<life-event-hint>`` block.
"""

from __future__ import annotations

import logging

from opencomputer.awareness.life_events import actions
from opencomputer.awareness.life_events.pattern import PatternFiring
from opencomputer.awareness.life_events.registry import get_global_registry
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

_log = logging.getLogger(__name__)

#: Per-event tone directive appended after a firing's ``hint_text``. Keys
#: are ``LifeEventPattern.pattern_id`` values. Patterns with ``surfacing
#: ="silent"`` (health_event, relationship_shift) never reach the chat
#: queue, so only the four ``"hint"`` patterns are mapped. A firing whose
#: ``pattern_id`` is absent here still surfaces its hint — just with no
#: directive line.
_TONE_DIRECTIVES: dict[str, str] = {
    "burnout": "Respond gently and concisely; do not pile on tasks.",
    "exam_prep": (
        "Keep replies focused and low-friction; the user is time-pressured."
    ),
    "job_change": "Be encouraging and practical about the transition.",
    "travel": "Account for the user being away from their usual setup.",
}


class LifeEventInjectionProvider(DynamicInjectionProvider):
    """Drain pending life-event ``"hint"`` firings into a system-prompt block.

    Construction takes no arguments — the provider reads the process-wide
    life-event registry singleton via :func:`get_global_registry`.

    Each turn :meth:`collect` drains every queued firing. Firings whose
    pattern is muted are dropped: ``LifeEventRegistry.on_event`` already
    skips muted patterns at queue time, so the queue is normally
    mute-free, but a firing queued *before* the user muted that pattern
    (a mute-between-turns race) would still be present — the
    :meth:`~opencomputer.awareness.life_events.registry.LifeEventRegistry.is_muted`
    filter defensively catches it.
    """

    #: Lower runs first per InjectionEngine convention. 60 matches
    #: :class:`~opencomputer.agent.path_rules_injection.PathGlobRulesProvider`:
    #: between built-in modes (plan=10, yolo=20, custom 50+) and
    #: user-added providers (>=100).
    priority: int = 60

    @property
    def provider_id(self) -> str:
        return "life_event_hint"

    async def collect(self, ctx: InjectionContext) -> str | None:
        """Return a ``<life-event-hint>`` block for this turn's firings.

        Each non-muted firing also gets its proactive follow-up scheduled
        via :func:`actions.schedule_followup` — a one-shot check-in cron N
        days out, recorded in ``life_event_state.json``. ``schedule_followup``
        dedups internally (a re-fire while a follow-up is still active never
        schedules a second cron) and records the ``cron_id`` itself, so this
        method does not pre-check state or call ``mark_surfaced`` directly.

        Cron scheduling is **fail-open**: a backend failure is caught and
        logged at WARNING; ``collect`` still returns the hint block so a
        cron outage never blocks prompt assembly.

        ``ctx`` carries no active-channel coordinates —
        :class:`~plugin_sdk.runtime_context.RuntimeContext` exposes mode
        flags only, not the per-request ``platform``/``chat_id``/``thread_id``
        (those live on the separate ``RequestContext``, not surfaced through
        ``InjectionContext``). So ``origin=None`` is passed: the check-in
        cron is still created, just without channel targeting.

        ``ctx.turn_index`` (the 1-indexed turn this prompt is being
        assembled for) IS threaded through as ``surfaced_turn`` — the
        STOP-hook classifier compares the current turn against it so the
        surfacing turn's own STOP is skipped and only the user's NEXT
        reply is judged.
        """
        reg = get_global_registry()
        firings = [f for f in reg.drain_pending() if not reg.is_muted(f.pattern_id)]
        if not firings:
            return None
        # Dedup by pattern_id. ``LifeEventPattern.accumulate`` has no
        # post-fire reset — once a pattern's confidence crosses threshold it
        # returns a PatternFiring on EVERY subsequent matching event, so the
        # queue can hold multiple firings of the SAME pattern within one turn.
        # Without dedup the <life-event-hint> block would repeat that
        # pattern's hint_text + tone directive N times. hint_text is stable
        # per pattern, so keeping the LAST firing per pattern_id is fine.
        deduped: dict[str, PatternFiring] = {}
        for firing in firings:
            deduped[firing.pattern_id] = firing
        firings = list(deduped.values())
        lines: list[str] = []
        for firing in firings:
            lines.append(firing.hint_text)
            directive = _TONE_DIRECTIVES.get(firing.pattern_id)
            if directive:
                lines.append(directive)
            # Schedule the proactive follow-up cron for this surfaced hint.
            # ``ctx.turn_index`` is threaded through so state records the
            # turn the hint surfaced on — the STOP-hook classifier needs it
            # to skip the surfacing turn's own STOP and judge only the
            # user's NEXT reply.
            # Fail-open: a cron failure must NOT break prompt assembly — the
            # <life-event-hint> block is still returned below.
            try:
                actions.schedule_followup(
                    firing, origin=None, surfaced_turn=ctx.turn_index
                )
            except Exception:  # noqa: BLE001 - fail-open; cron outage isolated
                _log.warning(
                    "failed to schedule life-event follow-up cron for %s; "
                    "surfacing the hint anyway",
                    firing.pattern_id,
                    exc_info=True,
                )
        return "<life-event-hint>\n" + "\n".join(lines) + "\n</life-event-hint>"


__all__ = ["LifeEventInjectionProvider"]
