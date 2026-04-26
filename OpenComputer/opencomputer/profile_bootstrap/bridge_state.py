"""Browser-bridge state — token storage, port config.

State lives at ``<profile_home>/profile_bootstrap/bridge.json``:
``{"token": "<url-safe-32-bytes>", "port": 18791}``.

Tokens are generated via :func:`secrets.token_urlsafe(32)`. Rotation
is a destructive operation — old token is immediately invalid.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.agent.config import _home


@dataclass(frozen=True, slots=True)
class BridgeState:
    """Serialised browser-bridge config."""

    token: str = ""
    port: int = 18791


def state_path() -> Path:
    """Resolve the bridge state file path under the active profile home."""
    return _home() / "profile_bootstrap" / "bridge.json"


def load_or_create(*, rotate: bool = False) -> BridgeState:
    """Read existing state or generate a fresh token."""
    p = state_path()
    if p.exists() and not rotate:
        try:
            data = json.loads(p.read_text())
            return BridgeState(
                token=str(data.get("token", "")),
                port=int(data.get("port", 18791)),
            )
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    state = BridgeState(token=secrets.token_urlsafe(32), port=18791)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(state)))
    os.chmod(p, 0o600)  # restrict to owner only — token grants HTTP authority
    return state
