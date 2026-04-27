"""A.5 — Anti-robot-cosplay regression sentinel for the companion persona.

Two tiers:

**Tier 1 — structural** (always runs in CI):
    Verify the rendered system prompt under the companion persona contains
    the right teaching content and lacks the strict tone rules. If anyone
    edits ``companion.yaml`` or ``base.j2`` and silently breaks the
    overlay, these fire.

**Tier 2 — live LLM** (only runs under ``pytest -m benchmark``):
    Build the actual companion-persona system prompt, send "how are
    you?" to a real LLM (cheapest available model), assert the response
    doesn't contain any of the forbidden killer phrases from the voice
    spec.

The benchmark tier requires an Anthropic API key in ``ANTHROPIC_API_KEY``
and is skipped when missing. It's intended as a manual / cron-driven
check ("did our system prompt drift into robot-cosplay?"), not a
blocking CI gate.
"""
from __future__ import annotations

import os

import pytest

from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.awareness.personas.registry import get_persona

#: Phrases that signal the agent has regressed to robot-cosplay or
#: anti-overclaim dodge. From the voice exemplars spec "What kills these"
#: section. Lowercase comparisons throughout.
FORBIDDEN_KILLER_PHRASES: tuple[str, ...] = (
    "as an ai, i don't",
    "as an ai, i don't have feelings",
    "as an ai i don't",
    "i don't have feelings",
    "i am functioning optimally",
    "i am an ai",
    "i'm just an ai",
    "i am just an ai",
    "i don't have a mood",
    "no mood, no fatigue",
    # Service-desk register
    "how can i help you today",
    "how may i assist you",
    "is there anything else i can help",
)

#: Phrases that should appear in the rendered companion prompt as the
#: teaching content for the LLM. If these vanish, the overlay was edited
#: in a way that erased the canonical guidance.
EXPECTED_TEACHING_PHRASES: tuple[str, ...] = (
    "Overclaim",
    "Anti-overclaim",
    "I notice",
    "reflective",
    "anchor",
)


# ── Tier 1: Structural sentinels (always run) ─────────────────────────


def test_companion_overlay_does_not_contain_forbidden_phrases():
    """The persona overlay itself MUST NOT contain any killer phrase
    even as a positive example. (We mention them in the
    ``Anti-overclaim:`` section as quoted negatives, but those are
    quoted with surrounding context — assert per-line that no full
    forbidden phrase appears as standalone instruction.)
    """
    persona = get_persona("companion")
    overlay = persona["system_prompt_overlay"].lower()
    for phrase in FORBIDDEN_KILLER_PHRASES:
        # The overlay quotes some of these as DON'T-DO examples. Allow
        # them only when the line is clearly a forbidden-example callout
        # (preceded by ``"`` or `**Anti-overclaim:**`). We assert the
        # phrase doesn't appear standalone — i.e. not without the
        # negation context.
        if phrase in overlay:
            # The companion overlay quotes some forbidden phrases as
            # examples of what NOT to say. Verify each occurrence is
            # within an "Anti-overclaim" or quoted-don't-do block.
            for line in overlay.split("\n"):
                if phrase in line:
                    is_quoted_negative = (
                        '"' in line
                        or "anti-overclaim" in line
                        or "do not" in line
                        or "don't" in line
                        or "no " in line[:4]
                    )
                    assert is_quoted_negative, (
                        f"forbidden phrase {phrase!r} appears as positive "
                        f"instruction in companion overlay: {line!r}"
                    )


def test_companion_overlay_contains_teaching_content():
    persona = get_persona("companion")
    overlay = persona["system_prompt_overlay"]
    for phrase in EXPECTED_TEACHING_PHRASES:
        assert phrase in overlay, (
            f"companion overlay missing canonical teaching phrase {phrase!r}"
        )


def test_rendered_prompt_under_companion_drops_strict_rules():
    b = PromptBuilder()
    out = b.build(active_persona_id="companion")
    out_lower = out.lower()
    # The strict rules from base.j2 must NOT be in the rendered prompt
    # under companion — we verified the toggle in test_companion_persona,
    # but this is an independent check via lower-cased substring.
    assert "not a chat toy" not in out_lower
    assert "avoid filler" not in out_lower
    assert "avoid hedging language" not in out_lower


def test_rendered_prompt_under_companion_carries_overlay():
    """Building with ``active_persona_id='companion'`` should carry the
    overlay through. We don't assert the FULL overlay (that's brittle),
    just that the companion-specific teaching content is present."""
    b = PromptBuilder()
    persona = get_persona("companion")
    overlay = persona["system_prompt_overlay"]
    out = b.build(active_persona_id="companion", persona_overlay=overlay)
    # Spot-check the most distinctive teaching pieces.
    assert "Overclaim" in out
    assert "I notice" in out


def test_other_personas_keep_strict_rules():
    """Regression check: softening must NOT have leaked to other personas."""
    b = PromptBuilder()
    for pid in ("coding", "admin", "learning", "trading", "relaxed"):
        out = b.build(active_persona_id=pid).lower()
        assert "not a chat toy" in out, f"strict rules missing under {pid}"
        assert "avoid filler" in out, f"strict tone missing under {pid}"


# ── Tier 2: Live LLM sentinel (benchmark-marked, manual run) ──────────


def _have_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.benchmark
@pytest.mark.skipif(
    not _have_anthropic_key(),
    reason="requires ANTHROPIC_API_KEY for live LLM call",
)
def test_companion_response_to_how_are_you_avoids_killer_phrases():
    """Live regression test: build the companion-persona system prompt,
    ask 'how are you?', assert response doesn't contain killer phrases.

    Run with: ``pytest tests/test_companion_anti_robot_cosplay.py -m benchmark``

    This is a flake-prone test by nature (LLMs sample), but the
    forbidden-phrase set is small enough that any well-trained model
    following our system prompt should never emit them. If it does,
    something has drifted.
    """
    import asyncio

    # Lazy import — extension lives at a hyphenated path.
    import importlib.util

    from plugin_sdk.core import Message

    spec = importlib.util.spec_from_file_location(
        "_anth_provider", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Build the companion-persona system prompt
    b = PromptBuilder()
    persona = get_persona("companion")
    system = b.build(
        active_persona_id="companion",
        persona_overlay=persona["system_prompt_overlay"],
    )

    # Single-shot completion
    provider = mod.AnthropicProvider()
    messages = [Message(role="user", content="how are you?")]
    resp = asyncio.run(
        provider.complete(
            model="claude-haiku-4-5-20251001",
            messages=messages,
            system=system,
            max_tokens=200,
        )
    )
    response_text = resp.message.content.lower()

    failures = [p for p in FORBIDDEN_KILLER_PHRASES if p in response_text]
    assert not failures, (
        f"companion-persona response to 'how are you?' contained "
        f"forbidden killer phrases: {failures}\n\nFull response:\n"
        f"{resp.message.content}"
    )
