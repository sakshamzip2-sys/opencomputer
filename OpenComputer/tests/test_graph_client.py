"""Tests for ``opencomputer.integrations.graph.client.GraphClient``.

Build-chunk 1 of Milestone 3 — the Graph HTTP client library.

``respx`` is not a dev dependency, so ``httpx`` is mocked with the built-in
:class:`httpx.MockTransport`: a handler function inspects each
:class:`httpx.Request` and returns a canned :class:`httpx.Response`. The
transport is injected through ``GraphClient``'s ``http_client`` seam.

Coverage:

* the exact nested ``sendMail`` body (``message`` → ``toRecipients`` →
  ``emailAddress`` → ``address``, ``saveToSentItems`` as a sibling);
* HTTP 202 → success, non-2xx → :class:`GraphAPIError`;
* the pagination loop concatenates ``value`` across ``@odata.nextLink`` pages
  and stops when the link is absent — and respects ``max_items``;
* ``$top`` is set on ``calendarView`` (and ``drive``);
* a 429 + ``Retry-After`` is honored with bounded retries;
* ``mail.send`` is **not** auto-retried on 429.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from opencomputer.integrations.graph.client import (
    GRAPH_BASE_URL,
    MAX_THROTTLE_RETRIES,
    GraphAPIError,
    GraphClient,
    GraphTransportError,
)

# pytest-asyncio is in `asyncio_mode = "auto"` — `async def test_*` just works.


# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------


def _client_with_handler(
    handler: Any, *, token: str = "test-access-token"
) -> GraphClient:
    """Build a :class:`GraphClient` whose HTTP layer is a mock transport.

    ``handler`` is a callable ``httpx.Request -> httpx.Response``.
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(base_url=GRAPH_BASE_URL, transport=transport)
    return GraphClient(token, http_client=http_client)


def _json_response(
    status_code: int, payload: dict[str, Any], **kwargs: Any
) -> httpx.Response:
    """A JSON :class:`httpx.Response` with the canonical content type."""
    return httpx.Response(status_code, json=payload, **kwargs)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_rejects_empty_token() -> None:
    """An empty / whitespace token is rejected at construction time."""
    with pytest.raises(ValueError, match="non-empty access token"):
        GraphClient("")
    with pytest.raises(ValueError, match="non-empty access token"):
        GraphClient("   ")


async def test_owned_http_client_is_closed_on_aexit() -> None:
    """When ``GraphClient`` builds its own httpx client it closes it on exit."""
    async with GraphClient("tok") as client:
        owned = client._http
    assert owned.is_closed is True


async def test_injected_http_client_is_not_closed() -> None:
    """An injected httpx client is caller-owned — ``aclose`` must not close it."""
    transport = httpx.MockTransport(
        lambda req: _json_response(200, {"value": []})
    )
    injected = httpx.AsyncClient(base_url=GRAPH_BASE_URL, transport=transport)
    async with GraphClient("tok", http_client=injected) as client:
        assert client._http is injected
    assert injected.is_closed is False
    await injected.aclose()


# --------------------------------------------------------------------------
# Authorization header — token is sent, never mangled
# --------------------------------------------------------------------------


async def test_bearer_token_is_sent_on_every_request() -> None:
    """The access token rides in the ``Authorization: Bearer`` header."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return _json_response(200, {"value": []})

    async with _client_with_handler(handler, token="super-secret-token") as client:
        await client.drive.list()

    assert seen["auth"] == "Bearer super-secret-token"


async def test_token_not_baked_into_default_headers() -> None:
    """The token must not live in the httpx client's default headers.

    Keeping it out of the client defaults means it cannot leak via the
    client's repr or any header dump that excludes the per-request layer.
    """
    async with GraphClient("leak-check-token") as client:
        joined = " ".join(client._http.headers.values())
    assert "leak-check-token" not in joined


# --------------------------------------------------------------------------
# mail.send — body shape, 202 success, error mapping
# --------------------------------------------------------------------------


async def test_send_mail_builds_exact_nested_body() -> None:
    """``mail.send`` produces the precise Graph ``Message`` nesting."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = urlsplit(str(request.url)).path
        captured["body"] = json.loads(request.content)
        return httpx.Response(202)

    async with _client_with_handler(handler) as client:
        result = await client.mail.send(
            to=["frannis@contoso.com", "second@contoso.com"],
            subject="Meet for lunch?",
            body="The new cafeteria is open.",
            cc=["danas@contoso.com"],
            body_type="Text",
            save_to_sent_items=False,
        )

    assert result is None  # a successful send returns None
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1.0/me/sendMail"

    body = captured["body"]
    # saveToSentItems is a SIBLING of `message`, not nested inside it.
    assert body["saveToSentItems"] is False
    assert "saveToSentItems" not in body["message"]

    message = body["message"]
    assert message["subject"] == "Meet for lunch?"
    assert message["body"] == {
        "contentType": "Text",
        "content": "The new cafeteria is open.",
    }
    # Double nesting: toRecipients -> emailAddress -> address.
    assert message["toRecipients"] == [
        {"emailAddress": {"address": "frannis@contoso.com"}},
        {"emailAddress": {"address": "second@contoso.com"}},
    ]
    assert message["ccRecipients"] == [
        {"emailAddress": {"address": "danas@contoso.com"}}
    ]
    # bcc was not supplied — the key must be absent, not an empty list.
    assert "bccRecipients" not in message


