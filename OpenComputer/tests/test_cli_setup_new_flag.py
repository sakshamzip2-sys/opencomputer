"""Tests for the `oc setup --new` flag — exposes the new section-driven wizard."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner


def test_setup_default_invokes_legacy_run_setup(tmp_path):
    """Without --new, the existing legacy wizard runs."""
    from opencomputer.cli import app

    legacy_called = []

    def fake_legacy():
        legacy_called.append(True)

    with patch("opencomputer.setup_wizard.run_setup", side_effect=fake_legacy):
        runner = CliRunner()
        result = runner.invoke(app, ["setup"])

    assert legacy_called == [True]
    # The new wizard must NOT have been invoked.


def test_setup_new_invokes_section_driven_wizard():
    """`oc setup --new` calls cli_setup.wizard.run_setup."""
    from opencomputer.cli import app

    new_called = []

    def fake_new(**kwargs):
        new_called.append(kwargs)
        return 0

    with patch("opencomputer.cli_setup.wizard.run_setup", side_effect=fake_new):
        runner = CliRunner()
        result = runner.invoke(app, ["setup", "--new"])

    assert len(new_called) == 1


def test_setup_help_lists_new_flag():
    """`oc setup --help` documents --new."""
    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    assert "--new" in result.output
