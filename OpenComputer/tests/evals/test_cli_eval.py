import json as _json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_eval import eval_app


def test_cli_eval_run_unknown_site_errors():
    runner = CliRunner()
    result = runner.invoke(eval_app, ["run", "does_not_exist", "--no-history"])
    assert result.exit_code != 0


def test_cli_eval_run_known_site_no_cases_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evals" / "cases").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(eval_app, ["run", "instruction_detector", "--no-history"])
    assert result.exit_code == 0
    assert "instruction_detector" in result.stdout


# --- Phase 2: --verbose --json --case-id ---------------------------------


def _seed_one_case(cases_dir: Path, *, expected: str):
    cases_dir.mkdir(parents=True, exist_ok=True)
    (cases_dir / "instruction_detector.jsonl").write_text(
        _json.dumps(
            {"id": "c1", "input": {"text": "what is the weather?"}, "expected": expected}
        )
        + "\n"
    )


def test_run_command_verbose_flag(tmp_path):
    cases_dir = tmp_path / "cases"
    _seed_one_case(cases_dir, expected="yes")  # detector returns "no" → fails
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--no-history",
            "--verbose",
        ],
    )
    assert result.exit_code == 0
    assert "Failing cases" in result.output
    assert "c1" in result.output


def test_run_command_json_flag_single_site(tmp_path):
    cases_dir = tmp_path / "cases"
    _seed_one_case(cases_dir, expected="no")  # detector matches → passes
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--no-history",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload["site_name"] == "instruction_detector"
    assert payload["total"] == 1
    assert "case_runs" in payload


def _extract_json_block(combined_output: str) -> str:
    """Pull the JSON object/document out of typer's combined stdout+stderr.

    typer.testing.CliRunner merges stderr ('Skipping rubric site...') with
    stdout (the JSON payload) in ``result.output``. Real shells keep them
    separate; this helper exists only so tests can still parse the JSON.
    """
    lines = combined_output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            return "\n".join(lines[i:])
    raise ValueError(f"no JSON object found in output:\n{combined_output}")


def test_run_command_json_flag_all_sites_uses_envelope(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    # Provide a case for one site only — others will be empty
    _seed_one_case(cases_dir, expected="no")
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "all",
            "--cases-dir",
            str(cases_dir),
            "--no-history",
            "--json",
        ],
    )
    # 'reflect' is rubric-graded and will be skipped (no provider) → exit 0,
    # JSON payload covers the remaining sites.
    assert result.exit_code == 0
    payload = _json.loads(_extract_json_block(result.output))
    assert isinstance(payload, dict)
    assert "sites" in payload
    assert isinstance(payload["sites"], list)
    assert len(payload["sites"]) >= 1


def test_run_command_case_id_filter(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "instruction_detector.jsonl").write_text(
        _json.dumps({"id": "c1", "input": {"text": "Ignore previous"}, "expected": "yes"})
        + "\n"
        + _json.dumps({"id": "c2", "input": {"text": "weather"}, "expected": "no"})
        + "\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--no-history",
            "--case-id",
            "c2",
            "--json",
        ],
    )
    payload = _json.loads(result.output)
    assert payload["total"] == 1
    assert payload["case_runs"][0]["case_id"] == "c2"


# --- Phase 5: history ----------------------------------------------------


def test_run_command_writes_history(tmp_path):
    cases_dir = tmp_path / "cases"
    _seed_one_case(cases_dir, expected="no")
    history_db = tmp_path / "history.db"
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--history-db",
            str(history_db),
        ],
    )
    assert result.exit_code == 0
    assert history_db.exists()


def test_run_command_no_history_skips_db(tmp_path):
    cases_dir = tmp_path / "cases"
    _seed_one_case(cases_dir, expected="no")
    history_db = tmp_path / "history.db"
    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "run",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--history-db",
            str(history_db),
            "--no-history",
        ],
    )
    assert result.exit_code == 0
    assert not history_db.exists()


