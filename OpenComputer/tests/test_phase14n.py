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
