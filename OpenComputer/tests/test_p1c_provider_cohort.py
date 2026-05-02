"""Smoke tests for the P1.c provider cohort (Tencent, NVIDIA, HF, StepFun, Arcee).

Identical structure to test_p1b_provider_cohort.py — see that module's
docstring for rationale.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_provider_class(plugin_dirname: str, class_name: str):
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    unique = f"{plugin_dirname}_test"
    sys.modules.pop(unique, None)
    mod = _load_module(unique,
                        _REPO / "extensions" / plugin_dirname / "provider.py")
    return getattr(mod, class_name)


# (plugin_dir, class_name, provider_id, env_var, base_host_substring)
COHORT = [
    ("tencent-provider",     "TencentProvider",     "tencent",     "TOKENHUB_API_KEY", "tokenhub.tencentmaas.com"),
    ("nvidia-nim-provider",  "NvidiaNIMProvider",   "nvidia",      "NVIDIA_API_KEY",   "nvidia.com"),
    ("huggingface-provider", "HuggingFaceProvider", "huggingface", "HF_TOKEN",         "huggingface.co"),
    ("stepfun-provider",     "StepFunProvider",     "stepfun",     "STEPFUN_API_KEY",  "stepfun.ai"),
    ("arcee-provider",       "ArceeProvider",       "arcee",       "ARCEEAI_API_KEY",  "arcee.ai"),
]


@pytest.mark.parametrize("plugin_dir,class_name,expected_id,env_var,base_host", COHORT)
def test_provider_class_attributes(
    plugin_dir, class_name, expected_id, env_var, base_host, monkeypatch,
):
    Cls = _load_provider_class(plugin_dir, class_name)
    assert Cls.name == expected_id
    assert Cls._api_key_env == env_var
    assert Cls.default_model


@pytest.mark.parametrize("plugin_dir,class_name,expected_id,env_var,base_host", COHORT)
def test_provider_uses_correct_base_url(
    plugin_dir, class_name, expected_id, env_var, base_host, monkeypatch,
):
    monkeypatch.setenv(env_var, "sk-fake-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    Cls = _load_provider_class(plugin_dir, class_name)
    p = Cls()
    assert base_host in p._base


@pytest.mark.parametrize("plugin_dir,class_name,expected_id,env_var,base_host", COHORT)
def test_missing_api_key_raises_helpful_message(
    plugin_dir, class_name, expected_id, env_var, base_host, monkeypatch,
):
    monkeypatch.delenv(env_var, raising=False)
    Cls = _load_provider_class(plugin_dir, class_name)
    with pytest.raises(RuntimeError, match=env_var):
        Cls()


@pytest.mark.parametrize("plugin_dir,class_name,expected_id,env_var,base_host", COHORT)
def test_does_not_fall_back_to_openai_api_key(
    plugin_dir, class_name, expected_id, env_var, base_host, monkeypatch,
):
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-other-provider")
    Cls = _load_provider_class(plugin_dir, class_name)
    with pytest.raises(RuntimeError, match=env_var):
        Cls()


@pytest.mark.parametrize("plugin_dir,class_name,expected_id,env_var,base_host", COHORT)
def test_plugin_manifest_declares_correct_setup_provider(
    plugin_dir, class_name, expected_id, env_var, base_host,
):
    manifest_path = _REPO / "extensions" / plugin_dir / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == expected_id
    assert setup["env_vars"] == [env_var]


def test_all_five_providers_appear_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    discovered_ids = {p["name"] for p in _discover_providers()}
    expected = {"tencent", "nvidia", "huggingface", "stepfun", "arcee"}
    missing = expected - discovered_ids
    assert not missing, f"providers missing from wizard discovery: {missing}"
