"""Per-chat gateway runtime state — A2 (gateway-vs-CLI parity Wave 1).

The CLI can start a turn in plan mode (``oc --plan``). The gateway had
no equivalent: every Telegram/Discord message was full-execute. This
module holds a tiny per-session key/value store — currently just the
``plan_mode`` toggle — persisted to ``<profile>/gateway/runtime_state.json``
so a ``/plan on`` survives a daemon restart.

State is keyed by ``session_id`` (the deterministic
``sha256(platform + chat_id)``), so a key is effectively per-chat.

The active store is registered process-wide (mirroring
``queue_manager.set_active_manager``) so the ``/plan`` slash command —
which only receives a ``RuntimeContext`` — can reach the same instance
the gateway dispatcher reads.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.gateway.runtime_state")


class GatewayRuntimeState:
    """Per-session gateway runtime toggles, optionally disk-backed."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        # {session_id: {"plan_mode": bool, "profile_override": str|None}}
        self._state: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for sid, entry in raw.items():
                    if isinstance(sid, str) and isinstance(entry, dict):
                        override = entry.get("profile_override")
                        self._state[sid] = {
                            "plan_mode": bool(entry.get("plan_mode", False)),
                            "profile_override": (
                                override if isinstance(override, str) else None
                            ),
                        }
        except Exception:  # noqa: BLE001 — a corrupt file must not crash boot
            logger.warning(
                "runtime_state: could not load %s; starting empty",
                self._path, exc_info=True,
            )

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "runtime_state: could not persist %s", self._path, exc_info=True,
            )

    def get_plan_mode(self, session_id: str) -> bool:
        with self._lock:
            return self._state.get(session_id, {}).get("plan_mode", False)

    def set_plan_mode(self, session_id: str, enabled: bool) -> None:
        with self._lock:
            self._state.setdefault(session_id, {})["plan_mode"] = bool(enabled)
            self._persist()

    def get_profile_override(self, session_id: str) -> str | None:
        """Return the profile this chat has been handed off to, if any.

        A8 — ``/handoff`` on the gateway records the target here (its own
        command runtime is ephemeral). Unlike a one-shot flag this is a
        *persistent* override: the dispatcher applies it on every turn,
        the same way the CLI persists the active profile on disk. A later
        ``/handoff`` overwrites it.
        """
        with self._lock:
            return self._state.get(session_id, {}).get("profile_override")

    def set_profile_override(self, session_id: str, profile_id: str) -> None:
        with self._lock:
            self._state.setdefault(session_id, {})["profile_override"] = (
                profile_id
            )
            self._persist()

    def clear_profile_override(self, session_id: str) -> None:
        with self._lock:
            entry = self._state.get(session_id)
            if entry is not None and entry.get("profile_override") is not None:
                entry["profile_override"] = None
                self._persist()


# ─── Process-wide active store ──────────────────────────────────────────

_active: GatewayRuntimeState | None = None


def set_active_runtime_state(state: GatewayRuntimeState) -> None:
    """Register ``state`` as the process-wide active store (gateway boot)."""
    global _active
    _active = state


def get_runtime_state() -> GatewayRuntimeState:
    """Return the active store, lazily creating an in-memory one.

    Outside the gateway (CLI, tests) there is no registered store; the
    lazy in-memory instance keeps ``/plan`` working without persistence.
    """
    global _active
    if _active is None:
        _active = GatewayRuntimeState(path=None)
    return _active


__all__ = [
    "GatewayRuntimeState",
    "get_runtime_state",
    "set_active_runtime_state",
]
