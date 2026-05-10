"""M2.2 — --output text|json|stream-json on oc oneshot.

Covers:

* :class:`OutputMode` enum + parser contract.
* :class:`OneshotResult` aggregation (record_event, to_summary_dict).
* :func:`emit_final` per-mode stdout shape.
* :func:`stream_subscriber` registration + per-event emission.

These tests exercise the formatter directly via an in-memory buffer so
they don't need a real provider — the integration with
``_run_oneshot_turn`` is covered separately by the pre-existing
``oc oneshot`` smoke tests.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timezone

import pytest

from opencomputer.headless import OutputMode, parse_output_mode
from opencomputer.inference.observability import LLMCallEvent
from opencomputer.oneshot_output import (
    OneshotResult,
    emit_final,
    stream_subscriber,
)

# ─── OutputMode enum + parser ────────────────────────────────────────────


class TestOutputMode:
    def test_text_is_default_string(self) -> None:
        assert OutputMode.TEXT == "text"
        assert OutputMode.JSON == "json"
        assert OutputMode.STREAM_JSON == "stream-json"

    def test_parse_canonical_values(self) -> None:
        assert parse_output_mode("text") is OutputMode.TEXT
        assert parse_output_mode("json") is OutputMode.JSON
        assert parse_output_mode("stream-json") is OutputMode.STREAM_JSON

    def test_parse_unknown_raises_with_friendly_message(self) -> None:
        with pytest.raises(ValueError, match=r"unknown --output mode 'xml'"):
            parse_output_mode("xml")

    def test_parse_lists_valid_modes_in_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_output_mode("garbage")
        msg = str(exc_info.value)
        for canonical in ("text", "json", "stream-json"):
            assert canonical in msg


# ─── OneshotResult aggregation ───────────────────────────────────────────


class TestOneshotResultAggregation:
    def test_record_event_accumulates_tokens(self) -> None:
        r = OneshotResult()
        r.record_event(
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 5,
                "cost_usd": 0.001,
            }
        )
        r.record_event(
            {
                "input_tokens": 7,
                "output_tokens": 13,
                "cache_creation_tokens": 100,
                "cache_read_tokens": 0,
                "cost_usd": 0.002,
            }
        )
        assert r.num_turns == 2
        assert r.total_input_tokens == 17
        assert r.total_output_tokens == 33
        assert r.total_cache_creation_tokens == 100
        assert r.total_cache_read_tokens == 5
        assert r.total_cost_usd == pytest.approx(0.003)

    def test_record_event_handles_missing_fields(self) -> None:
        r = OneshotResult()
        r.record_event({})  # no token fields at all
        assert r.num_turns == 1
        assert r.total_input_tokens == 0
        assert r.total_cost_usd == 0.0

    def test_record_event_handles_none_cost(self) -> None:
        r = OneshotResult()
        r.record_event({"cost_usd": None})
        assert r.total_cost_usd == 0.0  # None doesn't poison the running sum

    def test_to_summary_dict_shape(self) -> None:
        r = OneshotResult(session_id="sess-abc", final_message="hello")
        r.record_event(
            {"input_tokens": 5, "output_tokens": 10, "cost_usd": 0.0001}
        )
        d = r.to_summary_dict()
        assert d["session_id"] == "sess-abc"
        assert d["final_message"] == "hello"
        assert d["num_turns"] == 1
        assert d["total_input_tokens"] == 5
        assert d["total_output_tokens"] == 10
        assert d["total_cost_usd"] == 0.0001
        assert "error" not in d  # only present when error_code set

    def test_to_summary_dict_includes_error_when_set(self) -> None:
        r = OneshotResult(error_code="provider_error", error_message="429 backoff")
        d = r.to_summary_dict()
        assert d["error"] == {"code": "provider_error", "message": "429 backoff"}


# ─── emit_final per-mode ─────────────────────────────────────────────────


class TestEmitFinalText:
    def test_text_mode_prints_final_message(self) -> None:
        r = OneshotResult(final_message="the answer is 42")
        buf = io.StringIO()
        emit_final(r, OutputMode.TEXT, out=buf)
        assert buf.getvalue() == "the answer is 42\n"

    def test_text_mode_empty_message_prints_nothing(self) -> None:
        r = OneshotResult(final_message="")
        buf = io.StringIO()
        emit_final(r, OutputMode.TEXT, out=buf)
        assert buf.getvalue() == ""


class TestEmitFinalJson:
    def test_json_mode_prints_one_parseable_object(self) -> None:
        r = OneshotResult(
            session_id="sess-xyz",
            final_message="ok",
        )
        r.record_event({"input_tokens": 3, "output_tokens": 4, "cost_usd": 0.0})
        buf = io.StringIO()
        emit_final(r, OutputMode.JSON, out=buf)
        line = buf.getvalue().strip()
        obj = json.loads(line)
        assert obj["session_id"] == "sess-xyz"
        assert obj["final_message"] == "ok"
        assert obj["num_turns"] == 1
        assert obj["total_input_tokens"] == 3
        assert obj["total_output_tokens"] == 4

    def test_json_mode_emits_exactly_one_line(self) -> None:
        r = OneshotResult(final_message="x")
        buf = io.StringIO()
        emit_final(r, OutputMode.JSON, out=buf)
        assert buf.getvalue().count("\n") == 1


class TestEmitFinalStreamJson:
    def test_stream_json_summary_line_tags_event_field(self) -> None:
        r = OneshotResult(session_id="s", final_message="done")
        buf = io.StringIO()
        emit_final(r, OutputMode.STREAM_JSON, out=buf)
        obj = json.loads(buf.getvalue().strip())
        assert obj["event"] == "summary"
        assert obj["session_id"] == "s"
        assert obj["final_message"] == "done"


# ─── stream_subscriber registration ──────────────────────────────────────


class TestStreamSubscriber:
    def _make_event(self, **overrides) -> LLMCallEvent:
        defaults = dict(
            ts=datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
            provider="anthropic",
            model="claude-opus-4-7",
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            latency_ms=500,
            cost_usd=0.001,
            site=None,
        )
        defaults.update(overrides)
        return LLMCallEvent(**defaults)

    def test_subscriber_records_event_into_result(self) -> None:
        r = OneshotResult()
        with stream_subscriber(r, OutputMode.JSON):
            # In JSON mode, subscriber records but does NOT print per-event.
            # We can't easily exercise record_llm_call without a real
            # filesystem; assert the subscriber WAS registered by checking
            # that the subscriber list captured it via direct invocation.
            from opencomputer.inference.observability import _subscribers

            assert len(_subscribers) >= 1
            # Manually fire a synthetic event through the subscriber chain
            for sub in list(_subscribers):
                sub(self._make_event())

        assert r.num_turns == 1
        assert r.total_input_tokens == 10
        assert r.total_output_tokens == 20

    def test_subscriber_unregisters_on_exit(self) -> None:
        from opencomputer.inference.observability import _subscribers

        before = len(_subscribers)
        r = OneshotResult()
        with stream_subscriber(r, OutputMode.TEXT):
            pass
        after = len(_subscribers)
        assert after == before  # cleanup happened

    def test_subscriber_unregisters_on_exception(self) -> None:
        from opencomputer.inference.observability import _subscribers

        before = len(_subscribers)
        r = OneshotResult()
        with pytest.raises(RuntimeError, match="boom"):
            with stream_subscriber(r, OutputMode.STREAM_JSON):
                raise RuntimeError("boom")
        after = len(_subscribers)
        assert after == before  # cleanup happened despite exception


# ─── stream-json mode emits per-event NDJSON to stdout ───────────────────


class TestStreamJsonRealtime:
    """Verify stream-json mode writes events to stdout AS THEY FIRE.

    We replace sys.stdout with an in-memory buffer for the duration of
    the subscriber to avoid polluting the test runner's stdout.
    """

    def test_stream_json_per_event_line_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)

        r = OneshotResult()
        ev = LLMCallEvent(
            ts=datetime(2026, 5, 9, tzinfo=UTC),
            provider="anthropic",
            model="claude-opus-4-7",
            input_tokens=42,
            output_tokens=7,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            latency_ms=250,
            cost_usd=0.0005,
            site=None,
        )
        with stream_subscriber(r, OutputMode.STREAM_JSON):
            from opencomputer.inference.observability import _subscribers

            for sub in list(_subscribers):
                sub(ev)

        line = buf.getvalue().strip()
        obj = json.loads(line)
        assert obj["event"] == "llm_call"
        assert obj["provider"] == "anthropic"
        assert obj["input_tokens"] == 42
        # ts is ISO-formatted, not a raw datetime
        assert isinstance(obj["ts"], str)
