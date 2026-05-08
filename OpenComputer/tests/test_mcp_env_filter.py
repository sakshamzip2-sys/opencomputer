"""Tests for Hermes-parity MCP subprocess env filter."""
from __future__ import annotations

from opencomputer.mcp.client import _build_mcp_subprocess_env


def test_passes_safe_vars():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "USER": "test",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "TERM": "xterm",
        "SHELL": "/bin/bash",
        "TMPDIR": "/tmp",
    }
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    for k, v in parent.items():
        assert out[k] == v


def test_passes_xdg_vars():
    parent = {
        "PATH": "/usr/bin",
        "XDG_DATA_HOME": "/home/test/.local/share",
        "XDG_CONFIG_HOME": "/home/test/.config",
        "XDG_CACHE_HOME": "/home/test/.cache",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    for k in parent:
        assert k in out, f"safe XDG var {k} missing"


def test_strips_anthropic_api_key():
    parent = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert "PATH" in out
    assert "ANTHROPIC_API_KEY" not in out


def test_strips_openai_api_key():
    parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-secret"}
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert "OPENAI_API_KEY" not in out


def test_strips_github_token():
    parent = {"PATH": "/usr/bin", "GITHUB_TOKEN": "ghp_secret"}
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert "GITHUB_TOKEN" not in out


def test_strips_aws_creds():
    parent = {
        "PATH": "/usr/bin",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "secret",
    }
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert "AWS_ACCESS_KEY_ID" not in out
    assert "AWS_SECRET_ACCESS_KEY" not in out


def test_strips_arbitrary_secrets():
    parent = {
        "PATH": "/usr/bin",
        "MY_PASSWORD": "p",
        "MY_TOKEN": "t",
        "SOME_CREDENTIAL": "c",
        "SOME_AUTH": "a",
    }
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert "PATH" in out
    for stripped in ("MY_PASSWORD", "MY_TOKEN", "SOME_CREDENTIAL", "SOME_AUTH"):
        assert stripped not in out, f"{stripped} should be stripped"


def test_declared_env_passes_through():
    """Per-server declared env (config.yaml) is explicit caller intent —
    it MUST pass through even if the key looks like a secret."""
    parent = {"PATH": "/usr/bin", "GITHUB_PERSONAL_ACCESS_TOKEN": "from-parent-stripped"}
    declared = {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_explicit-via-config"}
    out = _build_mcp_subprocess_env(parent, declared_env=declared)
    # Parent value was stripped; declared value passed through.
    assert out["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_explicit-via-config"


def test_declared_env_overrides_parent():
    parent = {"PATH": "/usr/bin", "MY_VAR": "from-parent"}
    declared = {"MY_VAR": "from-config"}
    out = _build_mcp_subprocess_env(parent, declared_env=declared)
    assert out["MY_VAR"] == "from-config"


def test_empty_declared_env_works():
    parent = {"PATH": "/usr/bin"}
    out = _build_mcp_subprocess_env(parent, declared_env={})
    assert out == {"PATH": "/usr/bin"}


def test_none_declared_env_works():
    parent = {"PATH": "/usr/bin"}
    out = _build_mcp_subprocess_env(parent, declared_env=None)
    assert out == {"PATH": "/usr/bin"}
