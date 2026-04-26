"""Onboarding UX overhaul — multi-channel checklist + Quick Setup mode.

Ports the hermes-agent ``_GATEWAY_PLATFORMS`` registry pattern (see
``sources/hermes-agent-2026.4.23/hermes_cli/setup.py:2210-2261``) and
the "Welcome Back!" returning-user menu from
``setup.py:2984-3018``. Tests pin the new behaviour against the
historical Telegram-only prompt and the destructive Overwrite? prompt.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest


def test_channel_platforms_registry_covers_all_known_channels() -> None:
    """Every entry in ``_CHANNEL_PLUGIN_MAP`` has a row in the registry."""
    from opencomputer import setup_wizard

    plugin_ids_in_map = set(setup_wizard._CHANNEL_PLUGIN_MAP.values())
    plugin_ids_in_registry = {p for _, _, p in setup_wizard._CHANNEL_PLATFORMS}
    assert plugin_ids_in_map == plugin_ids_in_registry, (
        f"registry drift — map has {plugin_ids_in_map}, "
        f"registry has {plugin_ids_in_registry}"
    )


def test_optional_channel_offers_all_channels_not_just_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard surfaces every channel in the platform registry."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import default_config

    captured: list[object] = []

    def fake_print(*args, **kwargs) -> None:
        captured.extend(args)

    monkeypatch.setattr(setup_wizard.console, "print", fake_print)
    monkeypatch.setattr(setup_wizard.Prompt, "ask", lambda *a, **k: "")

    setup_wizard._optional_channel(default_config())

    text = " ".join(str(c) for c in captured).lower()
    for plugin_id in ("telegram", "discord", "slack"):
        assert plugin_id in text, f"expected '{plugin_id}' in surfaced channel list"


def test_optional_channel_enables_only_known_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typos and unknown ids are silently dropped; known ids reach auto-enable."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import default_config

    enabled_seen: list[list[str]] = []

    monkeypatch.setattr(
        setup_wizard,
        "_auto_enable_plugins_for_channels",
        lambda channels: enabled_seen.append(list(channels)),
    )
    monkeypatch.setattr(
        setup_wizard.Prompt, "ask", lambda *a, **k: "discord telegram bogus"
    )

    setup_wizard._optional_channel(default_config())

    assert enabled_seen == [["discord", "telegram"]]


def test_optional_channel_blank_input_is_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input means 'no channels' — auto-enable not called."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import default_config

    enabled_seen: list[list[str]] = []
    monkeypatch.setattr(
        setup_wizard,
        "_auto_enable_plugins_for_channels",
        lambda channels: enabled_seen.append(list(channels)),
    )
    monkeypatch.setattr(setup_wizard.Prompt, "ask", lambda *a, **k: "")

    setup_wizard._optional_channel(default_config())

    assert enabled_seen == []


def test_optional_channel_pre_selects_already_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermes parity: channels with their env var already set are visible
    in the prompt as '[configured]' so the user knows they don't need to
    re-enter the token."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import default_config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")
    captured: list[str] = []

    monkeypatch.setattr(
        setup_wizard.console,
        "print",
        lambda *args, **_: captured.extend(str(a) for a in args),
    )
    monkeypatch.setattr(setup_wizard.Prompt, "ask", lambda *a, **k: "")

    setup_wizard._optional_channel(default_config())

    text = " ".join(captured).lower()
    assert "telegram" in text
    assert "configured" in text


def test_run_setup_offers_returning_user_menu_not_overwrite_yn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing config → menu (Quick / Full / individual sections / Exit),
    not a destructive Overwrite? Y/N."""
    from opencomputer import setup_wizard

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "model:\n  provider: anthropic\n  model: claude-opus-4-7\n"
        "  api_key_env: ANTHROPIC_API_KEY\n"
    )
    monkeypatch.setattr(setup_wizard, "config_file_path", lambda: cfg_path)

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY())

    asked: list[str] = []

    def fake_prompt_ask(prompt, choices=None, **_):
        asked.append(str(prompt))
        return "exit"

    monkeypatch.setattr(setup_wizard.Prompt, "ask", fake_prompt_ask)

    setup_wizard.run_setup()

    joined = " ".join(asked).lower()
    assert "quick" in joined and "full" in joined, (
        "expected returning users to see a Quick / Full menu, not a yes/no overwrite"
    )


def test_run_setup_refuses_in_non_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-TTY invocation refuses early instead of hanging on first prompt."""
    import io

    from opencomputer import setup_wizard

    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc:
        setup_wizard.run_setup()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "interactive terminal" in err


def test_quick_setup_skips_env_key_prompt_when_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quick mode doesn't pester users about a key they've already exported."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import ModelConfig, default_config

    cfg = default_config()
    cfg = replace(
        cfg,
        model=ModelConfig(
            provider="anthropic",
            model="claude-opus-4-7",
            api_key_env="ANTHROPIC_API_KEY",
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-looking")

    api_key_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup_wizard,
        "_prompt_api_key",
        lambda env_key, signup_url: api_key_calls.append((env_key, signup_url)),
    )
    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *a, **k: False)

    setup_wizard._quick_setup(cfg)

    assert api_key_calls == [], "should have skipped the API-key prompt"


def test_quick_setup_prompts_for_env_key_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quick mode does prompt for the env key when it's actually missing."""
    from opencomputer import setup_wizard
    from opencomputer.agent.config import ModelConfig, default_config

    cfg = default_config()
    cfg = replace(
        cfg,
        model=ModelConfig(
            provider="anthropic",
            model="claude-opus-4-7",
            api_key_env="ANTHROPIC_API_KEY",
        ),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    api_key_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup_wizard,
        "_prompt_api_key",
        lambda env_key, signup_url: api_key_calls.append((env_key, signup_url)),
    )
    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *a, **k: False)

    setup_wizard._quick_setup(cfg)

    assert len(api_key_calls) == 1
    assert api_key_calls[0][0] == "ANTHROPIC_API_KEY"
