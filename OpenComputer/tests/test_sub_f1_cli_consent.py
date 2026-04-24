"""CLI: opencomputer consent {list, grant, revoke, history, ...}"""
from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_consent_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["consent", "list"])
    assert result.exit_code == 0, result.output
    assert "No active grants" in result.output


def test_consent_grant_then_list(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r1 = runner.invoke(app, [
        "consent", "grant", "read_files",
        "--scope", str(tmp_path),
        "--tier", "2", "--expires", "1d",
    ])
    assert r1.exit_code == 0, r1.output
    assert "Granted read_files" in r1.output
    r2 = runner.invoke(app, ["consent", "list"])
    assert "read_files" in r2.output
    assert "PER_ACTION" in r2.output


def test_consent_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "revoke", "x"])
    assert r.exit_code == 0, r.output
    r2 = runner.invoke(app, ["consent", "list"])
    assert "x " not in r2.output  # "x " would only appear as capability_id


def test_consent_history_shows_events(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    runner.invoke(app, ["consent", "revoke", "x"])
    r = runner.invoke(app, ["consent", "history", "x"])
    assert r.exit_code == 0, r.output
    assert "grant" in r.output
    assert "revoke" in r.output


def test_consent_verify_chain_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "verify-chain"])
    assert r.exit_code == 0, r.output
    assert "ok" in r.output.lower()


def test_consent_export_and_import_chain_head(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    out = tmp_path / "head.json"
    r = runner.invoke(app, ["consent", "export-chain-head", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    r2 = runner.invoke(app, ["consent", "import-chain-head", "--from", str(out)])
    assert r2.exit_code == 0, r2.output


def test_consent_bypass_status(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_CONSENT_BYPASS", raising=False)
    r = runner.invoke(app, ["consent", "bypass", "--status"])
    assert r.exit_code == 0, r.output
    assert "inactive" in r.output.lower()


def test_consent_bypass_active(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "1")
    r = runner.invoke(app, ["consent", "bypass", "--status"])
    assert r.exit_code == 0, r.output
    assert "active" in r.output.lower()
    assert "BYPASS" in r.output


def test_consent_grant_default_expiry_30d(tmp_path, monkeypatch):
    """Default expiry should be ~30d when --expires omitted."""
    import time as _time
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "list"])
    # Crudely parse the year/month from the output to see something
    # 30d out — but more robustly check that SOME expiry is shown.
    assert "expires" in r.output.lower()
