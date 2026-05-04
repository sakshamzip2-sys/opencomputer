"""Registered remote-host peers + HMAC mutual-auth (Wave 6.E.13).

This module owns:

- The ``kanban_remote_hosts`` registry (add/list/remove).
- HMAC signing + verification for write-path requests.
- Replay-window enforcement (300s clock-skew tolerance).

Why HMAC and not bearer tokens? Bearer tokens replay trivially if a
network observer captures one. HMAC over (timestamp, method, path,
body_sha256) means each request is uniquely signed; replay requires
the original request body + sig within the 300s window, and even
then the timestamp check rejects.

Both peers store the same ``hmac_secret`` and use it to sign outgoing
requests + verify inbound. ``add_remote_host`` generates a 32-byte
secret; the operator copies it to the peer manually (the bootstrap
moment).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

# Wave 6.E.13 — replay window. Requests signed more than this many
# seconds in the past or future are rejected. 300s tolerates ~2.5
# minutes of NTP skew either direction. Document that ops are
# responsible for keeping host clocks within this window.
REPLAY_WINDOW_SECONDS = 300

# Header carrying the signature. Format: "v1:<timestamp>:<hex_signature>"
SIGNATURE_HEADER = "X-OC-Signature"


@dataclass(frozen=True, slots=True)
class RemoteHost:
    slug: str
    url: str
    hmac_secret: str
    added_at: int
    last_seen_at: int | None
    # Wave 6.E.15 — both sides must have this true for cross-host
    # ``dir:<path>`` workspace payload sync to fire. Default False
    # (back-compat with PR #460).
    workspace_sync_enabled: bool = False


class HmacAuthError(RuntimeError):
    """Raised when an inbound HMAC-signed request fails verification."""


def _row_to_host(row) -> RemoteHost:
    """Build a RemoteHost from a sqlite3.Row, defaulting new columns
    on legacy rows (back-compat for pre-migration installs)."""
    keys = row.keys() if hasattr(row, "keys") else []
    workspace_sync = (
        bool(row["workspace_sync_enabled"])
        if "workspace_sync_enabled" in keys else False
    )
    return RemoteHost(
        slug=row["slug"],
        url=row["url"],
        hmac_secret=row["hmac_secret"],
        added_at=row["added_at"],
        last_seen_at=row["last_seen_at"],
        workspace_sync_enabled=workspace_sync,
    )


def add_remote_host(
    conn: sqlite3.Connection,
    *,
    slug: str,
    url: str,
    hmac_secret: str | None = None,
    workspace_sync_enabled: bool = False,
) -> RemoteHost:
    """Register a peer host. Returns the new RemoteHost (with its secret).

    If ``hmac_secret`` is omitted, a 32-byte random secret is
    generated and returned. The operator must copy this secret to
    the peer manually so both sides hold the same value.

    ``workspace_sync_enabled`` opts this peer into cross-host
    ``dir:<path>`` workspace payload sync (Wave 6.E.15). Both sides
    must have it on; the sender checks before packing.
    """
    if not slug or not url:
        raise ValueError("slug and url required")
    if hmac_secret is None:
        hmac_secret = secrets.token_urlsafe(32)
    now = int(time.time())
    from opencomputer.kanban.db import write_txn
    with write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_remote_hosts "
            "(slug, url, hmac_secret, added_at, workspace_sync_enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            (slug, url, hmac_secret, now, 1 if workspace_sync_enabled else 0),
        )
    return RemoteHost(
        slug=slug, url=url, hmac_secret=hmac_secret,
        added_at=now, last_seen_at=None,
        workspace_sync_enabled=workspace_sync_enabled,
    )


def list_remote_hosts(conn: sqlite3.Connection) -> list[RemoteHost]:
    rows = conn.execute(
        "SELECT * FROM kanban_remote_hosts ORDER BY slug"
    ).fetchall()
    return [_row_to_host(r) for r in rows]


def find_remote_host(conn: sqlite3.Connection, slug: str) -> RemoteHost | None:
    row = conn.execute(
        "SELECT * FROM kanban_remote_hosts WHERE slug = ?", (slug,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_host(row)


def remove_remote_host(conn: sqlite3.Connection, slug: str) -> bool:
    from opencomputer.kanban.db import write_txn
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_remote_hosts WHERE slug = ?", (slug,),
        )
    return cur.rowcount > 0


def set_workspace_sync(
    conn: sqlite3.Connection, slug: str, enabled: bool,
) -> bool:
    """Toggle ``workspace_sync_enabled`` on an existing peer.

    Returns True if a row was updated, False if the slug is unknown.
    """
    from opencomputer.kanban.db import write_txn
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_remote_hosts SET workspace_sync_enabled = ? "
            "WHERE slug = ?",
            (1 if enabled else 0, slug),
        )
    return cur.rowcount > 0


def update_last_seen(conn: sqlite3.Connection, slug: str) -> None:
    """Bump the last_seen_at column on a successful round-trip."""
    from opencomputer.kanban.db import write_txn
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_remote_hosts SET last_seen_at = ? WHERE slug = ?",
            (int(time.time()), slug),
        )


# ---------------------------------------------------------------------------
# HMAC signing + verification
# ---------------------------------------------------------------------------


def _canonical_body(body: bytes | str | None) -> bytes:
    """Normalize request body to bytes for signing.

    Empty body → empty bytes (signs the empty string). Strings are
    UTF-8 encoded.
    """
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    return bytes(body)


def _build_string_to_sign(
    *, timestamp: int, method: str, path: str, body: bytes,
) -> bytes:
    """Build the canonical bytes that get HMAC-signed.

    Format: "v1\\n<timestamp>\\n<METHOD>\\n<path>\\n<body_sha256_hex>"
    The "v1" version prefix lets us evolve the format without
    breaking peers; older peers will fail to verify (HmacAuthError)
    rather than silently accept a different scheme.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return (
        f"v1\n{timestamp}\n{method.upper()}\n{path}\n{body_hash}".encode()
    )


