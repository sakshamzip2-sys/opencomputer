"""PyPI update check (hermes parity).

Mirrors hermes-agent's ``hermes_cli/banner.py::prefetch_update_check``
behaviour; OC's port hits PyPI's JSON endpoint instead of git fetch
because OC is pip-distributed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """The update-check module holds module-level state for the
    background check result; reset between tests so they don't see
    each other's results."""
    from opencomputer import cli_update_check

    cli_update_check._check_done.clear()
    cli_update_check._latest_version = None
    yield
    cli_update_check._check_done.clear()
    cli_update_check._latest_version = None


def test_is_outdated_running_older_returns_true() -> None:
    from opencomputer.cli_update_check import _is_outdated

    assert _is_outdated("2026.4.20", "2026.4.26") is True
    assert _is_outdated("2026.3.31", "2026.4.1") is True
    assert _is_outdated("2025.12.31", "2026.1.1") is True


def test_is_outdated_running_same_returns_false() -> None:
    from opencomputer.cli_update_check import _is_outdated

    assert _is_outdated("2026.4.26", "2026.4.26") is False


def test_is_outdated_running_newer_returns_false() -> None:
    from opencomputer.cli_update_check import _is_outdated

    assert _is_outdated("2026.5.1", "2026.4.26") is False


def test_is_outdated_handles_unknown_version_strings() -> None:
    """A malformed running version (broken install) must not nag."""
    from opencomputer.cli_update_check import _is_outdated

    assert _is_outdated("0.0.0+unknown", "2026.4.26") is False
    assert _is_outdated("dev", "2026.4.26") is False
    assert _is_outdated("2026.4.26", "garbage") is False


def test_opt_out_via_env_var_skips_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OPENCOMPUTER_NO_UPDATE_CHECK=1 short-circuits without HTTP."""
    from opencomputer import cli_update_check

    monkeypatch.setenv("OPENCOMPUTER_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(cli_update_check, "_cache_path", lambda: tmp_path / ".uc")

    fetch_called: list[bool] = []
    monkeypatch.setattr(
        cli_update_check, "_fetch_pypi_latest", lambda: fetch_called.append(True)
    )

    cli_update_check.prefetch_update_check()
    cli_update_check._check_done.wait(timeout=0.5)

    assert fetch_called == []
    assert cli_update_check.get_update_hint(timeout=0.1) is None


def test_cached_fresh_result_skips_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from opencomputer import cli_update_check

    cache_file = tmp_path / ".update_check.json"
    cache_file.write_text(
        json.dumps({"latest": "2030.1.1", "ts": time.time()})
    )

    monkeypatch.setattr(cli_update_check, "_cache_path", lambda: cache_file)
    monkeypatch.delenv("OPENCOMPUTER_NO_UPDATE_CHECK", raising=False)

    fetch_called: list[bool] = []
    monkeypatch.setattr(
        cli_update_check, "_fetch_pypi_latest", lambda: fetch_called.append(True)
    )

    cli_update_check.prefetch_update_check()
    cli_update_check._check_done.wait(timeout=0.5)

    assert fetch_called == [], "fresh cache must short-circuit HTTP"
    hint = cli_update_check.get_update_hint(timeout=0.5)
    assert hint is not None and "2030.1.1" in hint


def test_stale_cache_triggers_http_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from opencomputer import cli_update_check

    cache_file = tmp_path / ".update_check.json"
    cache_file.write_text(
        json.dumps(
            {"latest": "2026.1.1", "ts": time.time() - 48 * 3600}
        )
    )

    monkeypatch.setattr(cli_update_check, "_cache_path", lambda: cache_file)
    monkeypatch.delenv("OPENCOMPUTER_NO_UPDATE_CHECK", raising=False)

    monkeypatch.setattr(
        cli_update_check, "_fetch_pypi_latest", lambda: "2030.1.1"
    )

    cli_update_check.prefetch_update_check()
    cli_update_check._check_done.wait(timeout=2.0)

    hint = cli_update_check.get_update_hint(timeout=2.0)
    assert hint is not None and "2030.1.1" in hint

    written = json.loads(cache_file.read_text())
    assert written["latest"] == "2030.1.1"


def test_get_update_hint_returns_none_when_uptodate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from opencomputer import cli_update_check

    monkeypatch.setattr(
        cli_update_check, "_cache_path", lambda: tmp_path / ".uc.json"
    )
    monkeypatch.delenv("OPENCOMPUTER_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(
        cli_update_check, "_fetch_pypi_latest", lambda: "1900.1.1"
    )

    cli_update_check.prefetch_update_check()
    assert cli_update_check.get_update_hint(timeout=2.0) is None


def test_fetch_failure_returns_none_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Offline / PyPI-down must not nag the user with an error."""
    from opencomputer import cli_update_check

    monkeypatch.setattr(
        cli_update_check, "_cache_path", lambda: tmp_path / ".uc.json"
    )
    monkeypatch.delenv("OPENCOMPUTER_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(cli_update_check, "_fetch_pypi_latest", lambda: None)

    cli_update_check.prefetch_update_check()
    assert cli_update_check.get_update_hint(timeout=2.0) is None
