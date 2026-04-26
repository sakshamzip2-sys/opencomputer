"""OpenComputer plugin: self-hosted Honcho memory provider.

Phase 10f.L — register() instantiates ``HonchoSelfHostedProvider`` and
calls ``api.register_memory_provider``. Gracefully degrades on older
cores (pre-10f.G) that don't have the method.

## Deployment model

- We DO NOT vendor Honcho's source code. Honcho is AGPL-3.0;
  vendoring would propagate copyleft.
- The docker-compose bundle (Phase 10f.M) pulls the official image
  from Plastic Labs' registry at install time. Users accept AGPL
  terms by running the pulled container.

## Pinning

The image tag lives in ``IMAGE_VERSION`` next to this file. Update
that file (and run integration tests) before bumping.

## Config

- ``HONCHO_BASE_URL`` (default ``http://localhost:8000``).
- ``HONCHO_API_KEY`` (empty for self-hosted no-auth mode).
- ``HONCHO_WORKSPACE`` (default ``opencomputer``).
- ``HONCHO_HOST_KEY`` (default ``opencomputer``; Phase 14.J sets this
  to ``opencomputer.<profile>`` when a non-default profile is active).
- ``HONCHO_CONTEXT_CADENCE`` / ``HONCHO_DIALECTIC_CADENCE`` — how
  often to prefetch context (default every turn) and fire sync_turn
  (default every 3 turns).
"""

from __future__ import annotations

import os
from typing import Any


def _config_from_env():
    from .provider import HonchoConfig

    def _int(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    # Phase 14.J — host key derives from the active profile unless
    # HONCHO_HOST_KEY is explicitly set. This gives each OpenComputer
    # profile its own Honcho AI peer model; without it, enabling Honcho
    # across profiles muxes all observations into ONE peer and the
    # per-profile-persona promise breaks.
    explicit_host_key = os.environ.get("HONCHO_HOST_KEY", "").strip()
    host_key = explicit_host_key or _derive_host_key_from_profile()

    return HonchoConfig(
        base_url=os.environ.get("HONCHO_BASE_URL", "http://localhost:8000"),
        api_key=os.environ.get("HONCHO_API_KEY", ""),
        workspace=os.environ.get("HONCHO_WORKSPACE", "opencomputer"),
        host_key=host_key,
        context_cadence=_int("HONCHO_CONTEXT_CADENCE", 1),
        dialectic_cadence=_int("HONCHO_DIALECTIC_CADENCE", 3),
    )


def _derive_host_key_from_profile() -> str:
    """Return ``"opencomputer"`` for the default profile, ``"opencomputer.<name>"``
    for a named profile. Falls back to ``"opencomputer"`` on any error so a
    broken sticky file or missing opencomputer package never kills the plugin.
    """
    try:
        from opencomputer.profiles import read_active_profile

        active = read_active_profile()
    except Exception:
        return "opencomputer"
    if active is None or active == "default":
        return "opencomputer"
    return f"opencomputer.{active}"


def register(api: Any) -> None:
    """Register the Honcho memory provider with the plugin API.

    Tolerates older core versions (pre-10f.G) that don't have
    ``register_memory_provider`` yet — logs a warning and skips so the
    agent keeps working on baseline memory.
    """
    # KNOWN BUG (2026-04-26 smoke test): plugin loader uses
    # ``importlib.util.spec_from_file_location`` with a synthetic name,
    # so the ``from .provider`` relative import has no parent package
    # and fails at runtime. Tests pass because conftest pre-registers
    # the package; fresh installs fail. Quick fix attempts via
    # ``module_from_spec`` triggered a separate ``slots=True``
    # incompatibility. Real fix is in the plugin loader (register a
    # synthesised parent package before the entry import). Tracked as
    # a follow-up; honcho memory is opt-in so this isn't a hot blocker.
    from .provider import HonchoSelfHostedProvider

    provider = HonchoSelfHostedProvider(_config_from_env())
    register_fn = getattr(api, "register_memory_provider", None)
    if register_fn is None:
        import logging

        logging.getLogger("memory-honcho").warning(
            "core does not support register_memory_provider; Honcho plugin "
            "installed but inactive. Upgrade OpenComputer to Phase 10f.G+."
        )
        return
    register_fn(provider)
