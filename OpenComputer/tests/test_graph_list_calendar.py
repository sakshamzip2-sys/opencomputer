"""Tests for ``opencomputer.tools.graph_calendar.GraphListCalendarTool``.

Build-chunk 3 of Milestone 3 — the agent-facing Microsoft Graph calendar tool.

The HTTP layer is mocked with the built-in :class:`httpx.MockTransport`
(``respx`` is not a dev dependency). Token acquisition is stubbed by patching
``opencomputer.auth.graph_oauth.get_valid_access_token`` / ``has_stored_token``.

Coverage:

* the capability claim is ``EXPLICIT`` (not ``IMPLICIT`` — the tool reads cloud
  data);
* a default now → now+7d window is used when start/end are omitted, and the
  ``calendarView`` query carries ``startDateTime`` / ``endDateTime`` / ``$top``;
* malformed ISO datetimes / an end-before-start window are rejected;
* the 401 → force-refresh → retry-once path: a first-attempt 401 is followed
  by exactly one retry with a freshly-refreshed token;
* the not-authenticated path returns the clean "run `oc auth login graph`"
  error.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from opencomputer.integrations.graph.client import GRAPH_BASE_URL, GraphClient
from opencomputer.tools.graph_calendar import GraphListCalendarTool
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
    """Patch the calendar tool's token acquisition + ``GraphClient`` transport.

    ``token_factory`` — when given — replaces ``get_valid_access_token`` (so a
    test can observe ``force_refresh``); otherwise a constant token is used.
    """

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


def _events_response(events: list[dict[str, Any]]) -> httpx.Response:
    """A single-page ``calendarView`` collection response (no nextLink)."""
    return httpx.Response(200, json={"value": events})


def _sample_event() -> dict[str, Any]:
    return {
        "id": "evt-1",
        "subject": "Team standup",
        "start": {"dateTime": "2026-05-16T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-16T09:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Room 4"},
        "organizer": {
            "emailAddress": {"name": "Alice", "address": "alice@example.com"}
        },
        "isAllDay": False,
        "isCancelled": False,
    }


def _call(**arguments: Any) -> ToolCall:
    return ToolCall(id="cal-1", name="GraphListCalendar", arguments=arguments)


# --------------------------------------------------------------------------
# Capability claim
# --------------------------------------------------------------------------


def test_capability_claim_is_explicit() -> None:
    """Reading the calendar is a cloud-data read — EXPLICIT, not IMPLICIT."""
    claims = GraphListCalendarTool.capability_claims
    assert len(claims) == 1
    claim = claims[0]
    assert claim.tier_required is ConsentTier.EXPLICIT
    assert claim.capability_id == "graph.calendar.read"
    assert isinstance(claims, tuple)


# --------------------------------------------------------------------------
# Happy path + default window
# --------------------------------------------------------------------------


async def test_lists_events_with_explicit_window() -> None:
    """An explicit window is forwarded as startDateTime/endDateTime + $top."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _events_response([_sample_event()])

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(
            _call(start="2026-05-16T00:00:00Z", end="2026-05-17T00:00:00Z")
        )

    assert result.is_error is False
    assert "Team standup" in result.content
    assert "Room 4" in result.content
    assert "alice@example.com" in result.content or "Alice" in result.content

    assert log.count == 1
    request = log.requests[0]
    assert request.url.path == "/v1.0/me/calendarView"
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query["startDateTime"] == ["2026-05-16T00:00:00+00:00"]
    assert query["endDateTime"] == ["2026-05-17T00:00:00+00:00"]
    # $top is set explicitly — calendarView's server default is only 10.
    assert "$top" in query


async def test_default_window_is_now_plus_seven_days() -> None:
    """With no start/end, the window defaults to now → now + 7 days (UTC)."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _events_response([])

    before = datetime.now(UTC)
    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(_call())
    after = datetime.now(UTC)

    assert result.is_error is False
    query = parse_qs(urlsplit(str(log.requests[0].url)).query)
    start = datetime.fromisoformat(query["startDateTime"][0])
    end = datetime.fromisoformat(query["endDateTime"][0])
    # start is "now" (within the call window).
    assert before - timedelta(seconds=5) <= start <= after + timedelta(seconds=5)
    # end is start + 7 days.
    assert abs((end - start) - timedelta(days=7)) < timedelta(seconds=1)


async def test_empty_calendar_is_reported_cleanly() -> None:
    """An empty window yields a non-error 'no events' result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _events_response([])

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(_call())

    assert result.is_error is False
    assert "no events" in result.content.lower()


async def test_naive_datetime_is_treated_as_utc() -> None:
    """A start/end value with no offset is normalized to a UTC offset."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _events_response([])

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(
            _call(start="2026-05-16T08:00:00", end="2026-05-16T18:00:00")
        )

    assert result.is_error is False
    query = parse_qs(urlsplit(str(log.requests[0].url)).query)
    assert query["startDateTime"] == ["2026-05-16T08:00:00+00:00"]
    assert query["endDateTime"] == ["2026-05-16T18:00:00+00:00"]


# --------------------------------------------------------------------------
# Invalid window
# --------------------------------------------------------------------------


async def test_malformed_start_datetime_is_rejected() -> None:
    """A non-ISO start datetime is rejected before any request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _events_response([])

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(
            _call(start="last tuesday")
        )

    assert result.is_error is True
    assert "iso-8601" in result.content.lower()
    assert log.count == 0


async def test_end_before_start_is_rejected() -> None:
    """An end-before-start window is rejected before any request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _events_response([])

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(
            _call(start="2026-05-17T00:00:00Z", end="2026-05-16T00:00:00Z")
        )

    assert result.is_error is True
    assert "after" in result.content.lower()
    assert log.count == 0


# --------------------------------------------------------------------------
# 401 → force-refresh → retry once
# --------------------------------------------------------------------------


async def test_401_triggers_force_refresh_and_one_retry() -> None:
    """A first-attempt 401 is followed by exactly one retry after a refresh."""
    log = _RequestLog()
    # First request 401s; the retry (with a refreshed token) succeeds.
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
            _events_response([_sample_event()]),
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
        result = await GraphListCalendarTool().execute(_call())

    assert result.is_error is False
    assert "Team standup" in result.content
    # Two HTTP attempts — the 401 then the retry.
    assert log.count == 2
    # Token acquired twice: once normally, once force-refreshed.
    assert refresh_flags == [False, True]
    # The retry carried the freshly-refreshed token.
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
        result = await GraphListCalendarTool().execute(_call())

    assert result.is_error is True
    assert "401" in result.content or "revoked" in result.content.lower()
    # Exactly two attempts — the original and the single retry. No loop.
    assert log.count == 2


async def test_non_401_error_is_not_retried() -> None:
    """A non-401 Graph error is surfaced immediately — no force-refresh."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return httpx.Response(
            403, json={"error": {"code": "Forbidden", "message": "nope"}}
        )

    with _patched_graph(handler):
        result = await GraphListCalendarTool().execute(_call())

    assert result.is_error is True
    assert log.count == 1


# --------------------------------------------------------------------------
# Not authenticated
# --------------------------------------------------------------------------


async def test_not_authenticated_returns_clean_error() -> None:
    """With no stored token the tool refuses cleanly and makes no request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _events_response([])

    with _patched_graph(handler, has_token=False):
        result = await GraphListCalendarTool().execute(_call())

    assert result.is_error is True
    assert "oc auth login graph" in result.content
    assert log.count == 0
