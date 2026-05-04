"""Tests for opencomputer.profile_env_template (Phase 14.G)."""

from __future__ import annotations

from dataclasses import dataclass, field

from opencomputer.profile_env_template import (
    _render_env_var_block,
    _truncate_secret,
    render_env_template,
)

# ─── Fakes (avoid coupling to real PluginCandidate/PluginManifest) ───


@dataclass(frozen=True)
class _FakeProvider:
    id: str = ""
    env_vars: tuple[str, ...] = ()
    label: str = ""
    signup_url: str = ""


@dataclass(frozen=True)
class _FakeChannel:
    id: str = ""
    env_vars: tuple[str, ...] = ()
    label: str = ""
    signup_url: str = ""


@dataclass(frozen=True)
class _FakeSetup:
    providers: tuple[_FakeProvider, ...] = ()
    channels: tuple[_FakeChannel, ...] = ()


@dataclass(frozen=True)
class _FakeManifest:
    id: str = ""
    description: str = ""
    setup: _FakeSetup = field(default_factory=_FakeSetup)


@dataclass(frozen=True)
class _FakeCandidate:
    manifest: _FakeManifest


def _candidate(
    plugin_id: str,
    *,
    description: str = "",
    providers: tuple[_FakeProvider, ...] = (),
    channels: tuple[_FakeChannel, ...] = (),
) -> _FakeCandidate:
    return _FakeCandidate(
        manifest=_FakeManifest(
            id=plugin_id,
            description=description,
            setup=_FakeSetup(providers=providers, channels=channels),
        )
    )


# ─── _truncate_secret ───


def test_truncate_secret_short_value_only_shows_length():
    assert _truncate_secret("abcd") == "(4 chars)"


def test_truncate_secret_long_value_shows_prefix_and_length():
    out = _truncate_secret("sk-ant-abc123def456", prefix_len=5)
    assert out.startswith("sk-an")
    assert "(19 chars)" in out  # exact length of input
    assert "abc123def456" not in out  # full value never echoed


def test_truncate_secret_empty_returns_empty():
    assert _truncate_secret("") == ""


# ─── _render_env_var_block ───


def test_render_env_var_block_when_unset():
    out = _render_env_var_block("MY_VAR", current_env={})
    assert out == "MY_VAR="


def test_render_env_var_block_when_set_includes_hint_not_value():
    out = _render_env_var_block(
        "SECRET", current_env={"SECRET": "sk-very-long-secret-value-here"}
    )
    assert out.startswith("SECRET=")
    assert "currently:" in out
    assert "sk-very-long-secret-value-here" not in out  # not echoed


# ─── render_env_template ───


def test_render_includes_provider_env_vars():
    p = _candidate(
        "anthropic-provider",
        description="Anthropic Claude models",
        providers=(
            _FakeProvider(
                id="anthropic",
                env_vars=("ANTHROPIC_API_KEY",),
                label="Anthropic (Claude)",
                signup_url="https://console.anthropic.com/settings/keys",
            ),
        ),
    )
    out = render_env_template([p], current_env={})
    assert "ANTHROPIC_API_KEY=" in out
    assert "Anthropic (Claude)" in out
    assert "console.anthropic.com" in out


def test_render_includes_channel_env_vars():
    p = _candidate(
        "telegram",
        description="Telegram channel adapter",
        channels=(
            _FakeChannel(
                id="telegram",
                env_vars=("TELEGRAM_BOT_TOKEN",),
                label="Telegram",
                signup_url="https://t.me/BotFather",
            ),
        ),
    )
    out = render_env_template([p], current_env={})
    assert "TELEGRAM_BOT_TOKEN=" in out
    assert "Telegram" in out
    assert "BotFather" in out


def test_render_groups_per_plugin_with_label():
    p1 = _candidate(
        "openai-provider",
        providers=(_FakeProvider(env_vars=("OPENAI_API_KEY",), label="OpenAI"),),
    )
    p2 = _candidate(
        "discord",
        channels=(_FakeChannel(env_vars=("DISCORD_BOT_TOKEN",), label="Discord"),),
    )
    out = render_env_template([p1, p2], current_env={})
    # Each plugin section has a "# === <label> ===" header
    assert "# === OpenAI ===" in out
    assert "# === Discord ===" in out


def test_render_marks_disabled_plugins():
    p = _candidate(
        "rare-plugin",
        providers=(_FakeProvider(env_vars=("RARE_KEY",), label="Rare"),),
    )
    out = render_env_template(
        [p],
        enabled_ids={"some-other-plugin"},
        include_disabled=True,
        current_env={},
    )
    assert "[DISABLED]" in out
    assert "RARE_KEY=" in out


def test_render_skips_disabled_plugins_by_default():
    p = _candidate(
        "rare-plugin",
        providers=(_FakeProvider(env_vars=("RARE_KEY",), label="Rare"),),
    )
    out = render_env_template(
        [p],
        enabled_ids={"some-other-plugin"},
        include_disabled=False,
        current_env={},
    )
    assert "RARE_KEY=" not in out
    assert "Rare" not in out


def test_render_skips_empty_env_vars():
    p = _candidate(
        "nothing",
        providers=(_FakeProvider(env_vars=(), label="Nothing"),),
    )
    out = render_env_template([p], current_env={})
    # No section emitted when env_vars is empty
    assert "Nothing" not in out


def test_render_hints_existing_env_value():
    p = _candidate(
        "anthropic-provider",
        providers=(_FakeProvider(env_vars=("ANTHROPIC_API_KEY",), label="Anthropic"),),
    )
    out = render_env_template(
        [p],
        current_env={"ANTHROPIC_API_KEY": "sk-ant-abcdef123456"},
    )
    assert "ANTHROPIC_API_KEY=" in out
    assert "currently:" in out
    assert "sk-ant-abcdef123456" not in out  # full value not echoed


def test_render_handles_no_setup_block():
    """Plugin with empty setup block — no section emitted, no crash."""
    p = _candidate("toolless")
    out = render_env_template([p], current_env={})
    # toolless plugin should produce no section
    assert "toolless" not in out
    # Header still present
    assert "OpenComputer profile" in out


def test_render_header_includes_profile_name():
    out = render_env_template([], profile_name="work", current_env={})
    assert "profile: work" in out


def test_render_no_plugins_yields_empty_marker():
    out = render_env_template([], current_env={})
    assert "(no plugin env vars to declare)" in out


def test_render_includes_signup_url_when_present():
    p = _candidate(
        "x",
        providers=(
            _FakeProvider(
                env_vars=("X_KEY",), label="X", signup_url="https://x.example.com/signup"
            ),
        ),
    )
    out = render_env_template([p], current_env={})
    assert "docs: https://x.example.com/signup" in out


def test_render_omits_docs_line_when_no_signup_url():
    p = _candidate(
        "y", providers=(_FakeProvider(env_vars=("Y_KEY",), label="Y"),)
    )
    out = render_env_template([p], current_env={})
    assert "docs:" not in out
