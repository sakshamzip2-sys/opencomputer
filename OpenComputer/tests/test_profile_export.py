"""Tests for opencomputer.profile_export (Phase 14.H)."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
import yaml

from opencomputer.profile_export import (
    _redact_env_text,
    _redact_yaml_data,
    export_profile,
    import_profile,
    list_archive_files,
)

# ─── _redact_env_text ───


def test_redact_env_replaces_value_with_length_marker():
    out = _redact_env_text("MY_KEY=secret_value_here\n")
    assert "secret_value_here" not in out
    assert "MY_KEY=<REDACTED:17>" in out


def test_redact_env_preserves_blank_and_comments():
    src = """# top comment
MY_KEY=secret

# another comment
OTHER=longer-secret
"""
    out = _redact_env_text(src)
    assert "# top comment" in out
    assert "# another comment" in out
    assert "secret" not in out
    assert "longer-secret" not in out
    assert "MY_KEY=<REDACTED:6>" in out
    assert "OTHER=<REDACTED:13>" in out


def test_redact_env_handles_quoted_values():
    out = _redact_env_text('FOO="bar baz"\n')
    # "bar baz" stripped to bar baz (7 chars) for length count
    assert 'FOO=<REDACTED:7>' in out
    assert "bar baz" not in out


def test_redact_env_strips_only_matching_quotes():
    """If only one side is quoted, treat entire value as raw."""
    out = _redact_env_text('FOO="oops\n')
    # Length is len('"oops') = 5
    assert "FOO=<REDACTED:5>" in out


def test_redact_env_empty_input():
    assert _redact_env_text("") == ""


# ─── _redact_yaml_data ───


def test_redact_yaml_replaces_secret_keys():
    data = {
        "model": "claude-opus-4-7",
        "anthropic": {
            "api_key": "sk-ant-secret-very-long",
            "base_url": "https://api.anthropic.com",
        },
    }
    out = _redact_yaml_data(data)
    assert out["model"] == "claude-opus-4-7"
    assert out["anthropic"]["base_url"] == "https://api.anthropic.com"
    assert out["anthropic"]["api_key"].startswith("<REDACTED:")
    assert "sk-ant-secret-very-long" not in str(out)


def test_redact_yaml_preserves_short_values():
    """Short placeholder values aren't redacted (likely empty / not-yet-set)."""
    data = {"api_key": ""}
    out = _redact_yaml_data(data)
    assert out["api_key"] == ""  # below MIN_LEN, kept


def test_redact_yaml_walks_lists():
    data = {"servers": [{"token": "very-long-token-value", "name": "srv1"}]}
    out = _redact_yaml_data(data)
    assert out["servers"][0]["name"] == "srv1"
    assert "very-long-token-value" not in str(out)


def test_redact_yaml_passes_through_non_dict_non_list():
    assert _redact_yaml_data("plain") == "plain"
    assert _redact_yaml_data(42) == 42
    assert _redact_yaml_data(None) is None


def test_redact_yaml_case_insensitive_key_match():
    data = {"API_KEY": "long-secret-value", "ApiKey": "another-long-value"}
    out = _redact_yaml_data(data)
    assert out["API_KEY"].startswith("<REDACTED:")
    assert out["ApiKey"].startswith("<REDACTED:")


# ─── export_profile + import_profile ───


@pytest.fixture
def sample_profile(tmp_path):
    """Create a representative profile directory."""
    profile = tmp_path / "src_profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "anthropic:\n  api_key: sk-ant-very-secret-value\n  base_url: https://api.anthropic.com\n"
    )
    (profile / "profile.yaml").write_text("plugins:\n  enabled:\n    - foo\n    - bar\n")
    (profile / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-actual-secret\n")
    (profile / "MEMORY.md").write_text("# memory\nNothing important.\n")
    # Files that should be excluded from export
    (profile / "sessions.db").write_bytes(b"\x00\x01\x02")
    (profile / "llm_events.jsonl").write_text("{}\n")
    (profile / "logs").mkdir()
    (profile / "logs" / "x.log").write_text("log line\n")
    return profile


def test_export_creates_archive_with_manifest(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out, profile_name="test", oc_version="1.2.3")
    assert out.exists()

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        manifest_f = tar.extractfile("manifest.json")
        manifest = json.loads(manifest_f.read())
        assert manifest["profile_name"] == "test"
        assert manifest["oc_version"] == "1.2.3"
        assert manifest["format_version"] == "1"
        assert manifest["include_secrets"] is False


def test_export_excludes_sessions_by_default(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert not any(n.endswith("sessions.db") for n in names)
        assert not any(n.endswith("llm_events.jsonl") for n in names)
        assert not any(n.startswith("profile/logs/") for n in names)


def test_export_includes_sessions_when_flagged(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out, include_sessions=True)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("sessions.db") for n in names)
        assert any(n.endswith("llm_events.jsonl") for n in names)


def test_export_redacts_env_by_default(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out)
    with tarfile.open(out, "r:gz") as tar:
        env_member = tar.getmember("profile/.env")
        env_bytes = tar.extractfile(env_member).read()
        env_text = env_bytes.decode("utf-8")
    assert "sk-ant-actual-secret" not in env_text
    assert "<REDACTED:" in env_text


def test_export_redacts_yaml_secrets_by_default(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out)
    with tarfile.open(out, "r:gz") as tar:
        cfg_member = tar.getmember("profile/config.yaml")
        cfg_text = tar.extractfile(cfg_member).read().decode("utf-8")
    assert "sk-ant-very-secret-value" not in cfg_text
    parsed = yaml.safe_load(cfg_text)
    assert parsed["anthropic"]["api_key"].startswith("<REDACTED:")
    # Non-secret keys preserved
    assert parsed["anthropic"]["base_url"] == "https://api.anthropic.com"


def test_export_includes_secrets_when_flagged(sample_profile, tmp_path):
    out = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, out, include_secrets=True)
    with tarfile.open(out, "r:gz") as tar:
        env_text = tar.extractfile("profile/.env").read().decode("utf-8")
        cfg_text = tar.extractfile("profile/config.yaml").read().decode("utf-8")
    assert "sk-ant-actual-secret" in env_text  # NOT redacted
    assert "sk-ant-very-secret-value" in cfg_text  # NOT redacted


