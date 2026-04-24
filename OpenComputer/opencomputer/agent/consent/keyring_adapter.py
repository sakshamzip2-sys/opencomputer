"""Keyring adapter with file fallback for environments without D-Bus/Keychain.

On Linux without a running keyring daemon, on CI, or inside minimal Docker
images, `keyring` raises at import or call time. Rather than making the
consent layer unusable, fall back to a JSON file in the profile directory
and WARN that on-disk secret storage is less secure.

On macOS the first access prompts for Keychain permission via a GUI popup —
unavoidable. The fallback path keeps CI / headless-SSH paths working.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

import keyring  # type: ignore[import-untyped]

logger: Final = logging.getLogger(__name__)


class KeyringAdapter:
    """Wraps `keyring` with a JSON-file fallback."""

    def __init__(self, service: str, fallback_dir: Path) -> None:
        self._service = service
        self._fallback_dir = fallback_dir
        self._fallback_path = fallback_dir / f"{service}.json"

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(self._service, key, value)
            return
        except Exception as e:  # noqa: BLE001 — keyring raises many types
            logger.warning(
                "keyring unavailable (%s); falling back to file at %s. "
                "Secret storage on disk is less secure.",
                e, self._fallback_path,
            )
        self._write_file(key, value)

    def get(self, key: str) -> str | None:
        try:
            v = keyring.get_password(self._service, key)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001
            logger.warning(
                "keyring read failed; checking file fallback at %s",
                self._fallback_path,
            )
        return self._read_file(key)

    def _read_file(self, key: str) -> str | None:
        if not self._fallback_path.exists():
            return None
        data = json.loads(self._fallback_path.read_text())
        return data.get(key)

    def _write_file(self, key: str, value: str) -> None:
        self._fallback_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if self._fallback_path.exists():
            data = json.loads(self._fallback_path.read_text())
        data[key] = value
        self._fallback_path.write_text(json.dumps(data, indent=2))
