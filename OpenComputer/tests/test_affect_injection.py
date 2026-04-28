"""Unit tests for AffectInjectionProvider (Prompt B).

The provider is a DynamicInjectionProvider that contributes a structured
<user-state> block to the system prompt every turn. Output is bounded by
three signals — current vibe, recent arc, active life-event firing — and
a return-None gate when nothing carries signal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from extensions.affect_injection.provider import AffectInjectionProvider

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext


def _seed(db: SessionDB, session_id: str = "s1") -> None:
    db.create_session(session_id, platform="cli", model="m")
    db.append_message(session_id, Message(role="user", content="hello"))


def _ctx(
    session_id: str,
    *,
    user_msgs: tuple[str, ...] = (),
    turn_index: int = 1,
    agent_context: str = "chat",
) -> InjectionContext:
    msgs = tuple(Message(role="user", content=m) for m in user_msgs)
    return InjectionContext(
        messages=msgs,
        runtime=RuntimeContext(agent_context=agent_context),  # type: ignore[arg-type]
        session_id=session_id,
        turn_index=turn_index,
    )


def _patch_lifeevents_to_none() -> Any:
    """Patch get_global_registry so peek returns None."""
    import opencomputer.awareness.life_events.registry as reg_mod

    class _Stub:
        def peek_most_recent_firing(self) -> Any:
            return None

    return patch.object(reg_mod, "get_global_registry", return_value=_Stub())


# ── provider_id + priority ───────────────────────────────────────────


def test_provider_id_is_affect_injection_v1(tmp_path: Path) -> None:
    p = AffectInjectionProvider(db_path=tmp_path / "s.db")
    assert p.provider_id == "affect-injection:v1"


def test_provider_priority_is_60(tmp_path: Path) -> None:
    p = AffectInjectionProvider(db_path=tmp_path / "s.db")
    assert p.priority == 60


# ── return-None gates ────────────────────────────────────────────────


async def test_returns_none_when_calm_no_arc_no_pattern(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "calm")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)
    with _patch_lifeevents_to_none():
        out = await p.collect(_ctx("s1", user_msgs=("how does this work",)))
    # "how does this work" classifies as 'curious', so... actually that
    # creates an arc from None -> curious. The first turn has prev_turn_vibe
    # = None (no arc), session_vibe = calm, life-event = none. The per-turn
    # vibe IS curious. So output should NOT be None — vibe carries signal.
    # Adjust: re-run but with truly calm content.
    p._prev_turn_vibe.clear()  # reset
    out = await p.collect(_ctx("s1", user_msgs=("ok thanks",)))
    assert out is None


async def test_returns_none_for_cron_context(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "frustrated")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)
    with _patch_lifeevents_to_none():
        out = await p.collect(
            _ctx("s1", user_msgs=("nothing works",), agent_context="cron")
        )
    assert out is None


async def test_silent_for_first_turns_until_min(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "frustrated")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=2)
    with _patch_lifeevents_to_none():
        out_t1 = await p.collect(
            _ctx("s1", user_msgs=("nothing works",), turn_index=1)
        )
        out_t2 = await p.collect(
            _ctx("s1", user_msgs=("still broken",), turn_index=2)
        )
    assert out_t1 is None, "turn 1 should be silent under min_turns=2"
    assert out_t2 is not None, "turn 2 should activate (>= min_turns)"


# ── populated block ──────────────────────────────────────────────────


async def test_block_when_vibe_transitions_stuck_to_excited(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "stuck")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)

    with _patch_lifeevents_to_none():
        # Turn 1: classify as stuck.
        await p.collect(_ctx("s1", user_msgs=("i'm stuck on this",)))
        # Turn 2: classify as excited (multi-! triggers).
        out = await p.collect(_ctx("s1", user_msgs=("got it works!!!",)))

    assert out is not None
    assert "<user-state>" in out
    assert "</user-state>" in out
    assert "vibe: excited" in out
    assert "recent_arc: stuck -> excited" in out


async def test_block_includes_active_pattern_when_hint_surfacing(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "tired")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)

    import opencomputer.awareness.life_events.registry as reg_mod
    from opencomputer.awareness.life_events.pattern import PatternFiring

    class _Stub:
        def peek_most_recent_firing(self) -> PatternFiring | None:
            return PatternFiring(
                pattern_id="burnout",
                confidence=0.85,
                evidence_count=4,
                surfacing="hint",
                hint_text="possible burnout",
            )

    with patch.object(reg_mod, "get_global_registry", return_value=_Stub()):
        out = await p.collect(_ctx("s1", user_msgs=("i'm exhausted",)))

    assert out is not None
    assert "active_pattern: burnout" in out


async def test_block_omits_active_pattern_when_silent_surfacing(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "tired")
    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)

    import opencomputer.awareness.life_events.registry as reg_mod
    from opencomputer.awareness.life_events.pattern import PatternFiring

    class _Stub:
        def peek_most_recent_firing(self) -> PatternFiring | None:
            return PatternFiring(
                pattern_id="health_event",
                confidence=0.9,
                evidence_count=3,
                surfacing="silent",
                hint_text="",
            )

    with patch.object(reg_mod, "get_global_registry", return_value=_Stub()):
        out = await p.collect(_ctx("s1", user_msgs=("i'm exhausted",)))

    # Silent firings must not appear in the user-state block.
    assert out is None or "active_pattern" not in out


async def test_integration_compose_includes_user_state_tag(
    tmp_path: Path,
) -> None:
    """End-to-end: register the provider in a fresh InjectionEngine and
    confirm a synthetic turn's composed system-prompt fragment contains
    the ``<user-state>`` tag with a vibe label.
    """
    from opencomputer.agent.injection import InjectionEngine

    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "frustrated")

    engine = InjectionEngine()
    engine.register(
        AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)
    )

    with _patch_lifeevents_to_none():
        out = await engine.compose(
            _ctx("s1", user_msgs=("nothing works", "still broken"))
        )

    assert "<user-state>" in out
    assert "</user-state>" in out
    assert "vibe: frustrated" in out


async def test_does_not_mutate_db(tmp_path: Path) -> None:
    """Read-only contract: provider must not alter sessions.db."""
    db = SessionDB(tmp_path / "s.db")
    _seed(db)
    db.set_session_vibe("s1", "frustrated")

    # Snapshot all rows.
    import sqlite3

    with sqlite3.connect(tmp_path / "s.db") as conn:
        before_sessions = list(conn.execute("SELECT * FROM sessions"))
        before_vibe_log = list(conn.execute("SELECT * FROM vibe_log"))

    p = AffectInjectionProvider(db_path=tmp_path / "s.db", min_turns=0)
    with _patch_lifeevents_to_none():
        await p.collect(_ctx("s1", user_msgs=("nothing works",)))
        await p.collect(_ctx("s1", user_msgs=("still broken",)))

    with sqlite3.connect(tmp_path / "s.db") as conn:
        after_sessions = list(conn.execute("SELECT * FROM sessions"))
        after_vibe_log = list(conn.execute("SELECT * FROM vibe_log"))

    assert before_sessions == after_sessions
    assert before_vibe_log == after_vibe_log
