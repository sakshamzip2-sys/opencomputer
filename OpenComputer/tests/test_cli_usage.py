"""Tests for opencomputer.cli_usage — token + cache-stats CLI surface.

Closes the cache-stats deferral from PR #420 Wave 5 T5. Mocks
``llm_events.jsonl`` via OPENCOMPUTER_PROFILE_HOME so each test gets
its own scratch profile directory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_usage import usage_app

runner = CliRunner()


def _write_events(home: Path, events: list[dict]) -> None:
    """Write a list of dicts as JSON lines into the profile's llm_events.jsonl."""
    home.mkdir(parents=True, exist_ok=True)
    log = home / "llm_events.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _ev(
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    site: str = "agent_loop",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    cost_usd: float = 0.01,
    minutes_ago: int = 1,
    latency_ms: int = 250,
) -> dict:
    """Build a single LLMCallEvent-shaped dict for tests."""
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "ts": ts.isoformat(),
        "provider": provider,
        "model": model,
        "site": site,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Per-test profile home so events from one test don't bleed into another."""
    profile = tmp_path / "profile_home"
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(profile))
    return profile


# ─── default summary view ───


def test_no_events_shows_friendly_message(home):
    result = runner.invoke(usage_app, [])
    assert result.exit_code == 0
    assert "no llm events" in result.output.lower()


