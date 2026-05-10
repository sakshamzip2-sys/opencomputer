"""GET/PUT /api/v1/skills/* — list installed skills, toggle, browse hub.

Wraps :mod:`opencomputer.cli_skills_hub` for browse/install and
:mod:`opencomputer.cli_skills` for the local installed-skills listing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["skills"])


class ToggleBody(BaseModel):
    name: str
    enabled: bool


@router.get("/skills")
async def list_skills() -> dict:
    """List installed skills for the active profile."""
    try:
        from opencomputer.skills_hub.installed import list_installed_skills

        items = list_installed_skills()
        return {
            "items": [
                {
                    "name": getattr(s, "name", str(s)),
                    "version": getattr(s, "version", "—"),
                    "description": getattr(s, "description", ""),
                    "enabled": getattr(s, "enabled", True),
                    "path": str(getattr(s, "path", "")),
                }
                for s in items
            ]
        }
    except (ImportError, AttributeError):
        # Fallback: scan profile skills/ dir directly
        from pathlib import Path

        from opencomputer.agent.config import default_config

        skills_dir = Path(default_config().home) / "skills"
        if not skills_dir.exists():
            return {"items": []}
        items = []
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            items.append(
                {
                    "name": child.name,
                    "version": "—",
                    "description": "",
                    "enabled": True,
                    "path": str(child),
                }
            )
        return {"items": items}


@router.get("/skills/search")
async def search_skills(q: str = Query(..., min_length=1)) -> dict:
    """Search the skills hub."""
    try:
        from opencomputer import cli_skills_hub

        # cli_skills_hub.do_search prints; use the underlying router directly.
        router_obj = cli_skills_hub._build_router()  # noqa: SLF001
        results = router_obj.search(q, limit=20)
        return {
            "items": [
                {
                    "id": getattr(r, "identifier", ""),
                    "name": getattr(r, "name", ""),
                    "description": getattr(r, "description", ""),
                    "source": getattr(r, "source", ""),
                }
                for r in (results or [])
            ]
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"hub unavailable: {exc}")


@router.put("/skills/toggle")
async def toggle_skill(body: ToggleBody) -> dict:
    """Toggle a skill on/off (writes profile config)."""
    try:
        from opencomputer.agent.config_store import load_config, save_config

        cfg = load_config()
        # config has skills_disabled list; toggle membership
        disabled = list(getattr(cfg, "skills_disabled", []))
        if body.enabled and body.name in disabled:
            disabled.remove(body.name)
        elif not body.enabled and body.name not in disabled:
            disabled.append(body.name)
        if hasattr(cfg, "skills_disabled"):
            cfg.skills_disabled = disabled
        save_config(cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"toggle failed: {exc}")
    return {"ok": True, "name": body.name, "enabled": body.enabled}
