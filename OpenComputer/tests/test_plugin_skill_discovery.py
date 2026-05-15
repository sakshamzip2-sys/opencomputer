"""Plugin-bundled skill auto-discovery (fourth skill root).

OpenComputer's skill scanner walks four roots: user skills, bundled
``opencomputer/skills/``, Skills-Hub installs (``<skills_path>/.hub/``),
and — added here — skills shipped inside plugins at
``extensions/<plugin>/skills/``.

Behaviour contracts:

* A plugin that ships a ``skills/`` directory contributes its
  ``skills/<skill-name>/SKILL.md`` files to ``MemoryManager.list_skills()``.
* Each plugin-shipped skill is gated on its owning plugin via the EXISTING
  ``requires:`` machinery — an implicit ``requires.plugins`` entry for the
  plugin id is merged into whatever the skill declares.
* When the owning plugin IS installed → no unmet plugin requirement →
  the skill is eligible for the prompt.
* When the owning plugin is NOT installed → the skill still appears in the
  scan result (visible in ``oc skills``) but is flagged unmet → the prompt
  builder drops it (same lane as any other unmet-requirement skill).
* User/bundled skills still shadow a plugin skill on id collision.
* Bundled + hub skills are unaffected (no regression).
"""
from __future__ import annotations

import platform
from pathlib import Path

from opencomputer.agent.memory import MemoryManager

# Real plugin-shipped skill used as the integration anchor.
_PLUGIN_ID = "computer-use"
_PLUGIN_SKILL_ID = "macos-computer-use"


def _make_manager(tmp_path: Path, *, bundled: list[Path] | None = None) -> MemoryManager:
    """Construct a MemoryManager with an isolated user-skills dir.

    ``extensions_path`` is auto-derived from the repo tree, so the real
    ``extensions/<plugin>/skills/`` dirs are scanned without fixtures.
    """
    return MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "skills",
        bundled_skills_paths=[] if bundled is None else bundled,
    )


def _write_skill(skills_dir: Path, skill_id: str) -> None:
    """Write a minimal user/bundled SKILL.md (no ``requires:``)."""
    d = skills_dir / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: test skill\n---\nbody\n"
    )


# ─── fourth-root discovery ─────────────────────────────────────────────


def test_extensions_path_is_derived(tmp_path):
    """The MemoryManager locates ``extensions/`` as a sibling of ``opencomputer/``."""
    mm = _make_manager(tmp_path)
    assert mm.extensions_path is not None
    assert mm.extensions_path.name == "extensions"
    assert (mm.extensions_path / _PLUGIN_ID / "skills" / _PLUGIN_SKILL_ID).is_dir()


def test_plugin_shipped_skill_is_discovered(tmp_path):
    """A real plugin-bundled skill is found by list_skills()."""
    mm = _make_manager(tmp_path)
    by_id = {s.id: s for s in mm.list_skills()}
    assert _PLUGIN_SKILL_ID in by_id
    skill = by_id[_PLUGIN_SKILL_ID]
    # The skill carries the synthesized plugin requirement.
    assert _PLUGIN_ID in skill.requires.plugins


def test_all_four_plugin_skill_dirs_light_up(tmp_path):
    """computer-use, browser-control, coding-harness, code-modernization
    all ship discoverable skills."""
    mm = _make_manager(tmp_path)
    by_id = {s.id: s for s in mm.list_skills()}
    expected = {
        "macos-computer-use": "computer-use",
        "adapter-author": "browser-control",
        "code-reviewer": "coding-harness",
        "modernize-assess": "code-modernization",
    }
    for skill_id, plugin_id in expected.items():
        assert skill_id in by_id, f"{skill_id} not discovered"
        assert plugin_id in by_id[skill_id].requires.plugins


# ─── plugin-gating: enabled ────────────────────────────────────────────


def test_skill_eligible_when_owning_plugin_installed(tmp_path):
    """Owning plugin in installed_plugin_ids → no unmet plugin requirement."""
    mm = _make_manager(tmp_path)
    by_id = {
        s.id: s
        for s in mm.list_skills(installed_plugin_ids=frozenset({_PLUGIN_ID}))
    }
    skill = by_id[_PLUGIN_SKILL_ID]
    # No "plugin:" unmet tag — the plugin gate is satisfied.
    assert not any(r.startswith("plugin:") for r in skill.unmet_requirements)


# ─── plugin-gating: disabled ───────────────────────────────────────────


def test_skill_visible_but_gated_when_plugin_disabled(tmp_path):
    """Owning plugin NOT installed → skill still in scan (visible) but flagged
    unmet, so the prompt-eligible set drops it."""
    mm = _make_manager(tmp_path)
    all_skills = mm.list_skills(
        installed_plugin_ids=frozenset({"some-unrelated-plugin"})
    )
    by_id = {s.id: s for s in all_skills}
    # Still visible in the listing.
    assert _PLUGIN_SKILL_ID in by_id
    skill = by_id[_PLUGIN_SKILL_ID]
    # Flagged unmet on the plugin gate.
    assert f"plugin:{_PLUGIN_ID}" in skill.unmet_requirements
    # Prompt-eligible set (mirrors the agent loop's filter at loop.py
    # ~2115: `skills = [s for s in _all_skills if not s.unmet_requirements]`).
    prompt_eligible = {s.id for s in all_skills if not s.unmet_requirements}
    assert _PLUGIN_SKILL_ID not in prompt_eligible


