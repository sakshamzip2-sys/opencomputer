"""GET/POST /api/v1/plugins/* — list, enable/disable, install.

Wraps `opencomputer.cli_plugin` (singular) functions.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["plugins"])


class InstallBody(BaseModel):
    source: str  # URL, git url, or local path


def _list_loaded_plugins() -> list[dict]:
    """Return loaded plugin info via the registry."""
    try:
        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry.instance()
        out: list[dict] = []
        for lp in getattr(reg, "loaded_plugins", []):
            manifest = getattr(lp, "manifest", None) or {}
            out.append(
                {
                    "name": getattr(lp, "name", "") or manifest.get("name", "?"),
                    "version": manifest.get("version", "—"),
                    "kind": manifest.get("kind", "unknown"),
                    "enabled": getattr(lp, "enabled", True),
                    "description": manifest.get("description", ""),
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


def _list_discovered_plugins() -> list[dict]:
    """Return ALL discovered plugins (including disabled), via discovery."""
    try:
        from opencomputer.plugins.discovery import discover_plugins

        candidates = discover_plugins()
        out: list[dict] = []
        for c in candidates:
            m = getattr(c, "manifest", None) or {}
            out.append(
                {
                    "name": getattr(c, "name", None) or m.get("name", "?"),
                    "version": m.get("version", "—"),
                    "kind": m.get("kind", "unknown"),
                    "description": m.get("description", ""),
                    "path": str(getattr(c, "path", "")),
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


@router.get("/plugins")
async def list_plugins() -> dict:
    """Return loaded + discovered plugins."""
    return {
        "items": _list_loaded_plugins(),
        "discovered": _list_discovered_plugins(),
    }


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str) -> dict:
    try:
        from opencomputer.cli_plugin import plugin_enable

        plugin_enable(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"enable failed: {exc}")
    audit_log("plugin.enable", name=name)
    return {"ok": True, "name": name, "enabled": True}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str) -> dict:
    try:
        from opencomputer.cli_plugin import plugin_disable

        plugin_disable(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"disable failed: {exc}")
    audit_log("plugin.disable", name=name)
    return {"ok": True, "name": name, "enabled": False}


@router.post("/plugins/dashboard/install")
async def install_dashboard_plugin(body: InstallBody) -> dict:
    """Install a dashboard-side plugin (drops into dashboard/plugins/<name>/).

    Same backend as plugins/install — the distinction is purely UI: this
    endpoint is what dashboard's PluginsPage calls when the user picks
    'install dashboard plugin' (vs. agent plugin). Routes the source to
    the same installer; downstream sorting by manifest.kind decides
    which UI tab the new plugin shows in.
    """
    return await install_plugin(body)


@router.post("/plugins/install")
async def install_plugin(body: InstallBody) -> dict:
    """Install a plugin from a URL, git url, or local path.

    Wraps cli_plugin.install which is itself a Typer command — we call
    the underlying primitive helpers directly to avoid the Typer
    side-effects (printing, exit-code-on-failure).
    """
    try:
        from opencomputer import cli_plugin

        # cli_plugin.install is a Typer wrapper; the dispatcher chooses
        # local-fs / git / url. Call it via its underlying logic.
        if cli_plugin._is_url_arg(body.source):  # noqa: SLF001
            cli_plugin._install_from_url(body.source)  # noqa: SLF001
        elif cli_plugin._is_git_arg(body.source):  # noqa: SLF001
            cli_plugin._install_from_git(body.source)  # noqa: SLF001
        else:
            from pathlib import Path

            src = Path(body.source).expanduser().resolve()
            if not src.exists():
                raise HTTPException(status_code=400, detail="source path not found")
            cli_plugin._smoke_load_plugin(src)  # noqa: SLF001
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"install failed: {exc}")
    audit_log("plugin.install", source=body.source)
    return {"ok": True, "source": body.source}
