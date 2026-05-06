"""End-to-end-ish test: AgentLoop._run_one_step records into llm_calls.

Catches the gap between unit tests of usage_pricing (which mock the DB)
and unit tests of error_classifier (which never touch a loop). Without
this, a future refactor that drops the call from loop.py wouldn't
trigger any test failure.

Uses a minimal stub provider so we don't hit a network or API key. The
provider returns a fixed ``Usage(input_tokens=100, output_tokens=50)``;
the test asserts a row appears in ``llm_calls`` with those numbers and
a non-empty provider name (catches the ``getattr(self.provider, 'name', '')``
fallback risk explicitly).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    Usage,
)


class _RecordingProvider(BaseProvider):
    """Minimal provider that returns a fixed response with usage."""

    name = "stub-provider"
    default_model = "stub-model-1"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(
            message=Message(role="assistant", content="hello back"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=50),
        )

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


@pytest.mark.asyncio
async def test_run_one_step_records_to_llm_calls(tmp_path: Path) -> None:
    """After a real `_run_one_step`, llm_calls has a row with our tokens."""
    db_path = tmp_path / "rt.db"
    db = SessionDB(db_path)

    cfg = Config(
        model=ModelConfig(model="stub-model-1", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(),
    )
    provider = _RecordingProvider()
    loop = AgentLoop(config=cfg, provider=provider, db=db)

    db.ensure_session("rt-session", platform="cli", model="stub-model-1")
    loop._current_session_id = "rt-session"  # noqa: SLF001

    # Drive _run_one_step directly with one user message.
    out = await loop._run_one_step(  # noqa: SLF001
        messages=[Message(role="user", content="hi")],
        system="you are a stub",
        session_id="rt-session",
    )
    assert out.input_tokens == 100
    assert out.output_tokens == 50

    # The row should have appeared.
    rows = db.query_llm_calls(days=None, group_by="model")
    assert len(rows) == 1
    r = rows[0]
    assert r["key"] == "stub-model-1"
    assert r["calls"] == 1
    assert r["input_tokens"] == 100
    assert r["output_tokens"] == 50

    # Critically — provider name must NOT be empty (catches getattr fallback risk).
    rows_by_provider = db.query_llm_calls(days=None, group_by="provider")
    assert len(rows_by_provider) == 1
    assert rows_by_provider[0]["key"] == "stub-provider"


@pytest.mark.asyncio
async def test_provider_name_fallback_when_attr_missing(tmp_path: Path) -> None:
    """Provider w/o a ``name`` attr — ``type(...).__name__.lower()`` fallback fires."""

    class _NoNameProvider(BaseProvider):
        # Deliberately override the BaseProvider's name=None default to None — the
        # loop's fallback should derive 'noname' from the class name.
        name = None  # type: ignore[assignment]
        default_model = "x"

        async def complete(self, **kwargs: Any) -> ProviderResponse:
            return ProviderResponse(
                message=Message(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=10, output_tokens=5),
            )

        async def stream_complete(self, **kwargs: Any):
            if False:
                yield

        async def count_tokens(self, **kwargs: Any) -> int:
            return 0

    db_path = tmp_path / "rt2.db"
    db = SessionDB(db_path)
    cfg = Config(
        model=ModelConfig(model="x", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(),
    )
    provider = _NoNameProvider()
    loop = AgentLoop(config=cfg, provider=provider, db=db)
    db.ensure_session("s2", platform="cli", model="x")
    loop._current_session_id = "s2"  # noqa: SLF001

    await loop._run_one_step(  # noqa: SLF001
        messages=[Message(role="user", content="hi")],
        system="",
        session_id="s2",
    )

    rows = db.query_llm_calls(days=None, group_by="provider")
    assert len(rows) == 1
    # Fallback path: lowercased class name minus 'provider' suffix.
    assert rows[0]["key"]  # non-empty
    assert "noname" in rows[0]["key"].lower()
