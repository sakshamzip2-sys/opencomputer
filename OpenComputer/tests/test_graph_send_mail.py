"""Tests for ``opencomputer.tools.graph_mail.GraphSendMailTool``.

Build-chunk 3 of Milestone 3 — the agent-facing Microsoft Graph send-mail tool.

The HTTP layer is mocked with the built-in :class:`httpx.MockTransport`
(``respx`` is not a dev dependency): a handler inspects each
:class:`httpx.Request` and returns a canned :class:`httpx.Response`. The
transport is injected into the :class:`GraphClient` the tool constructs by
monkeypatching the ``GraphClient`` symbol the tool imported with a thin factory
that pre-binds ``http_client=``. Token acquisition is stubbed by patching
``opencomputer.auth.graph_oauth.get_valid_access_token`` /
``has_stored_token`` (the synchronous functions ``_graph_common`` calls).

Coverage:

* the send builds the exact nested Graph ``Message`` body (``message`` →
  ``toRecipients`` → ``emailAddress`` → ``address``, ``saveToSentItems`` as a
  sibling), with ``cc`` / ``bcc`` / ``body_type``;
* malformed ``to`` / ``cc`` / ``bcc`` addresses are rejected *before* any
  network call (no HTTP request is made);
* the capability claim is ``PER_ACTION``;
* the send tool does **not** retry — not on a 401, not on a 5xx;
* the not-authenticated path returns the clean "run `oc auth login graph`"
  error and makes no network call.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from opencomputer.integrations.graph.client import GRAPH_BASE_URL, GraphClient
from opencomputer.tools import graph_mail
from opencomputer.tools.graph_mail import GraphSendMailTool
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

# pytest-asyncio runs in `asyncio_mode = "auto"` — `async def test_*` just works.


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _RequestLog:
    """Records every request a mock transport sees, for later assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)

    @property
    def count(self) -> int:
        return len(self.requests)


@contextlib.contextmanager
def _patched_graph(handler: Any, *, has_token: bool = True, token: str = "tok"):
    """Patch the tool's token acquisition + ``GraphClient`` HTTP transport.

    ``handler`` is a callable ``httpx.Request -> httpx.Response``. While the
    context is active, every :class:`GraphClient` the tool constructs is wired
    to a :class:`httpx.MockTransport` running ``handler``, and
    ``get_valid_access_token`` / ``has_stored_token`` are stubbed.
    """

    def _client_factory(access_token: str, **_kwargs: Any) -> GraphClient:
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(base_url=GRAPH_BASE_URL, transport=transport)
        return GraphClient(access_token, http_client=http)

    with (
        patch.object(graph_mail, "GraphClient", _client_factory),
        patch(
            "opencomputer.tools._graph_common.get_valid_access_token",
            return_value=token,
        ),
        patch(
            "opencomputer.tools._graph_common.has_stored_token",
            return_value=has_token,
        ),
    ):
        yield


def _accepted(request: httpx.Request) -> httpx.Response:
    """A canonical successful ``sendMail`` response — HTTP 202, empty body."""
    return httpx.Response(202)


def _call(**arguments: Any) -> ToolCall:
    return ToolCall(id="call-1", name="GraphSendMail", arguments=arguments)


# --------------------------------------------------------------------------
# Capability claim
# --------------------------------------------------------------------------


def test_capability_claim_is_per_action() -> None:
    """Sending mail is irreversible + outward — the gate must be PER_ACTION."""
    claims = GraphSendMailTool.capability_claims
    assert len(claims) == 1
    claim = claims[0]
    assert claim.tier_required is ConsentTier.PER_ACTION
    assert claim.capability_id == "graph.mail.send"
    assert isinstance(claims, tuple)  # not a mutable list


def test_schema_requires_to_subject_body() -> None:
    """The schema's required fields are exactly to / subject / body."""
    schema = GraphSendMailTool().schema
    assert schema.name == "GraphSendMail"
    assert set(schema.parameters["required"]) == {"to", "subject", "body"}


