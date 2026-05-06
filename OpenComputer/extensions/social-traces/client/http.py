"""HTTP backend for the trace network — Phase 9.B.

Talks to a running OpenHub instance (separate repo, sibling on disk:
``~/Documents/GitHub/openhub``) over HTTP via :mod:`httpx`. Speaks the
v1 wire contract defined in ``plugin_sdk.traces``; OpenHub mirrors
those types as Pydantic models on its end.

Three endpoints consumed:

* ``POST {endpoint}/v1/traces/query`` — pre-task lookup
* ``POST {endpoint}/v1/traces/submit`` — post-task emit
* ``GET  {endpoint}/healthz``       — outbox-drain liveness probe

Failure-isolation contract (per :class:`TraceNetworkClient`):

* :meth:`query` and :meth:`health` MUST honour ``timeout_s`` and never
  raise on transient failures (network down, slow server, malformed
  response). They return empty / False so the agent's pre-task path
  falls through to "explore" without paralysing the user.
* :meth:`submit` MUST NOT raise on transient failures — return
  ``SubmitReceipt(accepted=False, reason=...)`` so the caller's outbox
  takes over (Phase 9.B+ outbox-drain work). Programmer errors
  (malformed card) DO raise so they're caught at dev time.

Every method opens + closes its own :class:`httpx.AsyncClient` rather
than holding a long-lived one. Trace ops are infrequent (per session
boundary, not per token), so the connection-pool cost isn't worth the
lifecycle complexity. Promote to a persistent client only when
profile-level metrics show real overhead.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import httpx

from plugin_sdk.traces import (
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceNetworkClient,
    TraceStep,
)

_log = logging.getLogger("opencomputer.social_traces.client.http")

#: User-Agent sent on every request. Lets OpenHub admins spot which
#: client version is in flight when reviewing logs / audit entries.
USER_AGENT = "opencomputer-social-traces/0.1"

#: Hard timeout on submit. Distinct from the soft 1s on query because
#: write paths can legitimately be slower (server-side validation +
#: insert + audit). Still bounded so a hung server can't pile up
#: orphaned tasks in the post-task subscriber.
DEFAULT_SUBMIT_TIMEOUT_S = 5.0


def _trace_card_to_wire(card: TraceCard) -> dict[str, Any]:
    """Serialize a TraceCard for the wire. Server-assigned fields
    (``id``, ``status``, ``score``) MUST be omitted on submission so
    the server can stamp them; we drop them rather than send None
    because OpenHub's Pydantic models reject unknown shapes."""
    raw = dataclasses.asdict(card)
    for server_field in ("id", "status", "score"):
        raw.pop(server_field, None)
    return raw


def _trace_card_from_wire(raw: dict[str, Any]) -> TraceCard:
    """Inverse of :func:`_trace_card_to_wire` — reconstruct a TraceCard
    from a query response. Tolerates JSON-tuple-as-list quirk; raises
    ``KeyError`` / ``TypeError`` on malformed input so the caller can
    skip-and-log rather than crash."""
    meta_raw = raw["meta"]
    meta = TraceMeta(
        tags=tuple(meta_raw["tags"]),
        outcome=meta_raw["outcome"],
        token_cost=int(meta_raw["token_cost"]),
        loop_count=int(meta_raw["loop_count"]),
        harness_version=meta_raw["harness_version"],
        submitter_hash=meta_raw["submitter_hash"],
    )
    steps = tuple(
        TraceStep(
            tool_name=s["tool_name"],
            arguments_summary=s["arguments_summary"],
            result_summary=s["result_summary"],
            duration_ms=int(s["duration_ms"]),
        )
        for s in raw["steps"]
    )
    return TraceCard(
        schema_version=raw["schema_version"],
        intent=raw["intent"],
        meta=meta,
        steps=steps,
        distilled_insight=raw["distilled_insight"],
        created_at=raw["created_at"],
        id=raw.get("id"),
        status=raw.get("status"),
        score=raw.get("score"),
    )


