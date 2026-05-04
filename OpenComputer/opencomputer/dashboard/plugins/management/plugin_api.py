"""Plugins-management dashboard plugin — backend API routes (Wave 6.D).

Mounted by :mod:`opencomputer.dashboard.server` at
``/api/plugins/management/``. Read-mostly: lists every plugin candidate
on the search path with manifest metadata + enabled/disabled status from
the active profile's ``profile.yaml``.

Why this is read-mostly: enable/disable from the dashboard requires
write access to ``profile.yaml`` on a host that is presumed to be
single-user-localhost — when the dashboard binds to ``0.0.0.0``, those
writes become a remote-config-modify primitive. We defer the mutation
endpoints until consent UX is wired (see follow-up at
``docs/superpowers/specs/`` once OC adds an ``--insecure`` write-token
gate). For now the user toggles plugins via ``oc plugin enable/disable``
on the CLI, and the dashboard simply reflects state.

Hermes ref: ``e2a490560 feat(dashboard): add Plugins page with
enable/disable, auth status, install/remove`` — we ship the listing +
auth-status half of that change.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter

from opencomputer.agent.profile_config import (
    ProfileConfigError,
    load_profile_config,
)
from opencomputer.plugins.discovery import discover, standard_search_paths

log = logging.getLogger(__name__)

router = APIRouter()


def _resolved_enabled_set() -> set[str] | str:
    """Return enabled plugin ids for the active profile.

    Returns:
        - ``"*"`` (string) when the profile allows all plugins
        - a ``set[str]`` of explicit ids otherwise
        - ``"*"`` if profile.yaml is malformed (fail-open for read view)
    """
    from opencomputer.agent.config import _home

    try:
        cfg = load_profile_config(_home())
    except ProfileConfigError as exc:
        log.warning("profile.yaml malformed; falling back to '*': %s", exc)
        return "*"
    if cfg.enabled_plugins == "*":
        return "*"
    return set(cfg.enabled_plugins)


def _provider_auth_status(env_vars: tuple[str, ...]) -> str:
    """Return one of ``configured``/``missing``/``unused``.

    ``configured`` — every declared env var is set (non-empty)
    ``missing``    — at least one declared env var is unset/empty
    ``unused``     — manifest declares no env vars (no auth needed)
    """
    if not env_vars:
        return "unused"
    for name in env_vars:
        if not os.environ.get(name):
            return "missing"
    return "configured"


@router.get("/list")
async def list_plugins() -> dict[str, Any]:
    """List every discovered plugin with manifest metadata + status.

    Response shape::

        {
          "active_profile": "default",
          "plugins": [
            {
              "id": "kanban",
              "name": "Kanban",
              "version": "1.0.0",
              "kind": "tool",
              "description": "...",
              "enabled": true,
              "auth_status": "configured" | "missing" | "unused",
              "env_vars": ["KEY1","KEY2"],
              "source_root": "/path/to/extensions/kanban"
            },
            ...
          ]
        }
    """
    candidates = discover(standard_search_paths())
    enabled = _resolved_enabled_set()
    enabled_all = enabled == "*"

    from opencomputer.profiles import read_active_profile

    plugins_out: list[dict[str, Any]] = []
    for cand in sorted(candidates, key=lambda c: c.manifest.id):
        m = cand.manifest
        is_enabled = enabled_all or (
            isinstance(enabled, set) and m.id in enabled
        )

        env_vars: tuple[str, ...] = ()
        if m.setup is not None:
            for prov in m.setup.providers:
                env_vars = prov.env_vars
                break

        plugins_out.append({
            "id": m.id,
            "name": m.name,
            "version": getattr(m, "version", "0.0.0"),
            "kind": getattr(m, "kind", ""),
            "description": getattr(m, "description", ""),
            "enabled": is_enabled,
            "auth_status": _provider_auth_status(env_vars),
            "env_vars": list(env_vars),
            "source_root": str(cand.root_dir),
        })

    return {
        "active_profile": read_active_profile() or "default",
        "plugins": plugins_out,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    """Quick status — count of plugins seen on the search path."""
    candidates = discover(standard_search_paths())
    return {"ok": True, "count": len(candidates)}
