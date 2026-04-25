"""Webhook token registry.

Stores per-token HMAC secrets + scopes in
``<profile_home>/webhook_tokens.json``. Tokens authenticate inbound POSTs
via ``X-Webhook-Signature: sha256=<hmac>`` headers; scopes restrict which
agent capabilities the webhook can trigger.

Storage shape:

```json
{
  "version": 1,
  "tokens": {
    "<token_id>": {
      "secret": "<hex>",
      "name": "tradingview-alerts",
      "scopes": ["skill:stock-market-analysis"],
      "notify": "telegram",
      "created_at": 1700000000.0,
      "last_used_at": null,
      "revoked": false
    }
  }
}
```

Tokens are 32-char hex (16 bytes). Secrets are 64-char hex (32 bytes).
HMAC-SHA256 over the raw POST body. File mode 0600 so secrets aren't
world-readable.

Reused from cron-jobs storage pattern: atomic writes via tmp + os.replace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)


# In-process lock for load → modify → save.
_tokens_lock = threading.Lock()


def tokens_file() -> Path:
    """Return profile-isolated webhook tokens file path."""
    return _home() / "webhook_tokens.json"


def _secure_file(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_tokens() -> dict[str, dict[str, Any]]:
    """Load all tokens from disk. Returns ``{}`` if no file exists."""
    f = tokens_file()
    if not f.exists():
        return {}
    try:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
            return data.get("tokens", {})
    except json.JSONDecodeError as exc:
        logger.error("Webhook tokens file corrupted: %s", exc)
        raise RuntimeError(f"Webhook token registry corrupted: {exc}") from exc


def save_tokens(tokens: dict[str, dict[str, Any]]) -> None:
    """Atomically write tokens to disk via tmp + os.replace."""
    f = tokens_file()
    f.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(f.parent), suffix=".tmp", prefix=".tokens_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "tokens": tokens, "updated_at": time.time()}, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, f)
        _secure_file(f)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def create_token(
    *,
    name: str,
    scopes: list[str] | None = None,
    notify: str | None = None,
) -> tuple[str, str]:
    """Create a new webhook token. Returns ``(token_id, secret)``.

    Secret is shown once and not recoverable — caller must persist it
    in their integration config (TradingView alert payload, Zapier setup,
    etc.). Subsequent listings show only the token_id and metadata.
    """
    token_id = secrets.token_hex(16)  # 32 chars
    secret = secrets.token_hex(32)  # 64 chars — full HMAC strength

    with _tokens_lock:
        tokens = load_tokens()
        tokens[token_id] = {
            "secret": secret,
            "name": name,
            "scopes": list(scopes) if scopes else [],
            "notify": notify,
            "created_at": time.time(),
            "last_used_at": None,
            "revoked": False,
        }
        save_tokens(tokens)

    return token_id, secret


def get_token(token_id: str) -> dict[str, Any] | None:
    """Get token metadata by id (or ``None``)."""
    return load_tokens().get(token_id)


def list_tokens(*, include_revoked: bool = False) -> list[dict[str, Any]]:
    """List all tokens. By default hides revoked.

    Each entry includes ``token_id`` but NOT ``secret``.
    """
    out: list[dict[str, Any]] = []
    for tid, meta in load_tokens().items():
        if not include_revoked and meta.get("revoked"):
            continue
        # Strip secret before returning to caller
        view = dict(meta)
        view.pop("secret", None)
        view["token_id"] = tid
        out.append(view)
    return out


def revoke_token(token_id: str) -> bool:
    """Mark a token as revoked. Returns True if found, False if missing."""
    with _tokens_lock:
        tokens = load_tokens()
        if token_id not in tokens:
            return False
        tokens[token_id]["revoked"] = True
        save_tokens(tokens)
    return True


def remove_token(token_id: str) -> bool:
    """Permanently delete a token from the registry."""
    with _tokens_lock:
        tokens = load_tokens()
        if token_id not in tokens:
            return False
        del tokens[token_id]
        save_tokens(tokens)
    return True


def mark_used(token_id: str) -> None:
    """Update ``last_used_at`` to now. Best-effort — failures are logged not raised."""
    try:
        with _tokens_lock:
            tokens = load_tokens()
            if token_id in tokens:
                tokens[token_id]["last_used_at"] = time.time()
                save_tokens(tokens)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to mark webhook token used: %s", exc)


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def verify_signature(*, body: bytes, signature_header: str, secret: str) -> bool:
    """Verify ``X-Webhook-Signature: sha256=<hex>`` against ``body``.

    Returns True only if:
    - Header has the expected ``sha256=`` prefix
    - HMAC-SHA256 over ``body`` with ``secret`` matches the header
    - Constant-time comparison (hmac.compare_digest)
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


__all__ = [
    "create_token",
    "get_token",
    "list_tokens",
    "load_tokens",
    "mark_used",
    "remove_token",
    "revoke_token",
    "save_tokens",
    "tokens_file",
    "verify_signature",
]
