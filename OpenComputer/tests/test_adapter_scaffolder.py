"""Tests for opencomputer adapter — scaffolder + capabilities CLI.

G.10 / Tier 2.16: discoverable channel-adapter scaffolding. The underlying
template render is from Sub-project B (already covered in
``test_phase12b2_plugin_scaffold.py``); this file tests the alias surface
+ the G.2 ChannelCapabilities content the channel template now emits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_adapter import adapter_app


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCapabilitiesCommand:
    def test_lists_all_caps(self, runner: CliRunner) -> None:
        result = runner.invoke(adapter_app, ["capabilities"])
        assert result.exit_code == 0
        # All non-NONE caps should appear
        for cap in (
            "TYPING",
            "REACTIONS",
            "VOICE_OUT",
            "VOICE_IN",
            "PHOTO_OUT",
            "PHOTO_IN",
            "DOCUMENT_OUT",
            "DOCUMENT_IN",
            "EDIT_MESSAGE",
            "DELETE_MESSAGE",
            "THREADS",
        ):
            assert cap in result.stdout, f"{cap!r} missing from `adapter capabilities` output"

    def test_method_names_appear(self, runner: CliRunner) -> None:
        """Each cap should advertise the method an author needs to override."""
        result = runner.invoke(adapter_app, ["capabilities"])
        assert result.exit_code == 0
        for method in (
            "send_typing",
            "send_reaction",
            "send_photo",
            "send_document",
            "send_voice",
            "edit_message",
            "delete_message",
            "download_attachment",
        ):
            assert method in result.stdout, f"{method!r} missing"


class TestNewCommandScaffolds:
    def test_new_creates_plugin_directory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            adapter_app, ["new", "test-channel-x", "--path", str(tmp_path)]
        )
        # Adapter scaffold should create the plugin dir
        assert result.exit_code == 0, f"stdout={result.stdout!r}"
        plugin_dir = tmp_path / "test-channel-x"
        assert plugin_dir.exists()
        assert (plugin_dir / "plugin.json").exists()
        assert (plugin_dir / "plugin.py").exists()
        assert (plugin_dir / "adapter.py").exists()


class TestNewCommandTemplate:
    """Verify the channel template now includes G.2 ChannelCapabilities."""

    def test_template_imports_channel_capabilities(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            adapter_app, ["new", "caps-check", "--path", str(tmp_path)]
        )
        assert result.exit_code == 0
        adapter_py = (tmp_path / "caps-check" / "adapter.py").read_text()
        assert "ChannelCapabilities" in adapter_py

    def test_template_default_caps_is_none(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(adapter_app, ["new", "default-caps", "--path", str(tmp_path)])
        assert result.exit_code == 0
        adapter_py = (tmp_path / "default-caps" / "adapter.py").read_text()
        assert "capabilities = ChannelCapabilities.NONE" in adapter_py

    def test_template_has_optional_method_stubs(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Each optional capability should have a commented-out method stub
        in the template so authors know what to uncomment."""
        result = runner.invoke(adapter_app, ["new", "stubs-check", "--path", str(tmp_path)])
        assert result.exit_code == 0
        adapter_py = (tmp_path / "stubs-check" / "adapter.py").read_text()
        for stub in (
            "send_typing",
            "send_reaction",
            "send_photo",
            "send_document",
            "send_voice",
            "edit_message",
            "delete_message",
            "download_attachment",
        ):
            assert stub in adapter_py, f"channel template missing {stub!r} stub"

    def test_template_class_name_pascal_cased(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(adapter_app, ["new", "my-thing", "--path", str(tmp_path)])
        assert result.exit_code == 0
        adapter_py = (tmp_path / "my-thing" / "adapter.py").read_text()
        assert "class MyThingAdapter" in adapter_py
