"""Credential-aware channel discovery — Layer A of the gateway fix.

`oc gateway` discovered and loaded every channel adapter regardless of
credentials: qqbot/wecom/weixin crashed at connect, the rest were dead
weight. `channel_credentials_satisfied` (consumed by
`PluginRegistry.load_all`) skips a *pure channel adapter* — one
declaring a non-empty `activation.on_channels` — when it declares
required `setup.channels[].env_vars` and NONE are present.

Channel-*kind* plugins that ALSO register tools (homeassistant,
discord) declare no `on_channels`; they must never be gated, or their
tools vanish. `OPENCOMPUTER_LOAD_ALL_PLUGINS=1` bypasses the gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.activation_planner import channel_credentials_satisfied
from opencomputer.plugins.registry import PluginRegistry

# ─── stub manifest objects for the unit tests ────────────────────────


class _Activation:
    def __init__(self, on_channels=()) -> None:  # noqa: ANN001
        self.on_channels = tuple(on_channels)


class _Channel:
    def __init__(self, env_vars=()) -> None:  # noqa: ANN001
        self.env_vars = tuple(env_vars)


class _Setup:
    def __init__(self, channels=()) -> None:  # noqa: ANN001
        self.channels = list(channels)


class _Manifest:
    """Minimal manifest stub exposing only what the gate predicate reads."""

    def __init__(self, *, on_channels=None, env_vars=None) -> None:  # noqa: ANN001
        self.activation = (
            _Activation(on_channels) if on_channels is not None else None
        )
        self.setup = _Setup([_Channel(env_vars)]) if env_vars is not None else None


# ─── unit: channel_credentials_satisfied ─────────────────────────────


class TestChannelCredentialsSatisfied:
    def test_pure_adapter_with_no_creds_is_unsatisfied(self) -> None:
        m = _Manifest(on_channels=["qqbot"], env_vars=["QQBOT_APPID", "QQBOT_SECRET"])
        assert channel_credentials_satisfied(m, {}) is False

    def test_pure_adapter_with_one_cred_set_is_satisfied(self) -> None:
        """OR-semantics — any one declared var present keeps the plugin."""
        m = _Manifest(on_channels=["qqbot"], env_vars=["QQBOT_APPID", "QQBOT_SECRET"])
        assert channel_credentials_satisfied(m, {"QQBOT_SECRET": "x"}) is True

    def test_adapter_with_empty_env_vars_is_always_satisfied(self) -> None:
        """matrix/slack declare on_channels but no env_vars — nothing to gate."""
        m = _Manifest(on_channels=["matrix"], env_vars=[])
        assert channel_credentials_satisfied(m, {}) is True

    def test_channel_kind_plugin_without_on_channels_is_never_gated(self) -> None:
        """homeassistant/discord declare env_vars but no on_channels and
        register tools — gating them would delete their tools."""
        m = _Manifest(
            on_channels=[], env_vars=["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"]
        )
        assert channel_credentials_satisfied(m, {}) is True

    def test_plugin_with_no_activation_or_setup_is_satisfied(self) -> None:
        """Every tool / provider plugin — no activation block, no setup."""
        assert channel_credentials_satisfied(_Manifest(), {}) is True


# ─── integration: load_all credential gate ───────────────────────────


def _write_channel_plugin(root: Path, pid: str, *, on_channels, env_vars) -> None:  # noqa: ANN001
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": pid,
        "name": pid.title(),
        "version": "0.0.1",
        "kind": "channel",
        "entry": "plugin",
        "activation": {"on_channels": list(on_channels)},
        "setup": {"channels": [{"id": pid, "env_vars": list(env_vars)}]},
    }
    (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")


def _write_tool_plugin(root: Path, pid: str) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps(
            {
                "id": pid,
                "name": pid.title(),
                "version": "0.0.1",
                "kind": "tool",
                "entry": "plugin",
            }
        ),
        encoding="utf-8",
    )
    (d / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    monkeypatch.delenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", raising=False)
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


def test_load_all_skips_credentialless_channel_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QQ_TEST_TOKEN", raising=False)
    root = tmp_path / "plugins"
    _write_channel_plugin(
        root, "qq-test", on_channels=["qq-test"], env_vars=["QQ_TEST_TOKEN"]
    )
    _write_tool_plugin(root, "tool-test")

    registry = PluginRegistry()
    registry.load_all([root], enabled_ids=None)  # wildcard — the gateway's mode

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    assert "qq-test" not in loaded, "credentialless channel adapter must be skipped"
    assert "tool-test" in loaded, "a non-channel plugin must still load"


def test_load_all_keeps_channel_adapter_when_cred_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QQ_TEST_TOKEN", "present")
    root = tmp_path / "plugins"
    _write_channel_plugin(
        root, "qq-test", on_channels=["qq-test"], env_vars=["QQ_TEST_TOKEN"]
    )

    registry = PluginRegistry()
    registry.load_all([root], enabled_ids=None)

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    assert "qq-test" in loaded, "channel adapter with a credential present must load"


def test_load_all_plugins_env_bypasses_credential_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QQ_TEST_TOKEN", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", "1")
    root = tmp_path / "plugins"
    _write_channel_plugin(
        root, "qq-test", on_channels=["qq-test"], env_vars=["QQ_TEST_TOKEN"]
    )

    registry = PluginRegistry()
    registry.load_all([root], enabled_ids=None)

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    assert "qq-test" in loaded, "OPENCOMPUTER_LOAD_ALL_PLUGINS=1 must bypass the gate"
