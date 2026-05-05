"""social-traces plugin — entry module.

Registers:

* The BEFORE_TASK hook (Phase 4 — real query/score/inject path).
* The post-task SessionEndEvent subscriber (Phase 5 — real decision
  tree; Phase 6/7 stubs make the LLM calls no-ops until those phases
  land).

The plugin SHIPS DISABLED. Two layers of opt-in must align before any
trace work happens:

1. ``plugin.json: enabled_by_default = false`` — operator must
   explicitly load the plugin via ``opencomputer plugin enable``.
2. ``<profile_home>/traces/state.json: {"enabled": true}`` — operator
   must explicitly turn the feature on via ``oc traces enable``.

Both must be set. Privacy-sensitive egress surface; default-off until
the user has read the README.
"""

from __future__ import annotations

import logging
from pathlib import Path

from plugin_sdk.hooks import HookEvent, HookSpec

from . import state as st_state
from .config import SocialTracesConfig, from_config_dict
from .prefetch import on_before_task
from .subscriber import TraceEmissionSubscriber

_log = logging.getLogger("opencomputer.social_traces.plugin")


def _profile_home_factory() -> Path:
    """Lazy resolver for the active profile home.

    Resolved at event-arrival time (not at register-time) so
    multi-profile dispatch sees the correct path. Uses the
    ``state.resolve_profile_home`` helper which stays inside the
    plugin_sdk + stdlib boundary.
    """
    return st_state.resolve_profile_home()


def _config_factory(profile_home: Path) -> SocialTracesConfig:
    """Lazy resolver for the parsed ``social_traces:`` config section.

    Re-reads ``config.yaml`` per call so the operator can edit knobs
    (relevance threshold, cost guard, etc.) without restarting the
    daemon. Cheap (~1ms for a small YAML); fine for the cadence here.
    """
    import yaml

    cfg_path = profile_home / "config.yaml"
    if not cfg_path.exists():
        return SocialTracesConfig()
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return SocialTracesConfig()
    return from_config_dict(raw.get("social_traces", {}))


def _client_factory(profile_home: Path, cfg: SocialTracesConfig):
    """Lazy resolver for the trace network client.

    Defers the import + construction so the plugin can register
    cleanly even if the http backend's deps are missing — only the
    actual subscriber path that calls submit() reaches the client.
    """
    from .client import make_client

    return make_client(
        backend=cfg.backend,
        profile_home=profile_home,
        endpoint=cfg.endpoint,
    )


def register(api) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Plugin entry. Wire BEFORE_TASK hook + start the post-task
    subscriber.

    The subscriber is started here so it's live for the lifetime of
    the plugin's load — gateway-mode daemons get full event coverage,
    and CLI single-shot ``opencomputer chat`` gets one-pass coverage
    since the bus persists for the duration of run_conversation.

    If subscriber-start fails (bus unavailable in some test contexts)
    the failure is logged but the BEFORE_TASK registration still
    succeeds — the plugin degrades to "pre-task lookup works,
    post-task emit doesn't" rather than failing entirely.
    """
    api.register_hook(
        HookSpec(
            event=HookEvent.BEFORE_TASK,
            handler=on_before_task,
            fire_and_forget=False,
            priority=20,
            timeout_ms=1500,
        )
    )

    try:
        from opencomputer.ingestion.bus import default_bus

        subscriber = TraceEmissionSubscriber(
            bus=default_bus,
            profile_home_factory=_profile_home_factory,
            client_factory=_client_factory,
            config_factory=_config_factory,
        )
        subscriber.start()
        # Stash on the api so a future ``unregister`` could call
        # subscriber.stop(). For Phase 5 OC has no formal plugin
        # unload path; the reference is held by the subscription
        # itself so GC won't collect the subscriber.
        setattr(api, "_social_traces_subscriber", subscriber)
        _log.info(
            "social-traces plugin registered (BEFORE_TASK hook + "
            "SessionEndEvent subscriber active)"
        )
    except Exception:  # noqa: BLE001
        _log.warning(
            "social-traces: subscriber start failed — pre-task lookup "
            "still works, post-task emit disabled this session",
            exc_info=True,
        )
