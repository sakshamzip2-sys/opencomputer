"""Wave 3 — OpenRouter provider_routing config + :nitro / :floor suffix."""

from __future__ import annotations

import importlib.util as _ilu
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import (
    ProviderRoutingConfig,
    split_or_routing_suffix,
)

_OR_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1] / "extensions" / "openrouter-provider" / "provider.py"
)


def _load_or_module():
    spec = _ilu.spec_from_file_location("_or_under_test", str(_OR_PROVIDER_PATH))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_provider_routing_to_body_block_minimal():
    pr = ProviderRoutingConfig()
    assert pr.to_body_block() is None


def test_provider_routing_to_body_block_full():
    pr = ProviderRoutingConfig(
        sort="price",
        only=("Anthropic", "Google"),
        ignore=("Together",),
        order=("Anthropic",),
        require_parameters=True,
        data_collection="deny",
    )
    block = pr.to_body_block()
    assert block == {
        "sort": "price",
        "only": ["Anthropic", "Google"],
        "ignore": ["Together"],
        "order": ["Anthropic"],
        "require_parameters": True,
        "data_collection": "deny",
    }


def test_provider_routing_yaml_lists_become_tuples(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
provider_routing:
  sort: price
  only:
    - Anthropic
  ignore:
    - Together
  data_collection: deny
""",
        encoding="utf-8",
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_path)
    pr = cfg.provider_routing
    assert pr.sort == "price"
    assert pr.only == ("Anthropic",)
    assert pr.ignore == ("Together",)
    assert pr.data_collection == "deny"


def test_split_or_routing_suffix_nitro():
    model, suffix = split_or_routing_suffix("anthropic/claude-sonnet-4:nitro")
    assert model == "anthropic/claude-sonnet-4"
    assert suffix == "nitro"


def test_split_or_routing_suffix_floor():
    model, suffix = split_or_routing_suffix("openai/gpt-4o:floor")
    assert model == "openai/gpt-4o"
    assert suffix == "floor"


def test_split_or_routing_suffix_unknown_passes_through():
    model, suffix = split_or_routing_suffix("anthropic/claude-opus-4:beta")
    assert model == "anthropic/claude-opus-4:beta"
    assert suffix is None


def test_split_or_routing_suffix_no_colon():
    model, suffix = split_or_routing_suffix("anthropic-claude-sonnet-4")
    assert model == "anthropic-claude-sonnet-4"
    assert suffix is None


def test_provider_local_split_helper_matches_global(monkeypatch):
    """The OR provider has an inlined copy due to plugin-SDK boundary."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    or_mod = _load_or_module()
    # Same behavior as the global helper for the recognized set.
    assert or_mod._split_or_routing_suffix("openai/gpt-4o:nitro") == (
        "openai/gpt-4o", "nitro",
    )
    assert or_mod._split_or_routing_suffix("openai/gpt-4o:floor") == (
        "openai/gpt-4o", "floor",
    )
    assert or_mod._split_or_routing_suffix("openai/gpt-4o:beta") == (
        "openai/gpt-4o:beta", None,
    )


def test_or_provider_routing_block_loaded_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OC_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
provider_routing:
  sort: price
  only:
    - Anthropic
  data_collection: deny
""",
        encoding="utf-8",
    )
    or_mod = _load_or_module()
    p = or_mod.OpenRouterProvider()
    assert p._provider_routing_block == {
        "sort": "price",
        "only": ["Anthropic"],
        "data_collection": "deny",
    }


def test_or_provider_no_routing_when_empty_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OC_HOME", str(tmp_path))
    or_mod = _load_or_module()
    p = or_mod.OpenRouterProvider()
    assert p._provider_routing_block is None
