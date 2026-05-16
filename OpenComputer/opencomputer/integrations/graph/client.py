"""``GraphClient`` — async HTTP client for the Microsoft Graph ``v1.0`` REST API.

Build-chunk 1 of Milestone 3. This module is the HTTP library *only*: it wraps
an :class:`httpx.AsyncClient`, takes an already-minted Graph access token as a
plain ``str``, and exposes three namespaced operation groups —
``client.mail``, ``client.calendar``, ``client.drive``.

Obtaining / refreshing the access token (the device-code OAuth flow) is a
later chunk's job; ``GraphClient`` never speaks to ``login.microsoftonline.com``.

Design notes (see ``docs/refs/microsoft-graph/2026-05-16-survey.md``):

* **Send mail** — ``POST /me/sendMail`` returns **HTTP 202 with an empty body**
  on success. We treat ``202`` (not ``200``) as success, and we **never**
  auto-retry a send: re-POSTing ``sendMail`` risks delivering a duplicate
  email (survey pre-mortem failure mode #2). A send that comes back ``429`` is
  surfaced as an error, not retried.
* **Calendar** — ``GET /me/calendarView`` (not ``/me/events``) so recurring
  events are expanded. ``calendarView``'s default page size is only 10, so we
  set ``$top`` explicitly and paginate.
* **Drive** — ``GET /me/drive/root/children`` plus a path variant
  ``/me/drive/root:/{path}:/children``.
* **Pagination** — :meth:`GraphClient.paginate` follows the ``@odata.nextLink``
  absolute URL verbatim until it is absent, with a ``max_items`` cap so a huge
  collection can't run unbounded (pre-mortem failure mode #4).
* **Throttling** — on ``HTTP 429`` for *read* requests we honor the
  ``Retry-After`` header exactly, with a small bounded retry budget, then a
  clean :class:`GraphAPIError`. We never loop unbounded.
* **Errors** — any non-2xx that we don't handle (i.e. everything except a
  retried 429) raises :class:`GraphAPIError` carrying the status code and the
  Graph error body. Nothing is swallowed.

The token is sent in the ``Authorization: Bearer`` header and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

#: Microsoft Graph stable API root. ``GraphClient`` targets ``v1.0`` only
#: (the ``beta`` endpoint is explicitly out of scope for M3).
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

#: Default per-request timeout (seconds). Matches the conservative timeout
#: style used elsewhere in OC's httpx callers (e.g. ``tools/web_fetch.py``).
DEFAULT_TIMEOUT_S = 30.0

#: Page size requested for collection endpoints. ``calendarView`` defaults to
#: only 10 server-side (survey §3 / mismatch #7), so we always send ``$top``.
#: Graph caps ``$top`` at 1000.
DEFAULT_PAGE_SIZE = 100

#: How many times a *read* request will be retried after an HTTP 429 before
#: giving up. Small and fixed — never an unbounded loop (pre-mortem #4).
MAX_THROTTLE_RETRIES = 3

#: Hard ceiling on a single ``Retry-After`` sleep (seconds). A hostile or
#: buggy header can't park the agent for minutes.
MAX_RETRY_AFTER_S = 60.0

#: Fallback delay (seconds) when a 429 arrives with no usable ``Retry-After``
#: header. The survey notes the surveyed endpoints always send one, but we
#: stay defensive. Index by retry attempt for a simple bounded backoff.
_FALLBACK_BACKOFF_S = (5.0, 15.0, 30.0)


class GraphError(Exception):
    """Base class for every error raised by :class:`GraphClient`.

    Catching :class:`GraphError` catches both transport failures
    (:class:`GraphTransportError`) and Graph-side HTTP errors
    (:class:`GraphAPIError`).
    """


class GraphTransportError(GraphError):
    """A request never produced an HTTP response.

    Wraps an :class:`httpx.HTTPError` — connection refused, DNS failure,
    timeout, etc. Carries the originating exception as ``__cause__``.
    """


class GraphAPIError(GraphError):
    """Microsoft Graph returned a non-2xx status that the client did not handle.

    ``status_code`` is the HTTP status. ``error_code`` / ``error_message`` are
    pulled from the Graph error envelope (``{"error": {"code", "message"}}``)
    when the body is JSON; ``raw_body`` is the verbatim (truncated) response
    text for diagnosis when it is not.
    """

    #: Response bodies are truncated to this many characters in the exception
    #: message so a giant HTML error page can't flood a log line.
    _MAX_BODY_CHARS = 2_000

    def __init__(
        self,
        status_code: int,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        raw_body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.raw_body = raw_body
        detail = error_message or raw_body or "(empty response body)"
        if raw_body is not None and len(detail) > self._MAX_BODY_CHARS:
            detail = detail[: self._MAX_BODY_CHARS] + "… (truncated)"
        code_part = f" [{error_code}]" if error_code else ""
        super().__init__(
            f"Microsoft Graph request failed: HTTP {status_code}{code_part}: {detail}"
        )

    @classmethod
    def from_response(cls, response: httpx.Response) -> GraphAPIError:
        """Build a :class:`GraphAPIError` from a non-2xx :class:`httpx.Response`.

        Parses the Graph error envelope when the body is JSON; otherwise keeps
        the raw text so nothing about the failure is lost.
        """
        error_code: str | None = None
        error_message: str | None = None
        raw_body: str | None = None
        try:
            payload = response.json()
        except ValueError:
            raw_body = response.text or None
        else:
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message")
                error_code = code if isinstance(code, str) else None
                error_message = message if isinstance(message, str) else None
            if error_code is None and error_message is None:
                # JSON, but not the expected envelope — surface it verbatim.
                raw_body = response.text or None
        return cls(
            response.status_code,
            error_code=error_code,
            error_message=error_message,
            raw_body=raw_body,
        )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds.

    Graph sends ``Retry-After`` as an integer number of seconds for the
    surveyed endpoints. We accept any non-negative number and clamp it to
    :data:`MAX_RETRY_AFTER_S`. Returns ``None`` when the header is missing or
    unparseable (an HTTP-date form is treated as unparseable — Graph does not
    use it here, and a bounded fallback is safer than date math).
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (ValueError, AttributeError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


class _MailOperations:
    """``client.mail`` — Microsoft Graph mail operations."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_type: str = "Text",
        save_to_sent_items: bool = True,
    ) -> None:
        """Send an email via ``POST /me/sendMail``.

        Builds the nested Graph ``Message`` body — note the double nesting of
        ``toRecipients`` → ``emailAddress`` → ``address``.

        Args:
            to: Recipient email addresses (must be non-empty).
            subject: Message subject line.
            body: Message body content.
            cc: Optional carbon-copy recipient addresses.
            bcc: Optional blind-carbon-copy recipient addresses.
            body_type: ``"Text"`` or ``"HTML"`` (case-insensitive on input;
                normalized to Graph's expected casing).
            save_to_sent_items: Whether Graph keeps a copy in Sent Items.
                ``saveToSentItems`` is a *sibling* of ``message``, not nested
                inside it.

        Returns:
            ``None`` — a successful send produces **HTTP 202 with an empty
            body**. ``202`` (accepted for delivery) is the success signal;
            delivery itself is async on Exchange's side.

        Raises:
            ValueError: If ``to`` is empty or ``body_type`` is not Text/HTML.
            GraphAPIError: If Graph returns any status other than 202. A send
                is **never** auto-retried — not on 429, not on a 5xx — because
                re-POSTing risks a duplicate email.
            GraphTransportError: If the request never reached Graph.
        """
        if not to:
            raise ValueError("mail.send requires at least one recipient in 'to'")
        normalized_type = self._normalize_body_type(body_type)

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": normalized_type, "content": body},
            "toRecipients": _recipient_list(to),
        }
        if cc:
            message["ccRecipients"] = _recipient_list(cc)
        if bcc:
            message["bccRecipients"] = _recipient_list(bcc)

        payload: dict[str, Any] = {
            "message": message,
            "saveToSentItems": save_to_sent_items,
        }

        # retry_on_throttle=False — a send is the one operation we must never
        # replay. An ambiguous 429 surfaces as a GraphAPIError so the caller
        # decides; the client does not silently re-POST.
        response = await self._client._request(
            "POST", "/me/sendMail", json=payload, retry_on_throttle=False
        )
        if response.status_code != 202:
            # Anything else — including a stray 200 — is not the documented
            # success contract. Treat it as an error rather than guessing.
            raise GraphAPIError.from_response(response)
        logger.debug(
            "mail.send accepted by Graph (HTTP 202, %d recipient(s))", len(to)
        )

    @staticmethod
    def _normalize_body_type(body_type: str) -> str:
        """Normalize a body-type string to Graph's expected ``Text``/``HTML``."""
        lowered = body_type.strip().lower()
        if lowered == "text":
            return "Text"
        if lowered == "html":
            return "HTML"
        raise ValueError(
            f"body_type must be 'Text' or 'HTML', got {body_type!r}"
        )