# --------------------------------------------------------------------------
# Happy path — body shape
# --------------------------------------------------------------------------


async def test_send_builds_the_nested_graph_message_body() -> None:
    """A successful send POSTs the exact nested Graph Message envelope."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(
                to=["alice@example.com", "bob@example.com"],
                subject="Lunch?",
                body="The new cafe is open.",
            )
        )

    assert result.is_error is False
    assert "accepted for delivery" in result.content.lower()

    assert log.count == 1
    request = log.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1.0/me/sendMail"

    import json as _json

    payload = _json.loads(request.content)
    # saveToSentItems is a SIBLING of message, not nested inside it.
    assert "saveToSentItems" in payload
    message = payload["message"]
    assert message["subject"] == "Lunch?"
    assert message["body"] == {
        "contentType": "Text",
        "content": "The new cafe is open.",
    }
    # Double nesting: toRecipients -> emailAddress -> address.
    assert message["toRecipients"] == [
        {"emailAddress": {"address": "alice@example.com"}},
        {"emailAddress": {"address": "bob@example.com"}},
    ]
    assert "ccRecipients" not in message
    assert "bccRecipients" not in message


async def test_send_includes_cc_bcc_and_html_body_type() -> None:
    """cc / bcc / body_type are threaded into the Graph Message body."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(
                to=["alice@example.com"],
                cc=["carol@example.com"],
                bcc=["dave@example.com"],
                subject="Report",
                body="<p>hello</p>",
                body_type="HTML",
            )
        )

    assert result.is_error is False
    import json as _json

    message = _json.loads(log.requests[0].content)["message"]
    assert message["body"]["contentType"] == "HTML"
    assert message["ccRecipients"] == [
        {"emailAddress": {"address": "carol@example.com"}}
    ]
    assert message["bccRecipients"] == [
        {"emailAddress": {"address": "dave@example.com"}}
    ]


# --------------------------------------------------------------------------
# Recipient validation at the trust boundary — reject BEFORE any network call
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_address",
    [
        "not-an-email",
        "missing-domain@",
        "@missing-local.com",
        "spaces in@example.com",
        "Display Name <real@example.com>",  # display form must be rejected
        "two@@example.com",
        "trailing@example.",
        "",
    ],
)
async def test_malformed_to_address_is_rejected_before_any_request(
    bad_address: str,
) -> None:
    """A malformed `to` address fails loudly with no HTTP request made."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(
                to=["good@example.com", bad_address],
                subject="s",
                body="b",
            )
        )

    assert result.is_error is True
    assert "malformed" in result.content.lower() or "empty" in result.content.lower()
    # The send must never have been attempted.
    assert log.count == 0


async def test_malformed_cc_address_is_rejected_before_any_request() -> None:
    """A malformed `cc` address is caught at the trust boundary too."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(
                to=["good@example.com"],
                cc=["bogus"],
                subject="s",
                body="b",
            )
        )

    assert result.is_error is True
    assert "cc" in result.content.lower()
    assert log.count == 0


async def test_empty_to_list_is_rejected() -> None:
    """An empty `to` list is rejected before any request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(to=[], subject="s", body="b")
        )

    assert result.is_error is True
    assert "to" in result.content.lower()
    assert log.count == 0


async def test_non_list_to_is_rejected() -> None:
    """A non-list `to` argument is rejected with a type error."""
    with _patched_graph(lambda r: _accepted(r)):
        result = await GraphSendMailTool().execute(
            _call(to="alice@example.com", subject="s", body="b")
        )
    assert result.is_error is True
    assert "list" in result.content.lower()


async def test_bad_body_type_is_rejected() -> None:
    """An unsupported body_type is rejected before any request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(
                to=["a@example.com"],
                subject="s",
                body="b",
                body_type="Markdown",
            )
        )
    assert result.is_error is True
    assert "body_type" in result.content.lower()
    assert log.count == 0


