"""M1 / T1.9 — the dispatcher actually emits parity telemetry.

Proves the instrumentation wired into ``Dispatch.__do_dispatch_inner``
flushes a 10-row ``gateway_parity_log`` record per turn, and that the
per-mechanism ``fired`` flags reflect the turn's real conditions.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.config import (
    RoutingConfig,
    RoutingMatch,
    RoutingRule,
)
from opencomputer.agent.state import SessionDB
from opencomputer.gateway.agent_router import AgentRouter
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.parity_probe import query_parity_log
from plugin_sdk.core import MessageEvent, Platform


def _event() -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hi",
        timestamp=0.0,
        attachments=[],
        metadata={},
    )


def _fake_loop(tmp_path: Path, *, routing: RoutingConfig | None = None) -> MagicMock:
    """A MagicMock loop with the real attributes the probe reads."""
    loop = MagicMock()
    loop.db = SessionDB(tmp_path / "sessions.db")
    loop.config.home = tmp_path
    loop.config.routing = routing
    loop.config.custom_providers = ()
    loop.config.model_context_overrides = None
    loop.allowed_tools = None  # CLI-parity: no tool allowlist

    async def fake_run(user_message: str, session_id: str, **kw):
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        result.input_tokens = 0
        return result

    loop.run_conversation = fake_run
    return loop


@pytest.mark.asyncio
async def test_dispatch_emits_ten_rows_per_turn(tmp_path: Path) -> None:
    loop = _fake_loop(tmp_path)
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    out = await Dispatch(router=router).handle_message(_event())
    assert out == "ok"

    rows = query_parity_log(tmp_path / "audit.db")
    assert len(rows) == 10  # exactly one turn × ten mechanisms
    assert {r["platform"] for r in rows} == {"telegram"}


@pytest.mark.asyncio
async def test_structural_mechanisms_fire_without_routing(tmp_path: Path) -> None:
    """No routing, no allowlist → conditional mechanisms OFF, structural ON."""
    loop = _fake_loop(tmp_path)
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    await Dispatch(router=router).handle_message(_event())

    fired = {
        r["mechanism_id"]: r["fired"]
        for r in query_parity_log(tmp_path / "audit.db")
    }
    # Conditional mechanisms — should NOT fire on a plain default turn.
    assert fired["prompt_override"] is False
    assert fired["tool_allowlist"] is False
    assert fired["profile_rebind"] is False
    assert fired["routing_decision_invisible"] is False
    # #5 — M3 made this per-turn, not structural: no gated tool was
    # invoked, so no async-consent round-trip happened → does not fire.
    assert fired["no_interactive_consent"] is False
    # #7 — the persona casual register still fires (no override set).
    assert fired["persona_casual_register"] is True
    assert fired["runtime_footer_off"] is True  # footer off by default


@pytest.mark.asyncio
async def test_tool_allowlist_fires_when_loop_has_one(tmp_path: Path) -> None:
    loop = _fake_loop(tmp_path)
    loop.allowed_tools = frozenset({"Read", "Edit"})  # gateway-style allowlist
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    await Dispatch(router=router).handle_message(_event())

    rows = query_parity_log(tmp_path / "audit.db")
    ta = next(r for r in rows if r["mechanism_id"] == "tool_allowlist")
    assert ta["fired"] is True
    assert ta["detail"]["tool_count"] == 2


@pytest.mark.asyncio
async def test_telemetry_flushes_on_the_error_path(tmp_path: Path) -> None:
    """A turn that raises inside run_conversation still emits 10 rows.

    The probe flushes in dispatch's ``finally`` block, so a failed turn
    is recorded too (mostly fired=0) — telemetry never loses a turn.
    """
    loop = _fake_loop(tmp_path)

    async def boom(user_message: str, session_id: str, **kw):
        raise RuntimeError("provider exploded")

    loop.run_conversation = boom
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    # handle_message swallows the error into a user-facing string.
    out = await Dispatch(router=router).handle_message(_event())
    assert out is not None  # a friendly error message, not a crash

    rows = query_parity_log(tmp_path / "audit.db")
    assert len(rows) == 10  # the failed turn still emitted a full record


@pytest.mark.asyncio
async def test_compaction_mechanism_uses_db_delta_not_shared_runtime(
    tmp_path: Path,
) -> None:
    """#10 fires iff sessions.compactions_count rose during the turn.

    Regression for the M1 bug: _build_channel_runtime returns the shared
    DEFAULT_RUNTIME_CONTEXT, so probing runtime.custom over-reported.
    Turn 1 bumps the counter (fires); turn 2 does not (must NOT fire,
    even though the shared runtime may still carry stale state).
    """
    loop = _fake_loop(tmp_path)
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    dispatch = Dispatch(router=router)

    bump = {"do": True}

    async def fake_run(user_message: str, session_id: str, **kw):
        # Real run_conversation creates the session row; the fake must
        # too, so increment_compaction_count has a row to bump.
        loop.db.ensure_session(session_id, platform="telegram")
        if bump["do"]:
            loop.db.increment_compaction_count(session_id)
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        result.input_tokens = 0
        return result

    loop.run_conversation = fake_run

    # Turn 1 — compaction happens.
    await dispatch.handle_message(_event())
    # Turn 2 — no compaction.
    bump["do"] = False
    await dispatch.handle_message(_event())

    rows = query_parity_log(tmp_path / "audit.db")
    comp = sorted(
        (r for r in rows if r["mechanism_id"] == "compaction_long_session"),
        key=lambda r: r["turn_id"],
    )
    assert [r["fired"] for r in comp] == [True, False]


@pytest.mark.asyncio
async def test_prompt_override_fires_with_routing(
    tmp_path: Path, monkeypatch
) -> None:
    """A matching routing rule supplying a system prompt fires #1 + #8."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram", chat_id="123"),
                agent="stocks",
            ),
        ),
    )
    loop = _fake_loop(tmp_path, routing=routing)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {
            "stocks": SimpleNamespace(
                name="stocks", system_prompt="You are a stock bot."
            )
        },
    )
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    await Dispatch(router=router).handle_message(_event())

    fired = {
        r["mechanism_id"]: r
        for r in query_parity_log(tmp_path / "audit.db")
    }
    assert fired["prompt_override"]["fired"] is True
    assert fired["prompt_override"]["detail"]["template"] == "stocks"
    # M3 #8 fix — the routing badge surfaces the decision on the first
    # turn, so routing_decision_invisible reads False (gap closed) and
    # the detail records that the badge was shown.
    assert fired["routing_decision_invisible"]["fired"] is False
    assert fired["routing_decision_invisible"]["detail"]["badge_shown"] is True


