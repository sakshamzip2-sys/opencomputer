"""tests/test_skill_evolution_subscriber.py"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.skill_evolution.subscriber import EvolutionSubscriber

from plugin_sdk.ingestion import SessionEndEvent


def _enabled_state(tmp_path: Path) -> None:
    """Create state.json with enabled=true."""
    p = tmp_path / "skills" / "evolution_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"enabled": True}))


@pytest.mark.asyncio
async def test_subscriber_subscribes_on_start():
    bus = MagicMock()
    bus.subscribe = MagicMock(return_value=MagicMock())
    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: Path("/tmp"),
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    sub.start()
    bus.subscribe.assert_called_once()
    args, _kwargs = bus.subscribe.call_args
    # The first positional arg should be "session_end" event_type
    assert args[0] == "session_end"


@pytest.mark.asyncio
async def test_subscriber_noops_when_disabled(tmp_path):
    bus = MagicMock()
    bus.subscribe = MagicMock(return_value=MagicMock())
    db = MagicMock()
    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: db,
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    # State file missing → disabled
    await sub._handle_event(SessionEndEvent(turn_count=10))
    db.get_session.assert_not_called()  # no work


@pytest.mark.asyncio
async def test_subscriber_writes_heartbeat_on_event(tmp_path):
    _enabled_state(tmp_path)
    bus = MagicMock()
    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    await sub._handle_event(SessionEndEvent(turn_count=10))
    hb = tmp_path / "skills" / "evolution_heartbeat"
    assert hb.exists()
    ts = float(hb.read_text())
    assert ts > 0


@pytest.mark.asyncio
async def test_subscriber_runs_pipeline_on_passing_session(tmp_path, monkeypatch):
    _enabled_state(tmp_path)
    bus = MagicMock()

    fake_score = MagicMock(
        is_candidate=True, session_id="sess1", turn_count=10, summary_hint="x"
    )
    fake_judge = MagicMock(confidence=85, is_novel=True, reason="x")
    from extensions.skill_evolution.skill_extractor import ProposedSkill

    fake_proposal = ProposedSkill(
        name="auto-test",
        description="d",
        body="---\nname: auto-test\ndescription: d\n---\n\n# T",
        provenance={
            "session_id": "sess1",
            "generated_at": time.time(),
            "confidence_score": 85,
        },
    )

    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.is_candidate_session",
        lambda *a, **kw: fake_score,
    )
    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.judge_candidate_async",
        AsyncMock(return_value=fake_judge),
    )
    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.extract_skill_from_session",
        AsyncMock(return_value=fake_proposal),
    )
    add_called = [False]
    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.add_candidate",
        lambda profile_home, proposal: (add_called.__setitem__(0, True), tmp_path)[1],
    )

    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    await sub._run_pipeline(SessionEndEvent(turn_count=10, end_reason="completed"))
    assert add_called[0] is True


@pytest.mark.asyncio
async def test_subscriber_skips_below_confidence_threshold(tmp_path, monkeypatch):
    _enabled_state(tmp_path)
    bus = MagicMock()

    fake_score = MagicMock(
        is_candidate=True, session_id="sess1", turn_count=10, summary_hint="x"
    )
    fake_judge = MagicMock(confidence=40, is_novel=True, reason="too generic")

    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.is_candidate_session",
        lambda *a, **kw: fake_score,
    )
    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.judge_candidate_async",
        AsyncMock(return_value=fake_judge),
    )
    extract_called = [False]

    async def _fake_extract(*_args, **_kwargs):
        extract_called[0] = True
        return None

    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.extract_skill_from_session",
        _fake_extract,
    )

    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
        confidence_threshold=70,
    )
    await sub._run_pipeline(SessionEndEvent(turn_count=10, end_reason="completed"))
    assert extract_called[0] is False  # extractor never called


@pytest.mark.asyncio
async def test_subscriber_swallows_pipeline_exception(tmp_path, monkeypatch, caplog):
    _enabled_state(tmp_path)
    bus = MagicMock()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "extensions.skill_evolution.subscriber.is_candidate_session", _raise
    )

    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    # Should NOT raise
    with caplog.at_level(logging.WARNING):
        await sub._run_pipeline(SessionEndEvent(turn_count=10))
    # But should log the error
    assert any(
        "boom" in r.message or "boom" in str(r) for r in caplog.records
    )


@pytest.mark.asyncio
async def test_subscriber_stop_unsubscribes():
    bus = MagicMock()
    sub_handle = MagicMock()
    bus.subscribe = MagicMock(return_value=sub_handle)
    sub = EvolutionSubscriber(
        bus=bus,
        profile_home_factory=lambda: Path("/tmp"),
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
    )
    sub.start()
    sub.stop()
    sub_handle.unsubscribe.assert_called_once()
