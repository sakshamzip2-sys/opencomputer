"""Tests for ``plugin_sdk.traces`` — TraceCard wire format and the
TraceNetworkClient ABC.

Phase 0 of the social-traces plugin (see
``docs/plans/social-traces-plugin.md``). Coverage focus:

* Frozen dataclasses reject mutation
* JSON round-trip preserves every field
* Public re-exports are reachable from ``plugin_sdk`` top-level
* TraceNetworkClient cannot be instantiated; concrete subclass works
* ALL_HOOK_EVENTS includes BEFORE_TASK in the documented position
"""

from __future__ import annotations

import dataclasses
import json
from typing import get_args

import pytest

import plugin_sdk
from plugin_sdk.hooks import ALL_HOOK_EVENTS, HookEvent
from plugin_sdk.traces import (
    TRACE_API_V1,
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceNetworkClient,
    TraceOutcome,
    TraceStatus,
    TraceStep,
)

# ─── public re-exports ────────────────────────────────────────────────


def test_public_reexports_reachable_from_plugin_sdk():
    """Every name that lives in ``plugin_sdk.traces`` must also be reachable
    from ``plugin_sdk`` top-level — that's the contract third-party plugins
    code against."""
    for name in (
        "TRACE_API_V1",
        "QueryResult",
        "SubmitReceipt",
        "TraceCard",
        "TraceMeta",
        "TraceNetworkClient",
        "TraceOutcome",
        "TraceStatus",
        "TraceStep",
    ):
        assert hasattr(plugin_sdk, name), f"plugin_sdk.{name} not exported"
        assert name in plugin_sdk.__all__, f"{name} missing from plugin_sdk.__all__"


def test_api_version_constant():
    assert TRACE_API_V1 == "v1"


# ─── frozen dataclass behaviour ───────────────────────────────────────


def _sample_card() -> TraceCard:
    """Build a minimal valid TraceCard for round-trip tests."""
    return TraceCard(
        schema_version=TRACE_API_V1,
        intent="sync files between two machines on LAN",
        meta=TraceMeta(
            tags=("homelab", "filesync", "lan"),
            outcome="success",
            token_cost=847,
            loop_count=3,
            harness_version="opencomputer/0.1.0",
            submitter_hash="0123456789abcdef0123456789abcdef",
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="rsync -avh --checksum src/ dst/",
                result_summary="files synced, 0 errors",
                duration_ms=1234,
            ),
        ),
        distilled_insight=(
            "rsync with --checksum is more reliable than --update on LAN "
            "when clocks are skewed."
        ),
        created_at="2026-05-05T12:00:00Z",
    )


@pytest.mark.parametrize(
    "cls",
    [TraceMeta, TraceStep, TraceCard, SubmitReceipt, QueryResult],
)
def test_dataclasses_are_frozen(cls):
    """Every public TraceCard-related dataclass is frozen — drive-by mutation
    by buggy plugin code must not be possible. Same rule as the rest of
    plugin_sdk (see plugin_sdk/CLAUDE.md §3)."""
    assert dataclasses.is_dataclass(cls)
    params = cls.__dataclass_params__
    assert params.frozen is True, f"{cls.__name__} must be frozen"
    assert getattr(cls, "__slots__", None) is not None, (
        f"{cls.__name__} must declare __slots__"
    )


def test_trace_card_field_assignment_raises():
    """Attempting to mutate a TraceCard field after construction must raise.

    Frozen dataclasses surface this as ``dataclasses.FrozenInstanceError``,
    which is a subclass of ``AttributeError``.
    """
    card = _sample_card()
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.intent = "something else"  # type: ignore[misc]


# ─── JSON round-trip ──────────────────────────────────────────────────


def _card_to_json(card: TraceCard) -> str:
    """Serialize a TraceCard to JSON in the wire format both halves expect.

    Pydantic-free: we use ``dataclasses.asdict`` so this works without
    pulling pydantic into plugin_sdk. OpenHub will define a Pydantic
    model that maps to the same shape.
    """
    return json.dumps(dataclasses.asdict(card))


