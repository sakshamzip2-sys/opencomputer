"""GET/POST /api/v1/models/* — list providers, set default.

Wraps `opencomputer.cli_model_picker._grouped_models()` to enumerate
provider→model combinations, and `cli_models.models_list/models_add` for
the underlying registry.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["models"])


class SetModelBody(BaseModel):
    provider: str
    model: str


@router.get("/models")
async def list_models() -> dict:
    """List provider → models groupings."""
    try:
        from opencomputer import cli_model_picker

        grouped = cli_model_picker._grouped_models()  # noqa: SLF001 — read-only dashboard use
        providers = [
            {"provider": prov, "models": sorted(models)}
            for prov, models in grouped.items()
        ]
        return {"providers": providers}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"models registry unavailable: {exc}")


@router.get("/models/info")
async def model_info() -> dict:
    """Return the currently bound default model + provider."""
    from opencomputer.agent.config import default_config

    cfg = default_config()
    return {
        "provider": getattr(cfg.model, "provider", None),
        "model": getattr(cfg.model, "model", None),
    }


@router.get("/models/auxiliary")
async def auxiliary_model_info() -> dict:
    """Return the auxiliary (cheap-route) model info if configured."""
    from opencomputer.agent.config import default_config

    cfg = default_config()
    aux = getattr(cfg.model, "auxiliary", None) or getattr(cfg, "auxiliary_model", None)
    if not aux:
        return {"provider": None, "model": None}
    return {
        "provider": getattr(aux, "provider", None),
        "model": getattr(aux, "model", None),
    }


@router.post("/models/set")
async def set_model(body: SetModelBody) -> dict:
    """Set the default model. Persists to the active profile config."""
    try:
        from opencomputer.agent.config_store import load_config, save_config

        cfg = load_config()
        # cfg.model is a dataclass; rebuild with new provider/model
        if hasattr(cfg.model, "provider"):
            cfg.model.provider = body.provider
        if hasattr(cfg.model, "model"):
            cfg.model.model = body.model
        save_config(cfg)
        return {"ok": True, "provider": body.provider, "model": body.model}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"set failed: {exc}")
