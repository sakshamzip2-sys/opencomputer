"""voice realtime CLI command — wires bridge + audio + router."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner


def test_voice_realtime_help_advertises_command() -> None:
    from opencomputer.cli_voice import voice_app

    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime", "--help"])
    assert result.exit_code == 0
    assert "realtime" in result.output.lower() or "OpenAI" in result.output


@pytest.fixture
def _registered_openai_bridge():
    """Register a stub openai realtime bridge so the registry lookup
    succeeds. After the gemini-realtime PR (39d0b0e5) the CLI resolves
    the bridge from the plugin registry BEFORE checking the API key,
    and the openai-provider plugin is not enabled in the default profile
    during test runs — which leaves the registry empty and breaks the
    pre-existing API-key error message contract this test guards.
    """
    from opencomputer.plugins.loader import PluginAPI
    from opencomputer.plugins.registry import registry as plugin_registry

    saved_api = plugin_registry.shared_api
    api = PluginAPI(
        tool_registry=None,
        hook_engine=None,
        provider_registry={},
        channel_registry={},
    )
    api.register_realtime_bridge(
        "openai", lambda **_: None, env_var="OPENAI_API_KEY",
    )
    plugin_registry.shared_api = api
    try:
        yield api
    finally:
        plugin_registry.shared_api = saved_api


def test_voice_realtime_errors_without_api_key(
    monkeypatch, _registered_openai_bridge,
) -> None:
    """Without OPENAI_API_KEY, the command must error out with a clear message."""
    from opencomputer.cli_voice import voice_app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime"])
    assert result.exit_code != 0
    assert "OPENAI_API_KEY" in result.output or "api key" in result.output.lower()
