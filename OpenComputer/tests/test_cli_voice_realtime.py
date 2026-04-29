"""voice realtime CLI command — wires bridge + audio + router."""
from __future__ import annotations

from typer.testing import CliRunner


def test_voice_realtime_help_advertises_command() -> None:
    from opencomputer.cli_voice import voice_app

    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime", "--help"])
    assert result.exit_code == 0
    assert "realtime" in result.output.lower() or "OpenAI" in result.output


def test_voice_realtime_errors_without_api_key(monkeypatch) -> None:
    """Without OPENAI_API_KEY, the command must error out with a clear message."""
    from opencomputer.cli_voice import voice_app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime"])
    assert result.exit_code != 0
    assert "OPENAI_API_KEY" in result.output or "api key" in result.output.lower()