def test_export_raises_for_missing_profile(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_profile(tmp_path / "does-not-exist", tmp_path / "out.tar.gz")


def test_import_extracts_and_returns_manifest(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive, profile_name="test")

    target = tmp_path / "imported"
    manifest = import_profile(archive, target)
    assert manifest["profile_name"] == "test"

    # Files extracted
    assert (target / "config.yaml").exists()
    assert (target / "profile.yaml").exists()
    assert (target / ".env").exists()
    assert (target / "MEMORY.md").exists()


def test_import_refuses_existing_target_without_force(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)

    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff.txt").write_text("don't overwrite me")

    with pytest.raises(FileExistsError):
        import_profile(archive, target)

    # File should still be there
    assert (target / "stuff.txt").read_text() == "don't overwrite me"


def test_import_overwrites_with_force(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)

    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff.txt").write_text("will be overwritten")

    import_profile(archive, target, force=True)
    assert (target / "config.yaml").exists()


def test_import_validates_format_version(tmp_path):
    """An archive with unknown format_version is rejected."""
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        manifest = json.dumps({"format_version": "99"}).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        import io as _io
        tar.addfile(info, _io.BytesIO(manifest))

    with pytest.raises(ValueError, match="format_version"):
        import_profile(bad, tmp_path / "imported")


def test_import_rejects_archive_without_manifest(tmp_path):
    bad = tmp_path / "no-manifest.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo(name="profile/some-file.txt")
        info.size = 5
        import io as _io
        tar.addfile(info, _io.BytesIO(b"hello"))

    with pytest.raises(ValueError, match="manifest"):
        import_profile(bad, tmp_path / "imported")


def test_roundtrip_redacted_export_to_import(sample_profile, tmp_path):
    """End-to-end: export with default redaction, import, verify."""
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)
    target = tmp_path / "round-trip"
    import_profile(archive, target)
    # .env was redacted before export, so the imported version is redacted too
    env_text = (target / ".env").read_text()
    assert "<REDACTED:" in env_text
    assert "sk-ant-actual-secret" not in env_text


# ─── dry_run + list_archive_files ───


def test_import_dry_run_writes_nothing(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive, profile_name="dry-test")

    target = tmp_path / "would-be-imported"
    manifest = import_profile(archive, target, dry_run=True)
    assert manifest["profile_name"] == "dry-test"
    # Target dir not created in dry-run
    assert not target.exists()


def test_import_dry_run_validates_format_version(tmp_path):
    """Bad archives still raise in dry-run so the preview never lies."""
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        manifest = json.dumps({"format_version": "99"}).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        import io as _io
        tar.addfile(info, _io.BytesIO(manifest))

    with pytest.raises(ValueError, match="format_version"):
        import_profile(bad, tmp_path / "imported", dry_run=True)


def test_import_dry_run_still_refuses_existing_target_without_force(
    sample_profile, tmp_path
):
    """Dry-run respects the same overwrite guard so the preview matches a
    real import's outcome."""
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)

    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff.txt").write_text("don't overwrite me")

    with pytest.raises(FileExistsError):
        import_profile(archive, target, dry_run=True)

    # Existing file untouched (it would be overwritten with --force, which
    # dry-run should preview without actually doing).
    assert (target / "stuff.txt").read_text() == "don't overwrite me"


def test_import_dry_run_with_force_does_not_touch_target(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)

    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff.txt").write_text("preserved")

    manifest = import_profile(archive, target, force=True, dry_run=True)
    assert manifest is not None
    # Force + dry-run: target preserved exactly
    assert (target / "stuff.txt").read_text() == "preserved"
    # Archive content NOT extracted
    assert not (target / "config.yaml").exists()


def test_list_archive_files_returns_profile_paths(sample_profile, tmp_path):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive, profile_name="list-test")
    files = list_archive_files(archive)
    # Strips the profile/ prefix
    assert ".env" in files
    assert "config.yaml" in files
    assert "profile.yaml" in files
    assert "MEMORY.md" in files
    # Excludes the manifest envelope
    assert "manifest.json" not in files
    # Sorted output
    assert files == sorted(files)


def test_list_archive_files_skips_excluded_session_artifacts(
    sample_profile, tmp_path
):
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive)  # default exclusion
    files = list_archive_files(archive)
    assert "sessions.db" not in files
    assert "llm_events.jsonl" not in files
    # logs/ subdir excluded
    assert not any(p.startswith("logs/") for p in files)


def test_list_archive_files_raises_for_missing_archive(tmp_path):
    with pytest.raises(FileNotFoundError):
        list_archive_files(tmp_path / "does-not-exist.tar.gz")


def test_dry_run_and_list_pair_for_cli_preview(sample_profile, tmp_path):
    """The CLI calls both functions to render a preview — verify they
    agree on what would happen."""
    archive = tmp_path / "exported.tar.gz"
    export_profile(sample_profile, archive, profile_name="preview")
    target = tmp_path / "fresh-target"

    manifest = import_profile(archive, target, dry_run=True)
    files = list_archive_files(archive)

    # Manifest values intact
    assert manifest["profile_name"] == "preview"
    # Files non-empty (a real export of a sample profile has multiple files)
    assert len(files) >= 4
    # Nothing actually written
    assert not target.exists()