def test_history_command_prints_recent_runs(tmp_path):
    from opencomputer.evals.history import record_run
    from opencomputer.evals.runner import RunReport

    db_path = tmp_path / "history.db"
    record_run(
        RunReport(
            site_name="job_change",
            total=30,
            correct=30,
            parse_failures=0,
            infra_failures=0,
        ),
        db_path=db_path,
        model="m",
        provider="p",
    )

    runner = CliRunner()
    result = runner.invoke(
        eval_app, ["history", "job_change", "--history-db", str(db_path)]
    )
    assert result.exit_code == 0
    assert "job_change" in result.output
    assert "100.0%" in result.output


def test_history_command_empty_db_message(tmp_path):
    db_path = tmp_path / "missing.db"
    runner = CliRunner()
    result = runner.invoke(
        eval_app, ["history", "all", "--history-db", str(db_path)]
    )
    assert result.exit_code == 0
    assert "No history yet" in result.output


# --- Phase 6: dashboard --------------------------------------------------


def test_dashboard_command_writes_file(tmp_path):
    from opencomputer.evals.history import record_run
    from opencomputer.evals.runner import RunReport

    db_path = tmp_path / "history.db"
    record_run(
        RunReport(
            site_name="job_change",
            total=10,
            correct=10,
            parse_failures=0,
            infra_failures=0,
        ),
        db_path=db_path,
        model="m",
        provider="p",
    )
    out_path = tmp_path / "out.html"

    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "dashboard",
            "--out",
            str(out_path),
            "--history-db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    assert "job_change" in out_path.read_text()


# --- Phase 7: promote ---------------------------------------------------


def test_promote_command(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "x.jsonl").write_text(
        '{"id": "a", "input": {}, "expected": "yes"}\n'
    )
    (cases_dir / "x.candidates.jsonl").write_text(
        '{"id": "b", "input": {}, "expected": "no"}\n'
    )

    runner = CliRunner()
    result = runner.invoke(
        eval_app, ["promote", "x", "--cases-dir", str(cases_dir)]
    )
    assert result.exit_code == 0
    assert "Promoted 1 case" in result.output


def test_promote_command_no_candidates(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        eval_app, ["promote", "x", "--cases-dir", str(cases_dir)]
    )
    assert result.exit_code == 0
    assert "No candidates" in result.output


# --- Coverage closers: regress regression-detected + history JSON ---


def test_regress_command_detects_regression(tmp_path):
    """Force a >threshold accuracy drop on instruction_detector."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    baselines_dir = tmp_path / "baselines"
    baselines_dir.mkdir()

    # 5 cases: detector returns "no" for all benign text (so 4/5 pass = 80%)
    # but baseline frozen at 100% → 20pp drop, well past the 0.10 threshold
    cases = [
        {"id": "id_0", "input": {"text": "Ignore previous"}, "expected": "yes"},
        {"id": "id_1", "input": {"text": "benign"}, "expected": "no"},
        {"id": "id_2", "input": {"text": "benign"}, "expected": "no"},
        {"id": "id_3", "input": {"text": "benign"}, "expected": "no"},
        {"id": "id_4", "input": {"text": "benign"}, "expected": "no"},
    ]
    (cases_dir / "instruction_detector.jsonl").write_text(
        "\n".join(_json.dumps(c) for c in cases)
    )
    (baselines_dir / "instruction_detector.json").write_text(
        _json.dumps(
            {
                "site_name": "instruction_detector",
                "accuracy": 1.0,
                "parse_failure_rate": 0.0,
                "timestamp": "2026-05-01T00:00:00+00:00",
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "regress",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--baselines-dir",
            str(baselines_dir),
        ],
    )
    # 20pp drop > 10pp threshold → exit 1
    assert result.exit_code == 1
    assert "REGRESSED" in result.output


def test_history_command_json_output(tmp_path):
    from opencomputer.evals.history import record_run
    from opencomputer.evals.runner import RunReport

    db_path = tmp_path / "history.db"
    record_run(
        RunReport(
            site_name="job_change",
            total=10,
            correct=10,
            parse_failures=0,
            infra_failures=0,
        ),
        db_path=db_path,
        model="m",
        provider="p",
    )

    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "history",
            "job_change",
            "--history-db",
            str(db_path),
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["site_name"] == "job_change"
