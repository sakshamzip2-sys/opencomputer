"""Tests for the <user-tone> block injection (Prompt C).

Verifies:
1. ``PromptBuilder.build_user_tone()`` returns "" when no F4 ``preference``
   node has the ``tone_preference:`` prefix.
2. ``build_user_tone()`` strips the prefix and returns the bare value.
3. When multiple ``tone_preference`` nodes exist, the highest-confidence
   AND most-recent one wins.
4. Integration: ``PromptBuilder.build()`` includes a ``<user-tone>``
   block in the rendered system prompt when the F4 graph has the node.
5. The block lands in the FROZEN base, not a per-turn delta — verified
   by calling ``build()`` (sync, frozen-base path).
"""
from __future__ import annotations

import time
from pathlib import Path

from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.user_model.store import UserModelStore


def _store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(tmp_path / "graph.sqlite")


# ── build_user_tone() unit tests ─────────────────────────────────────


def test_user_tone_empty_when_no_node(tmp_path: Path) -> None:
    pb = PromptBuilder()
    out = pb.build_user_tone(store=_store(tmp_path))
    assert out == ""


def test_user_tone_extracts_value_stripping_prefix(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert_node(
        kind="preference",
        value="tone_preference: concise and action-first",
        confidence=1.0,
    )
    pb = PromptBuilder()
    out = pb.build_user_tone(store=s)
    assert out == "concise and action-first"


def test_user_tone_skips_non_tone_preferences(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert_node(
        kind="preference",
        value="do_not: send emails without confirming",
        confidence=1.0,
    )
    s.upsert_node(
        kind="preference",
        value="favourite_editor: helix",
        confidence=1.0,
    )
    pb = PromptBuilder()
    out = pb.build_user_tone(store=s)
    assert out == ""


def test_user_tone_picks_highest_confidence_when_multiple(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    # Lower-confidence first.
    s.upsert_node(
        kind="preference",
        value="tone_preference: thorough with examples",
        confidence=0.6,
    )
    # Higher-confidence — should win.
    s.upsert_node(
        kind="preference",
        value="tone_preference: concise and action-first",
        confidence=1.0,
    )
    pb = PromptBuilder()
    out = pb.build_user_tone(store=s)
    assert out == "concise and action-first"


def test_user_tone_picks_most_recent_at_equal_confidence(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    s.upsert_node(
        kind="preference",
        value="tone_preference: thorough with examples",
        confidence=1.0,
    )
    # Force a later last_seen_at by re-upserting.
    time.sleep(0.005)
    s.upsert_node(
        kind="preference",
        value="tone_preference: concise and action-first",
        confidence=1.0,
    )
    pb = PromptBuilder()
    out = pb.build_user_tone(store=s)
    assert out == "concise and action-first"


# ── integration: prompt block lands in FROZEN base ───────────────────


def test_build_includes_user_tone_block_when_present(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert_node(
        kind="preference",
        value="tone_preference: concise and action-first",
        confidence=1.0,
    )
    pb = PromptBuilder()
    user_tone = pb.build_user_tone(store=s)
    out = pb.build(user_tone=user_tone)
    assert "<user-tone>" in out
    assert "concise and action-first" in out
    assert "</user-tone>" in out
    # The prefix must NOT appear inside the rendered block.
    assert "tone_preference:" not in out


def test_build_omits_user_tone_block_when_empty() -> None:
    pb = PromptBuilder()
    out = pb.build()  # default user_tone=""
    assert "<user-tone>" not in out


# ── persona preferred_tone (Prompt C, code-level precedence) ─────────


def test_persona_preferred_tone_renders_when_user_tone_absent() -> None:
    """The persona's YAML preferred_tone (e.g. companion=warm) renders as
    a <persona-tone> block when the user has NOT stated their own tone.
    """
    pb = PromptBuilder()
    out = pb.build(persona_preferred_tone="warm")
    assert "<persona-tone>" in out
    assert "warm" in out
    assert "</persona-tone>" in out


def test_user_tone_overrides_persona_preferred_tone_in_code() -> None:
    """When BOTH user_tone and persona_preferred_tone are set, only the
    <user-tone> block renders. The <persona-tone> block is suppressed —
    code-level enforcement of the precedence rule.
    """
    pb = PromptBuilder()
    out = pb.build(
        user_tone="concise and action-first",
        persona_preferred_tone="warm",
    )
    assert "<user-tone>" in out
    assert "concise and action-first" in out
    assert "<persona-tone>" not in out
    assert "warm" not in out


def test_persona_preferred_tone_omitted_when_persona_has_none() -> None:
    """No persona_preferred_tone → no <persona-tone> block, even when
    user_tone is also unset. Keeps the prompt clean for personas whose
    YAML doesn't declare a preferred_tone.
    """
    pb = PromptBuilder()
    out = pb.build()  # both default ""
    assert "<persona-tone>" not in out
    assert "<user-tone>" not in out
