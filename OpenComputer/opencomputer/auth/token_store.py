"""OAuth token persistence — JSON file under ``~/.opencomputer/auth_tokens.json``.

Modeled after Hermes's pattern of per-provider auth state
(``hermes_cli/auth.py``: ``_save_codex_tokens``, ``_save_qwen_cli_tokens``,
etc.) but unified into one keyed-by-provider store instead of one file
per provider.

File mode 0600 enforced on every write. Corrupt JSON is silently
treated as empty so a partially-written store doesn't brick the
agent.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_store_path() -> Path:
    """Return path to the active profile's auth_tokens.json.

    ``OPENCOMPUTER_HOME`` wins when set; else falls back to
    ``~/.opencomputer/auth_tokens.json``.
    """
    home = os.environ.get("OPENCOMPUTER_HOME")
    if home:
        return Path(home) / "auth_tokens.json"
    return Path.home() / ".opencomputer" / "auth_tokens.json"


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """One provider's OAuth token state.

    ``expires_at`` is unix epoch seconds (int). ``0`` means "no known
    expiry" (treated as not expired) — useful for providers that
    return long-lived tokens without an exp claim.
    """

    provider: str
    access_token: str
    refresh_token: str | None = None
    expires_at: int = 0
    scope: str = ""
    extra: dict = field(default_factory=dict)
    """Provider-specific fields (e.g. id_token, account_id) — stored
    opaquely so plugins can stash whatever they need."""

    def is_expired(self) -> bool:
        """True if ``expires_at`` is set and now past it. ``expires_at=0``
        means "no known expiry" — returns False."""
        if self.expires_at == 0:
            return False
        return int(time.time()) >= self.expires_at

    def expires_soon(self, skew_seconds: int = 60) -> bool:
        """True if the token expires within ``skew_seconds``. Used by
        callers to decide whether to refresh proactively."""
        if self.expires_at == 0:
            return False
        return int(time.time()) + skew_seconds >= self.expires_at


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path.chmod(0o600)


def load_token(provider: str, store_path: Path | None = None) -> OAuthToken | None:
    """Return the persisted token for ``provider``, or None when absent."""
    path = store_path if store_path is not None else default_store_path()
    data = _read_store(path)
    entry = data.get(provider)
    if not entry or not isinstance(entry, dict):
        return None
    try:
        return OAuthToken(
            provider=entry["provider"],
            access_token=entry["access_token"],
            refresh_token=entry.get("refresh_token"),
            expires_at=int(entry.get("expires_at", 0)),
            scope=entry.get("scope", ""),
            extra=entry.get("extra", {}) or {},
        )
    except (KeyError, ValueError, TypeError):
        return None


def save_token(token: OAuthToken, store_path: Path | None = None) -> None:
    """Persist ``token`` to the store, overwriting any existing entry
    for the same provider. Other providers' tokens are preserved."""
    path = store_path if store_path is not None else default_store_path()
    data = _read_store(path)
    data[token.provider] = asdict(token)
    _write_store(path, data)


def delete_token(provider: str, store_path: Path | None = None) -> None:
    """Remove ``provider``'s token from the store. No-op if absent."""
    path = store_path if store_path is not None else default_store_path()
    data = _read_store(path)
    if provider in data:
        del data[provider]
        _write_store(path, data)


__all__ = [
    "OAuthToken",
    "default_store_path",
    "delete_token",
    "load_token",
    "save_token",
]
