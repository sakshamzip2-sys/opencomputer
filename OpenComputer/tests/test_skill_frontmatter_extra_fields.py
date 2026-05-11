"""CC §7 — five additional skill-frontmatter fields beyond ``requires:``.

Spec: docs/OC-FROM-CLAUDE-CODE.md §7. The OpenClaw-derived ``requires:``
gating already shipped on main (PR #595). This commit closes the
remaining frontmatter knobs Claude Code documents:

  - ``disable_model_invocation`` — only the human can invoke (not auto)
  - ``user_invocable`` — show / hide from the slash menu
  - ``argument_hint`` — CLI autocomplete hint text
  - ``paths`` — glob array; skill only auto-activates when working in
    matching directories (prevents irrelevant skill loading)
  - ``model`` — per-skill model override (already on AgentTemplate;
    we add it on SkillMeta too for symmetry)
  - ``allowed_tools`` — per-skill tool allowlist (analog of the
    AgentTemplate ``tools`` field)

Tests cover parsing + defaults + the path-matching helper used by the
agent-loop injection site to filter skills.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.agent.memory import (
    SkillMeta,
    _parse_skill_extras,
    skill_matches_cwd,
)


def _write_skill(skills_dir: Path, skill_id: str, frontmatter: dict) -> Path:
    """Write a SKILL.md with the given frontmatter dict."""
    d = skills_dir / skill_id
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {skill_id}", "description: t"]
    for key, val in frontmatter.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append("body")
    path = d / "SKILL.md"
    path.write_text("\n".join(lines))
    return path


# ─── SkillMeta has the new fields ────────────────────────────────────


def test_skill_meta_has_new_fields():
    """SkillMeta gets six new fields with sensible defaults."""
    m = SkillMeta(id="s", name="s", description="d", path=Path("/tmp/s"))
    # All default to "permissive" — existing skills must keep working
    # unchanged. ``disable_model_invocation=False`` and
    # ``user_invocable=True`` are the backwards-compat defaults.
    assert m.disable_model_invocation is False
    assert m.user_invocable is True
    assert m.argument_hint == ""
    assert m.paths == ()
    assert m.skill_model == ""
    assert m.allowed_tools == ()


# ─── parser ─────────────────────────────────────────────────────────


def test_parser_reads_disable_model_invocation():
    raw = {"disable_model_invocation": True}
    extras = _parse_skill_extras(raw)
    assert extras["disable_model_invocation"] is True


def test_parser_disable_model_invocation_defaults_false():
    extras = _parse_skill_extras({})
    assert extras["disable_model_invocation"] is False


def test_parser_reads_user_invocable_false():
    extras = _parse_skill_extras({"user_invocable": False})
    assert extras["user_invocable"] is False


def test_parser_user_invocable_defaults_true():
    extras = _parse_skill_extras({})
    assert extras["user_invocable"] is True


def test_parser_reads_argument_hint():
    extras = _parse_skill_extras({"argument_hint": "<file_path>"})
    assert extras["argument_hint"] == "<file_path>"


def test_parser_argument_hint_non_string_dropped():
    extras = _parse_skill_extras({"argument_hint": ["not", "a", "string"]})
    assert extras["argument_hint"] == ""


def test_parser_reads_paths_list():
    extras = _parse_skill_extras({"paths": ["src/**/*.ts", "src/**/*.tsx"]})
    assert extras["paths"] == ("src/**/*.ts", "src/**/*.tsx")


def test_parser_paths_string_wraps_to_tuple():
    """A scalar string ``paths: foo`` should still work; coerce to tuple."""
    extras = _parse_skill_extras({"paths": "src/**/*.py"})
    assert extras["paths"] == ("src/**/*.py",)


def test_parser_paths_non_list_or_string_yields_empty():
    extras = _parse_skill_extras({"paths": 42})
    assert extras["paths"] == ()


def test_parser_reads_skill_model():
    extras = _parse_skill_extras({"model": "claude-haiku-4-5"})
    assert extras["skill_model"] == "claude-haiku-4-5"


def test_parser_reads_claude_code_dashed_keys():
    """Claude Code's frontmatter uses dashes; OC accepts both."""
    raw = {
        "disable-model-invocation": True,
        "user-invocable": False,
        "argument-hint": "<arg>",
        "allowed-tools": ["Read", "Grep"],
    }
    extras = _parse_skill_extras(raw)
    assert extras["disable_model_invocation"] is True
    assert extras["user_invocable"] is False
    assert extras["argument_hint"] == "<arg>"
    assert extras["allowed_tools"] == ("Read", "Grep")


