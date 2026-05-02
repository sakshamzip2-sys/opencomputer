"""Tests for the messaging-platforms wizard section."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_skip_branch_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    # First radiolist: 1 = Skip
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 1)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_setup_now_branch_calls_checklist_and_invokes_per_platform(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    fake_platforms = [
        {"name": "telegram", "label": "Telegram", "configured": False},
        {"name": "discord", "label": "Discord", "configured": False},
    ]
    monkeypatch.setattr(mp, "_discover_platforms", lambda: fake_platforms)

    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)  # set up now
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [0, 1])  # both

    invocations: list[str] = []

    def fake_invoke(name, ctx):
        invocations.append(name)
        return True

    monkeypatch.setattr(mp, "_invoke_platform_setup", fake_invoke)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)

    assert result == SectionResult.CONFIGURED
    assert invocations == ["telegram", "discord"]


def test_no_platforms_selected_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {"name": "telegram", "label": "Telegram", "configured": False},
    ])
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [])

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_is_messaging_platforms_configured(tmp_path):
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        is_messaging_platforms_configured,
    )

    empty = _make_ctx(tmp_path)
    assert is_messaging_platforms_configured(empty) is False

    with_platform = _make_ctx(
        tmp_path,
        config={"gateway": {"platforms": ["telegram"]}},
    )
    assert is_messaging_platforms_configured(with_platform) is True


# ─────────────────────────────────────────────────────────────────
# T — per-platform credential entry flow
# ─────────────────────────────────────────────────────────────────


def test_invoke_platform_setup_prompts_for_each_env_var(
    monkeypatch, tmp_path,
):
    """Telegram has 2 env_vars (BOT_TOKEN, USER_ID) — both prompted +
    saved when no existing values."""
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {
            "name": "telegram", "label": "Telegram",
            "env_vars": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"],
            "signup_url": "https://t.me/BotFather",
        },
    ])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)

    env_file = tmp_path / ".env"
    monkeypatch.setattr(mp, "default_env_file", lambda: env_file)
    monkeypatch.setattr(
        "opencomputer.cli_setup.env_writer.default_env_file",
        lambda: env_file,
    )

    prompts_seen: list[str] = []

    def fake_prompt(env_var):
        prompts_seen.append(env_var)
        return f"value-for-{env_var}"

    monkeypatch.setattr(mp, "_prompt_secret", fake_prompt)

    ctx = _make_ctx(tmp_path)
    mp._invoke_platform_setup("telegram", ctx)

    assert prompts_seen == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"]
    text = env_file.read_text()
    assert "TELEGRAM_BOT_TOKEN=value-for-TELEGRAM_BOT_TOKEN" in text
    assert "TELEGRAM_USER_ID=value-for-TELEGRAM_USER_ID" in text
    assert "telegram" in ctx.config["gateway"]["platforms"]


def test_invoke_platform_setup_use_existing_secret(monkeypatch, tmp_path):
    """When env var already set, user can pick 'use existing' (idx 0) — no prompt."""
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {
            "name": "discord", "label": "Discord",
            "env_vars": ["DISCORD_BOT_TOKEN"],
            "signup_url": "",
        },
    ])
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "existing-token")

    env_file = tmp_path / ".env"
    monkeypatch.setattr(mp, "default_env_file", lambda: env_file)
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)  # use existing

    prompt_called: list[bool] = []
    monkeypatch.setattr(
        mp, "_prompt_secret",
        lambda env_var: prompt_called.append(True) or "should-not-be-used",
    )

    ctx = _make_ctx(tmp_path)
    mp._invoke_platform_setup("discord", ctx)

    assert prompt_called == []
    assert not env_file.exists()
    assert "discord" in ctx.config["gateway"]["platforms"]


def test_invoke_platform_setup_unknown_platform_records_only_name(
    monkeypatch, tmp_path,
):
    """Falls back to name-only record when discovery doesn't include
    the requested platform."""
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [])

    ctx = _make_ctx(tmp_path)
    mp._invoke_platform_setup("custom-channel", ctx)

    assert ctx.config["gateway"]["platforms"] == ["custom-channel"]


def test_invoke_platform_setup_user_skips_prompt(monkeypatch, tmp_path):
    """User submits empty input → no .env write but platform still recorded."""
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {
            "name": "telegram", "label": "Telegram",
            "env_vars": ["TELEGRAM_BOT_TOKEN"],
            "signup_url": "",
        },
    ])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(mp, "default_env_file", lambda: env_file)
    monkeypatch.setattr(
        "opencomputer.cli_setup.env_writer.default_env_file",
        lambda: env_file,
    )
    monkeypatch.setattr(mp, "_prompt_secret", lambda env_var: None)

    ctx = _make_ctx(tmp_path)
    mp._invoke_platform_setup("telegram", ctx)

    assert not env_file.exists()
    assert "telegram" in ctx.config["gateway"]["platforms"]