@pytest.mark.asyncio
async def test_routing_badge_shows_once_per_session(
    tmp_path: Path, monkeypatch
) -> None:
    """M3 #6/#8 — the routing badge is appended to the FIRST routed
    reply of a session, then suppressed on later turns."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram", chat_id="123"),
                agent="stocks",
            ),
        ),
    )
    loop = _fake_loop(tmp_path, routing=routing)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {
            "stocks": SimpleNamespace(name="stocks", system_prompt="stock bot")
        },
    )
    dispatch = Dispatch(
        router=AgentRouter(
            loop_factory=lambda pid, home: loop,
            profile_home_resolver=lambda pid: tmp_path / pid,
        )
    )
    first = await dispatch.handle_message(_event())
    second = await dispatch.handle_message(_event())

    assert "↪ routed: agent=stocks" in (first or "")
    assert "↪ routed:" not in (second or "")  # suppressed on turn 2


@pytest.mark.asyncio
async def test_persona_override_closes_mechanism_7(tmp_path: Path) -> None:
    """M3 #7 — display.persona_override threads onto the runtime and
    flips persona_casual_register off (gap closed)."""
    loop = _fake_loop(tmp_path)
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    dispatch = Dispatch(
        router=router, config={"display": {"persona_override": "none"}}
    )
    await dispatch.handle_message(_event())

    rows = query_parity_log(tmp_path / "audit.db")
    persona = next(
        r for r in rows if r["mechanism_id"] == "persona_casual_register"
    )
    assert persona["fired"] is False
    assert persona["detail"]["persona_override"] is True


@pytest.mark.asyncio
async def test_consent_mechanism_5_fires_only_on_roundtrip(
    tmp_path: Path,
) -> None:
    """M3 #5 — no_interactive_consent fires only when a consent prompt
    was actually sent this turn, not structurally every turn."""
    loop = _fake_loop(tmp_path)
    router = AgentRouter(
        loop_factory=lambda pid, home: loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    dispatch = Dispatch(router=router)

    sid = {"v": ""}

    async def fake_run(user_message: str, session_id: str, **kw):
        # Simulate the consent gate sending an approval prompt mid-turn.
        sid["v"] = session_id
        dispatch._consent_prompt_counts[session_id] = (
            dispatch._consent_prompt_counts.get(session_id, 0) + 1
        )
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        result.input_tokens = 0
        return result

    loop.run_conversation = fake_run
    await dispatch.handle_message(_event())

    rows = query_parity_log(tmp_path / "audit.db")
    consent = next(
        r for r in rows if r["mechanism_id"] == "no_interactive_consent"
    )
    assert consent["fired"] is True  # a prompt was sent → fired
    assert consent["detail"]["prompts"] == 1
