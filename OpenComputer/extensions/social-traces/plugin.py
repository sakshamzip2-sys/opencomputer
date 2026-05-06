"""social-traces plugin — entry module.

Two surfaces:

* :func:`register` — called by the OC plugin loader at boot. Registers
  the ``BEFORE_TASK`` hook (the pre-task lookup path). Does NOT start
  the post-task subscriber — production wiring lives in :func:`wire_subscriber`
  so the caller (gateway or CLI) can supply a real provider + cost
  guard, mirroring how :mod:`extensions.skill_evolution`'s subscriber
  is bootstrapped from :class:`opencomputer.gateway.server.Gateway`.

* :func:`wire_subscriber` — gateway and CLI both call this with a
  resolved provider + cost guard. It constructs and starts the
  :class:`TraceEmissionSubscriber`. Idempotent: subsequent calls
  ``stop()`` the prior subscriber and replace it with a freshly-wired
  one (useful when config changes between calls).

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plugin_sdk.hooks import HookEvent, HookSpec

from . import state as st_state
from .config import SocialTracesConfig, from_config_dict
from .prefetch import on_before_task
from .subscriber import TraceEmissionSubscriber

_log = logging.getLogger("opencomputer.social_traces.plugin")

#: Module-level handle on the currently-running subscriber so
#: :func:`wire_subscriber` can stop a prior one before starting a new
#: one. ``None`` until a caller wires it; never auto-set by
#: :func:`register`. Mirrors how the gateway holds its
#: ``_evolution_subscriber`` attribute, but keeps the reference here
#: so a CLI single-shot path can also wire/unwire without touching
#: gateway internals.
_active_subscriber: TraceEmissionSubscriber | None = None


def _profile_home_factory() -> Path:
    """Lazy resolver for the active profile home — uses
    :func:`state.resolve_profile_home` (plugin_sdk + stdlib only)."""
    return st_state.resolve_profile_home()


def _config_factory(profile_home: Path) -> SocialTracesConfig:
    """Lazy resolver for the parsed ``social_traces:`` config section.

    Re-reads ``config.yaml`` per call so the operator can tune knobs
    (relevance threshold, cost guard, etc.) without a daemon restart.
    Cheap enough at the cadence of session_end firings.
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
    """Lazy resolver for the trace network client. Defers the import
    + construction so the plugin can register cleanly even if the
    http backend's deps are missing — only the actual subscriber path
    that calls submit() reaches the client.

    HMAC credentials precedence: env vars (``OPENHUB_SUBMITTER_HASH``,
    ``OPENHUB_SHARED_KEY``) win over ``config.yaml`` so secrets stay
    out of the YAML by default. Either-or-neither is fine — when
    unset, the http client sends unsigned requests (Stage-1 mode).
    """
    import os

    from .client import make_client

    submitter_hash = os.environ.get("OPENHUB_SUBMITTER_HASH") or cfg.submitter_hash
    shared_key = os.environ.get("OPENHUB_SHARED_KEY") or cfg.shared_key

    return make_client(
        backend=cfg.backend,
        profile_home=profile_home,
        endpoint=cfg.endpoint,
        submitter_hash=submitter_hash or None,
        shared_key=shared_key or None,
    )


def register(api) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Plugin entry. Registers ONLY the BEFORE_TASK hook.

    The post-task subscriber is started separately via
    :func:`wire_subscriber` so the production caller (gateway or CLI)
    can supply a real provider + cost guard. Mirrors
    :mod:`extensions.skill_evolution`'s pattern — its plugin.py is
    also lifecycle-free, with the subscriber started by
    ``Gateway._start_evolution_subscriber``.

    The pre-task hook is purely file-I/O against the local inbox —
    no LLM needed, so it works in every environment as soon as the
    plugin is loaded.
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
    _log.debug(
        "social-traces plugin registered (BEFORE_TASK hook only; "
        "subscriber lifecycle handled by wire_subscriber)"
    )


def wire_subscriber(
    *,
    provider: Any,
    cost_guard: Any,
    sensitive_filter: Callable[[str], bool] | None = None,
    harness_version: str = "",
) -> TraceEmissionSubscriber:
    """Start (or restart) the post-task SessionEndEvent subscriber.

    The gateway's ``_start_traces_subscriber`` and the CLI's
    ``_run_chat_session`` both call this with their resolved provider
    + cost_guard so the LLM judge + distiller actually fire in
    production. Without this call, no trace submissions will ever
    happen — the pre-task lookup path still works (file-I/O only),
    but no agent ever contributes back to the network.

    Idempotent: a prior subscriber (if any) is ``stop()``-ed and
    replaced with a freshly-wired one. Returns the new subscriber so
    callers can hold a reference for shutdown.
    """
    global _active_subscriber  # noqa: PLW0603 — module-level singleton by design

    if _active_subscriber is not None:
        try:
            _active_subscriber.stop()
        except Exception:  # noqa: BLE001 — never let stop() errors block restart
            _log.warning(
                "social-traces: prior subscriber.stop() raised — continuing",
                exc_info=True,
            )

    from opencomputer.ingestion.bus import default_bus

    subscriber = TraceEmissionSubscriber(
        bus=default_bus,
        profile_home_factory=_profile_home_factory,
        client_factory=_client_factory,
        config_factory=_config_factory,
        provider=provider,
        cost_guard=cost_guard,
        sensitive_filter=sensitive_filter,
        harness_version=harness_version,
    )
    subscriber.start()
    _active_subscriber = subscriber
    _log.info(
        "social-traces: subscriber wired (provider=%s harness=%s)",
        type(provider).__name__,
        harness_version or "<unset>",
    )
    return subscriber


def stop_subscriber() -> None:
    """Stop the currently-wired subscriber, if any. Idempotent.

    Called from :meth:`opencomputer.gateway.server.Gateway.stop` (and
    in tests that wire/unwire) so a daemon shutdown drains cleanly.
    """
    global _active_subscriber  # noqa: PLW0603

    if _active_subscriber is None:
        return
    try:
        _active_subscriber.stop()
    except Exception:  # noqa: BLE001
        _log.warning(
            "social-traces: subscriber.stop() raised on shutdown",
            exc_info=True,
        )
    _active_subscriber = None


def get_active_subscriber() -> TraceEmissionSubscriber | None:
    """Return the currently-wired subscriber (or None) — used by tests
    and ``oc traces status`` diagnostics."""
    return _active_subscriber