def test_plugin_gate_skipped_when_index_unknown(tmp_path):
    """installed_plugin_ids=None → conservative: plugin gate NOT applied,
    so a fresh shell that hasn't loaded the plugin index still shows the
    skill as prompt-eligible (matches existing requires: posture)."""
    mm = _make_manager(tmp_path)
    by_id = {s.id: s for s in mm.list_skills()}
    skill = by_id[_PLUGIN_SKILL_ID]
    assert not any(r.startswith("plugin:") for r in skill.unmet_requirements)


# ─── shadowing ─────────────────────────────────────────────────────────


def test_user_skill_shadows_plugin_skill(tmp_path):
    """A user skill with the same id shadows the plugin-shipped one — plugin
    roots are appended last, so higher-priority roots win on id collision."""
    user_skills = tmp_path / "skills"
    user_skills.mkdir(parents=True, exist_ok=True)
    _write_skill(user_skills, _PLUGIN_SKILL_ID)

    mm = _make_manager(tmp_path)
    matches = [s for s in mm.list_skills() if s.id == _PLUGIN_SKILL_ID]
    # Exactly one entry — the user one shadows the plugin one.
    assert len(matches) == 1
    skill = matches[0]
    # The user skill declares no `requires:` and is NOT under a plugin
    # root, so it carries no synthesized plugin requirement.
    assert skill.requires.plugins == ()
    assert skill.path.parent == user_skills / _PLUGIN_SKILL_ID


def test_bundled_skill_shadows_plugin_skill(tmp_path):
    """A bundled skill with the same id also shadows the plugin one
    (bundled roots precede plugin roots)."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_skill(bundled, _PLUGIN_SKILL_ID)

    mm = _make_manager(tmp_path, bundled=[bundled])
    matches = [s for s in mm.list_skills() if s.id == _PLUGIN_SKILL_ID]
    assert len(matches) == 1
    assert matches[0].requires.plugins == ()
    assert matches[0].path.parent == bundled / _PLUGIN_SKILL_ID


# ─── no regression: bundled + hub still discovered ─────────────────────


def test_bundled_and_hub_skills_still_discovered(tmp_path):
    """Adding the fourth root does not disturb user/bundled/hub roots."""
    user_skills = tmp_path / "skills"
    user_skills.mkdir(parents=True, exist_ok=True)
    _write_skill(user_skills, "my-user-skill")

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_skill(bundled, "my-bundled-skill")

    # Hub install: <skills_path>/.hub/<source>/<skill-name>/SKILL.md
    hub_source = user_skills / ".hub" / "some-source"
    hub_source.mkdir(parents=True, exist_ok=True)
    _write_skill(hub_source, "my-hub-skill")

    mm = _make_manager(tmp_path, bundled=[bundled])
    ids = {s.id for s in mm.list_skills()}
    assert {"my-user-skill", "my-bundled-skill", "my-hub-skill"} <= ids
    # And the plugin root is still active alongside them.
    assert _PLUGIN_SKILL_ID in ids


def test_real_bundled_corpus_still_loads(tmp_path):
    """With the real bundled skills root, the core corpus still scans
    cleanly next to plugin skills."""
    bundled = Path(__file__).resolve().parent.parent / "opencomputer" / "skills"
    mm = _make_manager(tmp_path, bundled=[bundled])
    skills = mm.list_skills()
    # Both a known bundled skill and a known plugin skill are present.
    ids = {s.id for s in skills}
    assert "opencomputer-skill-authoring" in ids
    assert _PLUGIN_SKILL_ID in ids


# ─── non-skill files under skills/ are ignored ─────────────────────────


def test_non_skill_files_under_plugin_skills_dir_ignored(tmp_path):
    """coding-harness/skills/ ships .py modules alongside skill dirs; only
    directories with a SKILL.md become skills."""
    mm = _make_manager(tmp_path)
    ids = {s.id for s in mm.list_skills()}
    # The real skill dirs are present.
    assert {"code-reviewer", "refactorer", "test-runner"} <= ids
    # Non-skill module file names never appear as skill ids.
    assert "registry" not in ids
    assert "__init__" not in ids
    assert "activation" not in ids


def test_macos_computer_use_gating_matches_platform(tmp_path):
    """On macOS the plugin-enabled scan leaves macos-computer-use fully
    eligible; the synthesized plugin gate is the only requirement (the
    SKILL.md itself declares none)."""
    mm = _make_manager(tmp_path)
    by_id = {
        s.id: s
        for s in mm.list_skills(installed_plugin_ids=frozenset({_PLUGIN_ID}))
    }
    skill = by_id[_PLUGIN_SKILL_ID]
    if platform.system().lower() == "darwin":
        assert skill.unmet_requirements == ()
