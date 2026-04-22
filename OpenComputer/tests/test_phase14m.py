"""Phase 14.M — named presets + ``opencomputer preset`` CLI.

Standalone tests: these exercise the preset model + CLI directly,
without depending on zesty's 14.A-E profile infrastructure
(``ProfileConfig``, loader integration). Those integration tests belong
in whichever phase wires ``profile.yaml`` -> ``resolve_enabled_plugins``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opencomputer.cli_preset import preset_app
from opencomputer.plugins.preset import (
    Preset,
    list_presets,
    load_preset,
    write_preset,
)

# ── Preset model ──────────────────────────────────────────────────────────


def test_preset_model_accepts_valid_list():
    p = Preset(plugins=["code-review", "repomap", "anthropic-provider"])
    assert p.plugins == ["code-review", "repomap", "anthropic-provider"]


def test_preset_model_accepts_empty_list():
    # An empty preset is valid — "this profile uses no plugins".
    p = Preset(plugins=[])
    assert p.plugins == []


def test_preset_model_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate"):
        Preset(plugins=["a", "b", "a"])


def test_preset_model_rejects_invalid_id_uppercase():
    with pytest.raises(ValueError, match="valid id"):
        Preset(plugins=["GoodPlugin"])


def test_preset_model_rejects_invalid_id_space():
    with pytest.raises(ValueError, match="valid id"):
        Preset(plugins=["good plugin"])


def test_preset_model_rejects_extra_fields():
    # extra="forbid" — typos like `plugns:` should not silently work.
    with pytest.raises(ValueError):
        Preset.model_validate({"plugins": ["a"], "plugns": ["b"]})


# ── Load / list / write ────────────────────────────────────────────────────


def test_load_preset_reads_yaml(tmp_path: Path):
    (tmp_path / "coding.yaml").write_text("plugins: [code-review, repomap]\n")
    p = load_preset("coding", root=tmp_path)
    assert p.plugins == ["code-review", "repomap"]


def test_load_preset_missing_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_preset("ghost", root=tmp_path)


def test_load_preset_rejects_non_mapping_top_level(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        load_preset("bad", root=tmp_path)


def test_load_preset_empty_file_parses_as_empty_model(tmp_path: Path):
    # Empty YAML -> None -> {} -> Preset requires plugins, so should fail.
    (tmp_path / "empty.yaml").write_text("")
    with pytest.raises(ValueError):
        load_preset("empty", root=tmp_path)


def test_list_presets_empty_dir_returns_empty(tmp_path: Path):
    assert list_presets(root=tmp_path) == []


def test_list_presets_returns_sorted(tmp_path: Path):
    (tmp_path / "zeta.yaml").write_text("plugins: []\n")
    (tmp_path / "alpha.yaml").write_text("plugins: []\n")
    (tmp_path / "mu.yaml").write_text("plugins: []\n")
    # Non-yaml files are ignored.
    (tmp_path / "README.md").write_text("ignore me")
    assert list_presets(root=tmp_path) == ["alpha", "mu", "zeta"]


def test_list_presets_skips_subdirs(tmp_path: Path):
    (tmp_path / "real.yaml").write_text("plugins: []\n")
    (tmp_path / "a-dir").mkdir()
    assert list_presets(root=tmp_path) == ["real"]


def test_write_preset_writes_valid_yaml(tmp_path: Path):
    path = write_preset("coding", ["code-review", "repomap"], root=tmp_path)
    assert path == tmp_path / "coding.yaml"
    parsed = yaml.safe_load(path.read_text())
    assert parsed == {"plugins": ["code-review", "repomap"]}


def test_write_preset_refuses_overwrite_by_default(tmp_path: Path):
    write_preset("x", ["a"], root=tmp_path)
    with pytest.raises(FileExistsError):
        write_preset("x", ["b"], root=tmp_path)


def test_write_preset_allows_overwrite_with_flag(tmp_path: Path):
    write_preset("x", ["a"], root=tmp_path)
    write_preset("x", ["b"], root=tmp_path, overwrite=True)
    assert load_preset("x", root=tmp_path).plugins == ["b"]


def test_write_preset_validates_before_writing(tmp_path: Path):
    # Bad input must NEVER leave a partial file behind.
    with pytest.raises(ValueError):
        write_preset("bad", ["Uppercase"], root=tmp_path)
    assert not (tmp_path / "bad.yaml").exists()


def test_write_preset_creates_parent_dir(tmp_path: Path):
    root = tmp_path / "not-yet"
    write_preset("x", ["a"], root=root)
    assert (root / "x.yaml").exists()


# ── Prompt-cache stability ────────────────────────────────────────────────


def test_preset_load_is_deterministic(tmp_path: Path):
    """Same preset file loaded twice -> byte-identical plugin list.

    This is correctness-critical for the prompt cache: non-deterministic
    ordering in tools/plugins invalidates the prefix cache across turns.
    """
    (tmp_path / "p.yaml").write_text("plugins: [gamma, alpha, delta, beta]\n")
    a = load_preset("p", root=tmp_path).plugins
    b = load_preset("p", root=tmp_path).plugins
    assert a == b
    # Preserves author-declared order — resolution-time sorting happens
    # in the LOADER (14.D integration), not in Preset itself.
    assert a == ["gamma", "alpha", "delta", "beta"]


# ── CLI ────────────────────────────────────────────────────────────────────


@pytest.fixture
def preset_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``presets_dir()`` at the global filesystem level."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Pydantic/yaml side don't cache; ensure Path.home() reads env.
    return fake_home / ".opencomputer" / "presets"


def test_cli_create(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["create", "coding", "--plugins", "code-review,repomap"])
    assert result.exit_code == 0, result.output
    body = yaml.safe_load((preset_home / "coding.yaml").read_text())
    assert body == {"plugins": ["code-review", "repomap"]}


def test_cli_create_refuses_overwrite_without_force(preset_home: Path):
    runner = CliRunner()
    runner.invoke(preset_app, ["create", "x", "--plugins", "a"])
    result = runner.invoke(preset_app, ["create", "x", "--plugins", "b"])
    assert result.exit_code != 0
    assert "exists" in result.output.lower() or "force" in result.output.lower()


def test_cli_create_allows_overwrite_with_force(preset_home: Path):
    runner = CliRunner()
    runner.invoke(preset_app, ["create", "x", "--plugins", "a"])
    result = runner.invoke(preset_app, ["create", "x", "--plugins", "b", "--force"])
    assert result.exit_code == 0, result.output
    body = yaml.safe_load((preset_home / "x.yaml").read_text())
    assert body == {"plugins": ["b"]}


def test_cli_list(preset_home: Path):
    preset_home.mkdir(parents=True)
    (preset_home / "coding.yaml").write_text("plugins: [a]\n")
    (preset_home / "stock.yaml").write_text("plugins: [b]\n")
    runner = CliRunner()
    result = runner.invoke(preset_app, ["list"])
    assert result.exit_code == 0
    assert "coding" in result.output
    assert "stock" in result.output


def test_cli_list_empty_dir(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["list"])
    assert result.exit_code == 0
    assert "no presets" in result.output.lower()


def test_cli_show(preset_home: Path):
    preset_home.mkdir(parents=True)
    (preset_home / "coding.yaml").write_text("plugins: [code-review, repomap]\n")
    runner = CliRunner()
    result = runner.invoke(preset_app, ["show", "coding"])
    assert result.exit_code == 0
    assert "code-review" in result.output
    assert "repomap" in result.output


def test_cli_show_missing(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["show", "ghost"])
    assert result.exit_code != 0


def test_cli_delete(preset_home: Path):
    preset_home.mkdir(parents=True)
    (preset_home / "gone.yaml").write_text("plugins: [a]\n")
    runner = CliRunner()
    result = runner.invoke(preset_app, ["delete", "gone", "--yes"])
    assert result.exit_code == 0
    assert not (preset_home / "gone.yaml").exists()


def test_cli_delete_missing(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["delete", "ghost", "--yes"])
    assert result.exit_code != 0


def test_cli_where(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["where"])
    assert result.exit_code == 0
    assert ".opencomputer/presets" in result.output


def test_cli_where_named(preset_home: Path):
    runner = CliRunner()
    result = runner.invoke(preset_app, ["where", "coding"])
    assert result.exit_code == 0
    assert "coding.yaml" in result.output
