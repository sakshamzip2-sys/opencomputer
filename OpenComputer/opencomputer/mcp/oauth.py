"""OAuth token storage for MCP servers (Tier 2.5 / G.13).

The full browser-based OAuth dance is significant work and varies per
provider (GitHub, Google, Notion, Atlassian, etc. each have different
flows). This module ships two complementary storage shapes:

1. **Legacy PAT store** (:class:`OAuthTokenStore`) — one file per
   provider at ``<profile_home>/mcp_oauth/<provider>.json``, used by
   the manual-paste CLI flow + ``${ENV_VAR}`` fallback resolution. In
   active production use by ``cli_mcp.py`` for github/notion PATs.

2. **SDK-aligned store** (:class:`OCMCPOAuthClient`, this PR) — single
   ``<profile_home>/mcp/tokens.json`` keyed by MCP server name, used as
   the storage backend for the MCP Python SDK's
   :class:`mcp.client.auth.OAuthClientProvider`. The SDK already handles
   dynamic client registration (RFC 7591), RFC 8414 discovery, PKCE,
   refresh, and step-up auth — we provide persistence + profile-aware
   paths + the protocol adapter (:class:`_SDKStorageAdapter`).

What's here:

- :class:`OAuthTokenStore` / :class:`OAuthToken` / :func:`paste_token` /
  :func:`get_token_for_env_lookup` — legacy PAT primitives.
- :class:`OCMCPOAuthClient` / :class:`_SDKStorageAdapter` /
  :func:`_tokens_path` — SDK-aligned token store + protocol adapter
  for ``mcp.client.auth.OAuthClientProvider``.

What's NOT here yet:

- Browser-launch wiring on top of :class:`OCMCPOAuthClient`. Callers
  obtain an ``OAuthClientProvider`` via ``as_sdk_provider(...)`` and
  pass it to the SDK's HTTP client; the SDK drives the browser flow
  (or the caller supplies a ``redirect_handler``).
- Refresh-token rotation in the legacy PAT store (the SDK handles
  refresh automatically for the new path).
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
from typing import Any

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


# ---------------------------------------------------------------------------
# SDK-aligned MCP OAuth (Task 1.2 / D4 from the rev-2 best-of-import plan)
# ---------------------------------------------------------------------------
#
# The MCP Python SDK ships ``mcp.client.auth.OAuthClientProvider`` — an
# ``httpx.Auth`` subclass that handles:
#
#   * Dynamic client registration (RFC 7591)
#   * Authorization-server discovery (RFC 8414)
#   * Authorization Code + PKCE flow
#   * Token refresh
#   * Step-up auth (when the resource demands richer scopes)
#
# Our job is the bits the SDK explicitly delegates: persistence + a
# profile-aware path. The SDK's ``TokenStorage`` Protocol declares four
# async methods (``get_tokens``, ``set_tokens``, ``get_client_info``,
# ``set_client_info``) — :class:`_SDKStorageAdapter` translates those to
# our synchronous on-disk JSON store.


def _tokens_path() -> Path:
    """``<profile_home>/mcp/tokens.json`` — single file keyed by server name.

    Re-imports ``_home`` at call time so test ``monkeypatch`` of
    ``opencomputer.agent.config._home`` is honoured (per-test isolation).
    """
    # Import inside the function: tests rebind ``_home`` on the
    # ``opencomputer.agent.config`` module, and a top-level
    # ``from … import _home`` would have captured the original.
    from opencomputer.agent import config as _config

    return _config._home() / "mcp" / "tokens.json"


class OCMCPOAuthClient:
    """Per-MCP-server OAuth token store.

    Each instance is bound to one MCP server name (``"github"``,
    ``"notion"``, …) and reads / writes that server's slot inside the
    shared ``<profile_home>/mcp/tokens.json`` file. Saving one server's
    tokens preserves every other server's entry (atomic merge-write).

    Use :meth:`as_sdk_provider` to obtain an
    :class:`mcp.client.auth.OAuthClientProvider` whose storage is backed
    by this instance.
    """

    def __init__(self, server_name: str) -> None:
        if not server_name or not server_name.strip():
            raise ValueError("server_name must be a non-empty string")
        self.server_name = server_name

    def _all_tokens(self) -> dict[str, Any]:
        """Read the entire tokens.json. Returns ``{}`` on missing / corrupt."""
        path = _tokens_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("mcp tokens file corrupted at %s: %s", path, exc)
            return {}

    def load_tokens(self) -> dict[str, Any]:
        """Return the stored payload for this server, or ``{}``."""
        return self._all_tokens().get(self.server_name, {})

    def save_tokens(self, tokens: dict[str, Any]) -> None:
        """Persist this server's tokens, preserving every other server's entry.

        Atomic via tmp-file + ``os.replace`` so a crash mid-write cannot
        leave a half-written ``tokens.json``. The merge step reads the
        current file under the same lock to avoid racing two
        ``save_tokens`` calls into a torn merge.
        """
        path = _tokens_path()
        with _store_lock:
            all_t = self._all_tokens()
            all_t[self.server_name] = tokens
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mcp_tokens_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(all_t, fh, indent=2)
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

    def as_sdk_provider(
        self,
        server_url: str,
        client_metadata: dict[str, Any],
        redirect_handler: Any | None = None,
        callback_handler: Any | None = None,
    ) -> Any:
        """Return an SDK :class:`OAuthClientProvider` backed by this store.

        ``client_metadata`` is the dict form of
        :class:`mcp.shared.auth.OAuthClientMetadata` — at minimum
        ``client_name`` and ``redirect_uris``. The SDK validates / coerces.

        ``redirect_handler`` and ``callback_handler`` are optional async
        callables forwarded to the SDK; when omitted, the SDK uses its
        default browser-launch + local-callback path.
        """
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=OAuthClientMetadata(**client_metadata),
            storage=_SDKStorageAdapter(self),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )


class _SDKStorageAdapter:
    """Translate the SDK's async ``TokenStorage`` Protocol to our JSON store.

    The SDK's contract (verified against ``mcp>=1.6``)::

        class TokenStorage(Protocol):
            async def get_tokens(self) -> OAuthToken | None: ...
            async def set_tokens(self, tokens: OAuthToken) -> None: ...
            async def get_client_info(self) -> OAuthClientInformationFull | None: ...
            async def set_client_info(self, client_info: OAuthClientInformationFull) -> None: ...

    Both ``set_*`` methods receive Pydantic models — we serialise via
    ``model_dump(mode='json')`` so any URL / datetime fields collapse to
    JSON-safe primitives. ``get_*`` returns plain dicts; the SDK accepts
    them and re-validates internally.
    """

    def __init__(self, client: OCMCPOAuthClient) -> None:
        self._client = client

    async def get_tokens(self) -> dict[str, Any] | None:
        toks = self._client.load_tokens()
        # The SDK stores client_info alongside the OAuth token payload in
        # our merged blob. Strip our internal key before handing back —
        # the SDK's ``OAuthToken`` model does not declare it.
        if not toks:
            return None
        token_only = {k: v for k, v in toks.items() if k != "client_info"}
        return token_only or None

    async def set_tokens(self, tokens: Any) -> None:
        existing = self._client.load_tokens()
        # Preserve client_info if present; only the token fields rotate.
        client_info = existing.get("client_info")
        payload = (
            tokens.model_dump(mode="json")
            if hasattr(tokens, "model_dump")
            else dict(tokens)
        )
        if client_info is not None:
            payload["client_info"] = client_info
        self._client.save_tokens(payload)

    async def get_client_info(self) -> dict[str, Any] | None:
        toks = self._client.load_tokens()
        ci = toks.get("client_info") if toks else None
        return ci if ci else None

    async def set_client_info(self, client_info: Any) -> None:
        existing = self._client.load_tokens()
        existing["client_info"] = (
            client_info.model_dump(mode="json")
            if hasattr(client_info, "model_dump")
            else dict(client_info)
        )
        self._client.save_tokens(existing)


__all__ = [
    "OAuthToken",
    "OAuthTokenStore",
    "OCMCPOAuthClient",
    "get_token_for_env_lookup",
    "oauth_dir",
    "paste_token",
    "token_path",
]
