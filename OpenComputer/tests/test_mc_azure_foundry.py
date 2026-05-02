"""Tests for M.c — Azure AI Foundry provider (OpenAI-style)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_AZURE_PROVIDER_PY = _REPO / "extensions" / "azure-foundry-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_class():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("azure_foundry_test", None)
    mod = _load_module("azure_foundry_test", _AZURE_PROVIDER_PY)
    return mod.AzureFoundryProvider


def test_class_attributes():
    Cls = _load_class()
    assert Cls.name == "azure-foundry"
    assert Cls._api_key_env == "AZURE_FOUNDRY_API_KEY"
    assert Cls.default_model


def test_constructs_with_both_env_vars(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "sk-azure-test")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL",
                       "https://my-resource.openai.azure.com/openai/deployments/gpt-5/")
    Cls = _load_class()
    p = Cls()
    assert p._base.startswith("https://my-resource.openai.azure.com")


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x.azure.com/")
    Cls = _load_class()
    with pytest.raises(RuntimeError, match="AZURE_FOUNDRY_API_KEY"):
        Cls()


def test_missing_base_url_raises(monkeypatch):
    """Azure deployments have unique URLs — no default makes sense."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "sk-azure-test")
    monkeypatch.delenv("AZURE_FOUNDRY_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    Cls = _load_class()
    with pytest.raises(RuntimeError, match="AZURE_FOUNDRY_BASE_URL"):
        Cls()


def test_does_not_fall_back_to_openai_api_key(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-other")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x.azure.com/")
    Cls = _load_class()
    with pytest.raises(RuntimeError, match="AZURE_FOUNDRY_API_KEY"):
        Cls()


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "azure-foundry-provider" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "azure-foundry"
    assert setup["env_vars"] == ["AZURE_FOUNDRY_API_KEY"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "azure-foundry" in ids
