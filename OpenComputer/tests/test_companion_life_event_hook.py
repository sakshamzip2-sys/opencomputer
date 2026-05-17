"""A.3 — Life-Event hook into companion overlay.

When companion is the active persona AND the Life-Event Detector has a
recent unconsumed firing, the firing's hint_text augments the system
prompt as a "RECENT LIFE EVENT" anchor. This gives the reflective lane
a real piece of context to point at when the user asks "how are you?".
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.agent.loop import AgentLoop
from opencomputer.awareness.life_events.pattern import PatternFiring
from opencomputer.awareness.life_events.registry import (
    get_global_registry,
    reset_global_registry_for_test,
)
from opencomputer.awareness.personas.registry import get_persona
from plugin_sdk.runtime_context import RuntimeContext


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test starts with a fresh global registry."""
    reset_global_registry_for_test()
    yield
    reset_global_registry_for_test()


def test_peek_returns_none_on_empty_registry():
    reg = get_global_registry()
    assert reg.peek_most_recent_firing() is None


def test_peek_returns_most_recent_firing():
    reg = get_global_registry()
    older = PatternFiring(
        pattern_id="job_change",
        confidence=0.7,
        evidence_count=3,
        surfacing="hint",
        hint_text="possible job change",
        timestamp=1000.0,
    )
    newer = PatternFiring(
        pattern_id="exam_prep",
        confidence=0.85,
        evidence_count=5,
        surfacing="hint",
        hint_text="3 days of intense studying",
        timestamp=2000.0,
    )
    # Mirror what on_event does for each non-silent firing: queue it AND
    # record it as the most-recent firing.
    for firing in (older, newer):
        reg._queue.append(firing)
        reg._last_firing = firing
    peeked = reg.peek_most_recent_firing()
    assert peeked is not None
    assert peeked.pattern_id == "exam_prep"
    # Peek does NOT drain
    assert len(reg._queue) == 2


def test_drain_still_works_after_peek():
    reg = get_global_registry()
    firing = PatternFiring(
        pattern_id="burnout",
        confidence=0.8,
        evidence_count=4,
        surfacing="hint",
        hint_text="late-night coding",
        timestamp=1500.0,
    )
    reg._queue.append(firing)
    reg._last_firing = firing
    peeked = reg.peek_most_recent_firing()
    assert peeked is not None
    drained = reg.drain_pending()
    assert len(drained) == 1
    assert drained[0].pattern_id == "burnout"
    # The injection provider drains every turn; peek must survive that so
    # the companion overlay still has an anchor for the rest of the turn.
    survivor = reg.peek_most_recent_firing()
    assert survivor is not None
    assert survivor.pattern_id == "burnout"


def test_companion_overlay_skips_anchor_when_no_firing():
    """Empty global registry → companion overlay base form does NOT
    already contain the anchor marker."""
    persona = get_persona("companion")
    assert persona is not None
    overlay = persona["system_prompt_overlay"]
    assert "RECENT LIFE EVENT" not in overlay


def test_loop_builds_overlay_with_anchor_under_companion():
    """End-to-end-ish: AgentLoop._build_persona_overlay augments the
    companion overlay with the most-recent firing when one is queued."""
    reg = get_global_registry()
    firing = PatternFiring(
        pattern_id="exam_prep",
        confidence=0.82,
        evidence_count=4,
        surfacing="hint",
        hint_text="user studying for licensing exam, intensity rising",
        timestamp=1700000000.0,
    )
    reg._queue.append(firing)
    reg._last_firing = firing

    stand_in = MagicMock()
    stand_in._active_persona_id = ""
    # _build_persona_overlay reads self._runtime.custom — give the
    # stand-in a real RuntimeContext (empty custom) so a stray
    # MagicMock attribute doesn't masquerade as a persona override.
    stand_in._runtime = RuntimeContext()
    fake_msg = MagicMock(role="user", content="how are you?", tool_calls=None)
    stand_in.db.get_messages.return_value = [fake_msg]

    overlay = AgentLoop._build_persona_overlay(stand_in, "test-session")

    assert "RECENT LIFE EVENT" in overlay
    assert "exam_prep" in overlay
    assert "licensing exam" in overlay
    assert stand_in._active_persona_id == "companion"


def test_loop_omits_anchor_under_non_companion_personas():
    """A coding-question that routes to coding persona must NOT get the
    life-event anchor (it's companion-only)."""
    reg = get_global_registry()
    firing = PatternFiring(
        pattern_id="exam_prep",
        confidence=0.82,
        evidence_count=4,
        surfacing="hint",
        hint_text="user studying intensely",
        timestamp=1700000000.0,
    )
    reg._queue.append(firing)
    reg._last_firing = firing

    stand_in = MagicMock()
    stand_in._active_persona_id = ""
    stand_in._runtime = RuntimeContext()  # real custom dict — see above
    fake_msg = MagicMock(role="user", content="explain this function", tool_calls=None)
    stand_in.db.get_messages.return_value = [fake_msg]

    # Force coding by simulating a coding-app foreground.
    from unittest.mock import patch

    import opencomputer.awareness.personas._foreground as fg_mod
    with patch.object(fg_mod, "detect_frontmost_app", return_value="cursor"):
        overlay = AgentLoop._build_persona_overlay(stand_in, "test-session")

    assert "RECENT LIFE EVENT" not in overlay
    assert stand_in._active_persona_id == "coding"
