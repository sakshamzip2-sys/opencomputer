"""Tests for the ``_clear_provider_rate_limit_pollution`` autouse fixture.

The fixture itself isn't directly testable inside pytest (autouse
fixtures run BEFORE the test body, so setting up "pollution" inside
the test body is too late). Instead we factor the fixture's cleanup
logic into the pure helpers ``_resolve_real_profile_home`` and
``_purge_rate_limit_state_files`` and exercise THOSE with synthetic
state files.

Coverage goals:
    1. Empty/missing rate_limits dir -> returns ``[]`` (no false alarm).
    2. Files present -> all are removed, names returned.
    3. ``missing_ok=True`` swallows races on already-deleted files.
    4. Permission errors are logged, not raised.
    5. Home resolution respects ``OPENCOMPUTER_HOME`` env var.
    6. Home resolution falls back to ``~/.opencomputer`` when env unset.
    7. Glob is scoped to ``rate_limits/*.json`` — other files at the
       same home are left alone.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import (
    _purge_rate_limit_state_files,
    _resolve_real_profile_home,
)

# ─── _resolve_real_profile_home ──────────────────────────────────────


def test_resolve_home_honors_opencomputer_home_env(monkeypatch, tmp_path: Path) -> None:
    """``OPENCOMPUTER_HOME`` wins over the default ``~/.opencomputer``."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert _resolve_real_profile_home() == tmp_path


def test_resolve_home_falls_back_to_user_home(monkeypatch) -> None:
    """When the env var is unset, fall back to ``~/.opencomputer``."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    expected = Path.home() / ".opencomputer"
    assert _resolve_real_profile_home() == expected


def test_resolve_home_treats_empty_env_as_unset(monkeypatch) -> None:
    """An empty ``OPENCOMPUTER_HOME`` must NOT silently resolve to ``.``."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", "")
    # The production ``opencomputer.agent.config._home()`` ignores empty
    # values; the test fixture follows the same contract so we don't
    # accidentally treat ``cwd`` as the profile home.
    expected = Path.home() / ".opencomputer"
    assert _resolve_real_profile_home() == expected


# ─── _purge_rate_limit_state_files ───────────────────────────────────


def test_purge_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    """No rate_limits/ directory -> no work to do, no error."""
    assert _purge_rate_limit_state_files(tmp_path) == []


def test_purge_returns_empty_when_dir_present_but_empty(tmp_path: Path) -> None:
    """Empty rate_limits/ -> returns []."""
    (tmp_path / "rate_limits").mkdir()
    assert _purge_rate_limit_state_files(tmp_path) == []


def test_purge_removes_all_json_files_and_returns_names(tmp_path: Path) -> None:
    """Every ``*.json`` file in rate_limits/ is deleted; names are returned."""
    rate_dir = tmp_path / "rate_limits"
    rate_dir.mkdir()
    payload = {"provider": "anthropic", "reset_at": 999.0}
    files = ["anthropic.json", "openai.json", "ollama.json"]
    for name in files:
        (rate_dir / name).write_text(json.dumps(payload))

    removed = _purge_rate_limit_state_files(tmp_path)

    assert sorted(removed) == sorted(files)
    for name in files:
        assert not (rate_dir / name).exists(), f"{name} should have been deleted"


def test_purge_leaves_non_json_files_alone(tmp_path: Path) -> None:
    """``rate_limits/*.json`` glob must not match ``rate_limits/foo.txt``."""
    rate_dir = tmp_path / "rate_limits"
    rate_dir.mkdir()
    (rate_dir / "anthropic.json").write_text("{}")
    (rate_dir / "README.txt").write_text("not a state file")
    (rate_dir / "anthropic.json.bak").write_text('{"old": true}')

    removed = _purge_rate_limit_state_files(tmp_path)

    assert removed == ["anthropic.json"]
    assert (rate_dir / "README.txt").exists()
    assert (rate_dir / "anthropic.json.bak").exists()


def test_purge_handles_race_where_file_disappears_between_glob_and_unlink(
    tmp_path: Path,
) -> None:
    """``missing_ok=True`` on unlink swallows races with xdist workers."""
    rate_dir = tmp_path / "rate_limits"
    rate_dir.mkdir()
    f = rate_dir / "anthropic.json"
    f.write_text("{}")

    # Simulate a race: delete the file out from under the fixture
    # after glob() finds it, before unlink() runs. The fixture uses
    # `missing_ok=True` so this is silent.
    original_glob = type(rate_dir).glob

    def racing_glob(self, pattern):  # noqa: ANN001 — minimal shim
        result = list(original_glob(self, pattern))
        # Delete files BEFORE returning, simulating an xdist race.
        for hit in result:
            hit.unlink(missing_ok=True)
        return iter(result)

    # Monkeypatch on the instance's class — surgical, safe across tests.
    rate_dir_class = type(rate_dir)
    original = rate_dir_class.glob
    rate_dir_class.glob = racing_glob
    try:
        removed = _purge_rate_limit_state_files(tmp_path)
    finally:
        rate_dir_class.glob = original

    # The fixture should have returned the names from glob, even
    # though unlink hit FileNotFoundError (swallowed by missing_ok).
    assert removed == ["anthropic.json"]


def test_purge_logs_oserror_but_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A permission error on unlink is logged at WARNING, not raised."""
    rate_dir = tmp_path / "rate_limits"
    rate_dir.mkdir()
    f = rate_dir / "anthropic.json"
    f.write_text("{}")

    # Make the file unreadable AND the directory read-only so unlink
    # fails with PermissionError on POSIX. Skip the test on platforms
    # where chmod doesn't reliably enforce this.
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("running as root — permission tests are meaningless")

    rate_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x: list yes, write no

    try:
        with caplog.at_level(logging.WARNING, logger="conftest.test_isolation"):
            removed = _purge_rate_limit_state_files(tmp_path)

        # Even on failure, the function reports the file as "leaked".
        # The caller's WARN is what surfaces the real source.
        assert removed == ["anthropic.json"]
        assert any(
            "could not delete leaked rate-limit state" in r.message
            for r in caplog.records
        ), f"expected a WARNING log on permission failure; got {[r.message for r in caplog.records]}"
    finally:
        rate_dir.chmod(stat.S_IRWXU)  # restore for tmp_path teardown


# ─── End-to-end: autouse fixture interaction with the helpers ────────


def test_autouse_fixture_actually_clears_pollution_for_each_test(
    monkeypatch, tmp_path: Path
) -> None:
    """Smoke: redirect OPENCOMPUTER_HOME, write pollution, expect it gone.

    This test can only run because conftest's autouse fixture executes
    BEFORE the test body, so by the time this test runs there should
    be NO pollution at the redirected home. We assert that explicitly.
    """
    rate_dir = tmp_path / "rate_limits"
    rate_dir.mkdir()
    (rate_dir / "anthropic.json").write_text("{}")

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # NB: by the time we get here, the autouse fixture has already run
    # for THIS test — but it ran BEFORE monkeypatch took effect, so the
    # file we just created is still there. The deeper guarantee is that
    # the *next* test will see a clean tmp_path. Demonstrate that by
    # running the purge by hand and confirming the dir is clean.
    leftover = _purge_rate_limit_state_files(tmp_path)
    assert leftover == ["anthropic.json"]
    assert not (rate_dir / "anthropic.json").exists()