class _CalendarOperations:
    """``client.calendar`` — Microsoft Graph calendar operations."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def list(
        self,
        *,
        start_date_time: str,
        end_date_time: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """List calendar events in a time window via ``GET /me/calendarView``.

        ``calendarView`` (unlike ``/me/events``) expands recurring series into
        one object per occurrence in the window — what a human means by "list
        my calendar."

        Args:
            start_date_time: ISO-8601 window start (e.g.
                ``2026-05-16T00:00:00Z``). A timezone offset in the value is
                honored; a naive value is treated as UTC by Graph.
            end_date_time: ISO-8601 window end.
            page_size: Value sent as ``$top``. ``calendarView`` defaults to
                only 10 server-side, so this is set explicitly. Clamped to
                Graph's 1..1000 range.
            max_items: Optional cap on total events returned across all pages.

        Returns:
            The concatenated ``value`` arrays from every page.

        Raises:
            ValueError: If either datetime is empty.
            GraphAPIError / GraphTransportError: On a Graph or transport error.
        """
        if not start_date_time or not end_date_time:
            raise ValueError(
                "calendar.list requires both start_date_time and end_date_time"
            )
        params = {
            "startDateTime": start_date_time,
            "endDateTime": end_date_time,
            "$top": _clamp_top(page_size),
        }
        return await self._client.paginate(
            "/me/calendarView", params=params, max_items=max_items
        )


class _DriveOperations:
    """``client.drive`` — Microsoft Graph OneDrive operations."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def list(
        self,
        *,
        folder_path: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """List OneDrive items via ``GET /me/drive/root/children``.

        Args:
            folder_path: Optional folder path relative to the drive root
                (e.g. ``Documents/Reports``). When given, the path-addressed
                endpoint ``/me/drive/root:/{path}:/children`` is used. When
                ``None``, the drive root is listed. A leading/trailing ``/``
                is tolerated.
            page_size: Value sent as ``$top`` (drive defaults to 200; we still
                send it for determinism). Clamped to Graph's 1..1000 range.
            max_items: Optional cap on total items returned across all pages.

        Returns:
            The concatenated ``value`` arrays (``driveItem`` objects) from
            every page.

        Raises:
            GraphAPIError / GraphTransportError: On a Graph or transport error.
        """
        path = self._build_path(folder_path)
        params = {"$top": _clamp_top(page_size)}
        return await self._client.paginate(
            path, params=params, max_items=max_items
        )

    @staticmethod
    def _build_path(folder_path: str | None) -> str:
        """Resolve the children-listing path for an optional folder.

        Root → ``/me/drive/root/children``. A named folder →
        ``/me/drive/root:/{escaped-path}:/children``. Each path *segment* is
        percent-escaped individually so spaces and unicode survive while the
        ``/`` separators stay intact.
        """
        if folder_path is None:
            return "/me/drive/root/children"
        trimmed = folder_path.strip().strip("/")
        if not trimmed:
            return "/me/drive/root/children"
        escaped = "/".join(quote(seg, safe="") for seg in trimmed.split("/"))
        return f"/me/drive/root:/{escaped}:/children"


