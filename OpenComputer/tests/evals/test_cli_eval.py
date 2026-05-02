from typer.testing import CliRunner

from opencomputer.cli_eval import eval_app


def test_cli_eval_run_unknown_site_errors():
    runner = CliRunner()
    result = runner.invoke(eval_app, ["run", "does_not_exist"])
    assert result.exit_code != 0


def test_cli_eval_run_known_site_no_cases_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evals" / "cases").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(eval_app, ["run", "instruction_detector"])
    assert result.exit_code == 0
    assert "instruction_detector" in result.stdout
