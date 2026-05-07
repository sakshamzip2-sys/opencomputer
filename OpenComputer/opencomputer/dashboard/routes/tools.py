"""GET /api/v1/tools/* — toolset enumeration."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["tools"])


@router.get("/tools/toolsets")
async def list_toolsets() -> dict:
    """Return registered tools."""
    try:
        from opencomputer.tools.registry import registry

        items: list[dict] = []
        for name, tool in registry._tools.items():  # noqa: SLF001 — dashboard read-only
            try:
                schema = tool.schema  # property, not method
            except Exception:  # noqa: BLE001
                schema = None
            desc = (getattr(schema, "description", "") or "") if schema else ""
            items.append(
                {
                    "name": name,
                    "description": desc[:200],
                    "parallel_safe": bool(getattr(tool, "parallel_safe", True)),
                    "strict": bool(getattr(tool, "strict_mode", False)),
                }
            )
        return {"items": sorted(items, key=lambda d: d["name"])}
    except Exception as exc:  # noqa: BLE001
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=f"tool registry unavailable: {exc}")
