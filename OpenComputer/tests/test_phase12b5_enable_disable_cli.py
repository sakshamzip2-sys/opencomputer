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


def test_disable_ordinary_plugin_removes_from_enabled_list(tmp_path, monkeypatch):
    """An ordinary (non-core) plugin is disabled by removal from
    plugins.enabled."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir,
        {"plugins": {"enabled": ["coding-harness", "telegram"]}},
    )

    result = _runner().invoke(plugin_app, ["disable", "telegram"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["plugins"]["enabled"] == ["coding-harness"]


def test_disable_ordinary_plugin_not_enabled_is_friendly_noop(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["telegram"]}})
    before_yaml = _load_profile_yaml(profile_dir)

    result = _runner().invoke(plugin_app, ["disable", "discord"])

    assert result.exit_code == 0, result.stdout
    assert "not enabled" in result.stdout
    after_yaml = _load_profile_yaml(profile_dir)
    assert before_yaml == after_yaml, "YAML content should be unchanged on no-op"


# ─── E.1: unified validation surfaces same errors on the CLI mutator path ───


def test_enable_rejects_plugins_block_not_mapping(tmp_path, monkeypatch):
    """E.1 — the same shape error the agent loop sees must surface on
    `oc plugin enable`. Previously this would silently accept and
    overwrite, now it fails loudly."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": ["a", "b"]})

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "must be a mapping" in result.stdout


def test_enable_rejects_plugins_enabled_wrong_type(tmp_path, monkeypatch):
    """plugins.enabled must be a list-of-strings or '*'."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": 42}})

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "list or" in result.stdout


def test_enable_rejects_plugins_enabled_non_string_items(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["ok", 42]}})

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "list of strings" in result.stdout


def test_enable_rejects_preset_plus_inline_enabled(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir, {"preset": "coding", "plugins": {"enabled": ["x"]}}
    )

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "both" in result.stdout


def test_disable_rejects_plugins_enabled_wrong_type(tmp_path, monkeypatch):
    """Symmetric: disable also runs the unified validator."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": 42}})

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 1
    assert "list or" in result.stdout


def test_enable_rejects_wildcard_when_adding_explicit_id(tmp_path, monkeypatch):
    """plugins.enabled: '*' is the wildcard. Adding an explicit id would
    NARROW the filter (surprising), so the CLI refuses with a clear
    message asking the user to remove the wildcard first."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": "*"}})

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "wildcard" in result.stdout.lower()


def test_enable_rejects_invalid_yaml(tmp_path, monkeypatch):
    """Malformed YAML must not produce a stack trace — friendly error
    that names the file."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    (profile_dir / "profile.yaml").write_text("plugins: {invalid: yaml: structure")

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 1
    assert "invalid yaml" in result.stdout.lower() or "yaml" in result.stdout.lower()


# ─── core-plugin disable / re-enable (Recipe A.2 — always-on trio) ────


def test_disable_core_plugin_writes_to_disabled_list(tmp_path, monkeypatch):
    """A core plugin is always-on, so disabling it writes to
    plugins.disabled (the explicit opt-out), not plugins.enabled."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": ["telegram"]}})

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert data["plugins"]["disabled"] == ["coding-harness"]
    assert data["plugins"]["enabled"] == ["telegram"]


def test_disable_core_plugin_also_drops_it_from_enabled(tmp_path, monkeypatch):
    """If the core plugin was also explicitly in enabled, the
    contradiction is resolved — it lands only in disabled."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir, {"plugins": {"enabled": ["coding-harness", "telegram"]}}
    )

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert "coding-harness" not in data["plugins"]["enabled"]
    assert data["plugins"]["disabled"] == ["coding-harness"]


def test_disable_core_plugin_on_wildcard_errors(tmp_path, monkeypatch):
    """plugins.disabled has no effect on a wildcard filter — refuse
    with a clear instruction rather than silently failing."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(profile_dir, {"plugins": {"enabled": "*"}})

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 1, result.stdout
    assert "wildcard" in result.stdout.lower()


def test_disable_core_plugin_absent_profile_errors(tmp_path, monkeypatch):
    """No profile.yaml == wildcard filter — same refusal."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    assert not (profile_dir / "profile.yaml").exists()

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 1, result.stdout
    assert "wildcard" in result.stdout.lower()


def test_disable_core_plugin_already_disabled_is_noop(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir,
        {"plugins": {"enabled": ["telegram"], "disabled": ["coding-harness"]}},
    )

    result = _runner().invoke(plugin_app, ["disable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    assert "already disabled" in result.stdout.lower()


def test_enable_core_plugin_removes_from_disabled(tmp_path, monkeypatch):
    """Re-enabling a disabled core plugin clears the plugins.disabled
    opt-out (the trio is always-on again); the emptied list is dropped."""
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    _write_profile_yaml(
        profile_dir,
        {"plugins": {"enabled": ["telegram"], "disabled": ["coding-harness"]}},
    )

    result = _runner().invoke(plugin_app, ["enable", "coding-harness"])

    assert result.exit_code == 0, result.stdout
    data = _load_profile_yaml(profile_dir)
    assert "disabled" not in data["plugins"]
    assert "re-enabled" in result.stdout.lower()
