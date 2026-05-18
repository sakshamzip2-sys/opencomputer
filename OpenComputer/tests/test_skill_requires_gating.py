"""Skill Requirements Gating — `requires:` frontmatter.

OpenClaw parity: skills declare what they need (binaries, env vars, OS,
plugins). Skills with unmet requirements are silently skipped from
agent-prompt injection so the model never sees a tool it cannot run.

Behaviour contracts:

* Frontmatter parser is permissive: malformed ``requires`` blocks degrade
  to "no requirements" (one broken skill must not starve the others).
* ``SkillMeta.unmet_requirements`` is empty for satisfied skills and
  populated with structured "kind:value" reasons otherwise.
* ``MemoryManager.list_skills()`` returns *all* skills (so the dashboard
  + CLI listings can show unmet status); the agent loop is responsible
  for filtering at the injection site.
"""
from __future__ import annotations

import platform
import shutil
from pathlib import Path

from opencomputer.agent.memory import (
    MemoryManager,
    SkillRequirements,
    _evaluate_skill_requirements,
    _parse_skill_requires,
)


def _write_skill(skills_dir: Path, skill_id: str, requires_block: str = "") -> None:
    """Write a minimal SKILL.md with optional ``requires:`` block."""
    d = skills_dir / skill_id
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {skill_id}", "description: t"]
    if requires_block:
        fm.append(requires_block.rstrip())
    fm.append("---")
    fm.append("body")
    (d / "SKILL.md").write_text("\n".join(fm))


# ─── parser ───────────────────────────────────────────────────────────


def test_parser_accepts_full_block():
    raw = {
        "binaries": ["pdftotext", "ghostscript"],
        "env": ["ADOBE_API_KEY"],
        "os": ["macos", "linux"],
        "plugins": ["unbrowse-openclaw"],
    }
    reqs = _parse_skill_requires(raw)
    assert reqs.binaries == ("pdftotext", "ghostscript")
    assert reqs.env == ("ADOBE_API_KEY",)
    assert reqs.os == ("macos", "linux")
    assert reqs.plugins == ("unbrowse-openclaw",)


def test_parser_accepts_partial_block():
    reqs = _parse_skill_requires({"binaries": ["jq"]})
    assert reqs.binaries == ("jq",)
    assert reqs.env == ()
    assert reqs.os == ()
    assert reqs.plugins == ()


def test_parser_degrades_on_malformed_input():
    # str instead of list → empty
    assert _parse_skill_requires({"binaries": "jq"}).binaries == ()
    # int instead of list → empty
    assert _parse_skill_requires({"env": 42}).env == ()
    # non-dict → empty SkillRequirements
    reqs = _parse_skill_requires("nope")
    assert reqs == SkillRequirements()
    # missing key → empty SkillRequirements (no crash)
    assert _parse_skill_requires(None) == SkillRequirements()


def test_parser_strips_empty_entries():
    reqs = _parse_skill_requires({
        "binaries": ["jq", "", "  "],
        "env": ["", "X"],
    })
    assert reqs.binaries == ("jq",)
    assert reqs.env == ("X",)


def test_parser_lowercases_os_names():
    reqs = _parse_skill_requires({"os": ["MacOS", "Linux", "WINDOWS"]})
    assert reqs.os == ("macos", "linux", "windows")


# ─── evaluator ────────────────────────────────────────────────────────


def test_evaluator_returns_empty_for_no_requirements():
    unmet = _evaluate_skill_requirements(SkillRequirements(), installed_plugin_ids=None)
    assert unmet == ()


