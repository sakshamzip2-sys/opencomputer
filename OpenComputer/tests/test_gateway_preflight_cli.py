"""CLI tests for ``oc service preflight``."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli import app
from opencomputer.gateway.preflight import Competitor

runner = CliRunner()


def test_preflight_reports_no_competitors_with_exit_zero() -> None:
    with patch(
        "opencomputer.gateway.preflight.detect_competitors",
        return_value=[],
    ):
        result = runner.invoke(app, ["service", "preflight"])
    assert result.exit_code == 0
    assert "no competitors detected" in result.stdout


def test_preflight_lists_competitors_and_exits_nonzero() -> None:
    fake = [
        Competitor(pid=1234, kind="hermes_gateway", cmdline_preview="python hermes"),
        Competitor(pid=5678, kind="claude_code_telegram_bridge", cmdline_preview="bun"),
    ]
    with patch(
        "opencomputer.gateway.preflight.detect_competitors",
        return_value=fake,
    ):
        result = runner.invoke(app, ["service", "preflight"])
    assert result.exit_code == 1
    assert "1234" in result.stdout
    assert "5678" in result.stdout
    assert "hermes_gateway" in result.stdout
    assert "--force-takeover" in result.stdout


def test_preflight_force_takeover_invokes_takeover() -> None:
    fake = [Competitor(pid=1234, kind="hermes_gateway", cmdline_preview="python hermes")]
    with patch(
        "opencomputer.gateway.preflight.run_preflight",
        return_value=[],
    ) as mock_run, \
         patch(
             "opencomputer.gateway.preflight.detect_competitors",
             return_value=fake,
         ):
        result = runner.invoke(app, ["service", "preflight", "--force-takeover"])
    assert result.exit_code == 0
    assert "takeover complete" in result.stdout
    # run_preflight invoked with takeover_on_start=True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("takeover_on_start") is True


def test_preflight_force_takeover_reports_survivors_with_nonzero_exit() -> None:
    fake_survivors = [
        Competitor(pid=9999, kind="rival_oc_gateway", cmdline_preview="oc gateway"),
    ]
    with patch(
        "opencomputer.gateway.preflight.run_preflight",
        return_value=fake_survivors,
    ):
        result = runner.invoke(
            app, ["service", "preflight", "--force-takeover"]
        )
    assert result.exit_code == 1
    assert "takeover incomplete" in (result.stdout + result.stderr)
    assert "9999" in (result.stdout + result.stderr)
