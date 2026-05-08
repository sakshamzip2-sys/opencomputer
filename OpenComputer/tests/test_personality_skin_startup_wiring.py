"""Verify the persistence -> read-at-startup loop works end to end.

The original PR shipped slash-command writes (`/personality concise`,
`/skin ares`) but no production code read those keys back. This test
suite ensures:

  1. ``get_default_personality`` and ``get_display_skin`` round-trip
     with the setters.
  2. ``get_custom_personalities`` returns sane shape from config.
  3. ``_load_custom_personalities`` (loop module-level helper) reads
     the active-profile config when env vars point at a tmp dir.
  4. ``_apply_personality_skin_at_startup`` (cli module-level helper)
     seeds runtime.custom and applies the skin.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.agent.profile_yaml import (
    get_custom_personalities,
    get_default_personality,
    get_display_skin,
    set_default_personality,
    set_display_skin,
)


@pytest.fixture(autouse=True)
def _isolate_profile_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    yield


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "default" / "config.yaml"


def test_default_personality_roundtrip(tmp_path):
    cfg = _config_path(tmp_path)

    assert get_default_personality(cfg) == ""

    set_default_personality(cfg, "concise")
    assert get_default_personality(cfg) == "concise"

    set_default_personality(cfg, "")
    assert get_default_personality(cfg) == ""


def test_display_skin_roundtrip(tmp_path):
    cfg = _config_path(tmp_path)

    assert get_display_skin(cfg) == ""

    set_display_skin(cfg, "ares")
    assert get_display_skin(cfg) == "ares"

    set_display_skin(cfg, "")
    assert get_display_skin(cfg) == ""


def test_get_custom_personalities_shape(tmp_path):
    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "agent:\n"
        "  personalities:\n"
        "    codereviewer: |\n"
        "      MARKER-CODE-REVIEW-XYZ\n"
        "    interviewer: 'Be a senior interviewer.'\n"
        "    bad: 42\n"      # non-string drops
        "    empty: ''\n"    # empty drops
    )

    custom = get_custom_personalities(cfg)
    assert "codereviewer" in custom
    assert "MARKER-CODE-REVIEW-XYZ" in custom["codereviewer"]
    assert custom["interviewer"] == "Be a senior interviewer."
    assert "bad" not in custom
    assert "empty" not in custom


def test_loop_helper_reads_active_profile(tmp_path):
    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "agent:\n"
        "  personalities:\n"
        "    pickme: 'live in active profile'\n"
    )

    from opencomputer.agent.loop import _load_custom_personalities
    custom = _load_custom_personalities()
    assert custom.get("pickme") == "live in active profile"


def test_cli_startup_helper_seeds_runtime_from_flag(tmp_path):
    """CLI flag wins over config."""
    from opencomputer.cli import _apply_personality_skin_at_startup
    from plugin_sdk.runtime_context import RuntimeContext

    set_default_personality(_config_path(tmp_path), "teacher")
    set_display_skin(_config_path(tmp_path), "ares")

    rt = RuntimeContext()
    _apply_personality_skin_at_startup(rt, "concise", "mono")

    assert rt.custom["personality"] == "concise"
    assert rt.custom["skin"] == "mono"


def test_cli_startup_helper_falls_back_to_config(tmp_path):
    """No flag → use persisted config."""
    from opencomputer.cli import _apply_personality_skin_at_startup
    from plugin_sdk.runtime_context import RuntimeContext

    set_default_personality(_config_path(tmp_path), "teacher")
    set_display_skin(_config_path(tmp_path), "ares")

    rt = RuntimeContext()
    _apply_personality_skin_at_startup(rt, "", "")

    assert rt.custom["personality"] == "teacher"
    assert rt.custom["skin"] == "ares"


def test_cli_startup_helper_defaults_skin_to_default(tmp_path):
    """No flag, no config → skin defaults to 'default'."""
    from opencomputer.cli import _apply_personality_skin_at_startup
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    _apply_personality_skin_at_startup(rt, "", "")

    assert rt.custom["skin"] == "default"
    # personality stays unset when no source provides one
    assert "personality" not in rt.custom or not rt.custom.get("personality")


def test_cli_startup_helper_swallows_errors(tmp_path, monkeypatch):
    """Bad config doesn't crash startup."""
    from opencomputer.cli import _apply_personality_skin_at_startup
    from plugin_sdk.runtime_context import RuntimeContext

    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("not :: valid\n  - [unclosed\n")

    rt = RuntimeContext()
    # Must not raise.
    _apply_personality_skin_at_startup(rt, "", "")


def test_slash_personality_accepts_custom_from_config(tmp_path, monkeypatch):
    """`/personality codereviewer` works after user adds it to config."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.skin_personality_cmd import (
        PersonalityCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "agent:\n"
        "  personalities:\n"
        "    codereviewer: 'Be a meticulous reviewer.'\n"
    )

    rt = RuntimeContext()
    result = asyncio.run(
        PersonalityCommand().execute("codereviewer", rt)
    )
    assert "Unknown" not in result.output
    assert rt.custom["personality"] == "codereviewer"


def test_slash_personality_rejects_unknown_with_no_custom(tmp_path):
    """`/personality bogus` still refuses when no custom defined."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.skin_personality_cmd import (
        PersonalityCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    result = asyncio.run(
        PersonalityCommand().execute("evil-overlord", rt)
    )
    assert "Unknown personality" in result.output
    assert "personality" not in rt.custom
