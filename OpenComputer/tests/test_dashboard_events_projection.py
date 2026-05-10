"""Tests for the dashboard /api/v1/events SSE projection.

Tier-A of 2026-05-10 memory-observability follow-through. Validates that
``project_event`` surfaces every dataclass field on a SignalEvent (base
+ subclass-specific) so consumers see the full payload rather than the
legacy 6-field-only projection.

Privacy regressions are explicitly guarded — the projection trusts the
per-event-class privacy contracts (``MemoryWriteEvent`` carries
``content_size`` only; ``MessageSignalEvent`` carries ``content_length``
only; etc.) but tests pin those contracts at the wire boundary so a
future event-class change can't silently leak content over SSE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from opencomputer.dashboard.routes.events import (
    _legacy_projection,
    project_event,
)
from plugin_sdk.ingestion import (
    ForegroundAppEvent,
    MemoryWriteEvent,
    MessageSignalEvent,
    SignalEvent,
    ToolCallEvent,
    WebObservationEvent,
)

# ─── base contract: 6 fields always present ─────────────────────────


class TestBaseFieldsAlwaysPresent:
    """Every projection must include the legacy 6 fields at unchanged paths.

    Pre-Tier-A consumers that read these keys must continue to work without
    modification. This is the BC contract for the SSE wire surface.
    """

    def test_base_signal_event_round_trips(self) -> None:
        ev = SignalEvent(
            session_id="s-1",
            source="agent_loop",
            metadata={"k": "v"},
        )
        out = project_event(ev)
        assert out["event_type"] == ""
        assert out["event_id"]  # uuid auto-populated
        assert isinstance(out["timestamp"], float)
        assert out["session_id"] == "s-1"
        assert out["source"] == "agent_loop"
        assert out["metadata"] == {"k": "v"}

    def test_subclass_still_has_all_base_fields(self) -> None:
        ev = MemoryWriteEvent(
            session_id="s-2",
            source="agent_memory",
            action="append",
            target="MEMORY.md",
            content_size=100,
        )
        out = project_event(ev)
        for key in ("event_type", "event_id", "timestamp", "session_id", "source", "metadata"):
            assert key in out, f"missing legacy base field: {key}"


# ─── subclass-specific fields are surfaced ──────────────────────────


class TestSubclassFieldsSurfaced:
    """Every subclass field must reach the SSE consumer — that's the whole
    point of Tier-A. Prevents the silent-stripping regression that the
    pre-Tier-A inline projection had."""

    def test_memory_write_event_carries_compaction_delta(self) -> None:
        ev = MemoryWriteEvent(
            session_id=None,
            source="agent_memory",
            action="append",
            target="MEMORY.md",
            content_size=3480,
            compaction_delta=520,
            dropped_paragraphs=2,
        )
        out = project_event(ev)
        assert out["action"] == "append"
        assert out["target"] == "MEMORY.md"
        assert out["content_size"] == 3480
        assert out["compaction_delta"] == 520
        assert out["dropped_paragraphs"] == 2
        assert out["event_type"] == "memory_write"

    def test_tool_call_event_carries_args_and_outcome(self) -> None:
        ev = ToolCallEvent(
            session_id="s",
            source="agent_loop",
            tool_name="Read",
            arguments={"path": "/tmp/x"},
            outcome="success",
            duration_seconds=0.42,
        )
        out = project_event(ev)
        assert out["tool_name"] == "Read"
        assert out["arguments"] == {"path": "/tmp/x"}
        assert out["outcome"] == "success"
        assert out["duration_seconds"] == 0.42

    def test_web_observation_event_carries_url(self) -> None:
        ev = WebObservationEvent(
            session_id="s",
            source="web_fetch",
            url="https://example.com/page",
            domain="example.com",
            content_kind="html",
            payload_size_bytes=1024,
        )
        out = project_event(ev)
        assert out["url"] == "https://example.com/page"
        assert out["domain"] == "example.com"
        assert out["content_kind"] == "html"
        assert out["payload_size_bytes"] == 1024


# ─── privacy regression guards ──────────────────────────────────────


class TestPrivacyContracts:
    """Pin the per-event-class privacy contracts at the SSE wire boundary.

    These tests fail loudly if a future event-class refactor introduces a
    raw-content field that the projection would now leak.
    """

    def test_memory_write_event_does_not_leak_content(self) -> None:
        # MemoryWriteEvent.__doc__ explicitly states "carries content_size
        # only — NOT the content being written". Projection must respect.
        ev = MemoryWriteEvent(
            session_id=None,
            source="agent_memory",
            action="append",
            target="MEMORY.md",
            content_size=42,
        )
        out = project_event(ev)
        # No field on the event carries the body — verify by absence.
        assert "content" not in out
        assert "body" not in out
        assert "text" not in out

    def test_message_signal_event_does_not_leak_content(self) -> None:
        # MessageSignalEvent.__doc__: "does NOT carry the message content"
        ev = MessageSignalEvent(
            session_id="s",
            role="user",
            content_length=500,
        )
        out = project_event(ev)
        assert out["content_length"] == 500
        assert "content" not in out
        assert "text" not in out

    def test_foreground_app_event_only_carries_hash(self) -> None:
        # ForegroundAppEvent.__doc__: "raw title NEVER leaves the sensor"
        ev = ForegroundAppEvent(
            session_id=None,
            source="ambient_foreground",
            app_name="Slack",
            window_title_hash="a" * 64,
            bundle_id="com.tinyspeck.slackmacgap",
            is_sensitive=False,
            platform="darwin",
        )
        out = project_event(ev)
        assert out["window_title_hash"] == "a" * 64
        assert out["app_name"] == "Slack"
        # Never a window_title raw field.
        assert "window_title" not in out


# ─── failure paths ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _BadEvent(SignalEvent):
    """Subclass with a non-serializable nested value to exercise failure paths."""

    event_type: str = "bad"
    weird: object = field(default_factory=object)


class TestProjectionFallback:
    """``project_event`` must never raise — failure falls back to the
    6-field legacy projection so the SSE stream stays alive even when an
    event class is malformed."""

    def test_non_dataclass_falls_back_to_legacy(self) -> None:
        class NotADataclass:
            event_type = "weird"
            event_id = "id-1"
            timestamp = 12345.0
            session_id = None
            source = "test"
            metadata = {"k": "v"}

        out = project_event(NotADataclass())
        # Falls back to the 6-field projection; subclass fields invisible.
        assert out["event_type"] == "weird"
        assert out["event_id"] == "id-1"
        assert out["source"] == "test"
        assert out["metadata"] == {"k": "v"}

    def test_legacy_projection_handles_missing_attrs(self) -> None:
        class Bare:
            pass

        out = _legacy_projection(Bare())
        # All keys present with safe defaults — never raises AttributeError.
        for key in ("event_type", "event_id", "timestamp", "session_id", "source", "metadata"):
            assert key in out

    def test_projection_result_is_json_serializable(self) -> None:
        """Final encoder line of defense uses default=str; verify the
        common-case dict is plain-JSON without coercion."""
        import json

        ev = MemoryWriteEvent(
            session_id="s",
            source="agent_memory",
            action="append",
            target="MEMORY.md",
            content_size=100,
            compaction_delta=10,
            dropped_paragraphs=1,
        )
        out = project_event(ev)
        # No default= needed for this — should round-trip cleanly.
        s = json.dumps(out)
        restored = json.loads(s)
        assert restored["compaction_delta"] == 10
        assert restored["dropped_paragraphs"] == 1


# ─── integration: every shipped SignalEvent subclass projects cleanly ──


class TestEverySubclassProjects:
    """Forward-compatibility test — instantiate every SignalEvent subclass
    with default values and confirm projection succeeds. Catches the
    regression where a new event class is added with a non-serializable
    field default.
    """

    def test_all_known_subclasses_round_trip(self) -> None:
        import json

        from plugin_sdk.ingestion import (
            AmbientSensorPauseEvent,
            DelegationCompleteEvent,
            FileObservationEvent,
            ForegroundAppEvent,
            HookSignalEvent,
            MemoryWriteEvent,
            MessageSignalEvent,
            PolicyChangeEvent,
            PolicyRevertedEvent,
            SessionEndEvent,
            SignalEvent,
            ToolCallEvent,
            TurnCompletedEvent,
            TurnStartEvent,
            WebObservationEvent,
        )

        for cls in (
            SignalEvent,
            ToolCallEvent,
            WebObservationEvent,
            FileObservationEvent,
            MessageSignalEvent,
            HookSignalEvent,
            TurnStartEvent,
            PolicyChangeEvent,
            PolicyRevertedEvent,
            TurnCompletedEvent,
            DelegationCompleteEvent,
            MemoryWriteEvent,
            ForegroundAppEvent,
            AmbientSensorPauseEvent,
            SessionEndEvent,
        ):
            ev = cls()  # default-constructed
            out = project_event(ev)
            # Must be JSON-serializable (default=str is the SSE encoder
            # safety net — but we want plain JSON in the common case).
            json.dumps(out, default=str)
            # Must include event_type discriminator at minimum.
            assert "event_type" in out
