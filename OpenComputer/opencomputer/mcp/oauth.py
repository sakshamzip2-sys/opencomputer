"""OAuth token storage for MCP servers (Tier 2.5 / G.13).

The full browser-based OAuth dance is significant work and varies per
provider (GitHub, Google, Notion, Atlassian, etc. each have different
flows). This v1 ships the storage primitives + a manual-paste CLI path
that works for any provider, plus a generic OAuth-callback server
helper that subsequent provider-specific flows can plug into.

What's here now:

- :class:`OAuthTokenStore` — read / write / list / revoke tokens at
  ``<profile_home>/mcp_oauth/<provider>.json`` (mode 0600). Atomic writes.
- :class:`OAuthToken` — frozen dataclass with ``access_token``,
  ``refresh_token``, ``token_type``, ``expires_at``, ``scope``,
  ``provider`` fields. Pure data; no transport coupling.
- :func:`paste_token` — accept a manually-pasted token (e.g. PAT)
  and persist it under a provider name.
- :func:`get_token_for_env_lookup` — agent-callable lookup that
  preferentially returns an OAuth-stored token when no env var is set
  (e.g. ``GITHUB_PERSONAL_ACCESS_TOKEN`` empty → fetch from store).

What's NOT here yet (deferred to G.13.x follow-ups):

- Provider-specific OAuth dances (browser launch + callback server).
- Refresh-token rotation.

The storage is forward-compatible — when the browser flow lands, it
just calls ``OAuthTokenStore.put(...)`` with the access + refresh tokens
it received and everything downstream keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)


_store_lock = threading.Lock()


def oauth_dir() -> Path:
    """``<profile_home>/mcp_oauth/`` — secure directory for token files."""
    d = _home() / "mcp_oauth"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except (OSError, NotImplementedError):
        pass
    return d


def token_path(provider: str) -> Path:
    """Path to one provider's token file."""
    return oauth_dir() / f"{_normalise(provider)}.json"


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """One stored OAuth token (or PAT). All fields except ``access_token`` optional.

    ``provider`` — short slug like ``"github"`` / ``"google"`` / ``"notion"``.
    ``token_type`` — ``"Bearer"`` / ``"Personal Access Token"`` / etc.
    ``expires_at`` — Unix epoch seconds, or ``None`` for non-expiring tokens
        (PATs typically don't expire).
    ``scope`` — space-separated scope string, or ``None``.
    ``refresh_token`` — for OAuth flows that support refresh.
    """

    provider: str
    access_token: str
    token_type: str = "Bearer"
    expires_at: float | None = None
    scope: str | None = None
    refresh_token: str | None = None
    created_at: float = 0.0


class OAuthTokenStore:
    """Per-profile OAuth token storage."""

    def put(self, token: OAuthToken) -> Path:
        """Persist (or overwrite) one provider's token. Returns the file path written."""
        path = token_path(token.provider)
        with _store_lock:
            self._atomic_write(path, asdict(token))
        return path

    def get(self, provider: str) -> OAuthToken | None:
        """Return the stored token for ``provider``, or ``None`` if absent / expired."""
        path = token_path(provider)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("oauth token file corrupted at %s: %s", path, exc)
            return None
        token = OAuthToken(
            provider=data.get("provider", _normalise(provider)),
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_at=data.get("expires_at"),
            scope=data.get("scope"),
            refresh_token=data.get("refresh_token"),
            created_at=data.get("created_at", 0.0),
        )
        # Reject expired tokens up-front. (OAuth refresh is a separate
        # concern — handled by the future provider-specific flows.)
        if token.expires_at is not None and token.expires_at <= time.time():
            logger.info("oauth token for %r expired (expires_at=%s)", provider, token.expires_at)
            return None
        return token

    def list(self) -> list[OAuthToken]:
        """List all stored tokens (skips expired)."""
        out: list[OAuthToken] = []
        for entry in oauth_dir().glob("*.json"):
            t = self.get(entry.stem)
            if t is not None:
                out.append(t)
        return out

    def revoke(self, provider: str) -> bool:
        """Delete a provider's token file. Returns True if it existed."""
        path = token_path(provider)
        with _store_lock:
            if path.exists():
                path.unlink()
                return True
        return False

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".oauth_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except (OSError, NotImplementedError):
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Convenience helpers used by CLI + provider-resolution paths
# ---------------------------------------------------------------------------


def paste_token(
    *,
    provider: str,
    access_token: str,
    token_type: str = "Personal Access Token",
    scope: str | None = None,
    expires_at: float | None = None,
    refresh_token: str | None = None,
) -> Path:
    """Persist a manually-pasted token (most common case for github PATs).

    Wraps :class:`OAuthTokenStore.put`. Returns the file path written.
    """
    if not access_token or not access_token.strip():
        raise ValueError("access_token must be non-empty")
    token = OAuthToken(
        provider=_normalise(provider),
        access_token=access_token.strip(),
        token_type=token_type,
        expires_at=expires_at,
        scope=scope,
        refresh_token=refresh_token,
        created_at=time.time(),
    )
    return OAuthTokenStore().put(token)


def get_token_for_env_lookup(
    *,
    provider: str,
    env_var: str,
) -> str | None:
    """Resolve a credential preferring env-var, falling back to OAuth store.

    Pattern used by MCP server-config rendering: when an MCP preset
    references ``${GITHUB_PERSONAL_ACCESS_TOKEN}`` and that env var is
    unset, fall back to the OAuth store rather than failing the launch.

    Returns the access_token string or ``None`` when neither source has it.
    """
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        return env_val
    token = OAuthTokenStore().get(provider)
    if token is None:
        return None
    return token.access_token


def _normalise(provider: str) -> str:
    if not provider or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    return provider.strip().lower()


__all__ = [
    "OAuthToken",
    "OAuthTokenStore",
    "get_token_for_env_lookup",
    "oauth_dir",
    "paste_token",
    "token_path",
]
