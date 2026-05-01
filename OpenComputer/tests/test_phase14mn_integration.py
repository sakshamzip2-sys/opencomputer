"""Phase 14.M/14.N integration — ProfileConfig + resolver.

Pure resolution tests (no loader yet — that's the next file). These
exercise the ``load_profile_config`` + ``resolve_enabled_plugins``
path end-to-end with preset and workspace-overlay inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.profile_config import (
    ProfileConfig,
    ProfileConfigError,
    load_profile_config,
    profile_config_path,
    resolve_enabled_plugins,
)
from opencomputer.agent.workspace import WorkspaceOverlay

# ── load_profile_config ───────────────────────────────────────────────────


def test_missing_profile_yaml_returns_defaults(tmp_path: Path):
    cfg = load_profile_config(tmp_path)
    assert cfg.preset is None
    assert cfg.enabled_plugins == "*"


def test_empty_profile_yaml_returns_defaults(tmp_path: Path):
    profile_config_path(tmp_path).write_text("")
    cfg = load_profile_config(tmp_path)
    assert cfg.preset is None
    assert cfg.enabled_plugins == "*"


def test_preset_only(tmp_path: Path):
    profile_config_path(tmp_path).write_text("preset: coding\n")
    cfg = load_profile_config(tmp_path)
    assert cfg.preset == "coding"
    assert cfg.enabled_plugins == "*"


def test_inline_enabled_list(tmp_path: Path):
    profile_config_path(tmp_path).write_text("plugins:\n  enabled: [code-review, repomap]\n")
    cfg = load_profile_config(tmp_path)
    assert cfg.preset is None
    assert cfg.enabled_plugins == frozenset({"code-review", "repomap"})


def test_inline_enabled_wildcard(tmp_path: Path):
    profile_config_path(tmp_path).write_text("plugins:\n  enabled: '*'\n")
    cfg = load_profile_config(tmp_path)
    assert cfg.enabled_plugins == "*"


def test_both_preset_and_enabled_raises(tmp_path: Path):
    profile_config_path(tmp_path).write_text("preset: coding\nplugins:\n  enabled: [code-review]\n")
    with pytest.raises(ProfileConfigError, match="both"):
        load_profile_config(tmp_path)


def test_rejects_unknown_top_level(tmp_path: Path):
    profile_config_path(tmp_path).write_text("preset: coding\nrogue: true\n")
    with pytest.raises(ProfileConfigError, match="unknown"):
        load_profile_config(tmp_path)


def test_rejects_non_mapping(tmp_path: Path):
    profile_config_path(tmp_path).write_text("- a\n- b\n")
    with pytest.raises(ProfileConfigError, match="mapping"):
        load_profile_config(tmp_path)


def test_plugins_must_be_mapping(tmp_path: Path):
    profile_config_path(tmp_path).write_text("plugins: [a, b]\n")
    with pytest.raises(ProfileConfigError, match="mapping"):
        load_profile_config(tmp_path)


def test_plugins_enabled_must_be_list_or_star(tmp_path: Path):
    profile_config_path(tmp_path).write_text("plugins:\n  enabled: 42\n")
    with pytest.raises(ProfileConfigError, match="list or"):
        load_profile_config(tmp_path)


def test_plugins_enabled_items_must_be_strings(tmp_path: Path):
    profile_config_path(tmp_path).write_text("plugins:\n  enabled: [a, 42]\n")
    with pytest.raises(ProfileConfigError, match="list of strings"):
        load_profile_config(tmp_path)


# ── resolve_enabled_plugins — profile alone ─────────────────────────────


def test_resolve_defaults_to_wildcard():
    out = resolve_enabled_plugins(ProfileConfig())
    assert out.enabled == "*"


def test_resolve_inline_enabled_list():
    cfg = ProfileConfig(enabled_plugins=frozenset({"a", "b"}))
    out = resolve_enabled_plugins(cfg)
    assert out.enabled == frozenset({"a", "b"})


def test_resolve_preset_expands(tmp_path: Path):
    (tmp_path / "coding.yaml").write_text("plugins: [code-review, repomap]\n")
    cfg = ProfileConfig(preset="coding")
    out = resolve_enabled_plugins(cfg, presets_root=tmp_path)
    assert out.enabled == frozenset({"code-review", "repomap"})


def test_resolve_missing_preset_raises(tmp_path: Path):
    cfg = ProfileConfig(preset="ghost")
    with pytest.raises(FileNotFoundError):
        resolve_enabled_plugins(cfg, presets_root=tmp_path)


# ── resolve_enabled_plugins — with workspace overlay ─────────────────────


def test_overlay_preset_overrides_profile_preset(tmp_path: Path):
    (tmp_path / "coding.yaml").write_text("plugins: [c1, c2]\n")
    (tmp_path / "stock.yaml").write_text("plugins: [s1, s2]\n")
    cfg = ProfileConfig(preset="coding")
    overlay = WorkspaceOverlay(preset="stock")
    out = resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)
    assert out.enabled == frozenset({"s1", "s2"})


def test_overlay_preset_overrides_profile_inline_enabled(tmp_path: Path):
    (tmp_path / "stock.yaml").write_text("plugins: [s1]\n")
    cfg = ProfileConfig(enabled_plugins=frozenset({"c1", "c2"}))
    overlay = WorkspaceOverlay(preset="stock")
    out = resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)
    assert out.enabled == frozenset({"s1"})


def test_overlay_additional_unions(tmp_path: Path):
    (tmp_path / "coding.yaml").write_text("plugins: [c1, c2]\n")
    cfg = ProfileConfig(preset="coding")
    overlay = WorkspaceOverlay.model_validate({"plugins": {"additional": ["extra1", "extra2"]}})
    out = resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)
    assert out.enabled == frozenset({"c1", "c2", "extra1", "extra2"})


def test_overlay_additional_on_wildcard_is_noop():
    # "*" means "everything"; adding more cannot mean more than everything.
    cfg = ProfileConfig()  # enabled = "*"
    overlay = WorkspaceOverlay.model_validate({"plugins": {"additional": ["a", "b"]}})
    out = resolve_enabled_plugins(cfg, overlay)
    assert out.enabled == "*"
    # Source string should explain the no-op so it's visible in logs.
    assert "ignored" in out.source


def test_overlay_preset_and_additional_compose(tmp_path: Path):
    (tmp_path / "stock.yaml").write_text("plugins: [s1]\n")
    cfg = ProfileConfig(preset="coding")  # will be overridden
    overlay = WorkspaceOverlay.model_validate(
        {"preset": "stock", "plugins": {"additional": ["s2"]}}
    )
    out = resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)
    assert out.enabled == frozenset({"s1", "s2"})


def test_overlay_preset_missing_raises(tmp_path: Path):
    cfg = ProfileConfig()
    overlay = WorkspaceOverlay(preset="ghost")
    with pytest.raises(FileNotFoundError):
        resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)


# ── Source trail for operational logging ─────────────────────────────────


def test_source_trail_mentions_preset(tmp_path: Path):
    (tmp_path / "p.yaml").write_text("plugins: [a]\n")
    cfg = ProfileConfig(preset="p")
    out = resolve_enabled_plugins(cfg, presets_root=tmp_path)
    assert "preset 'p'" in out.source


def test_source_trail_mentions_overlay_override(tmp_path: Path):
    (tmp_path / "p.yaml").write_text("plugins: [a]\n")
    (tmp_path / "q.yaml").write_text("plugins: [b]\n")
    cfg = ProfileConfig(preset="p")
    overlay = WorkspaceOverlay(preset="q")
    out = resolve_enabled_plugins(cfg, overlay, presets_root=tmp_path)
    assert "overlay preset 'q'" in out.source
    assert "overrode base" in out.source


# ── Loader filter — unit level (mocked load to avoid global state) ───────


def _fake_candidates_for(ids: list[str]) -> list:
    """Build a minimal list of PluginCandidates for filter-only tests.

    Avoids actually importing plugin code (which would touch the global
    tool/hook registries across tests and collide).
    """
    from opencomputer.plugins.discovery import PluginCandidate
    from plugin_sdk.core import PluginManifest

    out = []
    for pid in ids:
        m = PluginManifest(
            id=pid,
            name=pid,
            version="0.1.0",
            description="",
            author="",
            homepage="",
            license="MIT",
            kind="tool",
            entry="plugin",
        )
        out.append(
            PluginCandidate(
                manifest=m,
                root_dir=Path("/nonexistent"),
                manifest_path=Path("/nonexistent/plugin.json"),
            )
        )
    return out


def _patch_discovery(monkeypatch, candidates: list) -> None:
    """Patch discover() + load_plugin() to be side-effect-free."""
    from opencomputer.plugins import registry as registry_module
    from opencomputer.plugins.loader import LoadedPlugin

    monkeypatch.setattr(registry_module, "discover", lambda _paths: candidates)
    monkeypatch.setattr(
        registry_module,
        "load_plugin",
        lambda cand, _api: LoadedPlugin(candidate=cand, module=None),
    )


def test_loader_none_filter_loads_everything(monkeypatch: pytest.MonkeyPatch):
    """Backward compat: no filter -> same behaviour as before Phase 14.M."""
    from opencomputer.plugins.registry import PluginRegistry

    _patch_discovery(monkeypatch, _fake_candidates_for(["a", "b", "c"]))
    reg = PluginRegistry()
    reg.load_all([Path("/fake")])
    assert {lp.candidate.manifest.id for lp in reg.loaded} == {"a", "b", "c"}


def test_loader_star_filter_loads_everything(monkeypatch: pytest.MonkeyPatch):
    """Sentinel "*" behaves identically to None."""
    from opencomputer.plugins.registry import PluginRegistry

    _patch_discovery(monkeypatch, _fake_candidates_for(["a", "b", "c"]))
    reg = PluginRegistry()
    reg.load_all([Path("/fake")], enabled_ids="*")
    assert {lp.candidate.manifest.id for lp in reg.loaded} == {"a", "b", "c"}


def test_loader_frozenset_filter_loads_subset(monkeypatch: pytest.MonkeyPatch):
    """Only named ids get loaded."""
    from opencomputer.plugins.registry import PluginRegistry

    _patch_discovery(monkeypatch, _fake_candidates_for(["a", "b", "c", "d"]))
    reg = PluginRegistry()
    reg.load_all([Path("/fake")], enabled_ids=frozenset({"a", "c"}))
    assert {lp.candidate.manifest.id for lp in reg.loaded} == {"a", "c"}


def test_loader_empty_filter_loads_nothing(monkeypatch: pytest.MonkeyPatch):
    """Empty frozenset -> no plugins. (Distinct from None, which loads all.)"""
    from opencomputer.plugins.registry import PluginRegistry

    _patch_discovery(monkeypatch, _fake_candidates_for(["a", "b"]))
    reg = PluginRegistry()
    reg.load_all([Path("/fake")], enabled_ids=frozenset())
    assert reg.loaded == []


# ── Doctor M/N checks ────────────────────────────────────────────────────


@pytest.fixture
def iso_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME + CWD so doctor reads a clean environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    # CWD must be somewhere with no ancestor .opencomputer/ — tmp_path
    # is fresh so its parents won't have one.
    monkeypatch.chdir(tmp_path)
    return home


def _run_profile_and_overlay_checks():
    from opencomputer.doctor import _check_profile_and_overlay

    return _check_profile_and_overlay()


def test_doctor_skip_when_no_profile_and_no_overlay(iso_home: Path):
    checks = _run_profile_and_overlay_checks()
    statuses = {c.name: c.status for c in checks}
    # profile-preset skip (no preset referenced); workspace-overlay skip
    # (no .opencomputer/ in CWD tree).
    assert statuses.get("profile preset") == "skip"
    assert statuses.get("workspace overlay") == "skip"


def test_doctor_pass_when_profile_preset_exists(iso_home: Path):
    # Set up .opencomputer/ with profile.yaml referencing an existing preset.
    oc = iso_home / ".opencomputer"
    oc.mkdir()
    (oc / "profile.yaml").write_text("preset: coding\n")
    presets = oc / "presets"
    presets.mkdir()
    (presets / "coding.yaml").write_text("plugins: [code-review]\n")

    checks = _run_profile_and_overlay_checks()
    profile_check = next(c for c in checks if c.name == "profile preset")
    assert profile_check.status == "pass"
    assert "coding" in profile_check.detail


def test_doctor_fail_when_profile_preset_missing(iso_home: Path):
    oc = iso_home / ".opencomputer"
    oc.mkdir()
    (oc / "profile.yaml").write_text("preset: ghost\n")

    checks = _run_profile_and_overlay_checks()
    profile_check = next(c for c in checks if c.name == "profile preset")
    assert profile_check.status == "fail"
    assert "ghost" in profile_check.detail


def test_doctor_fail_when_profile_yaml_malformed(iso_home: Path):
    oc = iso_home / ".opencomputer"
    oc.mkdir()
    (oc / "profile.yaml").write_text("preset: coding\nplugins:\n  enabled: [x]\n")

    checks = _run_profile_and_overlay_checks()
    # The malformed-profile check produces "profile.yaml" status=fail.
    yaml_check = next(c for c in checks if c.name == "profile.yaml")
    assert yaml_check.status == "fail"
    assert "both" in yaml_check.detail.lower()


def test_doctor_pass_when_overlay_preset_exists(
    iso_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Install a preset.
    oc = iso_home / ".opencomputer"
    presets = oc / "presets"
    presets.mkdir(parents=True)
    (presets / "stock.yaml").write_text("plugins: [s1]\n")

    # Put a workspace overlay in CWD referencing it.
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".opencomputer").mkdir()
    (project / ".opencomputer" / "config.yaml").write_text("preset: stock\n")
    monkeypatch.chdir(project)

    checks = _run_profile_and_overlay_checks()
    ov = next(c for c in checks if c.name == "workspace overlay")
    assert ov.status == "pass"
    assert "stock" in ov.detail


def test_doctor_fail_when_overlay_preset_missing(
    iso_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".opencomputer").mkdir()
    (project / ".opencomputer" / "config.yaml").write_text("preset: ghost\n")
    monkeypatch.chdir(project)

    checks = _run_profile_and_overlay_checks()
    ov = next(c for c in checks if c.name == "workspace overlay")
    assert ov.status == "fail"
    assert "ghost" in ov.detail.lower()


def test_doctor_fail_when_overlay_malformed(
    iso_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".opencomputer").mkdir()
    (project / ".opencomputer" / "config.yaml").write_text("rogue_field: whatever\n")
    monkeypatch.chdir(project)

    checks = _run_profile_and_overlay_checks()
    ov = next(c for c in checks if c.name == "workspace overlay")
    assert ov.status == "fail"
