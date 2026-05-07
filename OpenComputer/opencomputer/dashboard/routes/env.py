"""GET/PUT/DELETE/POST /api/v1/env/* — env vars (consent-gated reveal).

The reveal endpoint requires an `X-OC-Confirm: yes` header AND emits an
audit log line (key name only — never the value).

The .env file lives at `~/.opencomputer/<profile>/.env`. The dashboard
NEVER writes other env files (no /etc/, no `os.environ`); it only
manages the profile-local one.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["env"])


class PutBody(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: str


def _env_path() -> Path:
    from opencomputer.agent.config import default_config

    return Path(default_config().home) / ".env"


def _read_env() -> dict[str, str]:
    p = _env_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _write_env(items: dict[str, str]) -> None:
    p = _env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(items.items())]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # owner read/write only
    except OSError:
        pass


def _redacted(items: dict[str, str]) -> list[dict]:
    """Return key list with redacted hints — never the values."""
    out = []
    for k, v in sorted(items.items()):
        out.append(
            {
                "key": k,
                "set": bool(v),
                "length": len(v),
                "hint": (v[:3] + "…" + str(len(v)) + " chars") if v else "",
            }
        )
    return out


@router.get("/env")
async def list_env() -> dict:
    """List env keys. NEVER returns values."""
    items = _read_env()
    return {"items": _redacted(items)}


@router.put("/env")
async def put_env(body: PutBody) -> dict:
    items = _read_env()
    items[body.key] = body.value
    _write_env(items)
    audit_log("env.put", key=body.key, value_len=len(body.value))
    return {"ok": True, "key": body.key, "set": True}


@router.delete("/env")
async def delete_env(key: str = Query(..., min_length=1)) -> dict:
    items = _read_env()
    existed = key in items
    items.pop(key, None)
    _write_env(items)
    audit_log("env.delete", key=key)
    return {"ok": True, "key": key, "existed": existed}


@router.post("/env/reveal")
async def reveal_env(
    body: PutBody | None = None,
    key: str | None = Query(None),
    x_oc_confirm: str | None = Header(None),  # → 'x-oc-confirm' (case-insensitive)
) -> dict:
    """Return a single env value. REQUIRES `X-OC-Confirm: yes` header
    AND emits an audit log entry (key name only). Loopback-only safe."""
    if x_oc_confirm != "yes":
        raise HTTPException(
            status_code=403,
            detail="reveal requires X-OC-Confirm: yes header",
        )
    target_key = (body.key if body else None) or key
    if not target_key:
        raise HTTPException(status_code=400, detail="missing key")
    items = _read_env()
    if target_key not in items:
        raise HTTPException(status_code=404, detail="key not found")
    audit_log("env.reveal", key=target_key)
    return {"key": target_key, "value": items[target_key]}