def sign_request(
    *,
    secret: str,
    method: str,
    path: str,
    body: bytes | str | None = None,
    timestamp: int | None = None,
) -> str:
    """Build the value for the ``X-OC-Signature`` header.

    Returns ``"v1:<timestamp>:<hex_sig>"``. Including the timestamp
    in the header means the server doesn't have to trust a clock-
    aligned timestamp the client claims separately.
    """
    ts = timestamp if timestamp is not None else int(time.time())
    body_bytes = _canonical_body(body)
    msg = _build_string_to_sign(
        timestamp=ts, method=method, path=path, body=body_bytes,
    )
    sig = hmac.new(
        secret.encode("utf-8"), msg, hashlib.sha256,
    ).hexdigest()
    return f"v1:{ts}:{sig}"


def verify_request(
    header_value: str | None,
    *,
    secret: str,
    method: str,
    path: str,
    body: bytes | str | None = None,
    now: int | None = None,
    replay_window: int = REPLAY_WINDOW_SECONDS,
) -> None:
    """Validate an inbound signature. Raises HmacAuthError on failure.

    Checks (in order):
    1. Header present and parses as ``v1:<int>:<hex>``.
    2. Timestamp within ``replay_window`` seconds of ``now``.
    3. Computed signature matches header (constant-time compare).

    Future-proofing: a non-"v1" version prefix raises a clear error
    so newer-version messages are rejected by older servers (rather
    than silently accepting under a downgrade).
    """
    if not header_value:
        raise HmacAuthError("missing X-OC-Signature header")
    parts = header_value.split(":")
    if len(parts) != 3:
        raise HmacAuthError(
            f"signature must be 'v1:<ts>:<hex>'; got {header_value!r}"
        )
    version, ts_str, hex_sig = parts
    if version != "v1":
        raise HmacAuthError(f"unsupported signature version {version!r}")
    try:
        ts = int(ts_str)
    except ValueError as exc:
        raise HmacAuthError(f"timestamp must be int; got {ts_str!r}") from exc
    actual_now = now if now is not None else int(time.time())
    if abs(actual_now - ts) > replay_window:
        raise HmacAuthError(
            f"timestamp {ts} outside replay window of {replay_window}s "
            f"(now={actual_now}, drift={actual_now - ts}s)"
        )

    body_bytes = _canonical_body(body)
    msg = _build_string_to_sign(
        timestamp=ts, method=method, path=path, body=body_bytes,
    )
    expected = hmac.new(
        secret.encode("utf-8"), msg, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, hex_sig):
        raise HmacAuthError("signature does not match expected HMAC")


def signed_headers(
    *,
    secret: str,
    method: str,
    path: str,
    body: bytes | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a header dict with the X-OC-Signature filled in.

    Convenience for the client side. Caller is still responsible for
    Content-Type etc.
    """
    headers = dict(extra or {})
    headers[SIGNATURE_HEADER] = sign_request(
        secret=secret, method=method, path=path, body=body,
    )
    return headers


__all__ = [
    "RemoteHost",
    "HmacAuthError",
    "REPLAY_WINDOW_SECONDS",
    "SIGNATURE_HEADER",
    "add_remote_host",
    "list_remote_hosts",
    "find_remote_host",
    "remove_remote_host",
    "update_last_seen",
    "sign_request",
    "verify_request",
    "signed_headers",
]
