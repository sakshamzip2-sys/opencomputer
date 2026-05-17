"""One helper to register every built-in injection provider per surface.

OC has four built-in :class:`~plugin_sdk.injection.DynamicInjectionProvider`
implementations that contribute per-turn text to the system prompt:

================================  ==================  =========================
Provider                          ``provider_id``     Module
================================  ==================  =========================
``ThinkingInjector``               thinking_tags_fb*   ``agent.thinking_injector``
``PathGlobRulesProvider``          path_glob_rules     ``agent.path_rules_injection``
``HandoffInjectionProvider``       handoff_inbox       ``agent.handoff``
``LifeEventInjectionProvider``     life_event_hint     ``awareness.life_events``
================================  ==================  =========================

\\* ``thinking_tags_fallback``

Historically each surface registered these ad hoc. The CLI
(``cli.py::_run_chat_session``) registered all four; every other surface
(gateway, wire, webui) registered only the life-event one. That left
``ThinkingInjector``, ``PathGlobRulesProvider`` and
``HandoffInjectionProvider`` CLI-only — even though
``HandoffInjectionProvider``'s own docstring claims it "Applies to ALL
surfaces". This module collapses the four registrations into ONE
surface-parameterised helper so every surface has a single call site and
the gap cannot reopen.

The handoff provider is the delicate one. ``HandoffInjectionProvider``
takes a ``profile_home_resolver`` callable and on every turn builds a
:class:`~opencomputer.agent.handoff.inbox.HandoffInbox` from the resolved
path, then calls ``read_and_process_all()`` — which is **destructive**
(it ``os.replace``-archives every pending handoff into ``inbox/processed/``).
So the resolver MUST point at the EXACT directory the handoff *writer*
uses, or a surface would read + archive the wrong profile's inbox.

Two resolvers, picked by ``surface``:

* **CLI** keeps the legacy sticky-active-profile resolver:
  ``get_profile_dir(read_active_profile()) / "home"``. This honours a
  mid-session ``/handoff`` profile swap (the CLI rewrites the sticky
  ``active_profile`` marker on swap).
* **Every other surface** uses a resolver based on
  :func:`opencomputer.agent.config._home`, which honours the
  ``current_profile_home`` ContextVar the gateway's per-dispatch
  ``set_profile()`` sets — so concurrent dispatches each route to their
  own profile.

Both must yield the IDENTICAL directory for a given profile.
``_home()`` returns the profile *root* (the ContextVar value, or the
``OPENCOMPUTER_HOME`` env / ``~/.opencomputer`` fallback — it never
appends ``home/``). The CLI resolver and the handoff writer
(``loop.py::_resolve_target_home`` and ``orchestrator.py``) both use
``get_profile_dir(profile) / "home"``. So the non-CLI resolver is
``_home() / "home"`` — yielding the same ``<profile_root>/home``
directory ``HandoffInbox`` then suffixes with ``inbox/``.

Every provider is registered inside its OWN ``try``/``except`` logging a
WARNING on failure: one provider failing to construct must not block the
other three. Imports are lazy (inside the function) to keep this module
import-cheap and avoid import-time cycles.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def register_default_injection_providers(surface: str) -> None:
    """Idempotently register OC's built-in injection providers for ``surface``.

    Registers four providers — ``ThinkingInjector``,
    ``PathGlobRulesProvider``, ``HandoffInjectionProvider`` and
    ``LifeEventInjectionProvider`` — on the process-wide injection engine
    (:data:`opencomputer.agent.injection.engine`). Each provider does an
    ``unregister`` before ``register`` so calling this twice replaces
    rather than collides (the engine raises on a duplicate
    ``provider_id``).

    ``surface`` is the surface name the calling code serves — ``"cli"``,
    ``"gateway"``, ``"wire"`` or ``"webui"``. It selects the
    handoff-provider's profile-home resolver (CLI keeps the
    sticky-active-profile resolver; every other surface uses a
    ContextVar-aware ``_home()``-based resolver) and is threaded into
    :class:`~opencomputer.awareness.life_events.injection.LifeEventInjectionProvider`.

    Each of the four registrations is wrapped in its own
    ``try``/``except`` logging a WARNING on failure — one provider
    failing to construct or register must never block the others or
    break a surface's boot.
    """
    # --- 1. ThinkingInjector -------------------------------------------------
    # <think>-tag fallback for providers without native thinking. Zero-arg,
    # unconditional, every surface.
    try:
        from opencomputer.agent.injection import engine
        from opencomputer.agent.thinking_injector import ThinkingInjector

        engine.unregister("thinking_tags_fallback")
        engine.register(ThinkingInjector())
    except Exception:  # noqa: BLE001 - one provider failing must not block others
        _log.warning(
            "failed to register ThinkingInjector for surface %r",
            surface,
            exc_info=True,
        )

    # --- 2. PathGlobRulesProvider --------------------------------------------
    # Fires .opencomputer/rules/*.md after path-touching tool calls. An empty
    # rules list leaves the provider registered as a cheap per-turn no-op.
    # Unconditional, every surface.
    try:
        from opencomputer.agent.injection import engine
        from opencomputer.agent.path_rules_injection import (
            PathGlobRulesProvider,
            load_rules_for_active_profile,
        )

        engine.unregister("path_glob_rules")
        engine.register(
            PathGlobRulesProvider(rules=load_rules_for_active_profile())
        )
    except Exception:  # noqa: BLE001 - one provider failing must not block others
        _log.warning(
            "failed to register PathGlobRulesProvider for surface %r",
            surface,
            exc_info=True,
        )

    # --- 3. HandoffInjectionProvider -----------------------------------------
    # Surfaces pending profile handoffs. The resolver MUST be surface-correct:
    # HandoffInjectionProvider.collect() builds HandoffInbox(<resolved path>)
    # and calls read_and_process_all(), which DESTRUCTIVELY archives every
    # pending handoff. A resolver pointing at the wrong directory would make
    # the surface read + archive another profile's inbox.
    #
    #   * CLI    -> sticky-active-profile resolver:
    #              get_profile_dir(read_active_profile()) / "home".
    #              Honours a mid-session /handoff profile swap (the CLI
    #              rewrites the sticky active_profile marker on swap).
    #   * other  -> _home()-based resolver. _home() returns the profile
    #              ROOT (ContextVar value / OPENCOMPUTER_HOME env / default;
    #              it never appends "home/"). The CLI resolver and the
    #              handoff writer both use <profile_root>/home, so the
    #              non-CLI resolver is `_home() / "home"` — the IDENTICAL
    #              directory, honouring the current_profile_home ContextVar
    #              the gateway's per-dispatch set_profile() sets.
    try:
        from pathlib import Path

        from opencomputer.agent.handoff import HandoffInjectionProvider
        from opencomputer.agent.injection import engine

        if surface == "cli":

            def _resolver() -> object:
                from opencomputer.profiles import (
                    get_profile_dir,
                    read_active_profile,
                )

                active = read_active_profile()
                root = get_profile_dir(active)
                return Path(root) / "home"

        else:

            def _resolver() -> object:
                from opencomputer.agent.config import _home

                return _home() / "home"

        engine.unregister("handoff_inbox")
        engine.register(
            HandoffInjectionProvider(profile_home_resolver=_resolver)
        )
    except Exception:  # noqa: BLE001 - one provider failing must not block others
        _log.warning(
            "failed to register HandoffInjectionProvider for surface %r",
            surface,
            exc_info=True,
        )

    # --- 4. LifeEventInjectionProvider ---------------------------------------
    # Delegate to the existing surface-parameterised helper — it does its own
    # idempotent (re)registration and fail-soft logging.
    try:
        from opencomputer.awareness.life_events.injection import (
            register_life_event_injection_provider,
        )

        register_life_event_injection_provider(surface)
    except Exception:  # noqa: BLE001 - one provider failing must not block others
        _log.warning(
            "failed to register LifeEventInjectionProvider for surface %r",
            surface,
            exc_info=True,
        )


__all__ = ["register_default_injection_providers"]
