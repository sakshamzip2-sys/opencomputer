"""Tests for ``opencomputer.tools.graph_drive.GraphListDriveFilesTool``.

Build-chunk 3 of Milestone 3 — the agent-facing Microsoft Graph OneDrive tool.

The HTTP layer is mocked with the built-in :class:`httpx.MockTransport`
(``respx`` is not a dev dependency). Token acquisition is stubbed by patching
``opencomputer.auth.graph_oauth.get_valid_access_token`` / ``has_stored_token``.

Coverage:

* the capability claim is ``EXPLICIT`` (not ``IMPLICIT`` — the tool reads cloud
  data);
* a root listing hits ``/me/drive/root/children``; a ``folder_path`` hits the
  path-addressed ``/me/drive/root:/{path}:/children`` endpoint;
* folders vs files are discriminated on the ``folder`` / ``package`` facet, not
  on the ``file`` facet;
* the 401 → force-refresh → retry-once path;
* the not-authenticated path returns the clean "run `oc auth login graph`"
  error.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import httpx

from opencomputer.integrations.graph.client import GRAPH_BASE_URL, GraphClient
from opencomputer.tools.graph_drive import GraphListDriveFilesTool
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

# pytest-asyncio runs in `asyncio_mode = "auto"`.


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _RequestLog:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)

    @property
    def count(self) -> int:
        return len(self.requests)


@contextlib.contextmanager
def _patched_graph(
    handler: Any,
    *,
    has_token: bool = True,
    token_factory: Any = None,
):
    """Patch the drive tool's token acquisition + ``GraphClient`` transport."""

    def _client_factory(access_token: str, **_kwargs: Any) -> GraphClient:
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(base_url=GRAPH_BASE_URL, transport=transport)
        return GraphClient(access_token, http_client=http)

    token_patch = (
        patch(
            "opencomputer.tools._graph_common.get_valid_access_token",
            side_effect=token_factory,
        )
        if token_factory is not None
        else patch(
            "opencomputer.tools._graph_common.get_valid_access_token",
            return_value="tok",
        )
    )
    with (
        patch("opencomputer.tools._graph_common.GraphClient", _client_factory),
        token_patch,
        patch(
            "opencomputer.tools._graph_common.has_stored_token",
            return_value=has_token,
        ),
    ):
        yield


def _items_response(items: list[dict[str, Any]]) -> httpx.Response:
    """A single-page drive ``children`` collection response (no nextLink)."""
    return httpx.Response(200, json={"value": items})


def _folder(name: str, child_count: int = 3) -> dict[str, Any]:
    return {
        "id": f"folder-{name}",
        "name": name,
        "folder": {"childCount": child_count},
        "lastModifiedDateTime": "2026-05-10T12:00:00Z",
    }


def _file(name: str, size: int = 2048, mime: str = "text/plain") -> dict[str, Any]:
    return {
        "id": f"file-{name}",
        "name": name,
        "size": size,
        "file": {"mimeType": mime},
        "lastModifiedDateTime": "2026-05-12T09:30:00Z",
    }


def _call(**arguments: Any) -> ToolCall:
    return ToolCall(id="drv-1", name="GraphListDriveFiles", arguments=arguments)


# --------------------------------------------------------------------------
# Capability claim
# --------------------------------------------------------------------------


def test_capability_claim_is_explicit() -> None:
    """Reading the drive is a cloud-data read — EXPLICIT, not IMPLICIT."""
    claims = GraphListDriveFilesTool.capability_claims
    assert len(claims) == 1
    claim = claims[0]
    assert claim.tier_required is ConsentTier.EXPLICIT
    assert claim.capability_id == "graph.drive.read"
    assert isinstance(claims, tuple)


# --------------------------------------------------------------------------
# Root listing + folder/file discrimination
# --------------------------------------------------------------------------


