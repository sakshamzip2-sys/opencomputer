"""Phase 14.N — workspace ``.opencomputer/config.yaml`` overlay.

Standalone tests for discovery + parsing + shape. Merge-into-loader
integration is zesty 14.D's job; those tests live in whichever
phase does the wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.workspace import (
    WorkspaceOverlay,
    find_workspace_overlay,
)

# ── Walk semantics ────────────────────────────────────────────────────────


def test_no_overlay_returns_none(tmp_path: Path):
    assert find_workspace_overlay(start=tmp_path) is None


def test_overlay_in_cwd_found(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("preset: coding\n")
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.preset == "coding"


def test_overlay_in_ancestor_found(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("preset: stock\n")
    overlay = find_workspace_overlay(start=nested)
    assert overlay is not None
    assert overlay.preset == "stock"


def test_nearest_overlay_wins(tmp_path: Path):
    # Outer overlay says one thing, inner says another. Inner must win.
    inner = tmp_path / "inner"
    inner.mkdir()
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("preset: outer\n")
    (inner / ".opencomputer").mkdir()
    (inner / ".opencomputer" / "config.yaml").write_text("preset: inner\n")
    overlay = find_workspace_overlay(start=inner)
    assert overlay is not None
    assert overlay.preset == "inner"


def test_source_path_populated(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    p = tmp_path / ".opencomputer" / "config.yaml"
    p.write_text("preset: x\n")
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.source_path == p.resolve()


# ── Shape validation ──────────────────────────────────────────────────────


def test_overlay_accepts_preset_only(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("preset: coding\n")
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.preset == "coding"
    assert overlay.plugins.additional == []
    assert overlay.env == {}


def test_overlay_accepts_additional_plugins(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text(
        "plugins:\n  additional:\n    - project-lint\n    - project-docs\n"
    )
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.plugins.additional == ["project-lint", "project-docs"]


def test_overlay_accepts_env(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text(
        "env:\n  OPENCOMPUTER_LOG_LEVEL: debug\n"
    )
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.env == {"OPENCOMPUTER_LOG_LEVEL": "debug"}


def test_overlay_rejects_unknown_top_level_field(tmp_path: Path):
    # extra="forbid" — typos like `preseet:` should fail loudly.
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("preset: coding\nrogue_field: oops\n")
    with pytest.raises(ValueError):
        find_workspace_overlay(start=tmp_path)


def test_overlay_rejects_unknown_plugins_subfield(tmp_path: Path):
    # additional is the only allowed child; `enabled:` here would be
    # mistaken for profile-level config.
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("plugins:\n  enabled: [a, b]\n")
    with pytest.raises(ValueError):
        find_workspace_overlay(start=tmp_path)


def test_overlay_rejects_profile_field(tmp_path: Path):
    # Overlay cannot set `profile:` — that's the pre-import flag
    # routing's responsibility. Must fail loudly so users don't assume
    # it works.
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("profile: coder\n")
    with pytest.raises(ValueError):
        find_workspace_overlay(start=tmp_path)


def test_overlay_rejects_non_mapping_top_level(tmp_path: Path):
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        find_workspace_overlay(start=tmp_path)


def test_overlay_empty_file_parses_as_empty(tmp_path: Path):
    # Empty YAML -> None -> {} -> all defaults.
    (tmp_path / ".opencomputer").mkdir()
    (tmp_path / ".opencomputer" / "config.yaml").write_text("")
    overlay = find_workspace_overlay(start=tmp_path)
    assert overlay is not None
    assert overlay.preset is None
    assert overlay.plugins.additional == []
    assert overlay.env == {}


# ── Pydantic model direct tests (no filesystem) ──────────────────────────


def test_model_rejects_extra_at_root():
    with pytest.raises(ValueError):
        WorkspaceOverlay.model_validate({"rogue": True})


def test_model_accepts_all_whitelisted_fields():
    o = WorkspaceOverlay.model_validate(
        {
            "preset": "coding",
            "plugins": {"additional": ["a", "b"]},
            "env": {"K": "V"},
        }
    )
    assert o.preset == "coding"
    assert o.plugins.additional == ["a", "b"]
    assert o.env == {"K": "V"}


def test_home_opencomputer_is_never_treated_as_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """$HOME/.opencomputer/config.yaml is the MAIN config, not an overlay.

    Without this guard, walking up from any subdir of $HOME would hit
    ~/.opencomputer/config.yaml and try to parse it as an overlay,
    failing ``extra=forbid`` on all the main-config fields.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".opencomputer").mkdir()
    # Realistic main-config content — would fail WorkspaceOverlay validation.
    (fake_home / ".opencomputer" / "config.yaml").write_text(
        "model:\n  provider: anthropic\nloop:\n  max_iterations: 50\n"
    )
    monkeypatch.setenv("HOME", str(fake_home))

    # Walking up from a subdir of fake_home must NOT pick up the main config.
    subdir = fake_home / "projects" / "p"
    subdir.mkdir(parents=True)
    assert find_workspace_overlay(start=subdir) is None

    # Walking from fake_home itself: same guard — its .opencomputer/ is home.
    assert find_workspace_overlay(start=fake_home) is None


def test_project_overlay_still_wins_inside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The HOME guard must not block a legitimate per-project overlay."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".opencomputer").mkdir()
    (fake_home / ".opencomputer" / "config.yaml").write_text("model:\n  provider: anthropic\n")
    monkeypatch.setenv("HOME", str(fake_home))

    project = fake_home / "proj"
    project.mkdir()
    (project / ".opencomputer").mkdir()
    (project / ".opencomputer" / "config.yaml").write_text("preset: stock\n")

    overlay = find_workspace_overlay(start=project)
    assert overlay is not None
    assert overlay.preset == "stock"
