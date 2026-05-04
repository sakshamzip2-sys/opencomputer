"""HTTP client for the kanban remote-read proxy (Wave 6.E.11).

Reads a board hosted on another OC instance's dashboard. Useful for
monitoring + reporting; does NOT support writes (Hermes 'multi-host
coordination' minimum scope — distributed claim coordination requires
distributed locks + clock-skew handling that this primitive does not
attempt).

Usage::

    client = RemoteKanbanClient(
        url="http://other-host:9119",
        token=os.environ["OC_REMOTE_TOKEN"],
    )
    health = client.health()         # {schema_version, boards, active_board}
    board = client.board(slug="x")   # {tasks: [...]}
    task = client.task("t-123", slug="x")

All methods raise :class:`RemoteKanbanError` on network or HTTP
failures. Schema-version mismatches raise too — newer-than-expected
servers are accepted (forward-compat) but older-than-expected aren't.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("opencomputer.kanban.remote_client")

CLIENT_SCHEMA_VERSION = 1


class RemoteKanbanError(RuntimeError):
    """Network or HTTP failure talking to a remote board."""


class RemoteKanbanClient:
    """Synchronous HTTP client for ``/api/plugins/kanban/proxy/*``."""

    def __init__(
        self,
        *,
        url: str,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        url = f"{self._base}/api/plugins/kanban/proxy{path}"
        try:
            resp = httpx.get(
                url, params=params, headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise RemoteKanbanError(
                f"GET {url} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise RemoteKanbanError(f"GET {url} → 401 (bad/missing token)")
        if resp.status_code == 404:
            raise RemoteKanbanError(f"GET {url} → 404: {resp.text[:200]}")
        if resp.status_code != 200:
            raise RemoteKanbanError(
                f"GET {url} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RemoteKanbanError(
                f"GET {url} returned non-JSON: {resp.text[:200]}"
            ) from exc
        # Schema-version compat check: tolerate newer-than-expected
        # (forward-compat) but reject older.
        sv = data.get("schema_version")
        if sv is not None and sv < CLIENT_SCHEMA_VERSION:
            raise RemoteKanbanError(
                f"remote schema_version {sv} < client {CLIENT_SCHEMA_VERSION}"
            )
        return data

    def health(self) -> dict[str, Any]:
        """Return the proxy health envelope: {boards, active_board, ...}."""
        return self._get("/health")

    def board(
        self,
        *,
        slug: str | None = None,
        tenant: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Snapshot of a board's tasks.

        ``slug=None`` reads the remote's currently-active board.
        """
        params: dict[str, Any] = {}
        if slug is not None:
            params["slug"] = slug
        if tenant is not None:
            params["tenant"] = tenant
        if include_archived:
            params["include_archived"] = "true"
        return self._get("/board", **params)

    def task(self, task_id: str, *, slug: str | None = None) -> dict[str, Any]:
        """Read a single task + its comments."""
        params: dict[str, Any] = {}
        if slug is not None:
            params["slug"] = slug
        return self._get(f"/task/{task_id}", **params)


__all__ = ["RemoteKanbanClient", "RemoteKanbanError", "CLIENT_SCHEMA_VERSION"]
