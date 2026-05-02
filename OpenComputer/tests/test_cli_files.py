"""Tests for the `oc files` CLI subcommand group."""
# ruff: noqa: I001
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

# Importing cli_files first ensures the synthetic
# `extensions_anthropic_provider_files_client` module is registered in
# sys.modules so subsequent test-side imports of FileMetadata succeed.
from opencomputer.cli_files import files_app  # noqa: E402

from extensions_anthropic_provider_files_client import FileMetadata  # noqa: E402


def _runner() -> CliRunner:
    return CliRunner()


def test_files_missing_api_key_exits_with_message(monkeypatch):
    """No ANTHROPIC_API_KEY → friendly error + exit 1."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = _runner().invoke(files_app, ["list"])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in (result.stderr or result.stdout)


def test_files_list_prints_table(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    fake_files = [
        FileMetadata(
            id="file_a",
            filename="a.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            created_at=datetime(2026, 5, 2, 10, 0, 0, tzinfo=UTC),
            downloadable=False,
        ),
    ]

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.list = AsyncMock(return_value=fake_files)
        result = _runner().invoke(files_app, ["list"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "file_a" in result.stdout
    assert "a.pdf" in result.stdout


def test_files_upload_prints_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    fake_meta = FileMetadata(
        id="file_new",
        filename="doc.pdf",
        mime_type="application/pdf",
        size_bytes=pdf.stat().st_size,
        created_at=datetime(2026, 5, 2, 10, 0, 0, tzinfo=UTC),
        downloadable=False,
    )

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.upload = AsyncMock(return_value=fake_meta)
        result = _runner().invoke(files_app, ["upload", str(pdf)])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "file_new" in result.stdout


def test_files_delete_calls_client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.delete = AsyncMock(return_value=None)
        result = _runner().invoke(files_app, ["delete", "file_xyz"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    instance.delete.assert_called_once_with("file_xyz")


def test_files_download_writes_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out_path = tmp_path / "downloaded.bin"
    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.download = AsyncMock(return_value=42)
        result = _runner().invoke(files_app, ["download", "file_xyz", str(out_path)])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "42" in result.stdout or "bytes" in result.stdout.lower()
    instance.download.assert_called_once()
