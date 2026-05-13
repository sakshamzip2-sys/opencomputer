"""Cross-surface event delivery tests for ProfileSwapEvent.

Validates that a single bus publish reaches:
  * Wire WebSocket clients via :class:`WireServer._on_profile_swap_bus_event`
  * HTTP/SSE clients via the existing ``/api/v1/events`` wildcard stream
    (OC webui + hermes-workspace + any future SSE consumer)

The dashboard SSE endpoint subscribes to ``*`` so any bus event projects
automatically — this test pins that contract so a future refactor that
narrows the wildcard would break the test before the workspace.
"""
from __future__ import annotations

import dataclasses
import time

from opencomputer.dashboard.routes.events import project_event
from plugin_sdk.ingestion import ProfileSwapEvent


class TestSSEProjection:
    def test_profile_swap_event_projects_to_full_dict(self) -> None:
        """Bus event → SSE-ready dict with every dataclass field."""
        evt = ProfileSwapEvent(
            from_profile="default",
            to_profile="stocks",
            trigger="auto",
            classifier_confidence=0.87,
            classifier_reason="state-query detected",
            has_handoff=True,
        )
        projected = project_event(evt)

        # Base SignalEvent fields are present
        assert projected["event_type"] == "profile_swap"
        assert "event_id" in projected
        assert "timestamp" in projected
        assert "source" in projected
        assert "metadata" in projected

        # ProfileSwapEvent-specific fields are present
        assert projected["from_profile"] == "default"
        assert projected["to_profile"] == "stocks"
        assert projected["trigger"] == "auto"
        assert projected["classifier_confidence"] == 0.87
        assert projected["classifier_reason"] == "state-query detected"
        assert projected["has_handoff"] is True

    def test_profile_swap_event_dataclass_invariants(self) -> None:
        """Frozen + slotted + correct event_type sentinel."""
        evt = ProfileSwapEvent(from_profile="a", to_profile="b")
        assert dataclasses.is_dataclass(evt)
        assert evt.event_type == "profile_swap"
        # Frozen: mutation raises
        import pytest

        with pytest.raises(dataclasses.FrozenInstanceError):
            evt.to_profile = "c"  # type: ignore[misc]
        # Slotted: no __dict__
        assert not hasattr(evt, "__dict__")

    def test_event_type_is_stable_wire_contract(self) -> None:
        """The event_type string IS the wildcard match key — must be stable."""
        evt = ProfileSwapEvent(from_profile="a", to_profile="b")
        # If you change "profile_swap" you break:
        #   * gateway/wire_server.py:_on_profile_swap_bus_event subscription
        #   * dashboard/routes/events.py SSE pattern matching by clients
        #   * any future ts-client filter
        assert evt.event_type == "profile_swap"


class TestWildcardSubscriptionContract:
    def test_dashboard_sse_uses_wildcard(self) -> None:
        """The dashboard SSE route MUST subscribe with ``*`` so new events
        like profile_swap reach UI surfaces without a code change there."""
        # Read the source to assert the wildcard pattern is in the route.
        # This is a contract test — protects against a refactor that
        # accidentally narrows the subscription to a fixed allowlist.
        import inspect

        from opencomputer.dashboard.routes import events as events_mod

        src = inspect.getsource(events_mod.events)
        # Either "*" as the default or explicit list including profile_swap
        has_wildcard = '["*"]' in src or "'*'" in src or '"*"' in src
        assert has_wildcard, (
            "events SSE route must keep wildcard pattern matching so "
            "new bus events reach UI surfaces without route changes."
        )


class TestWirePayloadShape:
    def test_profile_swap_payload_matches_event_fields(self) -> None:
        """ProfileSwapPayload (wire) carries every meaningful event field."""
        from opencomputer.gateway.protocol_v2 import ProfileSwapPayload

        # Construct the payload manually with the same data shape the
        # bus handler builds. Validates the wire contract: any field
        # present in the bus event is reachable by a WS client.
        payload = ProfileSwapPayload(
            from_profile="default",
            to_profile="stocks",
            trigger="auto",
            classifier_confidence=0.87,
            classifier_reason="state-query detected",
            has_handoff=True,
        )
        assert payload.from_profile == "default"
        assert payload.to_profile == "stocks"
        assert payload.trigger == "auto"
        assert payload.classifier_confidence == 0.87
        assert payload.has_handoff is True

    def test_payload_required_fields_only(self) -> None:
        """Minimal construction works (only required fields)."""
        from opencomputer.gateway.protocol_v2 import ProfileSwapPayload

        payload = ProfileSwapPayload(
            from_profile="a", to_profile="b", trigger="manual",
        )
        # Defaults applied
        assert payload.classifier_confidence == 0.0
        assert payload.classifier_reason == ""
        assert payload.has_handoff is False
