"""B1 — oc backup / oc backup restore tests.

Disaster-recovery CLI for ~/.opencomputer/<profile>/. Tar.gz format
with MANIFEST.json at root. Restore verifies HMAC chain before
atomic-rename into place.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from io import BytesIO
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_backup import backup_app

runner = CliRunner()


def _seed_profile(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("model: test\n")
    skills = profile_dir / "skills"
    skills.mkdir()
    (skills / "hello.md").write_text("# hello\n")
    cache = profile_dir / "cache"
    cache.mkdir()
    (cache / "transient.bin").write_bytes(b"\x00" * 16)


def test_backup_creates_archive_with_manifest(tmp_path: Path) -> None:
    profile = tmp_path / "test-profile"
    _seed_profile(profile)
    out = tmp_path / "out.tar.gz"

    result = runner.invoke(
        backup_app,
        ["create", "--profile-dir", str(profile), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("MANIFEST.json") for n in names)
        assert any(n.endswith("config.yaml") for n in names)
        assert any(n.endswith("skills/hello.md") for n in names)
        # cache/ is excluded by default
        assert not any("cache/transient.bin" in n for n in names)

        manifest_member = next(
            m for m in tar.getmembers() if m.name.endswith("MANIFEST.json")
        )
        manifest = json.loads(tar.extractfile(manifest_member).read())
        assert manifest["schema"] == 1
        assert "created_utc" in manifest
        assert "oc_version" in manifest


def test_round_trip_backup_restore(tmp_path: Path) -> None:
    profile = tmp_path / "src-profile"
    _seed_profile(profile)
    archive = tmp_path / "round.tar.gz"

    r = runner.invoke(
        backup_app,
        ["create", "--profile-dir", str(profile), "--out", str(archive)],
    )
    assert r.exit_code == 0, r.output

    target = tmp_path / "restored"
    r = runner.invoke(
        backup_app,
        ["restore", str(archive), "--profile-dir", str(target)],
    )
    assert r.exit_code == 0, r.output

    assert (target / "config.yaml").read_text() == "model: test\n"
    assert (target / "skills" / "hello.md").read_text() == "# hello\n"
    # MANIFEST.json should land in the restored profile
    assert (target / "MANIFEST.json").is_file()


def test_restore_aborts_on_non_empty_target_without_force(tmp_path: Path) -> None:
    profile = tmp_path / "src-profile"
    _seed_profile(profile)
    archive = tmp_path / "x.tar.gz"
    runner.invoke(
        backup_app, ["create", "--profile-dir", str(profile), "--out", str(archive)]
    )

    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("DO NOT TOUCH\n")

    r = runner.invoke(
        backup_app,
        ["restore", str(archive), "--profile-dir", str(target)],
    )
    assert r.exit_code != 0
    assert (target / "existing.txt").read_text() == "DO NOT TOUCH\n"


def test_restore_force_overwrites(tmp_path: Path) -> None:
    profile = tmp_path / "src-profile"
    _seed_profile(profile)
    archive = tmp_path / "y.tar.gz"
    runner.invoke(
        backup_app, ["create", "--profile-dir", str(profile), "--out", str(archive)]
    )

    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("OLD\n")

    r = runner.invoke(
        backup_app,
        ["restore", str(archive), "--profile-dir", str(target), "--force"],
    )
    assert r.exit_code == 0, r.output
    assert not (target / "existing.txt").exists()
    assert (target / "config.yaml").is_file()


def test_restore_aborts_on_unsupported_schema(tmp_path: Path) -> None:
    profile = tmp_path / "src-profile"
    _seed_profile(profile)
    archive = tmp_path / "schema.tar.gz"
    runner.invoke(
        backup_app, ["create", "--profile-dir", str(profile), "--out", str(archive)]
    )

    # Tamper the manifest schema by rebuilding the tarball with schema=999.
    rebuilt = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(rebuilt, "w:gz") as dst:
        for m in src.getmembers():
            if m.name.endswith("MANIFEST.json"):
                manifest_bytes = src.extractfile(m).read()
                manifest = json.loads(manifest_bytes)
                manifest["schema"] = 999
                new_bytes = json.dumps(manifest).encode("utf-8")
                m.size = len(new_bytes)
                dst.addfile(m, BytesIO(new_bytes))
            else:
                f = src.extractfile(m)
                if f is None:
                    dst.addfile(m)
                else:
                    dst.addfile(m, f)

    target = tmp_path / "x"
    r = runner.invoke(
        backup_app,
        ["restore", str(rebuilt), "--profile-dir", str(target)],
    )
    assert r.exit_code != 0
    assert "schema" in r.output.lower()


def test_no_include_sessions_excludes_sessions_db(tmp_path: Path) -> None:
    profile = tmp_path / "with-sessions"
    _seed_profile(profile)
    # Seed a fake sessions.db
    sqlite3.connect(str(profile / "sessions.db")).close()

    archive = tmp_path / "nos.tar.gz"
    r = runner.invoke(
        backup_app,
        [
            "create",
            "--profile-dir",
            str(profile),
            "--out",
            str(archive),
            "--no-include-sessions",
        ],
    )
    assert r.exit_code == 0

    with tarfile.open(archive, "r:gz") as tar:
        assert not any(n.endswith("sessions.db") for n in tar.getnames())


def test_includes_live_sessions_db_via_sqlite_backup_api(tmp_path: Path) -> None:
    profile = tmp_path / "with-live-sessions"
    _seed_profile(profile)
    db_path = profile / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (k TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()

    archive = tmp_path / "live.tar.gz"
    r = runner.invoke(
        backup_app, ["create", "--profile-dir", str(profile), "--out", str(archive)]
    )
    assert r.exit_code == 0, r.output

    with tarfile.open(archive, "r:gz") as tar:
        sess_member = next(m for m in tar.getmembers() if m.name.endswith("sessions.db"))
        extracted = tmp_path / "extracted-sessions.db"
        with open(extracted, "wb") as f:
            f.write(tar.extractfile(sess_member).read())
    conn = sqlite3.connect(str(extracted))
    rows = conn.execute("SELECT k FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]
