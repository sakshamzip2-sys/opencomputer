"""Phase 3.1 tests: config persistence + Anthropic plugin manifest."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_load_config_returns_defaults_when_no_file(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    cfg = load_config(tmp_path / "does_not_exist.yaml")
    assert cfg.model.provider == "anthropic"
    assert cfg.loop.max_iterations == 50


def test_load_config_applies_yaml_overrides(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
model:
  provider: openai
  model: gpt-5.4
  max_tokens: 2048
loop:
  max_iterations: 100
""",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.model.provider == "openai"
    assert cfg.model.model == "gpt-5.4"
    assert cfg.model.max_tokens == 2048
    assert cfg.loop.max_iterations == 100
    # Unchanged fields keep defaults
    assert cfg.loop.parallel_tools is True


def test_save_config_roundtrip(tmp_path: Path) -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import load_config, save_config, set_value

    config_file = tmp_path / "config.yaml"
    cfg = default_config()
    new_cfg = set_value(cfg, "model.provider", "openai")
    save_config(new_cfg, config_file)

    loaded = load_config(config_file)
    assert loaded.model.provider == "openai"


def test_set_value_rejects_top_level_keys() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import set_value

    cfg = default_config()
    with pytest.raises(KeyError, match="Top-level set not supported"):
        set_value(cfg, "model", "whatever")


def test_get_value_dotted_key() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import get_value

    cfg = default_config()
    assert get_value(cfg, "model.provider") == "anthropic"
    assert get_value(cfg, "loop.max_iterations") == 50


def test_get_value_unknown_key_raises() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import get_value

    cfg = default_config()
    with pytest.raises(KeyError):
        get_value(cfg, "model.does_not_exist")


def test_anthropic_plugin_manifest_discoverable() -> None:
    """After Phase 3.1, anthropic is also a plugin."""
    from opencomputer.plugins.discovery import discover

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    candidates = discover([ext_dir])
    ids = [c.manifest.id for c in candidates]
    assert "anthropic-provider" in ids
    ap = next(c for c in candidates if c.manifest.id == "anthropic-provider")
    assert ap.manifest.kind == "provider"
    assert ap.manifest.entry == "plugin"
