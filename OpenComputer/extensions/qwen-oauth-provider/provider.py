"""Qwen OAuth provider — OAuth via Qwen CLI credentials, OpenAI-compatible inference.

Hermes pattern (verified against hermes_cli/auth.py:
``_read_qwen_cli_tokens``, ``_refresh_qwen_cli_tokens``,
``QWEN_OAUTH_CLIENT_ID``, ``QWEN_OAUTH_TOKEN_URL``):

  1. Read ``~/.qwen/oauth_creds.json`` (the Qwen CLI standard location)
  2. Use ``access_token`` if not expired
  3. If expired, POST refresh_token + client_id to
     ``https://chat.qwen.ai/api/v1/oauth2/token``
  4. Save the refreshed token back to the file (so subsequent runs
     don't re-refresh)

Inference is OpenAI-compatible at https://portal.qwen.ai/v1.

To use:
  1. Install Qwen CLI:  pip install qwen-cli  (or however)
  2. Authenticate:      qwen auth qwen-oauth
  3. OC reads the same creds file automatically.

Alternatively, set QWEN_API_KEY for a pre-issued portal key.

Env vars:
  QWEN_API_KEY      — optional; pre-issued portal API key (bypasses OAuth)
  QWEN_BASE_URL     — optional; default https://portal.qwen.ai/v1
  QWEN_CREDS_PATH   — optional; override default ~/.qwen/oauth_creds.json
"""
from __future__ import annotations

import importlib.util as _importlib_util
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name
# to avoid sys.modules['provider'] collision when multiple
# OpenAI-compat providers are loaded in the same process
# (PR #353 fix for zai-provider/openrouter-provider, extended here).
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_qwen_oauth", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_QWEN_BASE_URL = "https://portal.qwen.ai/v1"
DEFAULT_QWEN_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_REFRESH_SKEW_SECONDS = 60  # refresh 60s before expiry


def _qwen_creds_path() -> Path:
    """Default path to Qwen CLI credentials. Overridable via QWEN_CREDS_PATH env."""
    override = os.environ.get("QWEN_CREDS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".qwen" / "oauth_creds.json"


def _read_qwen_creds(path: Path | None = None) -> dict[str, Any] | None:
    """Read Qwen CLI credentials JSON. Returns None if absent or unparseable."""
    p = path if path is not None else _qwen_creds_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_qwen_creds(creds: dict[str, Any], path: Path | None = None) -> None:
    """Write refreshed Qwen credentials back to the standard path."""
    p = path if path is not None else _qwen_creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    p.chmod(0o600)


def _is_expiring(expiry_date_ms: Any, skew_seconds: int = QWEN_REFRESH_SKEW_SECONDS) -> bool:
    """Qwen stores ``expiry_date`` as a unix-millisecond timestamp."""
    try:
        expiry_ms = int(expiry_date_ms)
    except (TypeError, ValueError):
        return True  # Unknown expiry → assume needs refresh
    expiry_seconds = expiry_ms / 1000
    return time.time() + skew_seconds >= expiry_seconds


def _refresh_qwen_token(
    refresh_token: str,
    client_id: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """POST refresh_token grant to Qwen's OAuth token endpoint."""
    cid = client_id or os.environ.get("QWEN_OAUTH_CLIENT_ID") or DEFAULT_QWEN_CLIENT_ID
    response = httpx.post(
        QWEN_OAUTH_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cid,
        },
        timeout=timeout_seconds,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Qwen OAuth refresh failed: {response.status_code} {response.text[:200]}"
        )
    data = response.json()
    if not isinstance(data, dict) or "access_token" not in data:
        raise RuntimeError(f"Qwen OAuth refresh returned malformed response: {data!r}")
    return data


def _resolve_qwen_token() -> str | None:
    """Resolve Qwen access token: check env, then creds file (with refresh)."""
    static_key = os.environ.get("QWEN_API_KEY")
    if static_key:
        return static_key

    creds = _read_qwen_creds()
    if not creds:
        return None

    access_token = str(creds.get("access_token", "") or "").strip()
    if not access_token:
        return None

    if _is_expiring(creds.get("expiry_date")):
        refresh_token = str(creds.get("refresh_token", "") or "").strip()
        if not refresh_token:
            return None  # Can't refresh — return None so caller raises
        try:
            refreshed = _refresh_qwen_token(refresh_token)
        except Exception:  # noqa: BLE001
            return None
        # Update creds with refreshed values
        creds["access_token"] = refreshed["access_token"]
        if "expires_in" in refreshed:
            creds["expiry_date"] = int((time.time() + int(refreshed["expires_in"])) * 1000)
        if "refresh_token" in refreshed:
            creds["refresh_token"] = refreshed["refresh_token"]
        try:
            _save_qwen_creds(creds)
        except OSError:
            pass  # Best-effort
        access_token = creds["access_token"]

    return access_token


class QwenOAuthProvider(OpenAIProvider):
    name = "qwen-oauth"
    default_model = "qwen-max"
    _api_key_env: str = "QWEN_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            api_key = _resolve_qwen_token()
        if not api_key:
            raise RuntimeError(
                "Qwen OAuth: no token found. Run `qwen auth qwen-oauth` to "
                "authenticate (creates ~/.qwen/oauth_creds.json), or set "
                "QWEN_API_KEY for a pre-issued portal key. "
                "See https://chat.qwen.ai for sign-up."
            )
        resolved_base = (
            base_url
            or os.environ.get("QWEN_BASE_URL")
            or DEFAULT_QWEN_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
