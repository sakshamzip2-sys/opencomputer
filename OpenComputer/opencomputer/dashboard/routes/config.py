"""GET/PUT /api/v1/config/* — config edit (with .bak).

Mirrors `oc config edit` semantics. The PUT endpoint validates the
new YAML round-trips through `load_config` before swapping in (any
parse failure rolls back; never leaves a half-written config.yaml).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["config"])


class RawBody(BaseModel):
    text: str


def _config_path() -> Path:
    from opencomputer.agent.config import default_config

    cfg = default_config()
    return Path(cfg.home) / "config.yaml"


@router.get("/config")
async def get_config() -> dict:
    """Return the resolved config as a dict."""
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config()
        # Convert dataclass to dict via vars() / asdict
        from dataclasses import asdict, is_dataclass

        if is_dataclass(cfg):
            return asdict(cfg)
        return dict(getattr(cfg, "__dict__", {}))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"config load failed: {exc}")


@router.get("/config/raw")
async def get_config_raw() -> dict:
    p = _config_path()
    if not p.exists():
        return {"path": str(p), "text": ""}
    return {"path": str(p), "text": p.read_text(encoding="utf-8")}


@router.put("/config/raw")
async def put_config_raw(body: RawBody) -> dict:
    """Write raw YAML. Backs up the previous file as .bak; rolls back if
    the new content fails to round-trip via `load_config`."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = p.with_suffix(".yaml.bak")
    if p.exists():
        shutil.copy2(p, bak)
    p.write_text(body.text, encoding="utf-8")

    # Validate: round-trip through load_config; on failure restore bak.
    try:
        from opencomputer.agent.config_store import load_config

        load_config()
    except Exception as exc:  # noqa: BLE001
        if bak.exists():
            shutil.copy2(bak, p)
        raise HTTPException(
            status_code=400,
            detail=f"config invalid (rolled back): {exc}",
        )
    audit_log("config.raw_write", bytes=len(body.text))
    return {"ok": True, "path": str(p), "backup": str(bak) if bak.exists() else None}


@router.get("/config/defaults")
async def get_config_defaults() -> dict:
    """Return the dataclass defaults for each section."""
    try:
        from dataclasses import asdict

        from opencomputer.agent.config import (  # type: ignore[attr-defined]
            LoopConfig,
            MemoryConfig,
            ModelConfig,
            SessionConfig,
        )

        return {
            "model": asdict(ModelConfig()),
            "loop": asdict(LoopConfig()),
            "session": asdict(SessionConfig()),
            "memory": asdict(MemoryConfig()),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"defaults unavailable: {exc}")


@router.get("/config/schema")
async def get_config_schema() -> dict:
    """Return a minimal JSON schema describing the config shape."""
    try:
        from dataclasses import fields

        from opencomputer.agent.config import (  # type: ignore[attr-defined]
            LoopConfig,
            MemoryConfig,
            ModelConfig,
            SessionConfig,
        )

        def fields_of(c) -> list[dict]:
            return [
                {"name": f.name, "type": str(f.type), "default": str(f.default)}
                for f in fields(c)
            ]

        return {
            "model": fields_of(ModelConfig),
            "loop": fields_of(LoopConfig),
            "session": fields_of(SessionConfig),
            "memory": fields_of(MemoryConfig),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"schema unavailable: {exc}")