async def test_send_mail_html_body_type_normalized() -> None:
    """``body_type`` is normalized to Graph's expected casing."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202)

    async with _client_with_handler(handler) as client:
        await client.mail.send(
            to=["a@b.com"],
            subject="s",
            body="<p>hi</p>",
            body_type="html",  # lowercase on input
        )

    assert captured["body"]["message"]["body"]["contentType"] == "HTML"


async def test_send_mail_default_save_to_sent_items_true() -> None:
    """``saveToSentItems`` defaults to ``True`` when not overridden."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202)

    async with _client_with_handler(handler) as client:
        await client.mail.send(to=["a@b.com"], subject="s", body="b")

    assert captured["body"]["saveToSentItems"] is True


async def test_send_mail_202_is_success_even_with_empty_body() -> None:
    """A 202 with a genuinely empty body is success — no JSON parsing needed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)  # empty body, as Graph actually sends

    async with _client_with_handler(handler) as client:
        # Must not raise — the client treats 202 as success and does not try
        # to parse a body that is intentionally empty.
        assert await client.mail.send(to=["a@b.com"], subject="s", body="b") is None


async def test_send_mail_200_is_treated_as_error_not_success() -> None:
    """Only 202 is the documented success contract; a stray 200 is an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"unexpected": "shape"})

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.mail.send(to=["a@b.com"], subject="s", body="b")
    assert excinfo.value.status_code == 200


async def test_send_mail_400_raises_graph_api_error_with_body() -> None:
    """A non-2xx send surfaces a typed error carrying the Graph error body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            400,
            {
                "error": {
                    "code": "ErrorInvalidRecipients",
                    "message": "The recipient address is malformed.",
                }
            },
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.mail.send(to=["bad"], subject="s", body="b")

    err = excinfo.value
    assert err.status_code == 400
    assert err.error_code == "ErrorInvalidRecipients"
    assert err.error_message == "The recipient address is malformed."
    # The error code + message both surface in the string form.
    assert "ErrorInvalidRecipients" in str(err)
    assert "malformed" in str(err)


async def test_send_mail_requires_recipients() -> None:
    """An empty ``to`` list is a caller error caught before any HTTP call."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(202)

    async with _client_with_handler(handler) as client:
        with pytest.raises(ValueError, match="at least one recipient"):
            await client.mail.send(to=[], subject="s", body="b")

    assert call_count == 0  # no request was issued


async def test_send_mail_rejects_bad_body_type() -> None:
    """An unknown ``body_type`` is rejected before any HTTP call."""
    async with _client_with_handler(
        lambda req: httpx.Response(202)
    ) as client:
        with pytest.raises(ValueError, match="Text.*HTML|body_type"):
            await client.mail.send(
                to=["a@b.com"], subject="s", body="b", body_type="markdown"
            )


# --------------------------------------------------------------------------
# mail.send — NOT auto-retried on 429 (duplicate-email guard)
# --------------------------------------------------------------------------


async def test_send_mail_429_is_not_retried() -> None:
    """A 429 on ``sendMail`` is surfaced immediately — never re-POSTed.

    Re-POSTing ``sendMail`` could deliver a duplicate email; the client must
    issue the request exactly once and raise on the 429.
    """
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"error": {"code": "TooManyRequests", "message": "throttled"}},
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.mail.send(to=["a@b.com"], subject="s", body="b")

    assert request_count == 1, "mail.send must not retry — exactly one POST"
    assert excinfo.value.status_code == 429


