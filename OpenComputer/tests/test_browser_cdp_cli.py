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
