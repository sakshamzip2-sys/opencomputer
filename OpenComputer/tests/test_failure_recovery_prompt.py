"""Regression tests for the Failure-budget section of base.j2 + the
bundled `failure-recovery-ladder` skill.

The agent had a persistent bug where it folded at the first failure of
a research/fetch task — typically by saying "I tried X but it didn't
work, want me to try Y?" and handing the work back to the user. Root
cause was twofold:

1. base.j2 had a buried single-line "two retries budget" rule mixed in
   with editor-failure tips. It wasn't load-bearing — the model didn't
   internalize it as discipline for research tasks.
2. There was no operational recipe for what to do per failure type
   (403 vs empty-result vs scrape-empty), so even when the model
   wanted to try harder, it didn't have a concrete next step.

The fix added a dedicated "Failure budget" section to base.j2 with the
anti-pattern names called out by name, and a bundled
`failure-recovery-ladder` skill with the per-failure-type recovery
ladder. These tests guard both pieces from being softened later.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.prompt_builder import PromptBuilder


def test_base_prompt_has_failure_budget_section() -> None:
    """The dedicated section exists with the headline anti-fold rule."""
    rendered = PromptBuilder().build()
    # Section header must exist as its own H2 — not nested inside Error
    # recovery — so that the model treats it as a peer concern.
    assert "## Failure budget" in rendered
    # The headline must call out the bug pattern by name.
    assert "never fold at first failure" in rendered.lower()


def test_base_prompt_names_the_anti_patterns() -> None:
    """The four named anti-patterns must appear verbatim — they are the
    pattern-match handles the model uses to catch its own behavior."""
    rendered = PromptBuilder().build().lower()
    # These exact phrases are load-bearing pattern handles. If a future
    # edit softens them, the model loses the ability to catch itself.
    assert "asking-as-stalling" in rendered
    assert "narrating dead-ends" in rendered
    assert "folding before the budget" in rendered
    assert "optimizing for not-being-wrong" in rendered


def test_base_prompt_lists_recovery_ladders() -> None:
    """The five core failure-type ladders must be enumerated. Without
    these, the discipline has no concrete next-step recipe."""
    rendered = PromptBuilder().build().lower()
    assert "403" in rendered
    assert "paywall" in rendered or "login wall" in rendered
    assert "websearch" in rendered  # cached / archive ladder rung
    assert "rate-limit" in rendered
    assert "429" in rendered or "backoff" in rendered


def test_base_prompt_authorizes_read_only_retries() -> None:
    """The 'don't ask, just keep trying' rule must explicitly carve out
    read-only attempts — otherwise the model conservatively asks for
    permission on every alternative URL."""
    rendered = PromptBuilder().build().lower()
    assert "read-only" in rendered or "read only" in rendered
    # Mutating actions remain gated — the carve-out must say so.
    assert "mutating" in rendered or "irreversible" in rendered


def test_base_prompt_points_to_recovery_skill() -> None:
    """The prompt must surface the bundled skill so the model knows
    where to load the full decision tree."""
    rendered = PromptBuilder().build()
    assert "failure-recovery-ladder" in rendered


def test_recovery_ladder_skill_exists() -> None:
    """The bundled skill file must exist at the expected path."""
    skill_path = (
        Path(__file__).parent.parent
        / "opencomputer"
        / "skills"
        / "failure-recovery-ladder"
        / "SKILL.md"
    )
    assert skill_path.exists(), f"missing bundled skill: {skill_path}"


def test_recovery_ladder_skill_has_required_frontmatter() -> None:
    """The skill must have the standard frontmatter so the skill loader
    can discover it."""
    skill_path = (
        Path(__file__).parent.parent
        / "opencomputer"
        / "skills"
        / "failure-recovery-ladder"
        / "SKILL.md"
    )
    body = skill_path.read_text()
    assert body.startswith("---\n")
    assert "name: failure-recovery-ladder" in body
    assert "description:" in body
    # The description must mention failure types so it surfaces in
    # description-based skill matching when the model hits a 403/empty.
    desc_line = next(
        line for line in body.splitlines() if line.startswith("description:")
    )
    assert any(
        keyword in desc_line.lower()
        for keyword in ("403", "paywall", "empty", "fail", "fold")
    )


def test_recovery_ladder_skill_covers_each_failure_type() -> None:
    """The skill body must contain a section for each major failure
    type. These are the rungs the prompt promises exist."""
    skill_path = (
        Path(__file__).parent.parent
        / "opencomputer"
        / "skills"
        / "failure-recovery-ladder"
        / "SKILL.md"
    )
    body = skill_path.read_text().lower()
    # Each ladder must be present.
    assert "403" in body and "paywall" in body
    assert "search" in body and ("empty" in body or "irrelevant" in body)
    assert "scrape" in body or "parse" in body
    assert "rate-limit" in body or "429" in body
    assert "build" in body or "test" in body or "lint" in body


def test_recovery_ladder_skill_names_the_anti_patterns() -> None:
    """The skill operationalizes the same anti-patterns base.j2 names —
    they must match so the model gets a consistent vocabulary."""
    skill_path = (
        Path(__file__).parent.parent
        / "opencomputer"
        / "skills"
        / "failure-recovery-ladder"
        / "SKILL.md"
    )
    body = skill_path.read_text().lower()
    assert "asking-as-stalling" in body
    assert "narrating dead-ends" in body or "dead-ends" in body