def _recipient_list(addresses: list[str]) -> list[dict[str, Any]]:
    """Build a Graph recipient array from plain email-address strings.

    Each entry is ``{"emailAddress": {"address": <addr>}}`` — the double
    nesting the Graph ``Message`` schema requires.
    """
    return [{"emailAddress": {"address": addr}} for addr in addresses]


def _clamp_top(page_size: int) -> int:
    """Clamp a requested ``$top`` page size into Graph's valid 1..1000 range."""
    return max(1, min(int(page_size), 1000))


class GraphClient:
    """Async client for the Microsoft Graph ``v1.0`` REST API.

    Constructed with a Graph access token (a plain ``str`` — obtaining or
    refreshing it is a separate concern). Wraps a single long-lived
    :class:`httpx.AsyncClient` and exposes three namespaced operation groups:

    * :attr:`mail` — :meth:`_MailOperations.send`
    * :attr:`calendar` — :meth:`_CalendarOperations.list`
    * :attr:`drive` — :meth:`_DriveOperations.list`

    Use as an async context manager so the underlying connection pool is
    closed deterministically::

        async with GraphClient(access_token) as client:
            await client.mail.send(to=["a@b.com"], subject="hi", body="...")

    or call :meth:`aclose` explicitly.
    """

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = GRAPH_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a :class:`GraphClient`.

        Args:
            access_token: A Microsoft Graph OAuth access token. Sent in the
                ``Authorization: Bearer`` header on every request and **never
                logged**.
            base_url: Graph API root. Defaults to the stable ``v1.0`` endpoint.
            timeout: Per-request timeout in seconds.
            http_client: An optional pre-built :class:`httpx.AsyncClient` —
                primarily a test seam (inject a transport mock). When given,
                the caller owns its lifecycle and :meth:`aclose` will *not*
                close it. When omitted, the client builds and owns its own.
        """
        if not access_token or not access_token.strip():
            raise ValueError("GraphClient requires a non-empty access token")
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")

        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                # NOTE: the bearer token is deliberately NOT baked into the
                # client's default headers — it is attached per-request in
                # _auth_headers() so it never leaks via client repr/logging.
                headers={"Accept": "application/json"},
            )
            self._owns_http = True

        self.mail = _MailOperations(self)
        self.calendar = _CalendarOperations(self)
        self.drive = _DriveOperations(self)

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance owns it."""
        if self._owns_http:
            await self._http.aclose()

    # -- request plumbing --------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Per-request headers carrying the bearer token.

        Built fresh each call and never logged — the token does not live in
        the :class:`httpx.AsyncClient`'s default headers.
        """
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_on_throttle: bool = True,
        absolute_url: bool = False,
    ) -> httpx.Response:
        """Issue one Graph request, applying bounded 429 handling.

        Args:
            method: HTTP method.
            url: Either a path relative to the Graph base (``absolute_url``
                false) or a fully-qualified URL — e.g. an ``@odata.nextLink``
                — when ``absolute_url`` is true.
            params: Query parameters. Ignored for ``absolute_url`` requests:
                an ``@odata.nextLink`` already encodes its page state and must
                be fetched verbatim.
            json: Optional JSON request body.
            retry_on_throttle: When true (reads), an HTTP 429 is retried up to
                :data:`MAX_THROTTLE_RETRIES` times, honoring ``Retry-After``.
                When false (``mail.send``), a 429 is returned to the caller
                un-retried so a send is never replayed.
            absolute_url: Whether ``url`` is already fully-qualified.

        Returns:
            The :class:`httpx.Response`. For ``retry_on_throttle`` reads this
            is always a non-429 response (the 429s were consumed by retries,
            unless the budget was exhausted — then the final 429 is returned
            for the caller to convert into a :class:`GraphAPIError`).

        Raises:
            GraphTransportError: If the request never produced a response.
        """
        headers = self._auth_headers()
        # An @odata.nextLink already carries its own query state; sending
        # `params` alongside it would corrupt the skip token.
        request_params = None if absolute_url else params

        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=request_params,
                    json=json,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                # Connection/timeout/etc. — no HTTP response was produced.
                raise GraphTransportError(
                    f"Microsoft Graph request failed before a response "
                    f"({type(exc).__name__}): {exc}"
                ) from exc

            if response.status_code != 429 or not retry_on_throttle:
                return response

            # --- HTTP 429 on a retryable read ---------------------------
            if attempt >= MAX_THROTTLE_RETRIES:
                # Budget exhausted — hand the final 429 back so the caller
                # raises a clean GraphAPIError. Never an unbounded loop.
                logger.warning(
                    "Microsoft Graph still throttling after %d retr%s; "
                    "giving up on %s",
                    MAX_THROTTLE_RETRIES,
                    "y" if MAX_THROTTLE_RETRIES == 1 else "ies",
                    method,
                )
                return response

            delay = _parse_retry_after(response.headers.get("Retry-After"))
            if delay is None:
                # Rare for the surveyed endpoints — fall back to a bounded
                # backoff rather than retrying immediately.
                delay = _FALLBACK_BACKOFF_S[
                    min(attempt, len(_FALLBACK_BACKOFF_S) - 1)
                ]
            logger.warning(
                "Microsoft Graph throttled (HTTP 429); honoring Retry-After "
                "= %.0fs (attempt %d/%d)",
                delay,
                attempt + 1,
                MAX_THROTTLE_RETRIES,
            )
            await asyncio.sleep(delay)
            attempt += 1

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> httpx.Response:
        """Return ``response`` if it is 2xx, else raise :class:`GraphAPIError`.

        A 429 reaching here means the retry budget was exhausted (or retries
        were disabled) — it is converted to an error like any other non-2xx.
        """
        if 200 <= response.status_code < 300:
            return response
        raise GraphAPIError.from_response(response)

    # -- pagination --------------------------------------------------------

    async def paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every page of a Graph collection and concatenate the results.

        Issues a ``GET`` to ``url`` (a path relative to the Graph base), then
        follows the ``@odata.nextLink`` of each response — an absolute,
        fully-formed URL fetched **verbatim** — until a page has no
        ``@odata.nextLink``. The ``value`` arrays are concatenated in order.

        Args:
            url: Initial collection path (relative to the Graph base URL).
            params: Query parameters for the *first* request only. Subsequent
                requests use the ``@odata.nextLink`` as-is.
            max_items: Optional cap on the total number of items returned. The
                loop stops — and the final page is truncated — as soon as the
                cap is reached, so an enormous collection can never run
                unbounded.

        Returns:
            The concatenated ``value`` items, at most ``max_items`` of them.

        Raises:
            GraphAPIError: On any non-2xx response.
            GraphTransportError: If a request never produced a response.
        """
        if max_items is not None and max_items <= 0:
            return []

        items: list[dict[str, Any]] = []
        next_url: str | None = url
        is_absolute = False
        page_params = params

        while next_url is not None:
            response = self._raise_for_status(
                await self._request(
                    "GET",
                    next_url,
                    params=page_params,
                    absolute_url=is_absolute,
                )
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise GraphAPIError(
                    response.status_code,
                    raw_body=(
                        "expected a JSON collection from Microsoft Graph but "
                        f"the body was not JSON: {response.text!r}"
                    ),
                ) from exc

            page = payload.get("value", []) if isinstance(payload, dict) else []
            if not isinstance(page, list):
                raise GraphAPIError(
                    response.status_code,
                    raw_body=(
                        "Microsoft Graph collection 'value' was not a list: "
                        f"{type(page).__name__}"
                    ),
                )
            items.extend(item for item in page if isinstance(item, dict))

            if max_items is not None and len(items) >= max_items:
                return items[:max_items]

            # @odata.nextLink is an absolute URL; absent => last page.
            raw_next = (
                payload.get("@odata.nextLink") if isinstance(payload, dict) else None
            )
            next_url = raw_next if isinstance(raw_next, str) and raw_next else None
            is_absolute = True
            page_params = None  # only the first request carries `params`

        return items
