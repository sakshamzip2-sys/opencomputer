"""Tests for the GitHub Copilot provider plugin."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_COPILOT_PROVIDER_PY = _REPO / "extensions" / "github-copilot-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_class():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("copilot_test", None)
    return _load_module("copilot_test", _COPILOT_PROVIDER_PY)


def _clear_all_token_envs(monkeypatch):
    for env in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(env, raising=False)


def _stub_no_gh_cli(monkeypatch, tmp_path):
    """Make _read_gh_cli_token return None by pointing at empty home."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)


def test_class_attributes():
    mod = _load_class()
    assert mod.GitHubCopilotProvider.name == "copilot"
    assert mod.GitHubCopilotProvider.default_model
    assert mod.GitHubCopilotProvider._api_key_env == "COPILOT_GITHUB_TOKEN"


def test_uses_copilot_github_token_first(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp-copilot")
    monkeypatch.setenv("GH_TOKEN", "ghp-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-github")
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert p._api_key == "ghp-copilot"


def test_falls_back_to_gh_token(monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-github")
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert p._api_key == "ghp-gh"


def test_falls_back_to_github_token(monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-github")
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert p._api_key == "ghp-github"


def test_falls_back_to_gh_cli_hosts_yml(monkeypatch, tmp_path):
    _clear_all_token_envs(monkeypatch)
    # Set up a fake ~/.config/gh/hosts.yml with a token
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    hosts_dir = tmp_path / ".config" / "gh"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yml").write_text(
        "github.com:\n"
        "    user: alice\n"
        "    oauth_token: gho_from_gh_cli\n"
        "    git_protocol: https\n"
    )
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert p._api_key == "gho_from_gh_cli"


def test_raises_when_no_token_anywhere(monkeypatch, tmp_path):
    _clear_all_token_envs(monkeypatch)
    _stub_no_gh_cli(monkeypatch, tmp_path)
    mod = _load_class()
    with pytest.raises(RuntimeError, match="GitHub token"):
        mod.GitHubCopilotProvider()


def test_uses_default_copilot_base_url(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp-x")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("COPILOT_API_BASE_URL", raising=False)
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert "api.githubcopilot.com" in p._base


def test_respects_copilot_api_base_url_override(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp-x")
    monkeypatch.setenv("COPILOT_API_BASE_URL", "https://internal-copilot/v1")
    mod = _load_class()
    p = mod.GitHubCopilotProvider()
    assert p._base == "https://internal-copilot/v1"


def test_gh_cli_parser_handles_quoted_token(monkeypatch, tmp_path):
    _clear_all_token_envs(monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    hosts_dir = tmp_path / ".config" / "gh"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yml").write_text(
        'github.com:\n'
        '    oauth_token: "ghp_quoted_token"\n'
    )
    mod = _load_class()
    assert mod._read_gh_cli_token() == "ghp_quoted_token"


def test_gh_cli_parser_returns_none_when_no_github_block(monkeypatch, tmp_path):
    _clear_all_token_envs(monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    hosts_dir = tmp_path / ".config" / "gh"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yml").write_text(
        "enterprise.example.com:\n    oauth_token: enterprise-only\n"
    )
    mod = _load_class()
    assert mod._read_gh_cli_token() is None


def test_gh_cli_parser_returns_none_when_file_absent(monkeypatch, tmp_path):
    _clear_all_token_envs(monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    mod = _load_class()
    assert mod._read_gh_cli_token() is None


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "github-copilot-provider" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "copilot"
    assert "COPILOT_GITHUB_TOKEN" in setup["env_vars"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "copilot" in ids
