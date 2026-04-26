"""Tests for ``opencomputer pair <platform>`` CLI (Phase 1.3).

Live-check is mocked; no network calls. Profile dir redirected to tmp_path
via OPENCOMPUTER_HOME.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from opencomputer.channels.pairing import PAIRERS, write_secret
from opencomputer.cli_pair import pair_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Redirect OPENCOMPUTER_HOME so tests don't touch the user's real profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))
    return home


# ---------- pairer registry ----------


def test_registry_lists_three_platforms():
    assert set(PAIRERS) >= {"telegram", "discord", "slack"}


@pytest.mark.parametrize(
    "platform,sample,valid",
    [
        ("telegram", "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_-1234567", True),
        ("telegram", "not-a-valid-token", False),
        ("telegram", "1234567890:has spaces no good", False),
        # Discord tokens are dot-separated; we only check format here, not realism.
        ("discord", "abcdefghijklmnopqrstuvwx.abcdef.0123456789012345678901234", True),
        ("discord", "single-segment-no-dots-here", False),
        ("slack", "xoxb-12345-67890-AbCdEfGhIjKlMnOpQrSt", True),
        ("slack", "xoxp-not-a-bot-token-bad", False),
    ],
)
def test_format_validation(platform, sample, valid):
    pairer = PAIRERS[platform]
    if valid:
        pairer.validate_format(sample)
    else:
        with pytest.raises(ValueError):
            pairer.validate_format(sample)


# ---------- write_secret ----------


def test_write_secret_creates_file_with_0600_perms(tmp_path):
    pairer = PAIRERS["telegram"]
    path = write_secret(tmp_path, pairer, "1234:abcXYZ_-")
    assert path.exists()
    assert path.read_text().strip() == "1234:abcXYZ_-"
    # On macOS / Linux check the permissions are restrictive
    import os
    if hasattr(os, "stat") and os.name != "nt":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


# ---------- list ----------


def test_pair_list_shows_supported_platforms():
    result = runner.invoke(pair_app, ["--list"])
    assert result.exit_code == 0
    assert "telegram" in result.stdout
    assert "discord" in result.stdout
    assert "slack" in result.stdout


# ---------- happy path: telegram with --token --skip-live-check ----------


def test_pair_telegram_writes_secret_and_grants_consent(_isolate_home):
    home = _isolate_home
    valid_token = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_-1234567"
    result = runner.invoke(
        pair_app, ["telegram", "--token", valid_token, "--skip-live-check"]
    )
    assert result.exit_code == 0, result.stdout
    secret_file = home / "secrets" / "telegram.token"
    assert secret_file.exists()
    assert secret_file.read_text().strip() == valid_token

    # Consent grant landed in sessions.db
    db = home / "sessions.db"
    assert db.exists()
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT capability_id, tier FROM consent_grants WHERE capability_id=?",
        ("channel.send.telegram",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "channel.send.telegram"

    # The F1 audit_log table exists after apply_migrations even if no rows yet
    conn = sqlite3.connect(db)
    has_audit_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone()
    conn.close()
    assert has_audit_table is not None


def test_pair_invalid_token_format_exits_nonzero(_isolate_home):
    result = runner.invoke(
        pair_app, ["telegram", "--token", "not-valid", "--skip-live-check"]
    )
    assert result.exit_code != 0
    # Secret file should NOT exist on failed format
    assert not (_isolate_home / "secrets" / "telegram.token").exists()


def _swap_live_check(monkeypatch: pytest.MonkeyPatch, platform: str, fn) -> None:
    """Replace ``PAIRERS[platform]`` with a clone whose ``live_check`` is ``fn``.

    Pairer is a frozen dataclass so we can't mutate it; we replace the
    dict entry instead. monkeypatch undoes the swap automatically.
    """
    from dataclasses import replace
    monkeypatch.setitem(PAIRERS, platform, replace(PAIRERS[platform], live_check=fn))


def test_pair_telegram_runs_live_check_when_not_skipped(_isolate_home, monkeypatch):
    valid_token = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_-1234567"
    calls: list[str] = []

    def fake(token: str) -> bool:
        calls.append(token)
        return True

    _swap_live_check(monkeypatch, "telegram", fake)
    result = runner.invoke(pair_app, ["telegram", "--token", valid_token])
    assert result.exit_code == 0
    assert calls == [valid_token]
    assert "live check passed" in result.stdout


def test_pair_telegram_continues_when_live_check_fails(_isolate_home, monkeypatch):
    """Live-check failure prints a warning but still writes the secret —
    user might be offline or behind a corporate proxy."""
    valid_token = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_-1234567"
    _swap_live_check(monkeypatch, "telegram", lambda t: False)
    result = runner.invoke(pair_app, ["telegram", "--token", valid_token])
    assert result.exit_code == 0
    assert "live check failed" in result.stdout
    assert (_isolate_home / "secrets" / "telegram.token").exists()


# ---------- discord / slack: format validation only (no live check) ----------


def test_pair_discord_with_valid_token(_isolate_home):
    valid = "abcdefghijklmnopqrstuvwx.abcdef.0123456789012345678901234"
    result = runner.invoke(pair_app, ["discord", "--token", valid])
    assert result.exit_code == 0
    assert (_isolate_home / "secrets" / "discord.token").exists()


def test_pair_slack_with_valid_token(_isolate_home):
    valid = "xoxb-12345-67890-AbCdEfGhIjKlMnOpQrSt"
    result = runner.invoke(pair_app, ["slack", "--token", valid])
    assert result.exit_code == 0
    assert (_isolate_home / "secrets" / "slack.token").exists()


# ---------- taxonomy entries ----------


def test_taxonomy_lists_channel_send_capabilities():
    from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
    from plugin_sdk import ConsentTier

    assert F1_CAPABILITIES["channel.send.telegram"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["channel.send.discord"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["channel.send.slack"] == ConsentTier.EXPLICIT
