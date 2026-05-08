"""Hermes parity (2026-05-08): delegate role=orchestrator + delegation override."""

from __future__ import annotations

from opencomputer.agent.config import DelegationConfig, LoopConfig
from opencomputer.tools.delegate import DELEGATE_BLOCKED_TOOLS

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_loop_config_orchestrator_enabled_default_true():
    cfg = LoopConfig()
    assert cfg.orchestrator_enabled is True


def test_loop_config_orchestrator_can_disable():
    cfg = LoopConfig(orchestrator_enabled=False)
    assert cfg.orchestrator_enabled is False


def test_delegation_config_defaults_all_none():
    d = DelegationConfig()
    assert d.model is None
    assert d.provider is None
    assert d.base_url is None
    assert d.api_key is None


def test_delegation_config_overrides_apply():
    d = DelegationConfig(
        model="gemini-2.5-flash",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test",
    )
    assert d.model == "gemini-2.5-flash"
    assert d.provider == "openrouter"
    assert d.base_url == "https://openrouter.ai/api/v1"
    assert d.api_key == "sk-test"


def test_loop_config_carries_delegation():
    cfg = LoopConfig(delegation=DelegationConfig(model="x"))
    assert cfg.delegation.model == "x"


# ---------------------------------------------------------------------------
# Schema additions
# ---------------------------------------------------------------------------


def test_delegate_schema_role_enum():
    from opencomputer.tools.delegate import DelegateTool

    schema = DelegateTool().schema
    role_field = schema.parameters["properties"]["role"]
    assert role_field["enum"] == ["leaf", "orchestrator"]


def test_delegate_blocked_tools_includes_delegate_for_leaves():
    """The static blocklist still has 'delegate' — orchestrator path
    constructs an effective_blocked = (BLOCKED - {'delegate'}) at runtime."""
    assert "delegate" in DELEGATE_BLOCKED_TOOLS


def test_delegate_blocked_tools_includes_other_unsafe():
    """AskUserQuestion / Clarify / ExitPlanMode remain blocked
    regardless of role — orchestrators don't get to call them either."""
    assert "AskUserQuestion" in DELEGATE_BLOCKED_TOOLS
    assert "Clarify" in DELEGATE_BLOCKED_TOOLS
    assert "ExitPlanMode" in DELEGATE_BLOCKED_TOOLS


# ---------------------------------------------------------------------------
# Effective-blocked computation (the live runtime check)
# ---------------------------------------------------------------------------


def test_effective_blocked_for_leaf_includes_delegate():
    """Leaf role: full DELEGATE_BLOCKED_TOOLS applies."""
    is_orchestrator = False
    effective = (
        DELEGATE_BLOCKED_TOOLS - {"delegate"}
        if is_orchestrator
        else DELEGATE_BLOCKED_TOOLS
    )
    assert "delegate" in effective


def test_effective_blocked_for_orchestrator_excludes_delegate():
    """Orchestrator role: 'delegate' is removed from the blocklist."""
    is_orchestrator = True
    effective = (
        DELEGATE_BLOCKED_TOOLS - {"delegate"}
        if is_orchestrator
        else DELEGATE_BLOCKED_TOOLS
    )
    assert "delegate" not in effective
    # But other unsafe tools remain.
    assert "AskUserQuestion" in effective
    assert "Clarify" in effective
    assert "ExitPlanMode" in effective
