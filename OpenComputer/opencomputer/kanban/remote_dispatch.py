"""Multi-host kanban write coordination (Wave 6.E.13).

Production-grade end-to-end implementation of distributed task
delegation, server-time leases, heartbeat refresh, and terminal
callbacks. Closes PR #457's "out of scope" deferral.

Three flows:

1. **Local → Remote spawn** (:func:`delegate_task_to_remote`)
   Local dispatcher sees ``task.assignee = "<peer-slug>/<profile>"``,
   POSTs ``/proxy/spawn`` to that peer with the task payload + a
   callback URL+token. The peer claims locally and spawns its own
   worker. Local writes a ``kanban_remote_claims`` row with a server-
   time TTL.

2. **Remote heartbeat** (:func:`heartbeat_remote_claim`)
   The local dispatcher tick re-checks every pending remote claim;
   if the lease is expiring within 1 minute, POSTs ``/proxy/heartbeat``
   to refresh. Server-time TTL means the peer dictates expiry, not us.

3. **Remote callback** (:func:`reconcile_callback`)
   When the peer's worker completes, the peer POSTs back to our
   ``/proxy/callback`` endpoint (registered via the dashboard plugin)
   with HMAC signature + remote_task_id. We reconcile the local
   ``kanban_remote_claims`` row + transition the underlying local
   task (done | blocked | failed).

All three flows authenticate via HMAC-SHA256 over (timestamp, method,
path, body_sha256). 300s replay window. See ``remote_hosts.py``.

Workspace handling
==================

This module ships ``scratch`` and ``worktree`` workspace kinds across
hosts (each peer has its own workspace dir, no shared FS needed).
``dir:<path>`` workspaces require the path to exist on the peer (out-
of-band shared FS responsibility). The peer rejects spawn with HTTP 422
if the dir doesn't exist locally.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

import httpx

from opencomputer.kanban import db as kdb
from opencomputer.kanban.remote_hosts import (
    HmacAuthError,
    RemoteHost,
    find_remote_host,
    signed_headers,
    update_last_seen,
    verify_request,
)

logger = logging.getLogger("opencomputer.kanban.remote_dispatch")

# Default TTL for a fresh remote claim. Heartbeat extends.
DEFAULT_REMOTE_TTL_SECONDS = 600  # 10 minutes
# How close to lease_until before we heartbeat.
HEARTBEAT_LEAD_SECONDS = 60


class RemoteDispatchError(RuntimeError):
    """Raised when delegating to a peer fails (network / HTTP / HMAC)."""


@dataclass(frozen=True, slots=True)
class RemoteClaim:
    local_task_id: str
    remote_slug: str
    remote_task_id: str
    leased_at: int
    lease_until: int
    status: str
    last_heartbeat: int | None


# ---------------------------------------------------------------------------
# Assignee parsing — recognize "slug/profile" form
# ---------------------------------------------------------------------------


def parse_remote_assignee(assignee: str | None) -> tuple[str, str] | None:
    """If ``assignee`` looks like ``"<slug>/<profile>"``, return ``(slug, profile)``.

    ``slug`` follows the same rules as board slugs (kebab-case-ish).
    Anything else (including bare ``"<profile>"``) returns None — the
    dispatcher treats it as local.
    """
    if not assignee or "/" not in assignee:
        return None
    slug, _, profile = assignee.partition("/")
    if not slug or not profile:
        return None
    # Cheap shape check; full validate_slug runs at registration time.
    if not all(ch.isalnum() or ch in "-_" for ch in slug):
        return None
    return slug, profile


# ---------------------------------------------------------------------------
# Local → Remote: delegate
# ---------------------------------------------------------------------------


def delegate_task_to_remote(
    conn: sqlite3.Connection,
    *,
    task: kdb.Task,
    host: RemoteHost,
    profile: str,
    local_callback_url: str,
    ttl_seconds: int = DEFAULT_REMOTE_TTL_SECONDS,
) -> RemoteClaim:
    """POST ``/proxy/spawn`` to ``host`` and record the lease locally.

    ``profile`` is the remote-side profile that will execute the task.
    ``local_callback_url`` is where the peer should POST terminal
    callbacks (typically this host's
    ``http://<our-host>:9119/api/plugins/kanban/proxy/callback``).

    Wave 6.E.15: when ``task.workspace_kind == "dir"``, the workspace
    path exists locally, AND the host has ``workspace_sync_enabled``,
    packs a gzipped tarball of the workspace and includes it as
    ``workspace_payload_b64`` in the request body. Capped at the
    workspace_payload module's default (50 MiB).

    Idempotency: if a row already exists for ``(task.id, host.slug)``,
    raises sqlite3.IntegrityError on the INSERT — caller should detect
    and skip re-delegation.
    """
    payload: dict = {
        "schema_version": 2,
        "task": {
            "id": task.id,
            "title": task.title,
            "body": task.body,
            "assignee": profile,
            "priority": task.priority,
            "tenant": task.tenant,
            "workspace_kind": task.workspace_kind,
            "workspace_path": task.workspace_path,
        },
        "callback_url": local_callback_url,
    }

    # Wave 6.E.15 — pack the workspace if both sides opted in.
    if (
        task.workspace_kind == "dir"
        and task.workspace_path
        and host.workspace_sync_enabled
    ):
        try:
            import base64
            from pathlib import Path as _Path

            from opencomputer.kanban.workspace_payload import (
                WorkspacePayloadError,
                pack_workspace,
            )
            ws_path = _Path(task.workspace_path)
            if ws_path.is_dir():
                tarball = pack_workspace(ws_path)
                payload["workspace_payload_b64"] = base64.b64encode(
                    tarball,
                ).decode("ascii")
        except WorkspacePayloadError as exc:
            raise RemoteDispatchError(
                f"workspace pack failed for task {task.id}: {exc}"
            ) from exc

    body = json.dumps(payload).encode("utf-8")
    path = "/api/plugins/kanban/proxy/spawn"
    headers = signed_headers(
        secret=host.hmac_secret,
        method="POST",
        path=path,
        body=body,
        extra={"Content-Type": "application/json"},
    )

    try:
        resp = httpx.post(
            f"{host.url.rstrip('/')}{path}",
            content=body,
            headers=headers,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise RemoteDispatchError(
            f"POST {host.url}{path} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code == 401:
        raise RemoteDispatchError(
            f"{host.slug}: 401 — HMAC verification failed (secrets mismatched?)"
        )
    if resp.status_code == 422:
        raise RemoteDispatchError(
            f"{host.slug}: 422 — peer rejected task: {resp.text[:200]}"
        )
    if resp.status_code != 200:
        raise RemoteDispatchError(
            f"{host.slug}: HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RemoteDispatchError(
            f"{host.slug}: non-JSON response: {resp.text[:200]}"
        ) from exc

    remote_task_id = data.get("remote_task_id") or task.id
    lease_until = int(data.get("lease_until") or (int(time.time()) + ttl_seconds))
    now = int(time.time())

    # Record the claim locally.
    with kdb.write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_remote_claims "
            "(local_task_id, remote_slug, remote_task_id, leased_at, "
            " lease_until, status, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (task.id, host.slug, remote_task_id, now, lease_until, now),
        )

    # Update peer's last_seen on successful round-trip
    try:
        update_last_seen(conn, host.slug)
    except Exception:  # noqa: BLE001
        pass

    return RemoteClaim(
        local_task_id=task.id,
        remote_slug=host.slug,
        remote_task_id=remote_task_id,
        leased_at=now,
        lease_until=lease_until,
        status="pending",
        last_heartbeat=now,
    )


# ---------------------------------------------------------------------------
# Heartbeat refresh
# ---------------------------------------------------------------------------


def heartbeat_remote_claim(
    conn: sqlite3.Connection,
    *,
    claim: RemoteClaim,
    host: RemoteHost,
    ttl_seconds: int = DEFAULT_REMOTE_TTL_SECONDS,
) -> int:
    """Refresh ``claim``'s lease via ``POST /proxy/heartbeat``.

    Returns the new ``lease_until`` reported by the peer (server-time
    TTL — we don't fake it from our clock). On HTTP/HMAC failure
    raises RemoteDispatchError; the caller's policy decides whether
    to abandon the claim or retry next tick.
    """
    payload = {
        "schema_version": 2,
        "remote_task_id": claim.remote_task_id,
    }
    body = json.dumps(payload).encode("utf-8")
    path = "/api/plugins/kanban/proxy/heartbeat"
    headers = signed_headers(
        secret=host.hmac_secret,
        method="POST",
        path=path,
        body=body,
        extra={"Content-Type": "application/json"},
    )

    try:
        resp = httpx.post(
            f"{host.url.rstrip('/')}{path}",
            content=body,
            headers=headers,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise RemoteDispatchError(
            f"heartbeat to {host.slug} failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise RemoteDispatchError(
            f"heartbeat {host.slug}: HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
        new_lease_until = int(data.get("lease_until"))
    except (ValueError, TypeError) as exc:
        raise RemoteDispatchError(
            f"heartbeat {host.slug}: bad response {resp.text[:200]}"
        ) from exc

    now = int(time.time())
    with kdb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_remote_claims SET lease_until = ?, last_heartbeat = ? "
            "WHERE local_task_id = ? AND remote_slug = ?",
            (new_lease_until, now, claim.local_task_id, claim.remote_slug),
        )
    return new_lease_until


# ---------------------------------------------------------------------------
# Remote → Local: callback reconciliation
# ---------------------------------------------------------------------------


def list_pending_remote_claims(conn: sqlite3.Connection) -> list[RemoteClaim]:
    """Return every claim with ``status = 'pending'``."""
    rows = conn.execute(
        "SELECT * FROM kanban_remote_claims WHERE status = 'pending'"
    ).fetchall()
    return [
        RemoteClaim(
            local_task_id=r["local_task_id"],
            remote_slug=r["remote_slug"],
            remote_task_id=r["remote_task_id"],
            leased_at=r["leased_at"],
            lease_until=r["lease_until"],
            status=r["status"],
            last_heartbeat=r["last_heartbeat"],
        )
        for r in rows
    ]


def find_claim_by_remote_id(
    conn: sqlite3.Connection, *, remote_slug: str, remote_task_id: str,
) -> RemoteClaim | None:
    row = conn.execute(
        "SELECT * FROM kanban_remote_claims "
        "WHERE remote_slug = ? AND remote_task_id = ?",
        (remote_slug, remote_task_id),
    ).fetchone()
    if row is None:
        return None
    return RemoteClaim(
        local_task_id=row["local_task_id"],
        remote_slug=row["remote_slug"],
        remote_task_id=row["remote_task_id"],
        leased_at=row["leased_at"],
        lease_until=row["lease_until"],
        status=row["status"],
        last_heartbeat=row["last_heartbeat"],
    )


def reconcile_callback(
    conn: sqlite3.Connection,
    *,
    remote_slug: str,
    payload: dict,
) -> None:
    """Apply a terminal callback from a peer. Idempotent.

    ``payload`` must contain ``remote_task_id`` and ``outcome``
    (``done`` | ``blocked`` | ``failed``). Optional ``summary``,
    ``error``, ``metadata``, and (Wave 6.E.15) ``workspace_payload_b64``.

    When ``workspace_payload_b64`` is present AND this peer has
    ``workspace_sync_enabled``, the modified workspace contents are
    extracted into the LOCAL task's original ``dir:<path>`` location
    via :func:`replace_workspace_atomic`. Failure to apply rolls the
    local task to ``blocked`` rather than ``done`` so the operator
    can see the partial result.

    Updates the kanban_remote_claims row + transitions the local
    task. Audit lens A6: caller MUST have already verified the HMAC
    signature before calling this — we don't re-verify here because
    the verify step belongs at the HTTP boundary, not this DB-level
    helper.
    """
    remote_task_id = payload.get("remote_task_id")
    outcome = (payload.get("outcome") or "").lower()
    if not remote_task_id or outcome not in {"done", "blocked", "failed"}:
        raise ValueError(
            f"reconcile_callback: invalid payload {payload!r}"
        )
    claim = find_claim_by_remote_id(
        conn, remote_slug=remote_slug, remote_task_id=remote_task_id,
    )
    if claim is None:
        # Audit lens A6 (defense in depth): even with a valid HMAC,
        # the remote_task_id must match an active claim. A signed-but-
        # spoofed callback is rejected.
        raise ValueError(
            f"no active claim for {remote_slug}/{remote_task_id}"
        )

    summary = payload.get("summary")
    error = payload.get("error")
    metadata = payload.get("metadata")

    # Wave 6.E.15 — return-trip workspace payload. Apply BEFORE the
    # status transition so a failed apply downgrades 'done' to 'blocked'.
    ws_apply_error: str | None = None
    ws_payload_b64 = payload.get("workspace_payload_b64")
    if ws_payload_b64 and outcome == "done":
        from opencomputer.kanban import remote_hosts as _rh
        host = _rh.find_remote_host(conn, remote_slug)
        if host is None or not host.workspace_sync_enabled:
            ws_apply_error = (
                f"peer sent workspace payload but sync is disabled "
                f"for {remote_slug!r}"
            )
        else:
            local_task = kdb.get_task(conn, claim.local_task_id)
            if (
                local_task is not None
                and local_task.workspace_kind == "dir"
                and local_task.workspace_path
            ):
                try:
                    import base64
                    from pathlib import Path

                    from opencomputer.kanban.workspace_payload import (
                        WorkspacePayloadError,
                        replace_workspace_atomic,
                        unpack_workspace,
                    )
                    tarball = base64.b64decode(ws_payload_b64, validate=True)
                    target = Path(local_task.workspace_path)
                    staging = target.parent / (target.name + ".incoming")
                    if staging.exists():
                        import shutil
                        shutil.rmtree(staging)
                    extracted = unpack_workspace(tarball, dest=staging)
                    replace_workspace_atomic(target, extracted)
                except (ValueError, WorkspacePayloadError) as exc:
                    ws_apply_error = (
                        f"workspace payload apply failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
    if ws_apply_error is not None:
        # Demote 'done' to 'blocked' so the operator notices.
        outcome = "blocked"
        error = (error + " | " + ws_apply_error) if error else ws_apply_error

    # Map outcome → local task status.
    final_claim_status = "done" if outcome == "done" else outcome

    with kdb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_remote_claims SET status = ? "
            "WHERE local_task_id = ? AND remote_slug = ?",
            (final_claim_status, claim.local_task_id, claim.remote_slug),
        )
        if outcome == "done":
            conn.execute(
                "UPDATE tasks SET status = 'done', "
                "result = ?, completed_at = ? WHERE id = ?",
                (
                    json.dumps({
                        "summary": summary,
                        "metadata": metadata,
                    }) if (summary or metadata) else None,
                    int(time.time()),
                    claim.local_task_id,
                ),
            )
        else:
            # blocked / failed
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                (claim.local_task_id,),
            )
        kdb._append_event(
            conn, claim.local_task_id, "remote_callback",
            {
                "remote_slug": remote_slug,
                "outcome": outcome,
                "summary": summary,
                "error": error,
            },
        )


# ---------------------------------------------------------------------------
# Inbound (peer side) — verify + claim locally
# ---------------------------------------------------------------------------


def verify_inbound_request(
    conn: sqlite3.Connection,
    *,
    slug: str,
    header_value: str | None,
    method: str,
    path: str,
    body: bytes,
) -> None:
    """Verify an inbound HMAC-signed request from peer ``slug``.

    Raises HmacAuthError if the signature is missing, expired, or
    forged. On success the ``last_seen_at`` column is bumped so the
    local operator can see when each peer last reached us.
    """
    host = find_remote_host(conn, slug)
    if host is None:
        raise HmacAuthError(f"unknown peer slug: {slug}")
    verify_request(
        header_value,
        secret=host.hmac_secret,
        method=method,
        path=path,
        body=body,
    )
    update_last_seen(conn, slug)


__all__ = [
    "RemoteClaim",
    "RemoteDispatchError",
    "DEFAULT_REMOTE_TTL_SECONDS",
    "HEARTBEAT_LEAD_SECONDS",
    "parse_remote_assignee",
    "delegate_task_to_remote",
    "heartbeat_remote_claim",
    "reconcile_callback",
    "list_pending_remote_claims",
    "find_claim_by_remote_id",
    "verify_inbound_request",
]
