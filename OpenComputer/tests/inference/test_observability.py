from datetime import datetime, timezone

from opencomputer.inference.observability import LLMCallEvent, record_llm_call


def test_llm_call_event_dataclass():
    event = LLMCallEvent(
        ts=datetime.now(timezone.utc),
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=20,
        cache_read_tokens=80,
        latency_ms=850,
        cost_usd=0.012,
        site="agent_loop",
    )
    assert event.provider == "anthropic"


def test_record_llm_call_appends_to_log(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    event = LLMCallEvent(
        ts=datetime.now(timezone.utc),
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        latency_ms=850,
        cost_usd=None,
        site=None,
    )
    record_llm_call(event)
    log = tmp_path / "llm_events.jsonl"
    assert log.exists()
    assert "anthropic" in log.read_text()