def test_evaluator_flags_missing_binary(tmp_path, monkeypatch):
    reqs = SkillRequirements(binaries=("definitely-not-a-real-binary-xyz",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert any("binary:definitely-not-a-real-binary-xyz" in r for r in unmet)


def test_evaluator_passes_present_binary():
    # `sh` is on every supported platform.
    reqs = SkillRequirements(binaries=("sh",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert unmet == ()


def test_evaluator_flags_missing_env_var(monkeypatch):
    monkeypatch.delenv("OC_TEST_REQ_VAR", raising=False)
    reqs = SkillRequirements(env=("OC_TEST_REQ_VAR",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert any("env:OC_TEST_REQ_VAR" in r for r in unmet)


def test_evaluator_passes_present_env_var(monkeypatch):
    monkeypatch.setenv("OC_TEST_REQ_VAR", "x")
    reqs = SkillRequirements(env=("OC_TEST_REQ_VAR",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert unmet == ()


def test_evaluator_passes_when_current_os_is_listed():
    current = platform.system().lower()
    # Map platform.system() → spec name.
    osname = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(current, current)
    reqs = SkillRequirements(os=(osname,))
    assert _evaluate_skill_requirements(reqs, installed_plugin_ids=None) == ()


def test_evaluator_flags_when_current_os_not_listed():
    # Pick a platform name we are NOT running on.
    reqs = SkillRequirements(os=("solaris",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert any(r.startswith("os:") for r in unmet)


def test_evaluator_skips_plugin_check_when_index_unknown():
    # installed_plugin_ids=None → conservative: do not gate on plugins
    # (we have no information; refusing every skill that lists plugins
    # would punish skills authored offline).
    reqs = SkillRequirements(plugins=("some-plugin",))
    unmet = _evaluate_skill_requirements(reqs, installed_plugin_ids=None)
    assert unmet == ()


def test_evaluator_flags_missing_plugin_when_index_supplied():
    reqs = SkillRequirements(plugins=("missing-plugin",))
    unmet = _evaluate_skill_requirements(
        reqs, installed_plugin_ids=frozenset({"some-other-plugin"}),
    )
    assert any("plugin:missing-plugin" in r for r in unmet)


def test_evaluator_passes_present_plugin():
    reqs = SkillRequirements(plugins=("my-plugin",))
    unmet = _evaluate_skill_requirements(
        reqs, installed_plugin_ids=frozenset({"my-plugin", "other"}),
    )
    assert unmet == ()


def test_evaluator_aggregates_multiple_failures(monkeypatch):
    monkeypatch.delenv("OC_TEST_REQ_VAR", raising=False)
    reqs = SkillRequirements(
        binaries=("definitely-not-a-real-binary-xyz",),
        env=("OC_TEST_REQ_VAR",),
        os=("solaris",),
        plugins=("missing-plugin",),
    )
    unmet = _evaluate_skill_requirements(
        reqs, installed_plugin_ids=frozenset(),
    )
    # Each kind contributes at least one entry.
    kinds = {r.split(":", 1)[0] for r in unmet}
    assert kinds == {"binary", "env", "os", "plugin"}


# ─── integration via list_skills ──────────────────────────────────────


def test_list_skills_attaches_unmet_for_unsatisfied_skill(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(
        skills,
        "needs-missing-bin",
        requires_block="requires:\n  binaries: [definitely-not-a-real-binary-xyz]",
    )
    _write_skill(skills, "no-reqs")

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
        extensions_path=None,
    )
    by_id = {s.id: s for s in mm.list_skills()}
    assert by_id["needs-missing-bin"].unmet_requirements != ()
    assert by_id["no-reqs"].unmet_requirements == ()


def test_list_skills_satisfied_skill_has_empty_unmet(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("OC_TEST_PRESENT", "1")
    _write_skill(
        skills,
        "ok-skill",
        requires_block="requires:\n  binaries: [sh]\n  env: [OC_TEST_PRESENT]",
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
        extensions_path=None,
    )
    skill = next(s for s in mm.list_skills() if s.id == "ok-skill")
    assert skill.unmet_requirements == ()
    assert skill.requires.binaries == ("sh",)


def test_list_skills_malformed_requires_does_not_break_loading(tmp_path):
    """A skill with malformed `requires:` still loads; it just has no
    requirements (and therefore no unmet ones). The loader's "broken
    skill must not starve other skills" invariant holds.
    """
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(
        skills,
        "garbled",
        # `requires` is a string, not a mapping — malformed.
        requires_block="requires: 'totally-wrong-shape'",
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
        extensions_path=None,
    )
    skills_out = mm.list_skills()
    assert any(s.id == "garbled" for s in skills_out)
    garbled = next(s for s in skills_out if s.id == "garbled")
    assert garbled.unmet_requirements == ()
    assert garbled.requires == SkillRequirements()


def test_existing_skill_priority_test_still_passes(tmp_path):
    """Backwards compatibility: skills without `requires:` work as before."""
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "alpha")
    _write_skill(skills, "beta")
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
        extensions_path=None,
    )
    skills_out = mm.list_skills()
    ids = sorted(s.id for s in skills_out)
    assert ids == ["alpha", "beta"]
    for s in skills_out:
        assert s.unmet_requirements == ()
        assert s.requires == SkillRequirements()