def test_summary_includes_totals_per_provider(home):
    _write_events(home, [
        _ev(provider="anthropic", input_tokens=1000, output_tokens=500, cost_usd=0.05),
        _ev(provider="anthropic", input_tokens=500, output_tokens=200, cost_usd=0.02),
        _ev(provider="openai", input_tokens=2000, output_tokens=300, cost_usd=0.04),
    ])
    # Use --json to avoid Rich-table column truncation in narrow CI terminals
    result = runner.invoke(usage_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["totals"]["calls"] == 3
    assert "anthropic" in payload["per_provider"]
    assert "openai" in payload["per_provider"]
    assert payload["per_provider"]["anthropic"]["calls"] == 2
    assert payload["per_provider"]["openai"]["calls"] == 1


def test_summary_renders_cache_columns(home):
    _write_events(home, [
        _ev(cache_creation=1000, cache_read=0, cost_usd=0.10),
        _ev(cache_creation=0, cache_read=2000, cost_usd=0.01),
    ])
    result = runner.invoke(usage_app, [])
    assert result.exit_code == 0
    # Cache hit ratio: 2000 / (1000 + 2000) = 66.7%
    assert "66.7%" in result.output


def test_provider_filter_excludes_others(home):
    _write_events(home, [
        _ev(provider="anthropic", cost_usd=1.00),
        _ev(provider="openai", cost_usd=2.00),
    ])
    result = runner.invoke(usage_app, ["--provider", "anthropic", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "anthropic" in payload["per_provider"]
    assert "openai" not in payload["per_provider"]


def test_model_filter_excludes_others(home):
    _write_events(home, [
        _ev(model="claude-sonnet-4-6", cost_usd=1.00),
        _ev(model="gpt-4o", cost_usd=2.00),
    ])
    result = runner.invoke(
        usage_app, ["--model", "claude-sonnet-4-6", "--cache-stats", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    models = {row["model"] for row in payload["rows"]}
    assert "claude-sonnet-4-6" in models
    assert "gpt-4o" not in models


def test_hours_window_filters_old_events(home):
    """Events older than --hours window should be excluded."""
    _write_events(home, [
        _ev(minutes_ago=30, cost_usd=0.10),  # within 1h
        _ev(minutes_ago=60 * 5, cost_usd=0.20),  # 5h ago
    ])
    result = runner.invoke(usage_app, ["--hours", "1"])
    # Only the 30min-ago event should appear in totals
    assert result.exit_code == 0
    assert "1 calls" in result.output.lower()


def test_days_window_filters_old_events(home):
    _write_events(home, [
        _ev(minutes_ago=60 * 24, cost_usd=0.10),  # 1d ago
        _ev(minutes_ago=60 * 24 * 5, cost_usd=0.20),  # 5d ago
    ])
    # --hours 0 to disable the default; --days 2 catches 1d-ago but not 5d-ago
    result = runner.invoke(usage_app, ["--hours", "0", "--days", "2", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["totals"]["calls"] == 1


def test_hours_and_days_both_set_errors(home):
    result = runner.invoke(usage_app, ["--hours", "5", "--days", "2"])
    assert result.exit_code == 1
    assert "use either" in result.output.lower()


# ─── --json output ───


def test_json_output_summary_shape(home):
    _write_events(home, [
        _ev(provider="anthropic", input_tokens=100, output_tokens=50,
            cache_read=200, cache_creation=300, cost_usd=0.01),
    ])
    result = runner.invoke(usage_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["totals"]["calls"] == 1
    assert payload["totals"]["input_tokens"] == 100
    assert payload["totals"]["output_tokens"] == 50
    assert payload["totals"]["cache_read_tokens"] == 200
    assert payload["totals"]["cache_creation_tokens"] == 300
    # cache_hit_ratio = 200 / (200 + 300) = 0.4
    assert abs(payload["totals"]["cache_hit_ratio"] - 0.4) < 1e-9
    assert "anthropic" in payload["per_provider"]


def test_json_output_no_events(home):
    result = runner.invoke(usage_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["events"] == 0


def test_json_output_cache_stats(home):
    _write_events(home, [
        _ev(provider="anthropic", model="claude-sonnet-4-6", site="agent_loop",
            cache_read=500, cache_creation=100, cost_usd=0.02),
        _ev(provider="anthropic", model="claude-sonnet-4-6", site="eval_grader",
            cache_read=100, cache_creation=300, cost_usd=0.01),
    ])
    result = runner.invoke(usage_app, ["--cache-stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    rows = payload["rows"]
    assert len(rows) == 2
    sites = {r["site"] for r in rows}
    assert sites == {"agent_loop", "eval_grader"}
    # Sort by cost desc — agent_loop first
    sorted_by_cost = sorted(rows, key=lambda r: r["cost_usd"], reverse=True)
    assert sorted_by_cost[0]["site"] == "agent_loop"


# ─── --cache-stats view ───


def test_cache_stats_renders_breakdown(home):
    _write_events(home, [
        _ev(provider="anthropic", model="claude-sonnet-4-6", site="agent_loop",
            cache_read=1000, cache_creation=200, cost_usd=0.05),
        _ev(provider="openai", model="gpt-4o", site="eval_grader",
            cache_read=500, cache_creation=500, cost_usd=0.10),
    ])
    # JSON to avoid Rich's column-narrowing truncation in narrow terminals
    result = runner.invoke(usage_app, ["--cache-stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    rows_by_provider = {r["provider"]: r for r in payload["rows"]}
    assert "anthropic" in rows_by_provider
    assert "openai" in rows_by_provider
    # Hit ratio for anthropic = 1000 / 1200 ≈ 0.833
    assert abs(rows_by_provider["anthropic"]["cache_hit_ratio"] - 1000 / 1200) < 1e-6
    # Hit ratio for openai = 500 / 1000 = 0.5
    assert abs(rows_by_provider["openai"]["cache_hit_ratio"] - 0.5) < 1e-9


def test_cache_stats_handles_no_cache_traffic(home):
    """Events without cache_creation/cache_read shouldn't crash."""
    _write_events(home, [
        _ev(provider="anthropic", input_tokens=100, output_tokens=50,
            cache_creation=0, cache_read=0, cost_usd=0.01),
    ])
    result = runner.invoke(usage_app, ["--cache-stats"])
    assert result.exit_code == 0
    # Hit ratio undefined ("—")
    assert "—" in result.output or "0.0%" in result.output


def test_cache_stats_overall_hit_ratio_in_header(home):
    _write_events(home, [
        _ev(cache_read=900, cache_creation=100, cost_usd=0.01),
    ])
    result = runner.invoke(usage_app, ["--cache-stats"])
    assert result.exit_code == 0
    # Overall hit ratio: 900 / 1000 = 90.0%
    assert "90.0%" in result.output


def test_cache_stats_no_events_gracefully_returns(home):
    result = runner.invoke(usage_app, ["--cache-stats"])
    assert result.exit_code == 0
    # Should NOT raise; just print the no-events message
    assert "no llm events" in result.output.lower()


# ─── data integrity ───


def test_corrupted_jsonl_lines_skipped_gracefully(home):
    """Malformed JSONL lines must not crash the entire summary."""
    home.mkdir(parents=True, exist_ok=True)
    log = home / "llm_events.jsonl"
    log.write_text(
        json.dumps(_ev(provider="anthropic", cost_usd=0.01)) + "\n"
        + "this is not json\n"
        + json.dumps(_ev(provider="openai", cost_usd=0.02)) + "\n"
        + "\n"  # blank line
    )
    result = runner.invoke(usage_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["totals"]["calls"] == 2  # corrupted line skipped


def test_events_missing_optional_fields_default_to_zero(home):
    """Tolerate sparse events — old recorder versions may have fewer fields."""
    home.mkdir(parents=True, exist_ok=True)
    log = home / "llm_events.jsonl"
    log.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        # no input_tokens, no cache_*, no cost_usd
    }) + "\n")

    result = runner.invoke(usage_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["totals"]["calls"] == 1
    assert payload["totals"]["input_tokens"] == 0


def test_unknown_site_grouped_as_none(home):
    """Events without a site go into the (none) bucket in cache-stats."""
    _write_events(home, [
        _ev(site=None, cache_read=100, cache_creation=100, cost_usd=0.01),  # type: ignore[arg-type]
    ])
    result = runner.invoke(usage_app, ["--cache-stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rows"][0]["site"] == "(none)"


# ─── window=0 (all-time) ───


def test_all_time_when_hours_zero(home):
    """hours=0 + days=0 means all-time."""
    _write_events(home, [
        _ev(minutes_ago=60 * 24 * 365, cost_usd=0.10),  # 1y ago
    ])
    result = runner.invoke(usage_app, ["--hours", "0"])
    assert result.exit_code == 0
    assert "1 calls" in result.output.lower()