async def test_send_mail_5xx_is_not_retried() -> None:
    """A 5xx on ``sendMail`` is also not retried — same duplicate-send guard."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response(
            503, {"error": {"code": "ServiceUnavailable", "message": "down"}}
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.mail.send(to=["a@b.com"], subject="s", body="b")

    assert request_count == 1
    assert excinfo.value.status_code == 503


# --------------------------------------------------------------------------
# calendar.list — endpoint, $top, ISO params
# --------------------------------------------------------------------------


async def test_calendar_list_uses_calendarview_with_top_and_window() -> None:
    """``calendar.list`` hits ``/me/calendarView`` with ``$top`` + the window."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        split = urlsplit(str(request.url))
        captured["path"] = split.path
        captured["query"] = parse_qs(split.query)
        return _json_response(200, {"value": [{"id": "evt-1"}]})

    async with _client_with_handler(handler) as client:
        events = await client.calendar.list(
            start_date_time="2026-05-16T00:00:00Z",
            end_date_time="2026-05-23T00:00:00Z",
        )

    assert events == [{"id": "evt-1"}]
    # calendarView, NOT /me/events.
    assert captured["path"] == "/v1.0/me/calendarView"
    query = captured["query"]
    # $top is set explicitly — calendarView's server default is only 10.
    assert "$top" in query
    assert int(query["$top"][0]) > 10
    assert query["startDateTime"] == ["2026-05-16T00:00:00Z"]
    assert query["endDateTime"] == ["2026-05-23T00:00:00Z"]


async def test_calendar_list_top_is_clamped_to_graph_max() -> None:
    """A ``page_size`` above Graph's 1000 ceiling is clamped, not passed raw."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = parse_qs(urlsplit(str(request.url)).query)
        return _json_response(200, {"value": []})

    async with _client_with_handler(handler) as client:
        await client.calendar.list(
            start_date_time="2026-05-16T00:00:00Z",
            end_date_time="2026-05-23T00:00:00Z",
            page_size=99_999,
        )

    assert int(captured["query"]["$top"][0]) == 1000


async def test_calendar_list_requires_both_datetimes() -> None:
    """Both window bounds are required — an empty bound is a caller error."""
    async with _client_with_handler(
        lambda req: _json_response(200, {"value": []})
    ) as client:
        with pytest.raises(ValueError, match="start_date_time and end_date_time"):
            await client.calendar.list(
                start_date_time="", end_date_time="2026-05-23T00:00:00Z"
            )


# --------------------------------------------------------------------------
# drive.list — root vs path-addressed
# --------------------------------------------------------------------------


async def test_drive_list_root_endpoint() -> None:
    """With no folder, ``drive.list`` lists the drive root."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = urlsplit(str(request.url)).path
        return _json_response(200, {"value": [{"id": "file-1", "name": "a.txt"}]})

    async with _client_with_handler(handler) as client:
        items = await client.drive.list()

    assert items == [{"id": "file-1", "name": "a.txt"}]
    assert captured["path"] == "/v1.0/me/drive/root/children"


async def test_drive_list_path_addressed_endpoint() -> None:
    """A folder path uses the ``root:/{path}:/children`` form, escaped."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Compare against the raw (un-decoded) path so we can see the escaping.
        captured["raw_path"] = urlsplit(str(request.url)).path
        return _json_response(200, {"value": []})

    async with _client_with_handler(handler) as client:
        # Leading/trailing slashes are tolerated; the space must be escaped.
        await client.drive.list(folder_path="/Documents/My Reports/")

    raw = captured["raw_path"]
    assert raw.startswith("/v1.0/me/drive/root:/")
    assert raw.endswith(":/children")
    # Separators stay literal; the space inside a segment is percent-escaped.
    assert "/Documents/" in raw
    assert "My%20Reports" in raw
    assert " " not in raw


async def test_drive_list_sets_top() -> None:
    """``drive.list`` sends ``$top`` for deterministic page sizing."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = parse_qs(urlsplit(str(request.url)).query)
        return _json_response(200, {"value": []})

    async with _client_with_handler(handler) as client:
        await client.drive.list()

    assert "$top" in captured["query"]