class HttpTraceNetworkClient(TraceNetworkClient):
    """Production client. Talks to an OpenHub instance over HTTP.

    ``endpoint`` is the base URL, e.g. ``http://127.0.0.1:8000`` (Stage 1)
    or ``https://openhub-archits.ngrok.app`` (Stage 2). No trailing slash
    required — we normalize.

    The optional ``transport`` parameter exists so tests can inject an
    :class:`httpx.MockTransport`; production callers leave it None.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._transport = transport

    # ─── helpers ───────────────────────────────────────────────────

    def _client(self, *, timeout_s: float) -> httpx.AsyncClient:
        """Construct a fresh client. Opening + closing per call is fine
        at our request rate (per session boundary)."""
        return httpx.AsyncClient(
            base_url=self._endpoint,
            timeout=timeout_s,
            transport=self._transport,
            headers={"User-Agent": USER_AGENT},
        )

    # ─── ABC methods ───────────────────────────────────────────────

    async def query(
        self,
        intent: str,
        tags: tuple[str, ...],
        *,
        limit: int = 3,
        timeout_s: float = 1.0,
    ) -> QueryResult:
        """Look up matching approved traces.

        Returns ``QueryResult(traces=())`` on:
        - network error (connection refused, DNS failure, etc.)
        - timeout exceeding ``timeout_s``
        - non-2xx HTTP status
        - malformed JSON or schema-violating response body

        These all map to "no matches" from the agent's perspective,
        so prefetch falls through to explore. Logged at WARNING for
        operator visibility but never raised.
        """
        try:
            async with self._client(timeout_s=timeout_s) as client:
                resp = await client.post(
                    "/v1/traces/query",
                    json={
                        "intent": intent,
                        "tags": list(tags),
                        "limit": limit,
                    },
                )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            _log.warning(
                "social-traces query: network failure (%s) — returning empty",
                type(exc).__name__,
            )
            return QueryResult()

        if resp.status_code != 200:
            _log.warning(
                "social-traces query: HTTP %d — returning empty",
                resp.status_code,
            )
            return QueryResult()

        try:
            body = resp.json()
        except ValueError:
            _log.warning("social-traces query: malformed JSON — returning empty")
            return QueryResult()

        traces = []
        for raw_trace in body.get("traces") or ():
            try:
                traces.append(_trace_card_from_wire(raw_trace))
            except (KeyError, TypeError, ValueError):
                # One bad trace shouldn't poison the whole result.
                _log.warning(
                    "social-traces query: skipping malformed trace in response",
                    exc_info=True,
                )

        return QueryResult(
            traces=tuple(traces),
            query_id=body.get("query_id", ""),
            served_from=body.get("served_from", "network"),
        )

    async def submit(self, card: TraceCard) -> SubmitReceipt:
        """Post a TraceCard to the network.

        Returns ``SubmitReceipt(accepted=False, reason=...)`` on:
        - network error
        - timeout (5s default, see ``DEFAULT_SUBMIT_TIMEOUT_S``)
        - non-2xx HTTP status
        - malformed response

        These all let the caller's outbox queue the submission for
        retry. Programmer errors (e.g. ``card`` missing required
        fields, dataclass.asdict raises) DO propagate so they surface
        in dev rather than being silently swallowed.
        """
        try:
            payload = _trace_card_to_wire(card)
        except Exception as exc:  # noqa: BLE001 — programmer error, surface
            raise ValueError(f"failed to serialize TraceCard: {exc}") from exc

        try:
            async with self._client(timeout_s=DEFAULT_SUBMIT_TIMEOUT_S) as client:
                resp = await client.post("/v1/traces/submit", json=payload)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            _log.warning(
                "social-traces submit: network failure (%s) — caller can outbox",
                type(exc).__name__,
            )
            return SubmitReceipt(
                accepted=False,
                queue_id=None,
                reason=f"network failure: {type(exc).__name__}",
            )

        if resp.status_code == 413:
            # Real protocol error — payload too large. Don't queue; the
            # plugin's outbox would just keep failing this forever.
            _log.warning("social-traces submit: 413 payload too large — dropping")
            return SubmitReceipt(
                accepted=False,
                queue_id=None,
                reason="payload too large (413)",
            )

        if resp.status_code != 200:
            _log.warning(
                "social-traces submit: HTTP %d — caller can outbox",
                resp.status_code,
            )
            return SubmitReceipt(
                accepted=False,
                queue_id=None,
                reason=f"HTTP {resp.status_code}",
            )

        try:
            body = resp.json()
        except ValueError:
            _log.warning("social-traces submit: malformed receipt JSON")
            return SubmitReceipt(
                accepted=False,
                queue_id=None,
                reason="malformed response",
            )

        return SubmitReceipt(
            accepted=bool(body.get("accepted", False)),
            queue_id=body.get("queue_id"),
            reason=body.get("reason", ""),
        )

    async def health(self, *, timeout_s: float = 1.0) -> bool:
        """Liveness probe. ``True`` only on a clean 200 response.

        Used by the (future) outbox drainer to decide whether to
        attempt re-submission of queued items. Never raises.
        """
        try:
            async with self._client(timeout_s=timeout_s) as client:
                resp = await client.get("/healthz")
        except (httpx.TimeoutException, httpx.HTTPError):
            return False
        return resp.status_code == 200


__all__ = [
    "DEFAULT_SUBMIT_TIMEOUT_S",
    "HttpTraceNetworkClient",
    "USER_AGENT",
]
