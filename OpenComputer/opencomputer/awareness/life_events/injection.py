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

Surface awareness
-----------------
Life-event teeth shipped CLI-only and unflagged (#630). Extending the
provider to gateway / wire / webui is opt-in: the provider takes a
``surface`` constructor argument, and on every non-CLI surface
:meth:`collect` surfaces a hint only when the active profile has set the
``life_events.multi_surface_life_events`` feature flag. The CLI surface
stays unconditionally on. :func:`register_life_event_injection_provider`
is the surface-parameterised registration helper each surface calls.
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


def _multi_surface_enabled() -> bool:
    """Return True when the active profile opted into multi-surface teeth.

    Reads ``life_events.multi_surface_life_events`` from the active
    profile's ``feature_flags.json``. The profile home is resolved via
    :func:`opencomputer.agent.config._home` — the canonical core resolver
    ``state.py`` also uses (ContextVar → ``OPENCOMPUTER_HOME`` → fallback).
    Resolved per call so a per-test ``OPENCOMPUTER_HOME`` monkey-patch is
    honoured.

    Fail-safe: any error (missing/corrupt flag file, resolver failure)
    is swallowed and reported as ``False`` — a flag-read failure must
    never break prompt assembly, and ``False`` is the safe default (the
    multi-surface behaviour stays off).
    """
    try:
        from opencomputer.agent.config import _home
        from opencomputer.agent.feature_flags import FeatureFlags

        flags = FeatureFlags(_home() / "feature_flags.json")
        return bool(
            flags.read("life_events.multi_surface_life_events", False)
        )
    except Exception:  # noqa: BLE001 - fail-safe; flag-read failure isolated
        _log.debug(
            "multi_surface_life_events flag read failed; treating as off",
            exc_info=True,
        )
        return False


def _resolve_origin() -> dict | None:
    """Return the active request's channel coordinates, or ``None``.

    On the gateway dispatch path the gateway wraps each inbound message's
    ``run_conversation`` in ``PluginAPI.in_request(ctx)``, parking a
    :class:`~plugin_sdk.runtime_context.RequestContext` on the
    process-wide shared ``PluginAPI``. That instance is reachable from
    module-level code via the plugin-registry singleton:
    ``opencomputer.plugins.registry.registry.shared_api`` (set by
    ``PluginRegistry.load_all``; it is the *same* object the gateway
    dispatch holds as ``Dispatch._plugin_api``). Its ``request_context``
    property returns the active ``RequestContext`` for the duration of a
    dispatch and ``None`` outside one.

    When a ``RequestContext`` is active and carries BOTH ``channel`` and
    ``user_id``, this returns ``{"platform": <channel>, "chat_id":
    <user_id>}`` — the mapping :func:`actions.schedule_followup` threads
    into ``create_job`` so the check-in cron is delivered back to that
    chat. CLI / wire / webui callers enter no request scope, so
    ``request_context`` is ``None`` there and this returns ``None`` (an
    untargeted check-in cron).

    Fail-safe: any unexpected error is swallowed and reported as ``None``
    — a missing origin only costs channel targeting, never the cron.
    """
    try:
        from opencomputer.plugins.registry import registry as _plugin_registry

        api = _plugin_registry.shared_api
        if api is None:
            return None
        ctx = api.request_context
        if ctx is None:
            return None
        channel = getattr(ctx, "channel", None)
        user_id = getattr(ctx, "user_id", None)
        if channel and user_id:
            return {"platform": channel, "chat_id": user_id}
        return None
    except Exception:  # noqa: BLE001 - fail-safe; origin lookup isolated
        _log.debug("life-event origin resolution failed", exc_info=True)
        return None


class LifeEventInjectionProvider(DynamicInjectionProvider):
    """Drain pending life-event ``"hint"`` firings into a system-prompt block.

    Construction takes one optional argument, ``surface`` (default
    ``"cli"``), recording which surface this provider serves. The CLI
    surface is unconditionally active — life-event teeth shipped CLI-only
    and unflagged in #630. Every other surface (``"gateway"``, ``"wire"``,
    ``"webui"``) is opt-in: :meth:`collect` surfaces a hint there only
    when the active profile set the ``life_events.multi_surface_life_events``
    feature flag. Apart from the surface gate the provider reads the
    process-wide life-event registry singleton via
    :func:`get_global_registry`.

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

    def __init__(self, surface: str = "cli") -> None:
        """Construct the provider for a given ``surface``.

        ``surface`` is the surface name this provider instance serves —
        ``"cli"`` (the always-on default), ``"gateway"``, ``"wire"`` or
        ``"webui"``. :meth:`collect` consults it to decide whether the
        multi-surface feature flag must be set before a hint surfaces.
        """
        self._surface = surface

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

        The registry queue is drained FIRST, then the surface gate is
        applied: on a non-CLI surface with the multi-surface feature flag
        off, ``collect`` returns ``None`` — but the firings were already
        consumed, which keeps the process-global queue bounded on a
        long-running flag-off gateway daemon.

        The check-in cron's ``origin`` is built from the active
        :class:`~plugin_sdk.runtime_context.RequestContext` via
        :func:`_resolve_origin`: on the gateway dispatch path that yields
        the user's ``platform``/``chat_id`` so the check-in is routed back
        to their chat; on CLI / wire / webui no request scope is active so
        ``origin`` is ``None`` and the cron is created without channel
        targeting.

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
        # Surface gate. The registry queue was drained above — draining BEFORE
        # this gate keeps the process-global queue bounded on a long-running
        # flag-off gateway daemon (a gated-off surface still consumes its
        # firings, it just doesn't surface them). The CLI surface is always
        # active (life-event teeth shipped CLI-only and unflagged in #630);
        # every other surface is opt-in via the multi_surface_life_events flag.
        if self._surface != "cli" and not _multi_surface_enabled():
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
        # Resolve the active-channel origin ONCE for the whole turn — every
        # firing surfaced this turn shares the same inbound request, so the
        # check-in crons all route back to the same chat.
        origin = _resolve_origin()
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
                    firing, origin=origin, surfaced_turn=ctx.turn_index
                )
            except Exception:  # noqa: BLE001 - fail-open; cron outage isolated
                _log.warning(
                    "failed to schedule life-event follow-up cron for %s; "
                    "surfacing the hint anyway",
                    firing.pattern_id,
                    exc_info=True,
                )
        return "<life-event-hint>\n" + "\n".join(lines) + "\n</life-event-hint>"


def register_life_event_injection_provider(surface: str = "cli") -> None:
    """Register the life-event injection provider for a given ``surface``.

    Each surface (CLI, gateway, wire, webui) calls this during its boot to
    install a :class:`LifeEventInjectionProvider` carrying its surface
    name. ``unregister`` runs first so a re-registration replaces rather
    than collides — the InjectionEngine raises on a duplicate
    ``provider_id``.

    Fail-soft: registration is wrapped so a failure here logs a WARNING
    (carrying the surface) and is otherwise swallowed — installing the
    life-event surfacer must never break a surface's boot.
    """
    try:
        from opencomputer.agent.injection import engine

        engine.unregister("life_event_hint")
        engine.register(LifeEventInjectionProvider(surface=surface))
    except Exception:  # noqa: BLE001 - never break surface boot
        _log.warning(
            "failed to register life-event injection provider for surface %r",
            surface,
            exc_info=True,
        )


__all__ = [
    "LifeEventInjectionProvider",
    "register_life_event_injection_provider",
]