# --------------------------------------------------------------------------
# pagination — @odata.nextLink follow + concatenation + stop + max_items
# --------------------------------------------------------------------------


async def test_pagination_follows_nextlink_and_concatenates() -> None:
    """The loop concatenates ``value`` across pages and stops when no link."""
    page2_url = "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=PAGE2"
    page3_url = "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=PAGE3"
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        visited.append(url)
        query = parse_qs(urlsplit(url).query)
        token = query.get("$skiptoken", [None])[0]
        if token is None:
            # Page 1 — has a nextLink.
            return _json_response(
                200,
                {"value": [{"id": "p1-a"}, {"id": "p1-b"}], "@odata.nextLink": page2_url},
            )
        if token == "PAGE2":
            return _json_response(
                200, {"value": [{"id": "p2-a"}], "@odata.nextLink": page3_url}
            )
        if token == "PAGE3":
            # Last page — NO nextLink — the loop must stop here.
            return _json_response(200, {"value": [{"id": "p3-a"}, {"id": "p3-b"}]})
        raise AssertionError(f"unexpected skiptoken {token!r}")

    async with _client_with_handler(handler) as client:
        items = await client.drive.list()

    # All three pages concatenated, in order.
    assert [it["id"] for it in items] == ["p1-a", "p1-b", "p2-a", "p3-a", "p3-b"]
    # Exactly three requests — stopped when the link was absent.
    assert len(visited) == 3


async def test_pagination_nextlink_followed_verbatim() -> None:
    """An ``@odata.nextLink`` is GET-ed verbatim — no params re-appended."""
    next_link = (
        "https://graph.microsoft.com/v1.0/me/calendarView"
        "?startDateTime=2026-05-16T00:00:00Z&endDateTime=2026-05-23T00:00:00Z"
        "&%24skiptoken=OPAQUE_STATE&%24top=100"
    )
    second_request_url: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "skiptoken" not in url:
            return _json_response(
                200, {"value": [{"id": "evt-1"}], "@odata.nextLink": next_link}
            )
        second_request_url["url"] = url
        return _json_response(200, {"value": [{"id": "evt-2"}]})

    async with _client_with_handler(handler) as client:
        events = await client.calendar.list(
            start_date_time="2026-05-16T00:00:00Z",
            end_date_time="2026-05-23T00:00:00Z",
        )

    assert [e["id"] for e in events] == ["evt-1", "evt-2"]
    # The second request URL is exactly the nextLink — the skiptoken survived
    # and no duplicate query keys were appended.
    followed = second_request_url["url"]
    assert "skiptoken=OPAQUE_STATE" in followed
    query = parse_qs(urlsplit(followed).query)
    assert query["$skiptoken"] == ["OPAQUE_STATE"]
    # startDateTime appears exactly once — params were not re-appended.
    assert len(query["startDateTime"]) == 1


async def test_pagination_max_items_caps_and_truncates() -> None:
    """``max_items`` caps the total and truncates the page that crosses it."""
    page2_url = "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=P2"
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if "skiptoken" not in str(request.url):
            return _json_response(
                200,
                {
                    "value": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    "@odata.nextLink": page2_url,
                },
            )
        # This page would push the total to 6, but max_items=4 stops us first.
        return _json_response(
            200, {"value": [{"id": "d"}, {"id": "e"}, {"id": "f"}]}
        )

    async with _client_with_handler(handler) as client:
        items = await client.drive.list(max_items=4)

    assert [it["id"] for it in items] == ["a", "b", "c", "d"]
    # Two requests: the cap was reached on page 2; a third page (if any) is
    # never fetched.
    assert request_count == 2


async def test_pagination_max_items_stops_before_following_link() -> None:
    """If page 1 alone satisfies ``max_items``, the nextLink is not followed."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response(
            200,
            {
                "value": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skiptoken=Z",
            },
        )

    async with _client_with_handler(handler) as client:
        items = await client.drive.list(max_items=2)

    assert [it["id"] for it in items] == ["a", "b"]
    assert request_count == 1  # the nextLink was never fetched


async def test_pagination_max_items_zero_returns_empty() -> None:
    """``max_items=0`` short-circuits to an empty list without any request."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response(200, {"value": [{"id": "a"}]})

    async with _client_with_handler(handler) as client:
        items = await client.paginate("/me/drive/root/children", max_items=0)

    assert items == []
    assert request_count == 0


