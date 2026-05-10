"""GET/PUT /api/v1/dashboard/* — themes + dashboard plugin metadata + docs."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["dashboard-meta"])


class ThemeBody(BaseModel):
    name: str


# Source of truth: the THEMES object in static/_themes.js. Keep aligned —
# tests/test_dashboard_themes_alignment.py regression-locks the match.
_THEMES = ["dark", "light", "solarized", "monokai"]


@router.get("/dashboard/themes")
async def list_themes() -> dict:
    """List available dashboard themes.

    The ``active`` field reflects the server-side default. Actual
    persistence is client-side (``localStorage["oc-dashboard-theme"]``,
    set by ``static/_themes.js``); the server has no concept of a
    per-user active theme.
    """
    return {"items": [{"name": t} for t in _THEMES], "active": _THEMES[0]}


@router.put("/dashboard/theme")
async def set_theme(body: ThemeBody) -> dict:
    if body.name not in _THEMES:
        raise HTTPException(status_code=400, detail="unknown theme")
    return {"ok": True, "active": body.name}


@router.get("/dashboard/plugins")
async def dashboard_plugins() -> dict:
    """List dashboard-side plugin tabs (from dashboard/plugins/ dir)."""
    try:
        from opencomputer.dashboard.server import _PLUGINS_DIR

        out = []
        if _PLUGINS_DIR.exists():
            for child in sorted(_PLUGINS_DIR.iterdir()):
                if not child.is_dir() or child.name.startswith(("_", ".")):
                    continue
                api_file = child / "plugin_api.py"
                if not api_file.exists():
                    continue
                out.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "has_dist": (child / "dist").exists(),
                    }
                )
        return {"items": out}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"plugins dir unreadable: {exc}")


# ---------- Docs (rendered markdown) ----------

_DOC_BASE: Path = Path(__file__).resolve().parents[3]  # OpenComputer/


@router.get("/dashboard/docs")
async def list_docs() -> dict:
    """Enumerate the docs the dashboard can render in DocsPage."""
    candidates = [
        ("readme", _DOC_BASE / "README.md", "README"),
        ("claude", _DOC_BASE / "CLAUDE.md", "CLAUDE.md"),
        ("agents", _DOC_BASE / "AGENTS.md", "AGENTS.md"),
        ("changelog", _DOC_BASE / "CHANGELOG.md", "CHANGELOG"),
        ("release", _DOC_BASE / "RELEASE.md", "Release notes"),
    ]
    items = []
    for slug, path, title in candidates:
        if path.exists():
            items.append({"slug": slug, "title": title, "size": path.stat().st_size})
    return {"items": items}


@router.get("/dashboard/docs/{slug}")
async def get_doc(slug: str) -> dict:
    docs = {
        "readme": _DOC_BASE / "README.md",
        "claude": _DOC_BASE / "CLAUDE.md",
        "agents": _DOC_BASE / "AGENTS.md",
        "changelog": _DOC_BASE / "CHANGELOG.md",
        "release": _DOC_BASE / "RELEASE.md",
    }
    p = docs.get(slug)
    if not p or not p.exists():
        raise HTTPException(status_code=404, detail="doc not found")
    return {"slug": slug, "path": str(p), "text": p.read_text(encoding="utf-8")}
