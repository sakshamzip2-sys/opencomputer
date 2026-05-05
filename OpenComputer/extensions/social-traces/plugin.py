"""social-traces plugin — entry module.

Registers the BEFORE_TASK hook (real query/inject path lands in Phase 4)
and stands up the post-task SessionEndEvent subscriber (real
distillation lands in Phase 5-7). Phase 2 wires the registration shape
so the loop seam is exercised from day one and `oc traces enable`
turns visible behaviour on without further code changes.

The plugin SHIPS DISABLED. Two layers of opt-in must align before any
trace work happens:

1. ``plugin.json: enabled_by_default = false`` — operator must explicitly
   load the plugin via ``oc plugin enable social-traces``.
2. ``<profile_home>/traces/state.json: {"enabled": true}`` — operator
   must explicitly turn the feature on via ``oc traces enable``.

Both must be set. This is deliberate: the network is a privacy-sensitive
egress surface. Default-off until the user has read the README.
"""

from __future__ import annotations

import logging

from plugin_sdk.hooks import HookEvent, HookSpec

from .prefetch import on_before_task

_log = logging.getLogger("opencomputer.social_traces.plugin")


def register(api) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Plugin entry. Wire hooks + bus subscriber.

    Subscriber lifecycle is gateway-managed — the gateway boots the
    typed event bus and keeps it alive across the daemon's lifetime.
    For the CLI path (single-shot ``opencomputer chat``), the subscriber
    is started lazily on first SessionEndEvent emission via the bus's
    autoload hook (TBD in Phase 5; for Phase 2 the subscriber is just
    importable + constructible, not auto-started).
    """
    api.register_hook(
        HookSpec(
            event=HookEvent.BEFORE_TASK,
            handler=on_before_task,
            fire_and_forget=False,
            # Run early so the trace injection lands before any
            # other plugin's BEFORE_TASK handler can transform context.
            priority=20,
            # Soft 1s timeout to match the network-query budget in
            # ``SocialTracesConfig.query.soft_timeout_s``. If the hook
            # ever exceeds this (Phase 4+), the engine treats it as
            # ``pass`` (fail-open) and the agent proceeds to explore —
            # CLAUDE.md §7 contract: a wedged hook must never wedge
            # the loop.
            timeout_ms=1500,
        )
    )

    _log.debug(
        "social-traces plugin registered (Phase 2: BEFORE_TASK hook only; "
        "subscriber pending Phase 5)"
    )