async def test_pagination_missing_value_key_yields_empty_page() -> None:
    """A page with no ``value`` key contributes nothing rather than crashing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"@odata.context": "..."})  # no "value"

    async with _client_with_handler(handler) as client:
        items = await client.drive.list()

    assert items == []


async def test_pagination_non_2xx_raises() -> None:
    """A non-2xx mid-pagination raises a typed error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            403, {"error": {"code": "Forbidden", "message": "no access"}}
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.drive.list()
    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "Forbidden"


# --------------------------------------------------------------------------
# 429 throttling on reads — bounded, honors Retry-After
# --------------------------------------------------------------------------


async def test_read_429_then_success_honors_retry_after(monkeypatch) -> None:
    """A read 429 + ``Retry-After`` sleeps that long, then retries to success."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "opencomputer.integrations.graph.client.asyncio.sleep", fake_sleep
    )

    responses = iter(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"error": {"code": "TooManyRequests", "message": "slow"}},
            ),
            _json_response(200, {"value": [{"id": "after-throttle"}]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with _client_with_handler(handler) as client:
        items = await client.drive.list()

    assert items == [{"id": "after-throttle"}]
    # Slept exactly once, for exactly the Retry-After value.
    assert sleeps == [7.0]


async def test_read_429_retry_after_is_clamped(monkeypatch) -> None:
    """An absurd ``Retry-After`` is clamped to the client's ceiling."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "opencomputer.integrations.graph.client.asyncio.sleep", fake_sleep
    )

    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "999999"}),
            _json_response(200, {"value": []}),
        ]
    )
    async with _client_with_handler(lambda req: next(responses)) as client:
        await client.drive.list()

    assert len(sleeps) == 1
    assert sleeps[0] <= 60.0  # MAX_RETRY_AFTER_S


async def test_read_429_retries_are_bounded(monkeypatch) -> None:
    """Persistent 429s stop after the fixed retry budget — never unbounded."""
    request_count = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "opencomputer.integrations.graph.client.asyncio.sleep", fake_sleep
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"error": {"code": "TooManyRequests", "message": "still slow"}},
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.drive.list()

    # Initial attempt + exactly MAX_THROTTLE_RETRIES retries, then it gives up.
    assert request_count == MAX_THROTTLE_RETRIES + 1
    assert len(sleeps) == MAX_THROTTLE_RETRIES
    assert excinfo.value.status_code == 429


async def test_read_429_without_retry_after_uses_bounded_fallback(
    monkeypatch,
) -> None:
    """A 429 lacking ``Retry-After`` falls back to a bounded backoff."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "opencomputer.integrations.graph.client.asyncio.sleep", fake_sleep
    )

    responses = iter(
        [
            httpx.Response(429),  # no Retry-After header at all
            _json_response(200, {"value": []}),
        ]
    )
    async with _client_with_handler(lambda req: next(responses)) as client:
        await client.drive.list()

    assert len(sleeps) == 1
    assert sleeps[0] > 0  # a positive, finite fallback delay was used


# --------------------------------------------------------------------------
# Error mapping — non-2xx and transport failures
# --------------------------------------------------------------------------


async def test_non_json_error_body_is_preserved() -> None:
    """A non-JSON error body is kept verbatim on the exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError) as excinfo:
            await client.drive.list()

    err = excinfo.value
    assert err.status_code == 502
    assert err.error_code is None
    assert err.raw_body == "<html>Bad Gateway</html>"
    assert "Bad Gateway" in str(err)


async def test_transport_failure_raises_graph_transport_error() -> None:
    """A connection-level failure becomes :class:`GraphTransportError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphTransportError) as excinfo:
            await client.drive.list()

    # The originating httpx error is preserved as the cause.
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


async def test_timeout_raises_graph_transport_error() -> None:
    """A timeout is surfaced as :class:`GraphTransportError`, not swallowed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphTransportError):
            await client.calendar.list(
                start_date_time="2026-05-16T00:00:00Z",
                end_date_time="2026-05-23T00:00:00Z",
            )


async def test_collection_endpoint_returning_non_json_raises() -> None:
    """A 200 collection response that is not JSON raises a clear error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    async with _client_with_handler(handler) as client:
        with pytest.raises(GraphAPIError, match="not JSON"):
            await client.drive.list()
