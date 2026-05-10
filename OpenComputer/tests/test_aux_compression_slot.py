"""Tests for `auxiliary.compression.*` nested config slot.

Hermes config v2 contract: when ``auxiliary.compression.{provider, model,
base_url, api_key, timeout}`` is set, takes precedence over flat
``summary_model``. Backward-compat: flat shape continues to work unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.auxiliary_client import (
    AuxiliaryConfig,
    AuxSlotConfig,
    DEFAULT_MODEL_BY_TASK,
    effective_compression_model,
    resolve_compression_endpoint,
    resolve_compression_provider,
)


def test_aux_slot_config_defaults() -> None:
    slot = AuxSlotConfig()
    assert slot.provider == "auto"
    assert slot.model == ""
    assert slot.base_url == ""
    assert slot.api_key == ""
    assert slot.timeout == 120.0


def test_compression_slot_optional_default_none() -> None:
    cfg = AuxiliaryConfig()
    assert cfg.compression is None


def test_compression_slot_overrides_summary_model() -> None:
    """When `compression` is set, it takes precedence over `summary_model`."""
    cfg = AuxiliaryConfig(
        summary_model="gpt-4o",
        compression=AuxSlotConfig(model="google/gemini-2.5-flash"),
    )
    assert effective_compression_model(cfg) == "google/gemini-2.5-flash"


def test_flat_summary_model_still_works_when_compression_unset() -> None:
    cfg = AuxiliaryConfig(summary_model="gpt-4o")
    assert effective_compression_model(cfg) == "gpt-4o"


def test_default_compression_model_when_neither_set() -> None:
    cfg = AuxiliaryConfig()
    assert effective_compression_model(cfg) == DEFAULT_MODEL_BY_TASK["summary"]


def test_compression_slot_with_empty_model_falls_back_to_flat() -> None:
    """If compression slot is present but model is empty, defer to flat."""
    cfg = AuxiliaryConfig(
        summary_model="gpt-4o",
        compression=AuxSlotConfig(provider="openrouter"),  # model unset
    )
    assert effective_compression_model(cfg) == "gpt-4o"


def test_provider_main_alias_resolves_to_active() -> None:
    """`provider: main` is an explicit alias of `auto` — both fall back to
    the active main provider."""
    cfg_main = AuxiliaryConfig(compression=AuxSlotConfig(provider="main"))
    cfg_auto = AuxiliaryConfig(compression=AuxSlotConfig(provider="auto"))
    assert resolve_compression_provider(cfg_main, "openrouter") == "openrouter"
    assert resolve_compression_provider(cfg_auto, "openrouter") == "openrouter"


def test_provider_named_overrides_main() -> None:
    cfg = AuxiliaryConfig(compression=AuxSlotConfig(provider="anthropic"))
    assert resolve_compression_provider(cfg, "openrouter") == "anthropic"


def test_provider_unset_returns_main() -> None:
    cfg = AuxiliaryConfig()
    assert resolve_compression_provider(cfg, "openrouter") == "openrouter"


def test_base_url_takes_precedence_over_provider() -> None:
    cfg = AuxiliaryConfig(
        compression=AuxSlotConfig(
            provider="openrouter",
            base_url="https://api.z.ai/api/coding/paas/v4",
        ),
    )
    assert resolve_compression_endpoint(cfg) == "https://api.z.ai/api/coding/paas/v4"


def test_no_base_url_returns_none() -> None:
    cfg = AuxiliaryConfig(compression=AuxSlotConfig(provider="anthropic"))
    assert resolve_compression_endpoint(cfg) is None


def test_load_config_parses_nested_compression_block(tmp_path: Path) -> None:
    """Round-trip: YAML → AuxiliaryConfig with nested compression slot."""
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "auxiliary:\n"
        "  compression:\n"
        "    provider: openrouter\n"
        "    model: google/gemini-2.5-flash\n"
        "    timeout: 90.0\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.auxiliary.compression is not None
    assert cfg.auxiliary.compression.provider == "openrouter"
    assert cfg.auxiliary.compression.model == "google/gemini-2.5-flash"
    assert cfg.auxiliary.compression.timeout == 90.0


def test_load_config_compression_unset_stays_none(tmp_path: Path) -> None:
    """Without an `auxiliary.compression:` block, compression stays None."""
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "auxiliary:\n  summary_model: gpt-4o\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.auxiliary.compression is None
    assert cfg.auxiliary.summary_model == "gpt-4o"
