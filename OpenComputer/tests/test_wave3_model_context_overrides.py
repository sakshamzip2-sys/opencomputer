"""Wave 3 — model_context_overrides flat config + status_line wiring.

User report (2026-05-08): the status bar showed ``475/200K`` while running
``claude-opus-4-7``, which actually has a 1M context window. Two-layer fix:

  1. Updated DEFAULT_CONTEXT_WINDOWS for Opus 4.6/4.7 → 1M (out-of-the-box
     accuracy for the current Anthropic line).
  2. Added Config.model_context_overrides — flat dict letting users
     correct any wrong/stale entry without editing source.
"""

from __future__ import annotations

from opencomputer.agent.compaction import (
    DEFAULT_CONTEXT_WINDOWS,
    context_window_with_overrides,
)
from opencomputer.agent.config import (
    Config,
    CustomProvider,
    CustomProviderModelOverride,
    default_config,
)
from opencomputer.cli_ui.status_line import max_context_for


def test_opus_47_default_is_1m():
    """Status-bar regression — claude-opus-4-7 must default to 1M."""
    assert DEFAULT_CONTEXT_WINDOWS["claude-opus-4-7"] == 1_000_000


def test_opus_46_default_is_1m():
    assert DEFAULT_CONTEXT_WINDOWS["claude-opus-4-6"] == 1_000_000


def test_max_context_for_opus_47_returns_1m():
    """End-to-end: status_line.max_context_for picks up the new value."""
    assert max_context_for("claude-opus-4-7") == 1_000_000


def test_model_context_overrides_wins_over_table():
    """User override beats the embedded static table."""
    assert (
        context_window_with_overrides(
            "claude-opus-4-7",
            model_context_overrides={"claude-opus-4-7": 500_000},
        )
        == 500_000
    )


def test_model_context_overrides_works_for_unknown_model():
    """Unknown model id with override returns the override, not 64K default."""
    assert (
        context_window_with_overrides(
            "vendor-x/some-new-model",
            model_context_overrides={"vendor-x/some-new-model": 256_000},
        )
        == 256_000
    )


def test_model_context_overrides_passed_through_max_context_for():
    """Status-line layer passes the kwarg through correctly."""
    assert (
        max_context_for(
            "anything",
            model_context_overrides={"anything": 750_000},
        )
        == 750_000
    )


def test_priority_global_overrides_wins_over_custom_provider_override():
    """When both layers set a value for the same model, global overrides
    wins (it's the explicit per-call user knob)."""
    cp = CustomProvider(
        name="local",
        base_url="http://x",
        models={"shared-id": CustomProviderModelOverride(context_length=8192)},
    )
    result = context_window_with_overrides(
        "shared-id",
        custom_providers=(cp,),
        model_context_overrides={"shared-id": 32_768},
    )
    assert result == 32_768


def test_default_config_has_empty_overrides():
    cfg = default_config()
    assert cfg.model_context_overrides == {}


def test_config_yaml_loads_model_context_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
model_context_overrides:
  some-model: 750000
  another: 128000
""",
        encoding="utf-8",
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_path)
    assert cfg.model_context_overrides["some-model"] == 750_000
    assert cfg.model_context_overrides["another"] == 128_000


def test_save_load_roundtrip_preserves_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config_store import load_config, save_config

    cfg = default_config()
    cfg = Config(
        **{
            **{
                f.name: getattr(cfg, f.name)
                for f in __import__("dataclasses").fields(cfg)
            },
            "model_context_overrides": {"my-model": 999_999},
        }
    )
    path = save_config(cfg, tmp_path / "config.yaml")
    cfg2 = load_config(path)
    assert cfg2.model_context_overrides["my-model"] == 999_999
