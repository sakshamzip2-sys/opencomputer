"""Phase 12b.5 — Sub-project E, Task E4.

Tests for ``opencomputer plugin enable <id>`` / ``plugin disable <id>``
CLI commands.

The enable / disable commands mutate the active profile's
``profile.yaml``, appending / removing ids from the ``plugins.enabled``
list. Both write atomically (tmp + os.replace), validate the id against
discovered plugins (enable only), and preserve any non-plugin top-level
keys already present in profile.yaml.

All tests are isolated via ``OPENCOMPUTER_HOME`` → ``tmp_path`` so we
never touch the user's real ``~/.opencomputer/`` during a test run.

Since the CLI uses ``_home()`` to resolve the active profile dir,
pointing ``OPENCOMPUTER_HOME`` at tmp_path makes tmp_path the profile
root. That matches the default-profile layout (profile.yaml lives at
the root), which is exactly what we want for isolation.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from opencomputer.cli_plugin import plugin_app


def _runner() -> CliRunner:
    return CliRunner()


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    """Point OPENCOMPUTER_HOME at tmp_path so profile.yaml lands there."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Also isolate HOME_ROOT so read_active_profile() doesn't hit user's real tree.
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    return tmp_path


def _write_profile_yaml(profile_dir: Path, data: dict) -> Path:
    path = profile_dir / "profile.yaml"
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    return path


def _load_profile_yaml(profile_dir: Path) -> dict:
    path = profile_dir / "profile.yaml"
    return yaml.safe_load(path.read_text()) or {}


# ─── enable ───────────────────────────────────────────────────────────


def test_enable_unknown_plugin_id_exits_1(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)

    result = _runner().invoke(plugin_app, ["enable", "no-such-plugin-xyz"])

    assert result.exit_code == 1, result.stdout
    assert "unknown plugin id" in result.stdout


def test_enable_writes_to_profile_yaml_when_absent(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    assert not (profile_dir / "profile.yaml").exists()

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["plugins"]["enabled"] == ["coding-harness"]


def test_enable_appends_to_existing_enabled_list(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["telegram"]}})

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["plugins"]["enabled"] == ["telegram", "coding-harness"]


def test_enable_already_enabled_is_friendly_noop(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["coding-harness"]}})
    before = (profile_dir / "profile.yaml").read_bytes()

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    assert "already enabled" in result.stdout
    after = (profile_dir / "profile.yaml").read_bytes()
    assert before == after, "profile.yaml should be byte-unchanged on no-op"


def test_enable_preserves_other_profile_yaml_keys(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    # `description` and `another_field` are not in the profile-config's
    # allowed-extras set but profile.yaml on-disk may have been edited
    # by the user; enable/disable MUST preserve them regardless.
    _write_profile_yaml(
        profile_dir,
        {
            "plugins": {"enabled": []},
            "description": "my profile",
            "another_field": {"x": 1},
        },
    )

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["description"] == "my profile"
    assert data["another_field"] == {"x": 1}
    assert data["plugins"]["enabled"] == ["coding-harness"]


# ─── disable ──────────────────────────────────────────────────────────


def test_disable_removes_from_enabled_list(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir,
        {"plugins": {"enabled": ["coding-harness", "telegram"]}},
    )

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["plugins"]["enabled"] == ["telegram"]


def test_disable_when_not_enabled_is_friendly_noop(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["telegram"]}})
    before_yaml = _load_profile_yaml(profile_dir)

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    assert "not enabled" in result.stdout
    after_yaml = _load_profile_yaml(profile_dir)
    assert before_yaml == after_yaml, "YAML content should be unchanged on no-op"