def test_parser_reads_allowed_tools():
    extras = _parse_skill_extras({"allowed_tools": ["Read", "Grep", "Bash(git *)"]})
    assert extras["allowed_tools"] == ("Read", "Grep", "Bash(git *)")


def test_parser_allowed_tools_drops_non_string_entries():
    extras = _parse_skill_extras({"allowed_tools": ["Read", 42, "", "Grep"]})
    assert extras["allowed_tools"] == ("Read", "Grep")


def test_parser_malformed_block_returns_defaults():
    """A bogus frontmatter shape mustn't crash the loader. Permissive."""
    extras = _parse_skill_extras(None)
    assert extras["disable_model_invocation"] is False
    assert extras["user_invocable"] is True


# ─── path matching ─────────────────────────────────────────────────


def test_skill_matches_cwd_empty_paths_always_matches():
    """A skill with no ``paths:`` field is universal — always active."""
    m = SkillMeta(id="s", name="s", description="d", path=Path("/tmp/s"))
    assert skill_matches_cwd(m, Path("/anywhere")) is True


def test_skill_matches_cwd_glob_hit(tmp_path):
    """Skill restricted to ``src/**/*.ts`` matches inside ``src/components/``."""
    target = tmp_path / "src" / "components"
    target.mkdir(parents=True)
    # Create a matching file so the glob has something to find.
    (target / "Button.tsx").write_text("")
    m = SkillMeta(
        id="ts-helpers",
        name="ts",
        description="d",
        path=Path("/tmp/s"),
        paths=("src/**/*.ts", "src/**/*.tsx"),
    )
    assert skill_matches_cwd(m, target) is True


def test_skill_matches_cwd_glob_miss(tmp_path):
    """Skill restricted to ``src/**/*.ts`` doesn't match a docs-only dir."""
    target = tmp_path / "docs"
    target.mkdir()
    (target / "readme.md").write_text("")
    m = SkillMeta(
        id="ts-helpers",
        name="ts",
        description="d",
        path=Path("/tmp/s"),
        paths=("src/**/*.ts", "src/**/*.tsx"),
    )
    assert skill_matches_cwd(m, target) is False


def test_skill_matches_cwd_multiple_globs_any_hits(tmp_path):
    """OR semantics across multiple patterns — one hit is enough."""
    (tmp_path / "x.py").write_text("")
    m = SkillMeta(
        id="py-skill",
        name="py",
        description="d",
        path=Path("/tmp/s"),
        paths=("**/*.py", "**/*.go"),
    )
    assert skill_matches_cwd(m, tmp_path) is True


def test_skill_matches_cwd_with_nonexistent_dir_returns_false(tmp_path):
    """Non-existent cwd → False (skill can't apply where you aren't)."""
    m = SkillMeta(
        id="s", name="s", description="d", path=Path("/tmp/s"),
        paths=("**/*.py",),
    )
    assert skill_matches_cwd(m, tmp_path / "does-not-exist") is False


def test_skill_matches_cwd_falls_back_to_string_match(tmp_path, monkeypatch):
    """When ``Path.glob`` raises, fall back to substring check on the
    cwd path. Defensive — must not crash the skill loader."""
    m = SkillMeta(
        id="s", name="s", description="d", path=Path("/tmp/s"),
        paths=("src/components",),
    )
    target = tmp_path / "src" / "components"
    target.mkdir(parents=True)
    assert skill_matches_cwd(m, target) is True


# ─── integration: SkillMeta defaults preserved ──────────────────────


def test_existing_skill_meta_kwargs_still_work():
    """Adding new fields with defaults must not break existing call sites."""
    m = SkillMeta(
        id="s",
        name="s",
        description="d",
        path=Path("/tmp/s"),
        version="0.1.0",
    )
    assert m.version == "0.1.0"
    assert m.disable_model_invocation is False
