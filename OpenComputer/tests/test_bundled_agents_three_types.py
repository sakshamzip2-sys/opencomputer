"""CC §9 — three bundled subagent types: Explore / Plan / General-purpose.

Verifies the three new templates parse cleanly via the existing
``agent_templates.discover_agents`` machinery, declare the right tool
allowlists, point at the right models, and stay registered as bundled.

Spec: docs/OC-FROM-CLAUDE-CODE.md §9. Implementation: drop-in markdown
files under ``opencomputer/agents/`` — no code changes required, the
discovery + DelegateTool wiring already supports the ``agent:`` arg.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.agent.agent_templates import (
    _default_bundled_root,
    discover_agents,
    parse_agent_template,
)

#: The three CC §9 subagent types that must ship as bundled templates.
_CC9_BUNDLED_NAMES = frozenset({"explore", "plan", "general-purpose"})


def test_three_bundled_agents_ship() -> None:
    """All three CC §9 templates exist on disk under the bundled root."""
    bundled = _default_bundled_root()
    assert bundled.is_dir(), f"bundled agents root missing: {bundled}"
    found = {p.stem for p in bundled.glob("*.md")}
    missing = _CC9_BUNDLED_NAMES - found
    assert not missing, f"bundled agent files missing: {missing}; found: {found}"


def test_three_bundled_agents_discover() -> None:
    """``discover_agents`` returns all three by name with ``source='bundled'``."""
    templates = discover_agents()
    for name in _CC9_BUNDLED_NAMES:
        assert name in templates, (
            f"agent {name!r} not discovered. Have: {sorted(templates)}"
        )
        assert templates[name].source == "bundled", (
            f"{name}.source = {templates[name].source!r}; expected 'bundled'"
        )


def test_explore_is_read_only_tools() -> None:
    """``explore`` allowlist excludes any write/edit tool."""
    tpl = discover_agents()["explore"]
    tools = set(tpl.tools)
    write_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    overlap = tools & write_tools
    assert not overlap, (
        f"explore allowlist must be read-only; found write tools: {overlap}"
    )
    # Sanity: it MUST have some read tools or it's useless.
    assert tools & {"Read", "Grep", "Glob"}, (
        f"explore must include at least one of Read/Grep/Glob; tools={tools}"
    )


def test_explore_targets_haiku() -> None:
    """``explore`` is the fast/cheap agent — must request Haiku."""
    tpl = discover_agents()["explore"]
    assert tpl.model and "haiku" in tpl.model.lower(), (
        f"explore.model = {tpl.model!r}; expected a haiku variant"
    )


def test_plan_is_read_only_tools() -> None:
    """``plan`` allowlist excludes write tools."""
    tpl = discover_agents()["plan"]
    tools = set(tpl.tools)
    write_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    overlap = tools & write_tools
    assert not overlap, (
        f"plan allowlist must be read-only; found write tools: {overlap}"
    )
    # TodoWrite is explicitly OK — it's plan-state tracking, not source mutation.
    # Sanity: plan must have research tooling.
    assert "WebFetch" in tools or "WebSearch" in tools, (
        f"plan should include WebFetch/WebSearch; tools={tools}"
    )


def test_plan_targets_sonnet() -> None:
    """``plan`` does architecture thinking — Sonnet weight class."""
    tpl = discover_agents()["plan"]
    assert tpl.model and "sonnet" in tpl.model.lower(), (
        f"plan.model = {tpl.model!r}; expected a sonnet variant"
    )


def test_general_purpose_inherits_full_tools() -> None:
    """``general-purpose`` omits the ``tools:`` frontmatter so the child
    loop inherits the parent's full set. Empty tuple in the template
    means "no allowlist filter" by the existing convention."""
    tpl = discover_agents()["general-purpose"]
    assert tpl.tools == (), (
        f"general-purpose.tools should be empty (inherit); got {tpl.tools!r}"
    )


def test_general_purpose_omits_model_override() -> None:
    """``general-purpose`` should not pin a model — use parent's choice."""
    tpl = discover_agents()["general-purpose"]
    assert tpl.model is None, (
        f"general-purpose should omit model; got {tpl.model!r}"
    )


def test_each_agent_has_substantive_prompt() -> None:
    """No stubs — each system prompt must say something meaningful."""
    templates = discover_agents()
    for name in _CC9_BUNDLED_NAMES:
        body = templates[name].system_prompt.strip()
        # Minimum length sanity. The shortest of mine is 'general-purpose'
        # which still runs ~25 lines — 200 chars is a floor not a ceiling.
        assert len(body) >= 200, (
            f"{name}.system_prompt is suspiciously short ({len(body)} chars): "
            f"{body[:100]!r}..."
        )


def test_each_agent_parses_via_parse_helper() -> None:
    """The discovery path uses ``parse_agent_template`` — exercise it
    directly so frontmatter typos surface in this dedicated test."""
    bundled = _default_bundled_root()
    for name in _CC9_BUNDLED_NAMES:
        path = bundled / f"{name}.md"
        tpl = parse_agent_template(path, source="bundled")
        assert tpl.name == name


def test_descriptions_are_short_enough_for_listing() -> None:
    """``oc agents list`` shows description as a single column. >300
    chars wraps badly. The contract is "one short sentence"."""
    templates = discover_agents()
    for name in _CC9_BUNDLED_NAMES:
        d = templates[name].description
        assert 20 <= len(d) <= 300, (
            f"{name}.description length {len(d)} out of range [20, 300]: {d!r}"
        )
