"""Tests for ``opencomputer telegram topic-*`` CLI (Hermes PR 5.4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from extensions.telegram.dm_topics import DMTopicManager
from opencomputer.cli_telegram import telegram_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def profile_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point the CLI at an isolated profile home."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(home))
    return home


# ─── topic-create ────────────────────────────────────────────────────


class TestTopicCreate:
    def test_persists_via_dm_topic_manager_with_bot_api_call(
        self, runner: CliRunner, profile_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

        # Mock httpx.post — capture the call and synthesize a Bot API response.
        mock_response = MagicMock()
        mock_response.json = MagicMock(
            return_value={
                "ok": True,
                "result": {
                    "message_thread_id": 7,
                    "name": "Trading",
                    "icon_color": 0x6FB9F0,
                },
            }
        )
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = runner.invoke(
                telegram_app,
                [
                    "topic-create",
                    "Trading",
                    "--chat", "-100123",
                    "--skill", "stock-market-analysis",
                    "--system", "be terse",
                ],
            )

        assert result.exit_code == 0, result.output
        # Verify the Bot API was called with the right shape.
        assert mock_post.called
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json") or {}
        assert payload.get("chat_id") == "-100123"
        assert payload.get("name") == "Trading"

        # Verify persistence.
        mgr = DMTopicManager(profile_home)
        topic = mgr.get_topic("7")
        assert topic == {
            "label": "Trading",
            "skill": "stock-market-analysis",
            "system_prompt": "be terse",
            "parent_chat_id": "-100123",
        }

    def test_no_create_uses_topic_id_directly(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        with patch("httpx.post") as mock_post:
            result = runner.invoke(
                telegram_app,
                [
                    "topic-create",
                    "Existing",
                    "--no-create",
                    "--topic-id", "42",
                    "--skill", "alpha",
                ],
            )

        assert result.exit_code == 0, result.output
        assert not mock_post.called  # didn't hit the API

        mgr = DMTopicManager(profile_home)
        topic = mgr.get_topic("42")
        assert topic is not None
        assert topic["label"] == "Existing"
        assert topic["skill"] == "alpha"

    def test_no_create_without_topic_id_fails(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        result = runner.invoke(
            telegram_app, ["topic-create", "X", "--no-create"]
        )
        assert result.exit_code != 0
        assert "topic-id" in result.output.lower()

    def test_missing_chat_arg_fails(
        self, runner: CliRunner, profile_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
        result = runner.invoke(telegram_app, ["topic-create", "X"])
        assert result.exit_code != 0
        assert "--chat" in result.output

    def test_missing_token_fails(
        self, runner: CliRunner, profile_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        result = runner.invoke(
            telegram_app,
            ["topic-create", "X", "--chat", "-100123"],
        )
        assert result.exit_code != 0
        assert "TELEGRAM_BOT_TOKEN" in result.output

    def test_bot_api_error_surfaces(
        self, runner: CliRunner, profile_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")

        mock_response = MagicMock()
        mock_response.json = MagicMock(
            return_value={"ok": False, "description": "Bad Request: chat not found"}
        )
        with patch("httpx.post", return_value=mock_response):
            result = runner.invoke(
                telegram_app,
                ["topic-create", "X", "--chat", "-100123"],
            )

        assert result.exit_code != 0
        assert "chat not found" in result.output


# ─── topic-list ──────────────────────────────────────────────────────


class TestTopicList:
    def test_list_empty(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        result = runner.invoke(telegram_app, ["topic-list"])
        assert result.exit_code == 0
        assert "no DM topics registered" in result.output

    def test_list_shows_all_topics(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        mgr = DMTopicManager(profile_home)
        mgr.register_topic("1", label="Trading", skill="stocks")
        mgr.register_topic(
            "2",
            label="Research",
            system_prompt="cite primary sources only",
            parent_chat_id="-100",
        )

        result = runner.invoke(telegram_app, ["topic-list"])
        assert result.exit_code == 0
        out = result.output
        assert "Trading" in out
        assert "Research" in out
        assert "stocks" in out


# ─── topic-remove ────────────────────────────────────────────────────


class TestTopicRemove:
    def test_remove_existing(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        mgr = DMTopicManager(profile_home)
        mgr.register_topic("1", label="X")
        result = runner.invoke(telegram_app, ["topic-remove", "1"])
        assert result.exit_code == 0
        assert "removed" in result.output
        assert DMTopicManager(profile_home).get_topic("1") is None

    def test_remove_missing_returns_error(
        self, runner: CliRunner, profile_home: Path,
    ) -> None:
        result = runner.invoke(telegram_app, ["topic-remove", "missing"])
        assert result.exit_code != 0
        assert "no entry" in result.output


# ─── persistence sanity ─────────────────────────────────────────────


def test_cli_writes_to_profile_home(
    runner: CliRunner, profile_home: Path,
) -> None:
    """End-to-end: --no-create path produces telegram_dm_topics.json on disk."""
    result = runner.invoke(
        telegram_app,
        [
            "topic-create",
            "Mine",
            "--no-create",
            "--topic-id", "99",
        ],
    )
    assert result.exit_code == 0
    path = profile_home / "telegram_dm_topics.json"
    assert path.exists()
    raw = json.loads(path.read_text())
    assert "99" in raw
    assert raw["99"]["label"] == "Mine"
