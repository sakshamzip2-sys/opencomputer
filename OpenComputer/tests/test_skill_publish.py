"""Tests for ``oc skill publish`` (Hermes-followup 2026-05-07).

Covers:
- Successful no-tap mode prints next-step instructions.
- Validation failure exits non-zero.
- ``--tap`` mode stages into a registered tap clone.
- ``--tap`` with unregistered tap fails cleanly.
- ``--tap`` target-already-exists fails cleanly (no clobber).
"""

from __future__ import annotations

import json
from pathlib import Path

from opencomputer.cli_skills_hub import do_publish


def _write_valid_skill(skill_dir: Path, name: str = "demo-skill") -> Path:
    """Create a minimally-valid skill directory."""
    target = skill_dir / name
    target.mkdir()
    (target / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: When the user asks for a demo, run this skill to print a hello message.\n"
        "---\n"
        "\n"
        "# Demo Skill\n"
        "\n"
        "Print hello.\n"
    )
    return target


def _patch_hub_root(monkeypatch, hub_root: Path) -> None:
    """Redirect the hub-root resolver so tests use a temp dir."""
    hub_root.mkdir(parents=True, exist_ok=True)
    import opencomputer.cli_skills_hub as mod

    monkeypatch.setattr(mod, "_hub_root", lambda: hub_root)


def test_publish_no_tap_prints_instructions(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    skill_root = tmp_path / "src"
    skill_root.mkdir()
    skill = _write_valid_skill(skill_root)
    _patch_hub_root(monkeypatch, tmp_path / "hub")

    ok = do_publish(str(skill), tap=None)
    assert ok is True
    out = capsys.readouterr().out
    assert "validated" in out
    assert "Next steps" in out


def test_publish_missing_skill_md_fails(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _patch_hub_root(monkeypatch, tmp_path / "hub")

    ok = do_publish(str(empty), tap=None)
    assert ok is False
    assert "SKILL.md not found" in capsys.readouterr().out


def test_publish_validation_errors_fail(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    # Missing required ``name`` field — validator flags as error.
    (bad / "SKILL.md").write_text(
        "---\ndescription: nope\n---\n# whatever\n"
    )
    _patch_hub_root(monkeypatch, tmp_path / "hub")

    ok = do_publish(str(bad), tap=None)
    assert ok is False
    assert "validation" in capsys.readouterr().out.lower()


def test_publish_with_tap_stages_into_clone(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    skill_root = tmp_path / "src"
    skill_root.mkdir()
    skill = _write_valid_skill(skill_root, name="my-skill")
    hub = tmp_path / "hub"
    _patch_hub_root(monkeypatch, hub)

    # Register the tap first.
    taps_path = hub / "taps.json"
    hub.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(json.dumps({"taps": ["alice/skills"]}))

    # Create a fake clone (with a .git/ marker so _resolve_tap_clone accepts it).
    clone = hub / ".taps" / "skills"
    (clone / ".git").mkdir(parents=True)

    ok = do_publish(str(skill), tap="alice/skills")
    assert ok is True
    assert (clone / "skills" / "my-skill" / "SKILL.md").exists()
    out = capsys.readouterr().out
    assert "git add" in out and "git commit" in out


def test_publish_with_unregistered_tap_fails(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    skill_root = tmp_path / "src"
    skill_root.mkdir()
    skill = _write_valid_skill(skill_root)
    hub = tmp_path / "hub"
    _patch_hub_root(monkeypatch, hub)

    # No taps.json — tap is not registered.
    ok = do_publish(str(skill), tap="bob/whatever")
    assert ok is False
    assert "could not locate local clone" in capsys.readouterr().out


def test_publish_with_tap_refuses_clobber(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    skill_root = tmp_path / "src"
    skill_root.mkdir()
    skill = _write_valid_skill(skill_root, name="my-skill")
    hub = tmp_path / "hub"
    _patch_hub_root(monkeypatch, hub)

    taps_path = hub / "taps.json"
    hub.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(json.dumps({"taps": ["alice/skills"]}))

    clone = hub / ".taps" / "skills"
    (clone / ".git").mkdir(parents=True)
    # Pre-create the target so the publish refuses.
    pre_existing = clone / "skills" / "my-skill"
    pre_existing.mkdir(parents=True)
    (pre_existing / "SKILL.md").write_text("# pre-existing")

    ok = do_publish(str(skill), tap="alice/skills")
    assert ok is False
    assert "already exists" in capsys.readouterr().out


def test_publish_path_doesnt_exist(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _patch_hub_root(monkeypatch, tmp_path / "hub")
    ok = do_publish(str(tmp_path / "does-not-exist"), tap=None)
    assert ok is False
    assert "not a directory" in capsys.readouterr().out
