"""Tests for G.25 — SetupChannel metadata (Tier 4 OpenClaw port follow-up).

Symmetric to G.23 / G.24 (provider setup) but for channel plugins.
Channel plugins (Telegram, Discord, iMessage, etc.) now declare their
required env vars + signup URLs via manifest, so the setup wizard can
walk a user through any channel without core knowing about it.

Covers:

1. ``SetupChannel`` parses through the manifest schema.
2. ``PluginSetup.channels`` flattens correctly via ``_parse_manifest``.
3. The bundled telegram + discord plugins declare populated channel
   metadata — regression guard.
4. Empty / whitespace env_vars dropped (OpenClaw tolerance pattern).
5. Typo'd field names rejected via extra="forbid".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import PluginSetup, SetupChannel


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


def _write_channel_plugin(
    root: Path,
    plugin_id: str,
    *,
    setup_channels: list[dict] | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "channel",
        "entry": "plugin",
    }
    if setup_channels is not None:
        manifest["setup"] = {"channels": setup_channels}
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------------------
# 1. Schema parses SetupChannel
# ---------------------------------------------------------------------------


class TestChannelSchema:
    def test_minimal_channel(self) -> None:
        schema, err = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "channels": [
                        {"id": "telegram", "env_vars": ["TELEGRAM_BOT_TOKEN"]}
                    ]
                },
            }
        )
        assert schema is not None, err
        assert schema.setup is not None
        assert len(schema.setup.channels) == 1
        ch = schema.setup.channels[0]
        assert ch.id == "telegram"
        assert ch.env_vars == ["TELEGRAM_BOT_TOKEN"]
        assert ch.label == ""
        assert ch.signup_url == ""
        assert ch.requires_user_id is False

    def test_full_channel(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "channels": [
                        {
                            "id": "telegram",
                            "env_vars": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"],
                            "label": "Telegram",
                            "signup_url": "https://t.me/BotFather",
                            "requires_user_id": True,
                        }
                    ]
                },
            }
        )
        assert schema is not None
        ch = schema.setup.channels[0]
        assert ch.label == "Telegram"
        assert ch.requires_user_id is True
        assert ch.env_vars == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"]

    def test_drops_empty_env_vars(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "channels": [
                        {
                            "id": "x",
                            "env_vars": ["FOO", "", "  ", "BAR"],
                        }
                    ]
                },
            }
        )
        assert schema is not None
        assert schema.setup.channels[0].env_vars == ["FOO", "BAR"]

    def test_unknown_field_rejected(self) -> None:
        schema, err = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "channels": [
                        {"id": "x", "envVars": ["X"]}  # camelCase typo
                    ]
                },
            }
        )
        assert schema is None
        assert "envVars" in err

    def test_omitted_channels_default_empty(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {"providers": []},
            }
        )
        assert schema is not None
        assert schema.setup.channels == []


# ---------------------------------------------------------------------------
# 2. _parse_manifest flattens to PluginSetup with channels tuple
# ---------------------------------------------------------------------------


class TestParsedManifest:
    def test_flattens_to_setupchannel(self, tmp_path: Path) -> None:
        plugin_root = _write_channel_plugin(
            tmp_path,
            "fake-channel",
            setup_channels=[
                {
                    "id": "fake",
                    "env_vars": ["FAKE_TOKEN"],
                    "label": "Fake Channel",
                    "signup_url": "https://example.com/setup",
                    "requires_user_id": True,
                }
            ],
        )
        manifest = discovery._parse_manifest(plugin_root / "plugin.json")
        assert manifest is not None
        assert manifest.setup is not None
        assert isinstance(manifest.setup, PluginSetup)
        assert len(manifest.setup.channels) == 1
        ch = manifest.setup.channels[0]
        assert isinstance(ch, SetupChannel)
        # Tuple-valued so the dataclass stays hashable.
        assert isinstance(ch.env_vars, tuple)
        assert ch.env_vars == ("FAKE_TOKEN",)
        assert ch.requires_user_id is True


# ---------------------------------------------------------------------------
# 3. Bundled channel manifest regression guard
# ---------------------------------------------------------------------------


class TestBundledChannelManifests:
    def test_telegram_declares_setup_channel(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "telegram"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        channels = data["setup"]["channels"]
        assert len(channels) == 1
        ch = channels[0]
        assert ch["id"] == "telegram"
        assert "TELEGRAM_BOT_TOKEN" in ch["env_vars"]
        # Telegram bots default to allow-anyone; user-id allowlist is
        # the safer default for personal-bot setups.
        assert ch["requires_user_id"] is True
        assert "BotFather" in ch["signup_url"] or "t.me" in ch["signup_url"]

    def test_discord_declares_setup_channel(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "discord"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        channels = data["setup"]["channels"]
        assert len(channels) == 1
        ch = channels[0]
        assert ch["id"] == "discord"
        assert ch["env_vars"] == ["DISCORD_BOT_TOKEN"]
        assert "discord.com" in ch["signup_url"]


# ---------------------------------------------------------------------------
# 4. Backwards-compat: existing plugins without setup.channels still parse
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_omitted_setup_yields_no_channels(self, tmp_path: Path) -> None:
        plugin_root = _write_channel_plugin(tmp_path, "no-setup-plugin")
        manifest = discovery._parse_manifest(plugin_root / "plugin.json")
        assert manifest is not None
        assert manifest.setup is None  # entire setup field omitted

    def test_provider_only_setup_has_empty_channels_tuple(self, tmp_path: Path) -> None:
        # A plugin that declares ONLY providers (no channels) should
        # parse fine — channels just defaults to ().
        plugin_dir = tmp_path / "provider-only"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "provider-only",
                    "name": "Provider Only",
                    "version": "0.0.1",
                    "kind": "provider",
                    "entry": "plugin",
                    "setup": {
                        "providers": [{"id": "x", "env_vars": ["X_KEY"]}]
                    },
                }
            )
        )
        (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n")
        manifest = discovery._parse_manifest(plugin_dir / "plugin.json")
        assert manifest is not None
        assert manifest.setup is not None
        assert len(manifest.setup.providers) == 1
        assert manifest.setup.channels == ()
