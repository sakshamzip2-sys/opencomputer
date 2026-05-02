"""v2: cascade / explore / synthesize / generate CLI commands."""
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from opencomputer.cli_browser import browser_app


def _mock_response(status: int):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"ok": True}
    resp.text = "ok"
    return resp


def test_cascade_succeeds_exits_0(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)
    runner = CliRunner()
    with patch("httpx.get", return_value=_mock_response(200)):
        result = runner.invoke(browser_app, ["cascade", "https://example.com"])
    assert result.exit_code == 0
    assert "Strategy:" in result.stdout
    assert "public" in result.stdout


def test_cascade_all_fail_exits_1(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)
    runner = CliRunner()
    with patch("httpx.get", return_value=_mock_response(500)):
        result = runner.invoke(browser_app, ["cascade", "https://example.com"])
    assert result.exit_code == 1


def test_synthesize_no_artifacts_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(browser_app, ["synthesize", "nonexistent"])
    assert result.exit_code == 1
    assert "explore" in result.stderr or "explore" in result.output


def test_synthesize_with_artifacts_no_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    artifacts = tmp_path / ".opencli" / "explore" / "demo"
    artifacts.mkdir(parents=True)
    (artifacts / "endpoints.json").write_text("[]")

    runner = CliRunner()
    result = runner.invoke(browser_app, ["synthesize", "demo"])
    assert result.exit_code == 2
    assert (
        "ANTHROPIC_API_KEY" in (result.stderr + result.output)
        or "OPENAI_API_KEY" in (result.stderr + result.output)
    )
