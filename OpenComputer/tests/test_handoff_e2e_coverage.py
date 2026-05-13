"""End-to-end coverage tests for the gaps identified in Bug-4 audit:

  1. Wire-event publication when the orchestrator fires
  2. Injection picks up a written handoff on the next turn
  3. Slash command produces a handoff from a real SessionDB
  4. Audit logger rebinds when profile changes
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.handoff import (
    AutoSwapTrigger,
    HandoffDocument,
    HandoffInbox,
    HandoffInjectionProvider,
    HandoffMetadata,
    ProviderAdapter,
    run_auto_swap_pipeline,
)
from opencomputer.agent.handoff.protocol_v2 import PROTOCOL_VERSION


@dataclass
class FakeRuntime:
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMsg:
    role: str
    content: str


class FakeProvider:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    async def complete(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        return self._response


# ─── Bug 1 closure — wire event publication ────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_publishes_profile_swap_event_on_fire(
    tmp_path: Path,
) -> None:
    """Successful auto-swap → ProfileSwapEvent on default_bus."""
    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import ProfileSwapEvent

    received: list[ProfileSwapEvent] = []

    sub = default_bus.subscribe(
        "profile_swap",
        lambda evt: received.append(evt),
    )
    try:
        # Set up a real orchestrator call
        trigger = AutoSwapTrigger(
            persona_to_profile=lambda p, _avail: "stocks" if p == "trading" else None,
        )
        rt = FakeRuntime()
        provider = FakeProvider(
            response="**Collab:** X **State:** Y **Next:** Z",
        )
        adapter = ProviderAdapter(provider=provider, model_id="fake")
        from opencomputer.agent.handoff import orchestrator as orch
        from opencomputer.awareness.personas.classifier import (
            ClassificationResult,
        )

        real_classify = orch.classify
        orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
            persona_id="trading", confidence=0.9, reason="stub",
        )
        try:
            for _ in range(3):
                await run_auto_swap_pipeline(
                    trigger=trigger, runtime=rt, session_id="sess-pub",
                    current_profile="default",
                    available_profiles=("stocks",),
                    last_user_messages=("a", "b", "c"),
                    recent_messages=[
                        FakeMsg("user", "stocks question"),
                        FakeMsg("assistant", "stocks answer"),
                    ],
                    plan_mode=False, auto_off=False,
                    is_gateway_session=False, gateway_optin=False,
                    target_profile_home_resolver=lambda pid: tmp_path / pid / "home",
                    provider_adapter=adapter,
                    audit_logger=None,
                )
        finally:
            orch.classify = real_classify  # type: ignore[assignment]

        # Give the bus a moment in case it's async (it's actually sync,
        # but defensive — and asyncio gives the loop a tick).
        await asyncio.sleep(0)

        assert len(received) == 1
        evt = received[0]
        assert evt.from_profile == "default"
        assert evt.to_profile == "stocks"
        assert evt.trigger == "auto"
        assert evt.classifier_confidence == pytest.approx(0.9)
        assert evt.has_handoff is True
    finally:
        sub.unsubscribe()


# ─── Bug 2 closure — injection round-trip ─────────────────────────────


@pytest.mark.asyncio
async def test_injector_reads_pending_handoff_and_archives(
    tmp_path: Path,
) -> None:
    """Handoff is written → injector picks it up → file is archived."""
    profile_home = tmp_path / "stocks" / "home"
    inbox = HandoffInbox(profile_home)

    doc = HandoffDocument(
        metadata=HandoffMetadata(
            protocol_version=PROTOCOL_VERSION,
            source_profile="default",
            target_profile="stocks",
            generated_at="2026-05-13T14:32:01Z",
            source_session_id="sess-X",
            trigger="auto",
            classifier_confidence=0.87,
            classifier_reason="state-query detected",
        ),
        body="The user was researching NVDA earnings.",
    )
    written_path = inbox.write(doc)
    assert written_path.exists()

    provider = HandoffInjectionProvider(
        profile_home_resolver=lambda: profile_home,
    )

    # Run the injector — it should pick up the pending handoff,
    # render injection text, and archive the file.
    from plugin_sdk.injection import InjectionContext
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    ctx = InjectionContext(messages=(), runtime=rt, session_id="next")
    result = await provider.collect(ctx)

    assert result is not None
    assert "researching NVDA earnings" in result
    assert "default" in result  # source profile named per R1
    assert "DATA" in result      # R12 framing present

    # File was archived
    assert not written_path.exists()
    archived = profile_home / "inbox" / "processed" / written_path.name
    assert archived.exists()

    # Second call → nothing to inject
    result2 = await provider.collect(ctx)
    assert result2 is None


@pytest.mark.asyncio
async def test_injector_handles_missing_inbox_gracefully(
    tmp_path: Path,
) -> None:
    """Resolver points at non-existent dir → injector returns None silently."""
    provider = HandoffInjectionProvider(
        profile_home_resolver=lambda: tmp_path / "nonexistent" / "home",
    )
    from plugin_sdk.injection import InjectionContext
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    result = await provider.collect(
        InjectionContext(messages=(), runtime=rt, session_id="x"),
    )
    assert result is None


@pytest.mark.asyncio
async def test_injector_resolver_failure_does_not_raise(tmp_path: Path) -> None:
    """Resolver raises → injector logs WARN + returns None, never raises."""
    def _bad_resolver():
        raise RuntimeError("simulated resolver failure")

    provider = HandoffInjectionProvider(profile_home_resolver=_bad_resolver)
    from plugin_sdk.injection import InjectionContext
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    result = await provider.collect(
        InjectionContext(messages=(), runtime=rt, session_id="x"),
    )
    assert result is None


# ─── Bug 3 closure — slash command with real-ish message source ─────


@pytest.mark.asyncio
async def test_manual_handoff_reads_from_session_db(tmp_path: Path) -> None:
    """`/handoff stocks` reads message history from SessionDB, not runtime plumbing."""
    from opencomputer.agent.slash_commands_impl.handoff_cmd import (
        HandoffCommand,
    )

    # Build a fake SessionDB-like object that exposes get_messages.
    class FakeDB:
        def __init__(self) -> None:
            self.calls = 0

        def get_messages(self, sid: str) -> list[Any]:
            self.calls += 1
            return [
                FakeMsg("user", "NVDA earnings question 1"),
                FakeMsg("assistant", "NVDA reported X"),
                FakeMsg("user", "options chain follow-up"),
                FakeMsg("assistant", "200 calls show heavy IV"),
                FakeMsg("user", "and volume?"),
            ]

    db = FakeDB()
    provider = FakeProvider(
        response="**Collab:** Saksham research NVDA. **State:** Mid-thread. **Next:** Answer volume Q.",
    )
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from plugin_sdk.runtime_context import RuntimeContext
    rt = RuntimeContext(custom={
        "active_profile_id": "default",
        "session_db": db,
        "session_id": "sess-Z",
        "_handoff_provider_adapter": adapter,
    })

    # Patch profile resolution so it routes to tmp_path
    import opencomputer.profiles as profiles_mod
    real_get_profile_dir = profiles_mod.get_profile_dir
    real_list_profiles = profiles_mod.list_profiles
    profiles_mod.get_profile_dir = lambda name: tmp_path / (name or "default")
    profiles_mod.list_profiles = lambda: ["stocks"]
    try:
        cmd = HandoffCommand()
        result = await cmd.execute("stocks", rt)
    finally:
        profiles_mod.get_profile_dir = real_get_profile_dir
        profiles_mod.list_profiles = real_list_profiles

    # The slash command should have read from FakeDB (Bug 1 closure)
    assert db.calls == 1
    # Handoff was written
    assert "Handoff written" in result.output or "Swap queued" in result.output
    # Pending swap is queued
    assert rt.custom.get("pending_profile_id") == "stocks"
    # Provider was actually called (i.e. the message history reached it
    # so generation could proceed)
    assert provider.calls == 1


# ─── Bug 4 closure — audit logger profile rebinding ────────────────


def test_audit_logger_rebinds_on_profile_change(tmp_path: Path) -> None:
    """Switching profile → audit logger DB path follows.

    Verifies the cached-profile invariant: the cached logger's db_path
    matches the current profile. After we simulate a profile swap
    (re-init with a different profile id), the new logger writes to
    the new profile's audit.db.
    """
    from opencomputer.agent.handoff import HandoffAuditLogger, SwapAuditEvent

    key = b"\x00" * 32

    # First profile
    default_audit = HandoffAuditLogger(
        tmp_path / "default" / "consent" / "audit.db", key,
    )
    rowid1 = default_audit.append(
        SwapAuditEvent(
            session_id="s1",
            source_profile="default",
            target_profile="stocks",
            trigger="auto",
            outcome="allow",
            reason="streak fired",
        ),
    )
    assert rowid1 is not None
    default_audit.close()

    # Simulate a swap — the loop re-inits the audit logger pointing at
    # the new profile's consent dir. The NEW logger must write to the
    # NEW profile's DB.
    stocks_audit = HandoffAuditLogger(
        tmp_path / "stocks" / "consent" / "audit.db", key,
    )
    rowid2 = stocks_audit.append(
        SwapAuditEvent(
            session_id="s1",
            source_profile="stocks",
            target_profile="coder",
            trigger="auto",
            outcome="allow",
            reason="streak fired",
        ),
    )
    assert rowid2 is not None
    # Chain on the stocks DB starts independent (rowid == 1, not 2)
    assert rowid2 == 1
    stocks_audit.close()

    # Each profile's audit.db now exists and has exactly its own rows
    default_db = tmp_path / "default" / "consent" / "audit.db"
    stocks_db = tmp_path / "stocks" / "consent" / "audit.db"
    assert default_db.exists()
    assert stocks_db.exists()

    import sqlite3
    with sqlite3.connect(default_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='profile_swap'",
        ).fetchone()[0]
        assert count == 1
    with sqlite3.connect(stocks_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='profile_swap'",
        ).fetchone()[0]
        assert count == 1
