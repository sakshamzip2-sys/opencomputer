"""Smoke tests for M cohort — regional variant providers (Kimi China, Alibaba Coding Plan).

Same isolation pattern as test_p1d_provider_cohort. Values verified against
/Users/saksham/.hermes/hermes-agent/hermes_cli/auth.py.

Note: MiniMax China was excluded from this cohort because Hermes uses
anthropic_messages transport for it (not openai_chat) — same reason
the global MiniMax was removed in PR #303.
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


COHORT = [
    ("kimi-china-provider",          "KimiChinaProvider",
     "kimi-cn",              "KIMI_CN_API_KEY",              "moonshot.cn"),
    ("alibaba-coding-plan-provider", "AlibabaCodingPlanProvider",
     "alibaba-coding-plan",  "ALIBABA_CODING_PLAN_API_KEY",  "dashscope.aliyuncs.com"),
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
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)  # Alibaba fallback
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
    assert setup["env_vars"][0] == env_var


def test_alibaba_coding_plan_falls_back_to_dashscope_key(monkeypatch):
    """Alibaba Coding Plan accepts DASHSCOPE_API_KEY as a fallback —
    matches Hermes's api_key_env_vars=(primary, fallback) pattern."""
    monkeypatch.delenv("ALIBABA_CODING_PLAN_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fallback")
    Cls = _load_provider_class("alibaba-coding-plan-provider",
                                "AlibabaCodingPlanProvider")
    p = Cls()
    assert p._api_key == "sk-fallback"


def test_both_providers_appear_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    discovered_ids = {p["name"] for p in _discover_providers()}
    expected = {"kimi-cn", "alibaba-coding-plan"}
    missing = expected - discovered_ids
    assert not missing, f"providers missing from wizard discovery: {missing}"