async def test_lists_root_with_folders_and_files() -> None:
    """A root listing hits /me/drive/root/children and labels folders/files."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _items_response(
            [_folder("Documents"), _file("notes.txt", size=1536)]
        )

    with _patched_graph(handler):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is False
    assert "Documents" in result.content
    assert "notes.txt" in result.content
    # Folder vs file are visibly distinguished.
    assert "[DIR]" in result.content
    assert "[FILE]" in result.content

    assert log.count == 1
    assert log.requests[0].url.path == "/v1.0/me/drive/root/children"


async def test_folder_path_uses_path_addressed_endpoint() -> None:
    """A folder_path hits the /me/drive/root:/{path}:/children endpoint."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _items_response([_file("report.pdf")])

    with _patched_graph(handler):
        result = await GraphListDriveFilesTool().execute(
            _call(folder_path="Documents/Reports")
        )

    assert result.is_error is False
    assert log.count == 1
    # Path-addressed form: root:/{path}:/children.
    assert log.requests[0].url.path == (
        "/v1.0/me/drive/root:/Documents/Reports:/children"
    )


async def test_package_facet_is_treated_as_a_folder() -> None:
    """An item with a `package` facet (no `folder`) is listed as a directory."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _items_response(
            [
                {
                    "id": "pkg-1",
                    "name": "My Notebook",
                    "package": {"type": "oneNote"},
                }
            ]
        )

    with _patched_graph(handler):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is False
    assert "[DIR]" in result.content
    assert "My Notebook" in result.content
    # It must NOT be miscounted as a file.
    assert "0 file(s)" in result.content


async def test_empty_drive_is_reported_cleanly() -> None:
    """An empty folder yields a non-error 'no files or folders' result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _items_response([])

    with _patched_graph(handler):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is False
    assert "no files or folders" in result.content.lower()


async def test_non_string_folder_path_is_rejected() -> None:
    """A non-string folder_path is rejected before any request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _items_response([])

    with _patched_graph(handler):
        result = await GraphListDriveFilesTool().execute(
            _call(folder_path=["Documents"])
        )

    assert result.is_error is True
    assert "folder_path" in result.content.lower()
    assert log.count == 0


# --------------------------------------------------------------------------
# 401 → force-refresh → retry once
# --------------------------------------------------------------------------


async def test_401_triggers_force_refresh_and_one_retry() -> None:
    """A first-attempt 401 is followed by exactly one retry after a refresh."""
    log = _RequestLog()
    responses = iter(
        [
            httpx.Response(
                401,
                json={
                    "error": {
                        "code": "InvalidAuthenticationToken",
                        "message": "expired",
                    }
                },
            ),
            _items_response([_file("ok.txt")]),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return next(responses)

    refresh_flags: list[bool] = []

    def _token(*, force_refresh: bool = False) -> str:
        refresh_flags.append(force_refresh)
        return "refreshed" if force_refresh else "stale"

    with _patched_graph(handler, token_factory=_token):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is False
    assert "ok.txt" in result.content
    assert log.count == 2
    assert refresh_flags == [False, True]
    assert log.requests[1].headers["Authorization"] == "Bearer refreshed"


async def test_persistent_401_after_retry_is_surfaced() -> None:
    """If the retry also 401s, a clean error is returned (no third attempt)."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "InvalidAuthenticationToken",
                    "message": "expired",
                }
            },
        )

    def _token(*, force_refresh: bool = False) -> str:
        return "tok"

    with _patched_graph(handler, token_factory=_token):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is True
    assert log.count == 2


# --------------------------------------------------------------------------
# Not authenticated
# --------------------------------------------------------------------------


async def test_not_authenticated_returns_clean_error() -> None:
    """With no stored token the tool refuses cleanly and makes no request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _items_response([])

    with _patched_graph(handler, has_token=False):
        result = await GraphListDriveFilesTool().execute(_call())

    assert result.is_error is True
    assert "oc auth login graph" in result.content
    assert log.count == 0
