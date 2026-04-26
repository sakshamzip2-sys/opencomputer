"""profile-scraper skill tests.

Tests use ``OPENCOMPUTER_HOME`` to redirect snapshot output, and
``monkeypatch.setattr(Path, "home", ...)`` to point the denylist /
home-relative scrapers at a hermetic tmp_path. Real-system sources
(brew, gh, mdfind, system_profiler) may return [] in CI — that's
fine; the orchestrator's contract is "run every source, record
attempts vs successes," not "every source must produce facts."
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.skills.profile_scraper.schema import ProfileFact, Snapshot
from opencomputer.skills.profile_scraper.scraper import (
    _DENYLIST_GLOBS,
    _is_denied,
    run_scrape,
    scrape_secrets_audit,
)


def test_profile_fact_defaults():
    f = ProfileFact(field="x", value="y", source="z")
    assert f.confidence == 1.0
    assert f.timestamp > 0


def test_denylist_blocks_ssh(tmp_path: Path, monkeypatch):
    fake_home = tmp_path
    (fake_home / ".ssh").mkdir()
    (fake_home / ".ssh" / "id_rsa").write_text("private")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert _is_denied(fake_home / ".ssh" / "id_rsa") is True
    # Sanity — a non-denied sibling should not be denied.
    other = fake_home / "ok.txt"
    other.write_text("ok")
    assert _is_denied(other) is False


def test_run_scrape_returns_snapshot(tmp_path: Path, monkeypatch):
    """Orchestrator runs every registered source even if many return []."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    snapshot = run_scrape()
    assert isinstance(snapshot, Snapshot)
    # All 12 sources are *attempted* — successes vary by machine.
    assert len(snapshot.sources_attempted) == 12
    # At least identity should succeed (always returns something).
    assert "identity" in snapshot.sources_succeeded


def test_run_scrape_writes_snapshot_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    run_scrape()
    out_dir = tmp_path / "profile_scraper"
    assert out_dir.exists()
    snapshots = list(out_dir.glob("snapshot_*.json"))
    assert len(snapshots) == 1
    # ``latest.json`` is always written alongside.
    assert (out_dir / "latest.json").exists()


def test_run_scrape_keeps_only_last_10_snapshots(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out_dir = tmp_path / "profile_scraper"
    out_dir.mkdir()
    # Pre-seed 12 fake old snapshots — the GC step in ``_write_snapshot``
    # should leave at most 10 (including the new one this call writes).
    for i in range(12):
        (out_dir / f"snapshot_{1000 + i}.json").write_text("{}")

    run_scrape()
    snapshots = sorted(out_dir.glob("snapshot_*.json"))
    assert len(snapshots) <= 10


def test_secrets_audit_returns_count_not_value(tmp_path: Path, monkeypatch):
    """Privacy invariant: audit reports counts + filenames, never the secret."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # Plant a fake .zshrc + .zsh_history with bait values.
    secret_value = "ghp_super_secret_token_value_xyz"
    (fake_home / ".zshrc").write_text(
        f"export GITHUB_TOKEN={secret_value}\nexport NORMAL_VAR=hi\n"
    )
    (fake_home / ".zsh_history").write_text(
        f"export API_KEY={secret_value}\necho hello\n"
    )

    facts = scrape_secrets_audit()
    # Each file produces one fact.
    assert len(facts) == 2
    for f in facts:
        # Value carries filename + count, not the secret itself.
        assert isinstance(f.value, dict)
        assert "file" in f.value and "count" in f.value
        assert isinstance(f.value["count"], int)
        assert f.value["count"] >= 1
        # The bait token must NOT appear anywhere in the fact's payload.
        serialised = repr(f.value) + repr(f.field) + repr(f.source)
        assert secret_value not in serialised
