import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from opencomputer.cli_insights import insights_app


def _write_event(log_path, **overrides):
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "latency_ms": 800,
        "cost_usd": 0.001,
        "site": "agent_loop",
    }
    event.update(overrides)
    with log_path.open("a") as f:
        f.write(json.dumps(event) + "\n")


def test_insights_llm_no_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(insights_app, ["llm"])
    assert result.exit_code == 0
    assert "No LLM events" in result.stdout


def test_insights_llm_reads_recent_events(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    log = tmp_path / "llm_events.jsonl"
    _write_event(log)
    _write_event(log, provider="openai", model="gpt-4o", site="reflect")

    runner = CliRunner()
    result = runner.invoke(insights_app, ["llm"])
    assert result.exit_code == 0
    assert "anthropic" in result.stdout
    assert "openai" in result.stdout
    assert "claude-sonnet-4-6" in result.stdout or "Calls" in result.stdout


def test_insights_llm_filters_by_hours(tmp_path, monkeypatch):
    """Events older than --hours window are excluded."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    log = tmp_path / "llm_events.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    _write_event(log, ts=old_ts, provider="old-provider")
    _write_event(log, ts=new_ts, provider="anthropic")

    runner = CliRunner()
    result = runner.invoke(insights_app, ["llm", "--hours", "24"])
    assert result.exit_code == 0
    assert "anthropic" in result.stdout
    assert "old-provider" not in result.stdout


def test_insights_llm_skips_blank_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    log = tmp_path / "llm_events.jsonl"
    log.write_text("\n\n")
    runner = CliRunner()
    result = runner.invoke(insights_app, ["llm"])
    assert result.exit_code == 0
