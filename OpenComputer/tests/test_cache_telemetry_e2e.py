"""End-to-end regression test for cache-token telemetry pipeline.

The pipeline goes:
    Anthropic API response (cache_read_input_tokens / cache_creation_input_tokens)
      → AnthropicProvider._parse_response
      → ProviderResponse.usage (cache_read_tokens / cache_write_tokens)
      → AnthropicProvider._emit_llm_event
      → record_llm_call(LLMCallEvent(cache_creation_tokens, cache_read_tokens))
      → JSONL file at <profile>/llm_events.jsonl
      → oc usage --cache-stats reader

This test pins every hop. Previously diagnosed via 1,298-call telemetry
window showing all-zero cache stats — the pipeline was wired but
silently rotted any one hop would set the value to 0.

Lock the wiring with a single happy-path test that exercises every
seam: provider response → JSONL line → cli_usage reader sums.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load_anthropic_module():
    """Load the Anthropic provider module without going through plugin discovery."""
    name = "anthropic_provider_e2e_cache_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _REPO / "extensions" / "anthropic-provider" / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_fake_response(*, cache_read: int, cache_write: int) -> object:
    class _TextBlock:
        type = "text"
        text = "ok"

    class _Usage:
        input_tokens = 1500
        output_tokens = 200
        cache_read_input_tokens = cache_read
        cache_creation_input_tokens = cache_write

    class _FakeAnthropicMessage:
        content = [_TextBlock()]
        stop_reason = "end_turn"
        usage = _Usage()

    return _FakeAnthropicMessage()


# ─── Pipeline ─────────────────────────────────────────────────────────


def test_cache_tokens_flow_provider_to_jsonl(monkeypatch, tmp_path: Path):
    """A response with non-zero cache tokens must land in llm_events.jsonl."""
    mod = _load_anthropic_module()

    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    inst = mod.AnthropicProvider.__new__(mod.AnthropicProvider)
    fake_resp = _make_fake_response(cache_read=12000, cache_write=3000)

    parsed = inst._parse_response(fake_resp)
    # Hop 1: provider parsed canonical Usage correctly
    assert parsed.usage.cache_read_tokens == 12000
    assert parsed.usage.cache_write_tokens == 3000

    # Hop 2: emit the event the same way provider does
    inst._emit_llm_event(
        model="claude-opus-4-7", usage=parsed.usage, t0=0.0, t1=0.05,
    )

    # Hop 3: JSONL line written
    log_path = tmp_path / "llm_events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[-1])
    assert event["cache_read_tokens"] == 12000
    assert event["cache_creation_tokens"] == 3000
    assert event["input_tokens"] == 1500
    assert event["output_tokens"] == 200


def test_cli_usage_aggregates_cache_tokens_correctly(tmp_path: Path):
    """oc usage --cache-stats reader sums cache tokens across events."""
    from opencomputer.cli_usage import _render_cache_stats

    log_path = tmp_path / "llm_events.jsonl"
    events = [
        {
            "ts": "2026-05-05T10:00:00+00:00",
            "provider": "anthropic", "model": "claude-opus-4-7",
            "input_tokens": 1500, "output_tokens": 200,
            "cache_creation_tokens": 3000, "cache_read_tokens": 12000,
            "latency_ms": 1500, "cost_usd": 0.05, "site": "agent_loop",
        },
        {
            "ts": "2026-05-05T10:00:01+00:00",
            "provider": "anthropic", "model": "claude-opus-4-7",
            "input_tokens": 200, "output_tokens": 100,
            "cache_creation_tokens": 0, "cache_read_tokens": 12500,
            "latency_ms": 800, "cost_usd": 0.01, "site": "agent_loop",
        },
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    captured: list[str] = []

    class _ConsoleStub:
        def print(self, *args, **_kwargs):
            captured.append(" ".join(str(a) for a in args))

    import opencomputer.cli_usage as mod

    real_console = mod.console
    mod.console = _ConsoleStub()  # type: ignore[assignment]
    try:
        _render_cache_stats(events, hours=1, days=None, json_out=True)
    finally:
        mod.console = real_console

    out = "\n".join(captured)
    parsed = json.loads(out)
    # Aggregated row across the 2 sample events (same provider+model+site).
    row = parsed["rows"][0]
    assert row["cache_creation_tokens"] == 3000
    assert row["cache_read_tokens"] == 24500
    assert row["calls"] == 2


def test_zero_cache_tokens_does_not_break_pipeline(
    monkeypatch, tmp_path: Path
):
    """A response with zero cache tokens (auxiliary classifier call) still
    writes a clean JSONL row — values are 0, not missing fields."""
    mod = _load_anthropic_module()

    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    inst = mod.AnthropicProvider.__new__(mod.AnthropicProvider)
    fake_resp = _make_fake_response(cache_read=0, cache_write=0)

    parsed = inst._parse_response(fake_resp)
    inst._emit_llm_event(
        model="claude-haiku-4-5", usage=parsed.usage, t0=0.0, t1=0.01,
    )

    log_path = tmp_path / "llm_events.jsonl"
    event = json.loads(log_path.read_text().splitlines()[-1])
    assert event["cache_read_tokens"] == 0
    assert event["cache_creation_tokens"] == 0
    # Ensure fields exist (not just missing) so the reader does not
    # silently treat absence as 0.
    assert "cache_read_tokens" in event
    assert "cache_creation_tokens" in event


def test_missing_cache_attrs_default_to_zero(monkeypatch, tmp_path: Path):
    """An older Anthropic SDK without cache_*_input_tokens attrs must
    default to 0 — we use getattr(..., 0). Belt-and-suspenders for the
    field-name divergence the user flagged in the bug report."""
    mod = _load_anthropic_module()

    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    class _SkinnyUsage:
        input_tokens = 100
        output_tokens = 50
        # NOTE: no cache_*_input_tokens attrs

    class _FakeMessage:
        content = []
        stop_reason = "end_turn"
        usage = _SkinnyUsage()

    inst = mod.AnthropicProvider.__new__(mod.AnthropicProvider)
    parsed = inst._parse_response(_FakeMessage())
    assert parsed.usage.cache_read_tokens == 0
    assert parsed.usage.cache_write_tokens == 0

    # Doesn't crash on emit either.
    inst._emit_llm_event(
        model="claude-opus-4-7", usage=parsed.usage, t0=0.0, t1=0.01,
    )
    log_path = tmp_path / "llm_events.jsonl"
    assert log_path.exists()


def test_streaming_path_uses_same_parser(monkeypatch, tmp_path: Path):
    """The streaming code path also calls _parse_response on the final
    aggregated message. This locks that fact: as long as
    `_do_stream_complete` uses `_parse_response(final)`, cache tokens
    propagate the same way as the non-streaming path."""
    mod = _load_anthropic_module()

    src = (
        _REPO / "extensions" / "anthropic-provider" / "provider.py"
    ).read_text()
    # The streaming code path MUST funnel through _parse_response so the
    # streaming/non-streaming divergence the user worried about can't
    # silently re-emerge.
    assert "result = self._parse_response(final)" in src
    # And it must call _emit_llm_event with that result.usage. The
    # call may now span multiple lines and carry extra kwargs (e.g.
    # ``messages`` + ``response_text`` for langfuse input/output
    # capture). The lock here is: ``model=model`` and ``usage=result.usage``
    # both appear inside the same ``self._emit_llm_event(...)`` call.
    import re

    pattern = re.compile(
        r"self\._emit_llm_event\([^)]*?model=model[^)]*?usage=result\.usage",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "streaming path no longer threads model + result.usage into "
        "_emit_llm_event — the cache-telemetry pipeline could regress"
    )
