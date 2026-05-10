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


@router.post("/profiles/{name}/open-terminal")
async def open_terminal_route(name: str) -> dict:
    """Spawn an external terminal pinned to this profile.

    macOS: ``open -a Terminal --env OPENCOMPUTER_PROFILE=<name>``.
    Linux: ``x-terminal-emulator`` if available, falls back to ``gnome-terminal``.
    Windows: ``wt`` (Windows Terminal) if available, else ``cmd``.

    Loopback-public; spawns a shell process owned by the same user as
    the dashboard. NOT a remote-exec surface — the API contract is
    "open a terminal here, locally."
    """
    import shutil
    import subprocess
    import sys

    from opencomputer.profiles import list_profiles

    if name not in list_profiles():
        raise HTTPException(status_code=404, detail="profile not found")

    env_setup = f"export OPENCOMPUTER_PROFILE={name}"
    audit_log("profile.open_terminal", name=name, platform=sys.platform)

    try:
        if sys.platform == "darwin":
            applescript = (
                f'tell application "Terminal" to do script "{env_setup}; oc"'
            )
            subprocess.Popen(["osascript", "-e", applescript])
        elif sys.platform == "win32":
            cmd = shutil.which("wt") or shutil.which("cmd")
            if not cmd:
                raise HTTPException(status_code=503, detail="no terminal binary found")
            subprocess.Popen([cmd, "/k", f"set OPENCOMPUTER_PROFILE={name} && oc"])
        else:
            cmd = (
                shutil.which("x-terminal-emulator")
                or shutil.which("gnome-terminal")
                or shutil.which("xterm")
            )
            if not cmd:
                raise HTTPException(status_code=503, detail="no terminal binary found")
            subprocess.Popen([cmd, "-e", f"bash -c '{env_setup}; oc; exec bash'"])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"spawn failed: {exc}")
    return {"ok": True, "profile": name, "platform": sys.platform}


@router.get("/profiles/{name}/persona")
async def get_persona_route(name: str) -> dict:
    """Read the active persona for a profile."""
    from opencomputer.profiles import get_profile_dir, list_profiles

    if name not in list_profiles():
        raise HTTPException(status_code=404, detail="profile not found")
    try:
        d = get_profile_dir(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc))
    persona_file = d / "persona.txt"
    if persona_file.exists():
        return {"profile": name, "persona": persona_file.read_text(encoding="utf-8").strip()}
    return {"profile": name, "persona": ""}


class PersonaBody(BaseModel):
    persona: str = Field("", max_length=4096)


@router.put("/profiles/{name}/persona")
async def put_persona_route(name: str, body: PersonaBody) -> dict:
    """Set the active persona for a profile (writes profile_dir/persona.txt)."""
    from opencomputer.profiles import get_profile_dir, list_profiles

    if name not in list_profiles():
        raise HTTPException(status_code=404, detail="profile not found")
    try:
        d = get_profile_dir(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc))
    d.mkdir(parents=True, exist_ok=True)
    (d / "persona.txt").write_text(body.persona, encoding="utf-8")
    audit_log("profile.persona_set", name=name, len=len(body.persona))
    return {"ok": True, "profile": name}


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
