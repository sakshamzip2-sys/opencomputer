"""Tests for `oc setup` routing to the section-driven wizard."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner


def test_setup_default_invokes_section_driven_wizard():
    """Without flags, the section-driven wizard runs."""
    from opencomputer.cli import app

    new_called: list[dict] = []

    def fake_new(**kwargs):
        new_called.append(kwargs)
        return 0

    with patch("opencomputer.cli_setup.wizard.run_setup", side_effect=fake_new):
        runner = CliRunner()
        result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert len(new_called) == 1
    assert new_called[0]["quick"] is None

def test_setup_help_hides_compatibility_flags():
    """Users should only need `oc setup`; --new/--legacy are not public UX."""
    import re

    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--new" not in plain
    assert "--legacy" not in plain


def test_setup_non_interactive_implies_new_and_passes_flag():
    """--non-interactive routes to the new wizard with non_interactive=True."""
    from opencomputer.cli import app

    captured: list[dict] = []

    def fake_run_setup(**kwargs):
        captured.append(kwargs)
        return 0

    with patch("opencomputer.cli_setup.wizard.run_setup", side_effect=fake_run_setup):
        runner = CliRunner()
        result = runner.invoke(app, ["setup", "--non-interactive"])

    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0]["non_interactive"] is True
    assert captured[0]["quick"] is False


def test_setup_help_lists_non_interactive_flag():
    """`oc setup --help` documents --non-interactive."""
    import re

    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--non-interactive" in plain
