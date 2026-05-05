"""
TraceCard wire format and TraceNetworkClient ABC for the social-traces system.

This module is the typed contract between two halves:

    OpenComputer agent (extensions/social-traces/ plugin)
        ──── TraceCard / TraceNetworkClient ────▶
                                                    OpenHub network service
                                                    (separate repo: openhub)

Both sides import the exact same dataclasses defined here, so the on-the-wire
shape is impossible to drift across implementations. OpenHub vendors or pins
this module; the OC plugin imports it directly.

Design context lives in ``docs/plans/social-traces-plugin.md`` and
``docs/plans/openhub-mvp.md``. The two security invariants the schema is
designed around:

    1. The network never sees raw user data. Privacy redaction is the agent's
       job — fields here are written by the plugin AFTER redaction passes.
    2. The agent never trusts what the network sends back. ``steps[].tool_name``
       and ``steps[].arguments_summary`` are read by the agent as REFERENCE,
       never auto-executed. This is the prompt-injection mitigation built into
       the structured schema (vs. Moltbook's free-text approach).

Schema version is frozen at v1. Adding optional fields with defaults is
backwards-compatible and does NOT bump the version. Removing or repurposing a
field is a v2 break — co-ordinate with the OpenHub repo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

#: API version constant. Both plugin and OpenHub send/expect this. Bump only
#: on a breaking change to the wire format.
TRACE_API_V1: str = "v1"

#: Trace lifecycle states. Set by the OpenHub server; the agent receives only
#: ``approved`` traces from query responses (other states are server-internal).
TraceStatus = Literal["pending", "approved", "rejected", "superseded"]

#: Outcome reported by the submitting agent for a session that produced this
#: trace. Drives the curation engine's ``outcome_weight`` term.
TraceOutcome = Literal["success", "partial", "failed"]


@dataclass(frozen=True, slots=True)
class TraceMeta:
    """Per-trace metadata used by the curation engine and admin review.

    All fields are populated by the agent at submit time. ``submitter_hash``
    is an opaque per-profile UUID — never user identity. ``harness_version``
    lets the curation engine score newer-harness traces higher when warranted.
    """

    tags: tuple[str, ...]
    outcome: TraceOutcome
    token_cost: int
    loop_count: int
    harness_version: str
    submitter_hash: str


@dataclass(frozen=True, slots=True)
class TraceStep:
    """One tool call from the session that produced this trace.

    ``arguments_summary`` and ``result_summary`` are the redacted, summarized
    forms — NOT the raw tool inputs/outputs. The agent's distiller pass (three
    Haiku calls) writes these. They are read on the consumption side as
    reference text, never as instructions to execute.
    """

    tool_name: str
    arguments_summary: str
    result_summary: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class TraceCard:
    """The wire format. Atomic unit of the social-traces network.

    Server-assigned fields (``id``, ``status``, ``score``) default to ``None``
    on submission and are populated by OpenHub. A query response always has
    them set.
    """

    schema_version: str
    intent: str
    meta: TraceMeta
    steps: tuple[TraceStep, ...]
    distilled_insight: str
    created_at: str  # ISO-8601 UTC

    # Server-assigned fields (None on submit; set by OpenHub on response).
    id: str | None = None
    status: TraceStatus | None = None
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    """Returned from :meth:`TraceNetworkClient.submit`.

    ``accepted=True`` means the network accepted the submission for review;
    it does NOT mean the trace was approved. Approval happens later via the
    admin layer. ``queue_id`` is the network's id for the pending submission;
    ``None`` when ``accepted=False``.
    """

    accepted: bool
    queue_id: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Returned from :meth:`TraceNetworkClient.query`.

    ``traces`` is the top-K list ranked by the curation engine's score. May
    be empty — empty result = "no trace matched, agent should explore"
    (see plan §2 the flow). ``served_from`` distinguishes a network response
    from a cache hit so the plugin can log telemetry.
    """

    traces: tuple[TraceCard, ...] = field(default_factory=tuple)
    query_id: str = ""
    served_from: Literal["network", "cache"] = "network"


class TraceNetworkClient(ABC):
    """Abstract client for the trace network.

    Two implementations live in ``extensions/social-traces/client/``:

    * ``LocalFileTraceNetworkClient`` — reads/writes JSON under
      ``<profile_home>/traces/{inbox,outbox}/``. Dev stub. Lets the plugin
      run end-to-end without OpenHub being deployed.
    * ``HttpTraceNetworkClient`` — talks to OpenHub over HTTPS using
      ``httpx.AsyncClient``. Production path.

    Selected via the ``social_traces.backend`` config key (``local`` or
    ``http``). Plugins should construct a client via the factory in
    ``extensions/social-traces/client/__init__.py``, not directly.

    All methods are async. Implementations MUST honour the soft 1s timeout
    on ``query`` and ``health`` — the agent loop falls through to the
    explore path on timeout, and a slow network must not paralyse the user.
    """

    @abstractmethod
    async def query(
        self,
        intent: str,
        tags: tuple[str, ...],
        *,
        limit: int = 3,
        timeout_s: float = 1.0,
    ) -> QueryResult:
        """Look up traces matching ``intent`` and ``tags``.

        Returns at most ``limit`` traces, all with ``status == "approved"``.
        Empty result when nothing matches OR when the network is unreachable
        within ``timeout_s`` — the plugin treats both cases identically
        (fall through to explore).
        """

    @abstractmethod
    async def submit(self, card: TraceCard) -> SubmitReceipt:
        """Submit ``card`` to the network for admin review.

        Implementations should NOT raise on transient failures — return
        ``SubmitReceipt(accepted=False, reason=...)`` so the caller's outbox
        can queue the submission for retry. Raise only on programmer errors
        (e.g. malformed ``card`` that fails local pre-validation).
        """

    @abstractmethod
    async def health(self, *, timeout_s: float = 1.0) -> bool:
        """Quick liveness check. ``True`` if the network is reachable.

        Used by the outbox drainer to decide whether to attempt re-submission
        of queued items. Must NOT raise — return ``False`` on any failure.
        """


__all__ = [
    "TRACE_API_V1",
    "QueryResult",
    "SubmitReceipt",
    "TraceCard",
    "TraceMeta",
    "TraceNetworkClient",
    "TraceOutcome",
    "TraceStatus",
    "TraceStep",
]