def _card_from_json(payload: str) -> TraceCard:
    """Inverse of ``_card_to_json``. Reconstructs nested dataclasses."""
    raw = json.loads(payload)
    return TraceCard(
        schema_version=raw["schema_version"],
        intent=raw["intent"],
        meta=TraceMeta(
            tags=tuple(raw["meta"]["tags"]),
            outcome=raw["meta"]["outcome"],
            token_cost=raw["meta"]["token_cost"],
            loop_count=raw["meta"]["loop_count"],
            harness_version=raw["meta"]["harness_version"],
            submitter_hash=raw["meta"]["submitter_hash"],
        ),
        steps=tuple(
            TraceStep(
                tool_name=s["tool_name"],
                arguments_summary=s["arguments_summary"],
                result_summary=s["result_summary"],
                duration_ms=s["duration_ms"],
            )
            for s in raw["steps"]
        ),
        distilled_insight=raw["distilled_insight"],
        created_at=raw["created_at"],
        id=raw.get("id"),
        status=raw.get("status"),
        score=raw.get("score"),
    )


def test_trace_card_round_trip_preserves_all_fields():
    """Wire-format round-trip is the load-bearing invariant — both halves of
    the system serialize and deserialize the same TraceCard. Any drift
    here breaks production."""
    original = _sample_card()
    restored = _card_from_json(_card_to_json(original))
    assert restored == original


def test_trace_card_server_assigned_fields_default_none():
    """Submission shape: id/status/score are server-assigned and default to
    None on the agent side."""
    card = _sample_card()
    assert card.id is None
    assert card.status is None
    assert card.score is None


def test_query_result_default_empty():
    """QueryResult([]) is the canonical 'no trace matched' value the
    plugin's prefetch path falls through to explore on."""
    result = QueryResult()
    assert result.traces == ()
    assert result.served_from == "network"


def test_submit_receipt_rejected_shape():
    """A failed submit returns accepted=False with a reason — never raises.
    The outbox path depends on this contract."""
    receipt = SubmitReceipt(accepted=False, reason="network unreachable")
    assert receipt.accepted is False
    assert receipt.queue_id is None
    assert receipt.reason == "network unreachable"


# ─── ABC enforcement ──────────────────────────────────────────────────


def test_trace_network_client_cannot_be_instantiated_directly():
    """TraceNetworkClient is an ABC — direct instantiation must raise so
    subclasses are forced to implement the contract."""
    with pytest.raises(TypeError):
        TraceNetworkClient()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_concrete_trace_network_client_works():
    """A minimal concrete subclass that implements all three methods can be
    instantiated and used."""

    class _StubClient(TraceNetworkClient):
        async def query(
            self,
            intent: str,
            tags: tuple[str, ...],
            *,
            limit: int = 3,
            timeout_s: float = 1.0,
        ) -> QueryResult:
            return QueryResult(traces=(), query_id="stub", served_from="network")

        async def submit(self, card: TraceCard) -> SubmitReceipt:
            return SubmitReceipt(accepted=True, queue_id="stub-1")

        async def health(self, *, timeout_s: float = 1.0) -> bool:
            return True

    client = _StubClient()
    assert (await client.query("any", ())).traces == ()
    assert (await client.submit(_sample_card())).accepted is True
    assert await client.health() is True


# ─── BEFORE_TASK hook event registration ──────────────────────────────


def test_before_task_hook_event_present():
    """BEFORE_TASK is the new event added in Phase 0 — the social-traces
    plugin's prefetch path registers against it. Must round-trip its
    string value cleanly."""
    assert HookEvent.BEFORE_TASK.value == "BeforeTask"
    assert HookEvent("BeforeTask") is HookEvent.BEFORE_TASK


def test_before_task_in_all_hook_events_tuple():
    """ALL_HOOK_EVENTS is consumed by plugins that register one handler per
    event (audit logging etc.). BEFORE_TASK must be present so those
    plugins observe the new lifecycle point."""
    assert HookEvent.BEFORE_TASK in ALL_HOOK_EVENTS


def test_before_task_appears_after_existing_events():
    """ALL_HOOK_EVENTS preserves declaration order so iterators that depend
    on a specific position don't shift. BEFORE_TASK is appended at the
    tail, after the Wave 5 Hermes-port events.
    """
    last_known = ALL_HOOK_EVENTS.index(HookEvent.POST_APPROVAL_RESPONSE)
    new_position = ALL_HOOK_EVENTS.index(HookEvent.BEFORE_TASK)
    assert new_position > last_known


# ─── Literal type sanity ──────────────────────────────────────────────


def test_trace_status_literal_values():
    """The TraceStatus type alias enumerates the four lifecycle states. Both
    halves of the system rely on these strings; expand only via a
    co-ordinated change."""
    assert set(get_args(TraceStatus)) == {
        "pending",
        "approved",
        "rejected",
        "superseded",
    }


def test_trace_outcome_literal_values():
    assert set(get_args(TraceOutcome)) == {"success", "partial", "failed"}
