"""GET/POST/DELETE /api/v1/profiles/* — profile management.

Wraps :mod:`opencomputer.profiles` (list_profiles / create_profile /
delete_profile / rename_profile / get_profile_dir / read_active_profile /
write_active_profile).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["profiles"])


class CreateProfileBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class RenameProfileBody(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=64)


class SetActiveBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


@router.get("/profiles")
async def list_profiles_route() -> dict:
    """List all profiles with their dirs + active status."""
    from opencomputer.profiles import (
        get_profile_dir,
        list_profiles,
        read_active_profile,
    )

    active = read_active_profile()
    items = []
    for name in list_profiles():
        try:
            d = get_profile_dir(name)
        except Exception:  # noqa: BLE001
            d = None
        items.append(
            {
                "name": name,
                "dir": str(d) if d else None,
                "active": name == active,
            }
        )
    return {"items": items, "active": active}


@router.post("/profiles", status_code=201)
async def create_profile_route(body: CreateProfileBody) -> dict:
    from opencomputer.profiles import (
        ProfileExistsError,
        ProfileNameError,
        create_profile,
    )

    try:
        d = create_profile(body.name)
    except ProfileExistsError:
        raise HTTPException(status_code=409, detail="profile already exists")
    except ProfileNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_log("profile.create", name=body.name)
    return {"ok": True, "name": body.name, "dir": str(d)}


@router.delete("/profiles/{name}")
async def delete_profile_route(name: str) -> dict:
    from opencomputer.profiles import ProfileNotFoundError, delete_profile

    try:
        delete_profile(name)
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    audit_log("profile.delete", name=name)
    return {"ok": True, "name": name}


@router.post("/profiles/{name}/rename")
async def rename_profile_route(name: str, body: RenameProfileBody) -> dict:
    from opencomputer.profiles import (
        ProfileExistsError,
        ProfileNameError,
        ProfileNotFoundError,
        rename_profile,
    )

    try:
        rename_profile(name, body.new_name)
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    except ProfileExistsError:
        raise HTTPException(status_code=409, detail="target name already exists")
    except ProfileNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_log("profile.rename", old=name, new=body.new_name)
    return {"ok": True, "old": name, "new": body.new_name}


@router.post("/profiles/active")
async def set_active_route(body: SetActiveBody) -> dict:
    from opencomputer.profiles import (
        ProfileNotFoundError,
        list_profiles,
        write_active_profile,
    )

    if body.name not in list_profiles():
        raise HTTPException(status_code=404, detail="profile not found")
    try:
        write_active_profile(body.name)
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    audit_log("profile.set_active", name=body.name)
    return {"ok": True, "active": body.name}


@router.get("/profiles/{name}/setup-command")
async def setup_command_route(name: str) -> dict:
    """Return the shell snippet that activates this profile."""
    from opencomputer.profiles import list_profiles

    if name not in list_profiles():
        raise HTTPException(status_code=404, detail="profile not found")
    return {
        "name": name,
        "shell": f'export OPENCOMPUTER_PROFILE={name}',
        "alias_eval": f'eval "$(oc setup --profile {name})"',
    }
