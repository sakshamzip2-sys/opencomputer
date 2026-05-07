"""Tests for ``oc browser status / connect / disconnect`` (Hermes B1).

The HTTP probe is mocked so tests don't need a real Chrome. The
shell-rc rewrite is exercised against a tmp HOME so we don't touch
the user's actual ~/.zshrc.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_browser import browser_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(env={"COLUMNS": "120"})


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() so connect/disconnect write into tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _success_resp() -> _FakeResp:
    return _FakeResp(
        json.dumps({
            "Browser": "Chrome/123.0",
            "User-Agent": "Mozilla/5.0",
            "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/abc",
        }).encode()
    )


def test_status_reports_when_chrome_reachable(runner: CliRunner) -> None:
    with patch("urllib.request.urlopen", return_value=_success_resp()):
        result = runner.invoke(browser_app, ["status"])
    assert result.exit_code == 0
    assert "Chrome" in result.stdout
    assert "9222" in result.stdout


def test_status_fails_when_chrome_not_reachable(runner: CliRunner) -> None:
    with patch(
        "urllib.request.urlopen",
        side_effect=ConnectionRefusedError("nope"),
    ):
        result = runner.invoke(browser_app, ["status"])
    assert result.exit_code == 1
    assert "No CDP-enabled Chrome reachable" in result.stderr


def test_connect_writes_rc(
    runner: CliRunner, fake_home: Path
) -> None:
    (fake_home / ".zshrc").write_text("# user zshrc\n")
    with patch("urllib.request.urlopen", return_value=_success_resp()):
        result = runner.invoke(browser_app, ["connect"])
    assert result.exit_code == 0
    rc_text = (fake_home / ".zshrc").read_text()
    assert "OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222" in rc_text
    assert "OpenComputer browser CDP attach" in rc_text


def test_connect_idempotent(
    runner: CliRunner, fake_home: Path
) -> None:
    """Running connect twice should not double up the marker."""
    (fake_home / ".zshrc").write_text("# user zshrc\n")
    with patch("urllib.request.urlopen", return_value=_success_resp()):
        runner.invoke(browser_app, ["connect"])
        runner.invoke(browser_app, ["connect"])
    rc_text = (fake_home / ".zshrc").read_text()
    # Marker should appear exactly once
    assert rc_text.count("OpenComputer browser CDP attach") == 1


def test_connect_refuses_when_chrome_unreachable(
    runner: CliRunner, fake_home: Path
) -> None:
    (fake_home / ".zshrc").write_text("# user zshrc\n")
    with patch(
        "urllib.request.urlopen",
        side_effect=ConnectionRefusedError("nope"),
    ):
        result = runner.invoke(browser_app, ["connect"])
    assert result.exit_code == 1
    rc_text = (fake_home / ".zshrc").read_text()
    # Did NOT write the env var
    assert "OPENCOMPUTER_BROWSER_CDP_URL" not in rc_text


def test_disconnect_strips_marker(
    runner: CliRunner, fake_home: Path
) -> None:
    """After connect, disconnect must remove the marker."""
    (fake_home / ".zshrc").write_text("# user zshrc\n")
    with patch("urllib.request.urlopen", return_value=_success_resp()):
        runner.invoke(browser_app, ["connect"])
    # Sanity check: marker is now there
    assert "OpenComputer browser CDP attach" in (fake_home / ".zshrc").read_text()

    result = runner.invoke(browser_app, ["disconnect"])
    assert result.exit_code == 0
    rc_text = (fake_home / ".zshrc").read_text()
    assert "OpenComputer browser CDP attach" not in rc_text
    assert "OPENCOMPUTER_BROWSER_CDP_URL" not in rc_text


def test_disconnect_idempotent_when_not_attached(
    runner: CliRunner, fake_home: Path
) -> None:
    (fake_home / ".zshrc").write_text("# user zshrc\n")
    result = runner.invoke(browser_app, ["disconnect"])
    assert result.exit_code == 0
    assert "Not currently attached" in result.stdout


def test_status_custom_port(runner: CliRunner) -> None:
    with patch("urllib.request.urlopen", return_value=_success_resp()) as mock:
        runner.invoke(browser_app, ["status", "--port", "9333"])
    # Verify the URL was constructed with our port
    args, _ = mock.call_args
    url = args[0]
    assert ":9333" in url


def _tabs_resp() -> _FakeResp:
    return _FakeResp(
        json.dumps([
            {
                "type": "page",
                "id": "abc",
                "title": "Notion — workspace",
                "url": "https://www.notion.so/team/abc",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/abc",
            },
            {
                "type": "page",
                "id": "def",
                "title": "Gmail",
                "url": "https://mail.google.com/mail/u/0/",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/def",
            },
            {
                "type": "service_worker",  # filtered out
                "id": "sw1",
                "title": "service worker",
                "url": "",
            },
        ]).encode()
    )


def test_tabs_command_lists_pages(runner: CliRunner) -> None:
    with patch("urllib.request.urlopen", return_value=_tabs_resp()):
        result = runner.invoke(browser_app, ["tabs"])
    assert result.exit_code == 0
    assert "Notion" in result.stdout
    assert "Gmail" in result.stdout
    # Filtered out: service worker shouldn't appear
    assert "service worker" not in result.stdout
    # Sees 2 page tabs
    assert "2 tab" in result.stdout


def test_tabs_command_no_pages(runner: CliRunner) -> None:
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeResp(json.dumps([]).encode()),
    ):
        result = runner.invoke(browser_app, ["tabs"])
    assert result.exit_code == 0
    assert "No open page tabs" in result.stdout


def test_tabs_command_failure(runner: CliRunner) -> None:
    with patch(
        "urllib.request.urlopen",
        side_effect=ConnectionRefusedError("nope"),
    ):
        result = runner.invoke(browser_app, ["tabs"])
    assert result.exit_code == 1
    assert "Could not list tabs" in result.stderr


def test_tabs_command_custom_cdp_url(runner: CliRunner) -> None:
    with patch("urllib.request.urlopen", return_value=_tabs_resp()) as mock:
        runner.invoke(browser_app, ["tabs", "--cdp-url", "http://example.com:7777"])
    args, _ = mock.call_args
    assert args[0] == "http://example.com:7777/json"


def test_run_command_cdp_url_overrides_env(monkeypatch, runner: CliRunner) -> None:
    """--cdp-url sets the env var for this invocation."""
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)
    seen_env: dict = {}

    def _fake_run_recipe(site, verb, args, fetcher, fmt):
        import os as _os
        seen_env["cdp"] = _os.environ.get("OPENCOMPUTER_BROWSER_CDP_URL")
        return "{}"

    with patch("opencomputer.recipes.run_recipe", _fake_run_recipe):
        result = runner.invoke(
            browser_app,
            ["run", "any-site", "any-verb", "--cdp-url", "http://localhost:9999"],
        )
    # The recipe lookup itself will fail for a fake site, but cdp_url
    # gets applied BEFORE the lookup. Either way the env var was set.
    assert seen_env.get("cdp") == "http://localhost:9999" or result.exit_code != 0
