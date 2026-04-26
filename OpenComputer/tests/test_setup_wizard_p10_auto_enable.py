"""Round 2B P-10 — setup wizard auto-enables plugins for selected channels.

After the channel-selection step, the wizard offers to enable any
plugin ids the chosen channels need. Tests cover:

(a) all required plugins already enabled → no prompt fires
(b) needed-but-missing plugin → prompt fires
(c) user accepts → ``cli_plugin.plugin_enable`` is called per id
(d) user declines → no enable call
(e) channel name not in ``_CHANNEL_PLUGIN_MAP`` → quietly ignored

Hard constraint coverage: the wizard MUST NOT download / pip-install
plugins. The implementation only delegates to ``plugin_enable``,
which itself validates against discovered (already-on-disk) plugins.
We assert the call surface is exactly that helper — no network /
fetch hooks.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# _required_plugins_for_channels — pure mapping helper
# ---------------------------------------------------------------------------


class TestRequiredPluginsForChannels:
    def test_known_channels_map_to_plugin_ids(self) -> None:
        from opencomputer.setup_wizard import _required_plugins_for_channels

        result = _required_plugins_for_channels(["telegram", "discord"])
        assert result == {"telegram", "discord"}

    def test_unknown_channel_silently_dropped(self) -> None:
        # (e) — channel name not in the map → ignored, no error.
        from opencomputer.setup_wizard import _required_plugins_for_channels

        result = _required_plugins_for_channels(["telegram", "no-such-channel"])
        assert result == {"telegram"}

    def test_empty_channels_list_is_empty_set(self) -> None:
        from opencomputer.setup_wizard import _required_plugins_for_channels

        assert _required_plugins_for_channels([]) == set()

    def test_home_assistant_alias_routes_to_homeassistant_plugin_id(self) -> None:
        # The bundled plugin's id is ``homeassistant`` (no hyphen) but
        # the user-facing channel name is commonly ``home-assistant``.
        # Both spellings must route to the bundled plugin id.
        from opencomputer.setup_wizard import _required_plugins_for_channels

        assert _required_plugins_for_channels(["home-assistant"]) == {"homeassistant"}
        assert _required_plugins_for_channels(["homeassistant"]) == {"homeassistant"}

    def test_all_mapped_channels_resolve(self) -> None:
        # Sanity guard: every key in the map resolves to a non-empty
        # plugin id. Keeps a future copy-paste typo from silently
        # dropping a channel from the auto-enable flow.
        from opencomputer.setup_wizard import _CHANNEL_PLUGIN_MAP

        assert _CHANNEL_PLUGIN_MAP, "channel→plugin map must not be empty"
        for channel, plugin_id in _CHANNEL_PLUGIN_MAP.items():
            assert isinstance(channel, str) and channel, channel
            assert isinstance(plugin_id, str) and plugin_id, plugin_id


# ---------------------------------------------------------------------------
# _auto_enable_plugins_for_channels — prompt + enable wiring
# ---------------------------------------------------------------------------


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    """Point profile resolution at tmp_path so we never touch ~/."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    return tmp_path


def _write_profile(profile_dir: Path, enabled_ids: list[str]) -> None:
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(
            {"plugins": {"enabled": enabled_ids}}, sort_keys=False
        ),
    )


