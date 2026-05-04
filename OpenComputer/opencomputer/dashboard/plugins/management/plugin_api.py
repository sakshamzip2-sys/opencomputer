"""Plugins-management dashboard plugin — backend API routes (Wave 6.D + 6.D-α).

Mounted by :mod:`opencomputer.dashboard.server` at
``/api/plugins/management/``. Combines the read endpoints (PR #430) with
the Wave 6.D-α mutation endpoints: enable/disable + set-preset.

Mutation endpoints are gated by the dashboard session token (see
``opencomputer.dashboard._auth.require_session_token``). On default
localhost binding the gate is best-effort (the loopback peer check
already restricts WS reach); on ``--insecure`` binds the token is the
authoritative gate, just like for ``/api/pty``.

Hermes ref: ``e2a490560 feat(dashboard): add Plugins page with
enable/disable, auth status, install/remove``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from opencomputer.agent.profile_config import (
    ProfileConfigError,
    load_profile_config,
    profile_config_path,
)
from opencomputer.agent.profile_yaml import modify_yaml_locked
from opencomputer.dashboard._auth import require_session_token
from opencomputer.plugins.discovery import discover, standard_search_paths
from opencomputer.plugins.preset import load_preset

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


# ---------------------------------------------------------------------------
# Wave 6.D-α — mutation endpoints (token-gated)
# ---------------------------------------------------------------------------


class _SetPresetBody(BaseModel):
    preset: str


def _resolve_preset_to_inline(preset_name: str) -> list[str]:
    """Expand a preset reference into its concrete plugin id list.

    Used when the user clicks enable/disable while the active profile
    references a preset — we have to inline the list before mutating
    individual ids (you can't subtract from a preset by reference).

    Raises HTTPException 400 if the preset doesn't exist.
    """
    try:
        preset = load_preset(preset_name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"profile references preset '{preset_name}' but it cannot be loaded: {exc}",
        ) from exc
    return list(preset.plugins)


def _validate_plugin_id(plugin_id: str) -> str:
    """Verify ``plugin_id`` exists on the search path. Returns the id.

    Raises HTTPException 404 if unknown — prevents the dashboard from
    persisting typos into profile.yaml.
    """
    candidates = discover(standard_search_paths())
    for cand in candidates:
        if cand.manifest.id == plugin_id:
            return plugin_id
    raise HTTPException(
        status_code=404, detail=f"unknown plugin id: {plugin_id}",
    )


def _profile_yaml_path():
    """Path to the active profile's profile.yaml."""
    from opencomputer.agent.config import _home

    return profile_config_path(_home())


@router.post("/{plugin_id}/enable", dependencies=[Depends(require_session_token)])
async def enable_plugin(plugin_id: str) -> dict[str, Any]:
    """Add ``plugin_id`` to the active profile's plugins.enabled list.

    If the profile currently uses ``preset: <name>``, the preset is
    inlined first (response includes ``preset_dropped: true``). Locked
    via ``filelock`` against concurrent mutations.

    For wildcard state (default — everything allowed), the request is
    materialized: enable inline-expands the wildcard so the explicit
    list contains ``plugin_id`` plus everything else currently
    discovered. This makes the user's intent ("yes, this one in
    particular") explicit + survives discovery changes.
    """
    _validate_plugin_id(plugin_id)
    yaml_path = _profile_yaml_path()
    preset_dropped = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal preset_dropped
        # If a preset was set, inline it first
        if data.get("preset"):
            inline = _resolve_preset_to_inline(data["preset"])
            data.pop("preset", None)
            data.setdefault("plugins", {})["enabled"] = inline
            preset_dropped = True

        plugins_block = data.setdefault("plugins", {})
        enabled = plugins_block.get("enabled", "*")
        if enabled == "*":
            # Materialize the wildcard so the explicit ack of plugin_id
            # is recorded. The list reflects the discovered set + the
            # newly enabled id (always present even if discovery later
            # changes).
            cands = discover(standard_search_paths())
            inline = sorted({c.manifest.id for c in cands} | {plugin_id})
            plugins_block["enabled"] = inline
            return
        if not isinstance(enabled, list):
            raise HTTPException(
                status_code=409,
                detail=f"profile.yaml plugins.enabled is malformed (got {type(enabled).__name__})",
            )
        if plugin_id not in enabled:
            enabled.append(plugin_id)
        plugins_block["enabled"] = enabled

    modify_yaml_locked(yaml_path, _mutate)
    return {
        "ok": True,
        "plugin_id": plugin_id,
        "preset_dropped": preset_dropped,
        "action": "enable",
    }


@router.post("/{plugin_id}/disable", dependencies=[Depends(require_session_token)])
async def disable_plugin(plugin_id: str) -> dict[str, Any]:
    """Remove ``plugin_id`` from plugins.enabled.

    Same preset-inlining logic as enable. **Disable does NOT validate
    the plugin id** — a stale id from an uninstalled plugin should
    always be removable from profile.yaml. (Enable validates because
    we don't want typos to silently land in the file.)
    """
    yaml_path = _profile_yaml_path()
    preset_dropped = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal preset_dropped
        if data.get("preset"):
            inline = _resolve_preset_to_inline(data["preset"])
            data.pop("preset", None)
            data.setdefault("plugins", {})["enabled"] = [
                pid for pid in inline if pid != plugin_id
            ]
            preset_dropped = True
            return

        plugins_block = data.setdefault("plugins", {})
        enabled = plugins_block.get("enabled", "*")
        if enabled == "*":
            # User wants to disable from a wildcard list — must inline.
            # Build the full list first then remove.
            cands = discover(standard_search_paths())
            inline = [c.manifest.id for c in cands if c.manifest.id != plugin_id]
            plugins_block["enabled"] = inline
            return
        if isinstance(enabled, list) and plugin_id in enabled:
            enabled.remove(plugin_id)
            plugins_block["enabled"] = enabled

    modify_yaml_locked(yaml_path, _mutate)
    return {
        "ok": True,
        "plugin_id": plugin_id,
        "preset_dropped": preset_dropped,
        "action": "disable",
    }


@router.post("/set-preset", dependencies=[Depends(require_session_token)])
async def set_preset(body: _SetPresetBody) -> dict[str, Any]:
    """Switch the active profile to ``preset: <name>``.

    Replaces any inline ``plugins.enabled`` list. Validates that the
    preset exists before writing.
    """
    # Validate first — load_preset raises FileNotFoundError if absent.
    _resolve_preset_to_inline(body.preset)
    yaml_path = _profile_yaml_path()

    def _mutate(data: dict[str, Any]) -> None:
        data["preset"] = body.preset
        # Remove inline list to avoid the "both set" error
        if "plugins" in data and isinstance(data["plugins"], dict):
            data["plugins"].pop("enabled", None)
            if not data["plugins"]:
                data.pop("plugins", None)

    modify_yaml_locked(yaml_path, _mutate)
    return {"ok": True, "preset": body.preset, "action": "set-preset"}
