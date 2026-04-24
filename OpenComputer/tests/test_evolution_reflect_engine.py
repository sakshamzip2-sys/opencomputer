"""Tests for ReflectionEngine.reflect() — B2.3 implementation.

Uses FakeProvider / CapturingProvider test doubles; no real LLM calls.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from opencomputer.evolution.reflect import Insight, ReflectionEngine
from opencomputer.evolution.trajectory import SCHEMA_VERSION_CURRENT, TrajectoryRecord, new_record

# ---------------------------------------------------------------------------
# Shared JSON fixtures
# ---------------------------------------------------------------------------

SINGLE_INSIGHT_JSON = """[
  {
    "observation": "Read followed by Edit is a common 2-step pattern",
    "evidence_refs": [1, 2],
    "action_type": "create_skill",
    "payload": {
      "slug": "read-then-edit",
      "name": "Read-then-Edit",
      "description": "When you read a file then edit, use this combined skill",
      "body": "# Read-then-Edit\\n\\nReads then immediately edits..."
    },
    "confidence": 0.82
  }
]"""

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeProvider:
    response_text: str
    name: str = "fake"
    default_model: str = "fake-model"
    call_count: int = 0

    async def complete(self, **kwargs):
        self.call_count += 1
        from plugin_sdk.core import Message
        from plugin_sdk.provider_contract import ProviderResponse, Usage

        return ProviderResponse(
            message=Message(role="assistant", content=self.response_text),
            stop_reason="end_turn",
            usage=Usage(),
        )

    async def stream_complete(self, **kwargs):
        raise NotImplementedError


class CapturingProvider(FakeProvider):
    def __init__(self, response_text: str = "[]"):
        super().__init__(response_text=response_text)
        self.last_messages = None

    async def complete(self, **kwargs):
        self.last_messages = kwargs.get("messages")
        return await super().complete(**kwargs)


# ---------------------------------------------------------------------------
# Helper: build a minimal TrajectoryRecord with optional id
# ---------------------------------------------------------------------------


def _make_record(id: int | None = None, session_id: str = "sess") -> TrajectoryRecord:
    return TrajectoryRecord(
        id=id,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=1_000_000.0,
        ended_at=1_000_010.0,
        events=(),
        completion_flag=True,
    )


# ---------------------------------------------------------------------------
# 1. Empty records → empty list; provider IS called
# ---------------------------------------------------------------------------


def test_reflect_empty_records_returns_empty() -> None:
    """reflect([]) with provider returning '[]' returns empty list; provider is called."""
    provider = FakeProvider(response_text="[]")
    engine = ReflectionEngine(provider=provider, window=30)
    result = engine.reflect([])
    assert result == []
    assert provider.call_count == 1


# ---------------------------------------------------------------------------
# 2. Single valid insight is parsed correctly
# ---------------------------------------------------------------------------


def test_reflect_parses_single_insight() -> None:
    """Provider returning one valid Insight JSON object → list with one Insight."""
    provider = FakeProvider(response_text=SINGLE_INSIGHT_JSON)
    engine = ReflectionEngine(provider=provider, window=30)
    records = [_make_record(id=1), _make_record(id=2)]
    result = engine.reflect(records)
    assert len(result) == 1
    ins = result[0]
    assert isinstance(ins, Insight)
    assert ins.observation == "Read followed by Edit is a common 2-step pattern"
    assert ins.evidence_refs == (1, 2)
    assert ins.action_type == "create_skill"
    assert ins.confidence == pytest.approx(0.82)
    assert ins.payload["slug"] == "read-then-edit"


# ---------------------------------------------------------------------------
# 3. evidence_refs not in record_ids are filtered out
# ---------------------------------------------------------------------------


def test_reflect_filters_invalid_evidence_refs() -> None:
    """evidence_refs containing ids not in the records set are filtered to ()."""
    json_with_bad_ref = """[
      {
        "observation": "Some pattern",
        "evidence_refs": [99999],
        "action_type": "noop",
        "payload": {"reason": "test"},
        "confidence": 0.7
      }
    ]"""
    provider = FakeProvider(response_text=json_with_bad_ref)
    engine = ReflectionEngine(provider=provider, window=30)
    records = [_make_record(id=1)]
    result = engine.reflect(records)
    assert len(result) == 1
    assert result[0].evidence_refs == ()


# ---------------------------------------------------------------------------
# 4. Markdown fence is stripped before JSON parse
# ---------------------------------------------------------------------------


def test_reflect_handles_markdown_fence_wrapper() -> None:
    """Provider returning ```json ... ``` fence still yields correct Insight."""
    fenced = "```json\n" + SINGLE_INSIGHT_JSON.strip() + "\n```"
    provider = FakeProvider(response_text=fenced)
    engine = ReflectionEngine(provider=provider, window=30)
    records = [_make_record(id=1), _make_record(id=2)]
    result = engine.reflect(records)
    assert len(result) == 1
    assert result[0].action_type == "create_skill"


# ---------------------------------------------------------------------------
# 5. Malformed entries are skipped, valid ones returned
# ---------------------------------------------------------------------------


def test_reflect_skips_malformed_entries() -> None:
    """Array with one entry missing 'observation' → that entry skipped, rest returned."""
    mixed_json = """[
      {
        "observation": "Pattern A",
        "evidence_refs": [1],
        "action_type": "noop",
        "payload": {"reason": "valid first"},
        "confidence": 0.6
      },
      {
        "evidence_refs": [1],
        "action_type": "noop",
        "payload": {"reason": "missing observation key"},
        "confidence": 0.7
      },
      {
        "observation": "Pattern C",
        "evidence_refs": [1],
        "action_type": "edit_prompt",
        "payload": {"target": "system", "diff_hint": "some edit"},
        "confidence": 0.75
      }
    ]"""
    provider = FakeProvider(response_text=mixed_json)
    engine = ReflectionEngine(provider=provider, window=30)
    records = [_make_record(id=1)]
    result = engine.reflect(records)
    assert len(result) == 2
    assert result[0].observation == "Pattern A"
    assert result[1].observation == "Pattern C"


# ---------------------------------------------------------------------------
# 6. Top-level JSON parse failure → empty list, no exception
# ---------------------------------------------------------------------------


def test_reflect_returns_empty_on_top_level_parse_failure() -> None:
    """Non-JSON response → reflect returns [], no exception raised."""
    provider = FakeProvider(response_text="not valid json at all")
    engine = ReflectionEngine(provider=provider, window=30)
    result = engine.reflect([_make_record(id=1)])
    assert result == []


# ---------------------------------------------------------------------------
# 7. Non-list JSON → empty list, no exception
# ---------------------------------------------------------------------------


def test_reflect_returns_empty_on_non_list_json() -> None:
    """JSON dict (not array) → reflect returns [], no exception raised."""
    provider = FakeProvider(response_text='{"not": "a list"}')
    engine = ReflectionEngine(provider=provider, window=30)
    result = engine.reflect([_make_record(id=1)])
    assert result == []


# ---------------------------------------------------------------------------
# 8. Caching: second call with same records does not call provider again
# ---------------------------------------------------------------------------


def test_reflect_caches_results() -> None:
    """Second reflect() call with the same records returns cached result."""
    provider = FakeProvider(response_text=SINGLE_INSIGHT_JSON)
    engine = ReflectionEngine(provider=provider, window=30)
    records = [_make_record(id=1), _make_record(id=2)]

    first = engine.reflect(records)
    assert provider.call_count == 1

    second = engine.reflect(records)
    assert provider.call_count == 1  # no additional call
    assert first == second


# ---------------------------------------------------------------------------
# 9. Cache key with all-None ids uses sha256 of empty string
# ---------------------------------------------------------------------------


def test_reflect_cache_key_excludes_none_ids() -> None:
    """Records with id=None → cache key = sha256(''); two calls hit cache."""
    provider = FakeProvider(response_text="[]")
    engine = ReflectionEngine(provider=provider, window=30)

    # Build two different record lists, both with id=None
    records_a = [_make_record(id=None, session_id="a")]
    records_b = [_make_record(id=None, session_id="b")]

    engine.reflect(records_a)
    assert provider.call_count == 1

    # Different records, but same cache key (both have id=None → sha256(''))
    engine.reflect(records_b)
    assert provider.call_count == 1  # cache hit


# ---------------------------------------------------------------------------
# 10. Window trims oldest records
# ---------------------------------------------------------------------------


def test_reflect_window_trims_oldest() -> None:
    """50 records with window=10 → only the 10 most recent reach the provider."""
    provider = CapturingProvider(response_text="[]")
    engine = ReflectionEngine(provider=provider, window=10)

    records = [_make_record(id=i, session_id=f"s{i}") for i in range(50)]
    engine.reflect(records)

    assert provider.last_messages is not None
    content = provider.last_messages[0].content
    # Count trajectory headers — should be 10, not 50
    trajectory_headers = content.count("### Trajectory #")
    assert trajectory_headers == 10

    # Verify the last 10 records (ids 40-49) are present, first are not
    assert "session s49" in content
    assert "session s40" in content
    assert "session s39" not in content
    assert "session s0" not in content


# ---------------------------------------------------------------------------
# 11. Calling reflect() from inside a running event loop raises RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_called_from_running_loop_raises() -> None:
    """reflect() called from inside a running event loop raises RuntimeError."""
    provider = FakeProvider(response_text="[]")
    engine = ReflectionEngine(provider=provider, window=30)

    with pytest.raises(RuntimeError, match="running event loop"):
        engine.reflect([])