# --------------------------------------------------------------------------
# The send tool must NEVER retry
# --------------------------------------------------------------------------


async def test_send_does_not_retry_on_500() -> None:
    """A 5xx on send is surfaced as an error — the send is NOT replayed."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return httpx.Response(
            500, json={"error": {"code": "InternalError", "message": "boom"}}
        )

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(to=["a@example.com"], subject="s", body="b")
        )

    assert result.is_error is True
    # Exactly one POST — re-POSTing sendMail risks a duplicate email.
    assert log.count == 1


async def test_send_does_not_retry_on_401() -> None:
    """A 401 on send is surfaced as an error — NO force-refresh-retry.

    Unlike the read tools, the send tool must not retry on 401: a send is a
    one-shot side effect. Only ONE request must hit the wire.
    """
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "InvalidAuthenticationToken",
                    "message": "token expired",
                }
            },
        )

    refresh_calls: list[bool] = []

    def _fake_token(*, force_refresh: bool = False) -> str:
        refresh_calls.append(force_refresh)
        return "tok"

    with (
        patch.object(
            graph_mail,
            "GraphClient",
            lambda t, **_k: GraphClient(
                t,
                http_client=httpx.AsyncClient(
                    base_url=GRAPH_BASE_URL,
                    transport=httpx.MockTransport(handler),
                ),
            ),
        ),
        patch(
            "opencomputer.tools._graph_common.get_valid_access_token",
            side_effect=_fake_token,
        ),
        patch(
            "opencomputer.tools._graph_common.has_stored_token",
            return_value=True,
        ),
    ):
        result = await GraphSendMailTool().execute(
            _call(to=["a@example.com"], subject="s", body="b")
        )

    assert result.is_error is True
    # One HTTP attempt only.
    assert log.count == 1
    # The token was acquired exactly once, and never force-refreshed.
    assert refresh_calls == [False]


async def test_send_does_not_retry_on_429() -> None:
    """A 429 on send is surfaced — never retried (the client also disables
    throttle-retry for sendMail)."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"error": {"code": "TooManyRequests", "message": "slow down"}},
        )

    with _patched_graph(handler):
        result = await GraphSendMailTool().execute(
            _call(to=["a@example.com"], subject="s", body="b")
        )

    assert result.is_error is True
    assert log.count == 1


# --------------------------------------------------------------------------
# Not authenticated
# --------------------------------------------------------------------------


async def test_not_authenticated_returns_clean_error_and_no_request() -> None:
    """With no stored token the tool refuses cleanly and makes no request."""
    log = _RequestLog()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        log.record(request)
        return _accepted(request)

    with _patched_graph(handler, has_token=False):
        result = await GraphSendMailTool().execute(
            _call(to=["a@example.com"], subject="s", body="b")
        )

    assert result.is_error is True
    assert "oc auth login graph" in result.content
    assert log.count == 0


async def test_oauth_error_during_token_acquire_is_clean() -> None:
    """A GraphOAuthError raised by token acquisition becomes a clean result."""
    from opencomputer.auth.graph_oauth import GraphOAuthError

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return _accepted(request)

    with (
        patch.object(graph_mail, "GraphClient", lambda *a, **k: None),
        patch(
            "opencomputer.tools._graph_common.has_stored_token",
            return_value=True,
        ),
        patch(
            "opencomputer.tools._graph_common.get_valid_access_token",
            side_effect=GraphOAuthError(
                "Microsoft Graph token refresh rejected (invalid_grant)."
            ),
        ),
    ):
        result = await GraphSendMailTool().execute(
            _call(to=["a@example.com"], subject="s", body="b")
        )

    assert result.is_error is True
    assert "authentication failed" in result.content.lower()
    # The token must never appear in the surfaced message.
    assert "invalid_grant" in result.content
