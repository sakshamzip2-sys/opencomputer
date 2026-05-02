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
    """`oc setup --help` documents --new.

    Strip ANSI codes before asserting — Click/Typer's color rendering
    in CI splits ``--new`` into ``\\x1b[36m-\\x1b[0m\\x1b[36m-new`` which
    breaks a literal substring check. Locally tests run with
    ``force_terminal=False``; CI runners are detected as TTYs.
    """
    import re

    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--new" in plain


def test_setup_non_interactive_implies_new_and_passes_flag():
    """Q2: --non-interactive routes to new wizard with non_interactive=True."""
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


def test_setup_help_lists_non_interactive_flag():
    """`oc setup --help` documents --non-interactive."""
    import re

    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--non-interactive" in plain
