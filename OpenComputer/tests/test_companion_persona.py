"""Tests for Path A.1 + A.2: companion persona + base prompt softening.

Path A.1 — `companion.yaml` shipped + auto-classifier wires it as the
default register for state-query / no-strong-signal contexts.

Path A.2 — `base.j2` Jinja conditional drops the "no filler / no
hedging / not a chat toy" rules under the companion persona so the
overlay's warm-but-honest register can land.
"""
from __future__ import annotations

from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.awareness.personas.classifier import (
    ClassificationContext,
    classify,
    is_state_query,
)
from opencomputer.awareness.personas.registry import get_persona

# ── Path A.1: companion persona registered + reachable ────────────────


def test_companion_persona_is_registered():
    persona = get_persona("companion")
    assert persona is not None
    assert persona["id"] == "companion"
    assert persona["name"] == "Companion"


def test_companion_persona_overlay_references_voice_specs():
    persona = get_persona("companion")
    overlay = persona.get("system_prompt_overlay", "")
    # The overlay must point future-Claude at both reference docs so it
    # can re-derive the principles when needed.
    assert "voice-examples.md" in overlay
    assert "voice-mechanism" in overlay


def test_companion_persona_includes_failure_modes():
    persona = get_persona("companion")
    overlay = persona["system_prompt_overlay"]
    # The overlay must explicitly call out both overclaiming AND
    # anti-overclaim ("As an AI…") so the LLM doesn't drift into either.
    assert "Overclaim" in overlay
    assert "Anti-overclaim" in overlay
    # The exemplar response patterns must be present so the LLM has
    # concrete shapes to imitate.
    assert "I notice" in overlay
    assert "anchor" in overlay.lower()


# ── Path A.1: state-query detector ────────────────────────────────────


def test_state_query_recognized():
    assert is_state_query("how are you?") is True
    assert is_state_query("how are you feeling?") is True
    assert is_state_query("hey") is True
    assert is_state_query("hi") is True
    assert is_state_query("hello") is True
    assert is_state_query("what's up") is True
    assert is_state_query("sup") is True
    assert is_state_query("Good morning") is True
    assert is_state_query("you doing ok") is True


def test_non_state_query_rejected():
    assert is_state_query("how do I install pytest") is False
    assert is_state_query("write a function") is False
    assert is_state_query("") is False
    assert is_state_query("explain how are you supposed to handle errors") is False


# ── Path A.1: classifier picks companion appropriately ────────────────


def test_classify_state_query_in_coding_app_yields_companion():
    """User in VS Code asking 'how are you?' should get companion, not
    coding — the actual message is social, not coding."""
    r = classify(
        ClassificationContext(
            foreground_app="cursor",
            last_messages=("how are you?",),
        )
    )
    assert r.persona_id == "companion"


def test_classify_coding_question_in_coding_app_yields_coding():
    """A genuine coding question must still route to coding."""
    r = classify(
        ClassificationContext(
            foreground_app="cursor",
            last_messages=("explain this function",),
        )
    )
    assert r.persona_id == "coding"


def test_classify_default_fallback_is_companion():
    """No strong signal, no state-query, no time-of-day match —
    should land on companion (was admin before Path A.1)."""
    r = classify(ClassificationContext(time_of_day_hour=14))
    assert r.persona_id == "companion"


def test_classify_explicit_app_signals_still_win():
    """Explicit context signals (trading, relaxed) must still beat
    companion, even when there's a state-query."""
    r_trading = classify(
        ClassificationContext(
            foreground_app="zerodha", last_messages=("how are you?",)
        )
    )
    assert r_trading.persona_id == "trading"

    r_relaxed = classify(
        ClassificationContext(
            foreground_app="netflix", last_messages=("hi",)
        )
    )
    assert r_relaxed.persona_id == "relaxed"


# ── Path A.2: base prompt softens under companion ─────────────────────


def test_base_prompt_strict_rules_retained_under_coding():
    b = PromptBuilder()
    out = b.build(active_persona_id="coding")
    assert "not a chat toy" in out
    assert "Avoid filler" in out
    assert "Avoid hedging language" in out


def test_base_prompt_strict_rules_retained_under_admin():
    b = PromptBuilder()
    out = b.build(active_persona_id="admin")
    assert "not a chat toy" in out
    assert "Avoid filler" in out


def test_base_prompt_strict_rules_dropped_under_companion():
    b = PromptBuilder()
    out = b.build(active_persona_id="companion")
    assert "not a chat toy" not in out
    assert "Avoid filler" not in out
    assert "Avoid hedging language" not in out
    # And the soft replacements are present
    assert "Be present and natural" in out
    assert "warmth is welcome" in out


def test_base_prompt_default_falls_back_to_strict():
    """Empty active_persona_id (legacy callers, no classifier) keeps
    the original strict rules so nothing regresses."""
    b = PromptBuilder()
    out = b.build()
    assert "not a chat toy" in out
    assert "Avoid filler" in out


# ── 2026-04-28: regression — companion overlay must not be neutered
# by the "do NOT override working rules" line.
#
# The original wording told the model the persona is a tone overlay
# subordinate to working rules. The model interpreted that to mean
# "Be concise / 1-4 sentences / skip preamble" wins over the
# companion-warm register, producing answers like "No feelings here
# — just an agent waiting for a task" — exactly the anti-overclaim
# failure mode the overlay was written to prevent.


def test_companion_persona_section_explicitly_permits_tone_override():
    b = PromptBuilder()
    out = b.build(active_persona_id="companion", persona_overlay="x")
    # The persona MAY override task-mode tone preferences
    assert "MAY override" in out
    # But MUST NOT override security/consent
    assert "MUST NOT override" in out
    assert "security or consent" in out


def test_companion_drops_be_concise_rule():
    """Working rule #2 'Be concise' is the dominant pull toward flat
    one-sentence answers. Under companion, it must not appear."""
    b = PromptBuilder()
    out = b.build(active_persona_id="companion")
    assert "1-4 sentences for a routine answer" not in out
    assert "Be present, not padded" in out


def test_non_companion_keeps_be_concise_rule():
    """Coding / admin / default modes keep the conciseness pressure."""
    b = PromptBuilder()
    out = b.build(active_persona_id="coding")
    assert "1-4 sentences for a routine answer" in out
    assert "Be present, not padded" not in out


def test_companion_intro_explicitly_calls_out_anti_overclaim():
    """The companion-only intro must literally name the failure modes
    so the model can't drift into them under simple greetings."""
    b = PromptBuilder()
    out = b.build(active_persona_id="companion")
    assert "As an AI, I don't have feelings" in out
    assert "I'm feeling great today" in out
    assert "Two-to-five sentences" in out
