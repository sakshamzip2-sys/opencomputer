"""III.7 — plugin-dev skill library.

Verifies the six ``opencomputer-*`` skills that guide plugin authors
through the real OpenComputer API surface. These skills are the
OpenComputer analog to Claude Code's
``sources/claude-code/plugins/plugin-dev/skills/`` — reference work for
an agent writing a plugin.

Structural checks (each skill dir exists, has SKILL.md + populated
references/ + examples/) combine with content checks (description is
trigger-phrased, body mentions at least one canonical API name). The
content checks are deliberately weak — a skill that drifted into
generic prose would fail the API-name allowlist even though its
frontmatter stayed valid.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "opencomputer" / "skills"

#: Every III.7 skill and what it should cover (short label used in asserts).
PLUGIN_DEV_SKILLS: tuple[str, ...] = (
    "opencomputer-plugin-structure",
    "opencomputer-tool-development",
    "opencomputer-skill-authoring",
    "opencomputer-hook-authoring",
    "opencomputer-agent-templates",
    "opencomputer-mcp-integration",
)

#: Canonical API names — every SKILL.md body must mention at least one.
#: Weak check, but catches skills that drift into generic content.
CANONICAL_API_NAMES: frozenset[str] = frozenset({
    "register",
    "PluginManifest",
    "BaseTool",
    "ToolSchema",
    "ToolResult",
    "ToolCall",
    "HookSpec",
    "HookEvent",
    "HookDecision",
    "HookContext",
    "MCPConfig",
    "MCPServerConfig",
    "PluginAPI",
    "DelegateTool",
    "AgentTemplate",
    "MemoryManager",
})


# ─── structural checks ────────────────────────────────────────────────


@pytest.mark.parametrize("skill_id", PLUGIN_DEV_SKILLS)
def test_all_six_skills_present(skill_id: str) -> None:
    """Every III.7 skill dir exists with a SKILL.md at its root."""
    skill_dir = SKILLS_ROOT / skill_id
    assert skill_dir.is_dir(), f"missing skill dir: {skill_dir}"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"missing SKILL.md: {skill_md}"


@pytest.mark.parametrize("skill_id", PLUGIN_DEV_SKILLS)
def test_all_skill_md_have_valid_frontmatter(skill_id: str) -> None:
    """name / description / version are non-empty strings in every frontmatter."""
    skill_md = SKILLS_ROOT / skill_id / "SKILL.md"
    post = frontmatter.load(skill_md)
    for field in ("name", "description", "version"):
        value = post.metadata.get(field)
        assert isinstance(value, str), f"{skill_id}: '{field}' is not a string: {value!r}"
        assert value.strip(), f"{skill_id}: '{field}' is empty"


@pytest.mark.parametrize("skill_id", PLUGIN_DEV_SKILLS)
def test_references_and_examples_dirs_populated(skill_id: str) -> None:
    """Each skill ships >=1 file in references/ AND >=1 file in examples/."""
    skill_dir = SKILLS_ROOT / skill_id
    refs = skill_dir / "references"
    exs = skill_dir / "examples"
    assert refs.is_dir(), f"{skill_id}: references/ dir missing"
    assert exs.is_dir(), f"{skill_id}: examples/ dir missing"

    ref_files = [p for p in refs.iterdir() if p.is_file()]
    ex_files = [p for p in exs.iterdir() if p.is_file()]
    assert len(ref_files) >= 1, f"{skill_id}: references/ is empty"
    assert len(ex_files) >= 1, f"{skill_id}: examples/ is empty"


# ─── content checks ───────────────────────────────────────────────────


@pytest.mark.parametrize("skill_id", PLUGIN_DEV_SKILLS)
def test_descriptions_are_trigger_phrased(skill_id: str) -> None:
    """Every plugin-dev description opens with the canonical trigger phrase.

    "This skill should be used when" is the prefix Claude Code's retrieval
    layer recognizes — we enforce it for the plugin-dev library so the
    frontmatter stays retrieval-friendly and the shape is consistent.
    """
    skill_md = SKILLS_ROOT / skill_id / "SKILL.md"
    post = frontmatter.load(skill_md)
    desc = post.metadata.get("description", "")
    assert isinstance(desc, str)
    assert "This skill should be used when" in desc, (
        f"{skill_id}: description must contain "
        f"'This skill should be used when' — got: {desc!r}"
    )


@pytest.mark.parametrize("skill_id", PLUGIN_DEV_SKILLS)
def test_skills_reference_real_api_names(skill_id: str) -> None:
    """Each SKILL.md body mentions >=1 canonical OpenComputer API name.

    Weak sanity check — catches skills that drifted to generic advice
    without a single anchor back to a real OpenComputer type / function.
    The allowlist is the SDK's public vocabulary (register, PluginManifest,
    BaseTool, HookSpec, MCPServerConfig, etc.).
    """
    skill_md = SKILLS_ROOT / skill_id / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8")
    hits = [name for name in CANONICAL_API_NAMES if name in body]
    assert hits, (
        f"{skill_id}: SKILL.md body mentions none of the canonical API names "
        f"({sorted(CANONICAL_API_NAMES)}). Did the skill drift to generic content?"
    )


# ─── integration — MemoryManager discovery ────────────────────────────


def test_skills_discoverable_by_memory_manager(tmp_path: Path) -> None:
    """Instantiate MemoryManager against the bundled skills root and verify
    every III.7 skill is discovered.

    Uses an empty ``skills_path`` (user skills dir) so only the bundled
    roots contribute; confirms the skills travel with the package.
    """
    from opencomputer.agent.memory import MemoryManager

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "user-skills",  # empty
        bundled_skills_paths=[SKILLS_ROOT],
    )
    found_ids = {s.id for s in mm.list_skills()}
    missing = set(PLUGIN_DEV_SKILLS) - found_ids
    assert not missing, f"MemoryManager did not discover: {sorted(missing)}"
