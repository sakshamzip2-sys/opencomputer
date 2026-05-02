"""Tests for the GitHub Copilot ACP provider plugin scaffold.

Ships the auth/discovery layer; ACP JSON-RPC transport is a follow-up.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_PROVIDER_PY = _REPO / "extensions" / "copilot-acp-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("copilot_acp_test", None)
    return _load_module("copilot_acp_test", _PROVIDER_PY)


def test_class_attributes():
    mod = _load()
    assert mod.CopilotACPProvider.name == "copilot-acp"
    assert mod.CopilotACPProvider.default_model
    # Marker URL (acp://...) keeps the OpenAI HTTP shape from firing
    assert mod.DEFAULT_COPILOT_ACP_BASE_URL.startswith("acp://")


def test_resolve_command_uses_default_copilot():
    mod = _load()
    with patch("shutil.which", return_value="/usr/local/bin/copilot"):
        cmd, args = mod.resolve_acp_command()
    assert cmd == "/usr/local/bin/copilot"
    assert args == ["--acp", "--stdio"]


def test_resolve_command_honors_env_override(monkeypatch):
    """OPENCOMPUTER_COPILOT_ACP_COMMAND env var overrides the default."""
    monkeypatch.setenv("OPENCOMPUTER_COPILOT_ACP_COMMAND", "/opt/copilot-cli")
    mod = _load()
    with patch("shutil.which", return_value="/opt/copilot-cli"):
        cmd, _ = mod.resolve_acp_command()
    assert cmd == "/opt/copilot-cli"


def test_resolve_command_honors_copilot_cli_path_legacy(monkeypatch):
    """COPILOT_CLI_PATH (Hermes-compat) is also honored."""
    monkeypatch.setenv("COPILOT_CLI_PATH", "/legacy-copilot")
    monkeypatch.delenv("OPENCOMPUTER_COPILOT_ACP_COMMAND", raising=False)
    mod = _load()
    with patch("shutil.which", return_value="/legacy-copilot"):
        cmd, _ = mod.resolve_acp_command()
    assert cmd == "/legacy-copilot"


def test_resolve_command_honors_args_override(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_COPILOT_ACP_ARGS", "--alt --stdio")
    mod = _load()
    with patch("shutil.which", return_value="/usr/local/bin/copilot"):
        _, args = mod.resolve_acp_command()
    assert args == ["--alt", "--stdio"]


def test_resolve_command_raises_when_not_installed(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_COPILOT_ACP_COMMAND", raising=False)
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    mod = _load()
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="GitHub Copilot CLI"):
            mod.resolve_acp_command()


def test_constructor_runs_command_resolution(monkeypatch):
    """Constructing the provider performs early command-existence validation."""
    monkeypatch.delenv("OPENCOMPUTER_COPILOT_ACP_COMMAND", raising=False)
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    mod = _load()
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="GitHub Copilot CLI"):
            mod.CopilotACPProvider()


def test_complete_raises_not_implemented_with_pending_message():
    """ACP JSON-RPC transport is a focused follow-up — surface that loudly."""
    import asyncio

    from plugin_sdk.core import Message

    mod = _load()
    with patch("shutil.which", return_value="/usr/local/bin/copilot"):
        p = mod.CopilotACPProvider()
    with pytest.raises(NotImplementedError, match="ACP"):
        asyncio.run(p.complete(
            model="copilot-claude-3-5-sonnet",
            messages=[Message(role="user", content="hi")],
        ))


def test_plugin_manifest():
    manifest_path = _REPO / "extensions" / "copilot-acp-provider" / "plugin.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "copilot-acp"
    assert "external_process" in setup["auth_methods"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "copilot-acp" in ids