class TestAutoEnablePromptingFlow:
    def test_a_all_required_already_enabled_no_prompt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # (a) — every needed plugin is in profile.yaml already, so the
        # confirmation prompt MUST NOT fire and plugin_enable MUST NOT
        # be called.
        _isolate_home(tmp_path, monkeypatch)
        _write_profile(tmp_path, ["telegram"])

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=True)
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            _auto_enable_plugins_for_channels(["telegram"])

        confirm_mock.assert_not_called()
        enable_mock.assert_not_called()

    def test_b_missing_plugin_triggers_prompt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # (b) — telegram plugin not enabled → prompt fires.
        _isolate_home(tmp_path, monkeypatch)
        # Profile exists but no plugins.enabled list yet.
        _write_profile(tmp_path, [])

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=False)  # decline so we only test "did prompt fire"
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            _auto_enable_plugins_for_channels(["telegram"])

        assert confirm_mock.call_count == 1
        # Prompt text must mention the plugin id so the user knows
        # what they're agreeing to.
        prompt_text = confirm_mock.call_args[0][0]
        assert "telegram" in prompt_text

    def test_c_user_accepts_calls_enable_per_plugin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # (c) — user says yes → plugin_enable called once per missing id.
        _isolate_home(tmp_path, monkeypatch)
        _write_profile(tmp_path, [])

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=True)
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            _auto_enable_plugins_for_channels(["telegram", "discord"])

        # One confirmation, two enables — sorted order is deterministic.
        confirm_mock.assert_called_once()
        assert enable_mock.call_count == 2
        called_ids = sorted(c.args[0] for c in enable_mock.call_args_list)
        assert called_ids == ["discord", "telegram"]

    def test_d_user_declines_no_enable_called(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # (d) — user declines → no plugin_enable call.
        _isolate_home(tmp_path, monkeypatch)
        _write_profile(tmp_path, [])

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=False)
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            _auto_enable_plugins_for_channels(["telegram"])

        confirm_mock.assert_called_once()
        enable_mock.assert_not_called()

    def test_e_unknown_channel_no_prompt_no_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # (e) — channel name not in map → quietly skipped, no prompt.
        _isolate_home(tmp_path, monkeypatch)
        _write_profile(tmp_path, [])

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=True)
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            # Should not raise.
            _auto_enable_plugins_for_channels(["totally-made-up-channel"])

        confirm_mock.assert_not_called()
        enable_mock.assert_not_called()

    def test_typer_exit_swallowed_per_plugin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # If plugin_enable raises typer.Exit (e.g. unknown id), the
        # wizard must keep going and try the rest. Sub-test of (c)
        # that pins the safety-net behaviour.
        _isolate_home(tmp_path, monkeypatch)
        _write_profile(tmp_path, [])

        import typer

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=True)

        def _enable_side_effect(pid: str) -> None:
            if pid == "discord":
                raise typer.Exit(code=1)
            # telegram returns cleanly

        enable_mock = MagicMock(side_effect=_enable_side_effect)
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            # Must not propagate the typer.Exit out of the wizard.
            _auto_enable_plugins_for_channels(["telegram", "discord"])

        # Both ids were attempted — the discord failure didn't short-circuit.
        assert enable_mock.call_count == 2

    def test_wildcard_enabled_treated_as_all_present(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # ``plugins.enabled: "*"`` means every discovered plugin loads,
        # so the auto-enable prompt has nothing to do — no prompt, no
        # enable call.
        _isolate_home(tmp_path, monkeypatch)
        (tmp_path / "profile.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": "*"}})
        )

        from opencomputer import cli_plugin
        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        confirm_mock = MagicMock(return_value=True)
        enable_mock = MagicMock()
        with patch(
            "opencomputer.setup_wizard.Confirm.ask", confirm_mock
        ), patch.object(cli_plugin, "plugin_enable", enable_mock):
            _auto_enable_plugins_for_channels(["telegram"])

        confirm_mock.assert_not_called()
        enable_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: verify the no-network/no-pip constraint by inspecting
# the helper's import surface. This is a contract guard, not a runtime
# probe — but it pins the design so a future "auto-install" temptation
# leaves a noisy diff.
# ---------------------------------------------------------------------------


class TestNoNetworkInstall:
    def test_helper_does_not_import_pip_or_urllib(self) -> None:
        # Read the source and assert no install-from-network paths
        # snuck in. Crude but explicit. The implementation only
        # delegates to ``cli_plugin.plugin_enable``, which mutates
        # profile.yaml — never the filesystem-as-package manager.
        import inspect

        from opencomputer.setup_wizard import _auto_enable_plugins_for_channels

        src = inspect.getsource(_auto_enable_plugins_for_channels)
        forbidden = ("subprocess", "pip ", "urllib", "requests", "httpx", "shutil")
        for tok in forbidden:
            assert tok not in src, (
                f"_auto_enable_plugins_for_channels must not use {tok!r} "
                "(P-10 hard constraint: do not download plugins)"
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
