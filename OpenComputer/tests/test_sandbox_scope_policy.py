"""Tests for the sandbox scope policy — Milestone 1 (Hermes + OpenClaw parity).

Covers ``opencomputer.sandbox.policy`` (T1.2), the ``run_sandboxed`` scope
plumbing + ``SandboxConfig.container_key`` (T1.3), and the ``sandbox:``
config block round-trip (T1.5).
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.config_store import _to_yaml_dict, load_config, save_config
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.policy import (
    SandboxPolicy,
    SandboxScope,
    SandboxScopeContext,
    scope_key,
)
from opencomputer.sandbox.runner import run_sandboxed
from plugin_sdk.sandbox import SandboxConfig

# ─── SandboxScope enum ────────────────────────────────────────────────


def test_scope_enum_has_five_reference_aligned_values() -> None:
    assert {s.value for s in SandboxScope} == {
        "none",
        "tool",
        "session",
        "agent",
        "shared",
    }


# ─── SandboxPolicy defaults + enabled ─────────────────────────────────


def test_default_policy_is_disabled_no_restrictions() -> None:
    pol = SandboxPolicy()
    assert pol.scope is SandboxScope.NONE
    assert not pol.enabled
    assert pol.tools_allow == () and pol.tools_deny == ()


def test_policy_enabled_for_every_non_none_scope() -> None:
    for scope in SandboxScope:
        pol = SandboxPolicy(scope=scope)
        assert pol.enabled is (scope is not SandboxScope.NONE)


# ─── tool_allowed — deny-wins / allow-restricts semantics ─────────────


def test_empty_policy_allows_every_tool() -> None:
    assert SandboxPolicy(scope=SandboxScope.SESSION).tool_allowed("Bash")


def test_deny_beats_allow() -> None:
    pol = SandboxPolicy(tools_allow=("Bash", "Read"), tools_deny=("Bash",))
    assert not pol.tool_allowed("Bash")
    assert pol.tool_allowed("Read")


def test_non_empty_allow_blocks_unlisted_tools() -> None:
    pol = SandboxPolicy(tools_allow=("Read",))
    assert pol.tool_allowed("Read")
    assert not pol.tool_allowed("Write")


# ─── from_mapping / to_mapping round-trip ─────────────────────────────


def test_from_mapping_parses_scope_and_tool_lists() -> None:
    pol = SandboxPolicy.from_mapping(
        {"scope": "agent", "tools": {"allow": ["Read"], "deny": ["Bash"]}}
    )
    assert pol.scope is SandboxScope.AGENT
    assert pol.tools_allow == ("Read",) and pol.tools_deny == ("Bash",)


def test_from_mapping_non_dict_yields_default() -> None:
    assert SandboxPolicy.from_mapping(None) == SandboxPolicy()
    assert SandboxPolicy.from_mapping("nonsense") == SandboxPolicy()


def test_from_mapping_invalid_scope_raises() -> None:
    with pytest.raises(ValueError, match="invalid sandbox.scope"):
        SandboxPolicy.from_mapping({"scope": "bogus"})


def test_to_mapping_round_trips_through_from_mapping() -> None:
    pol = SandboxPolicy(
        scope=SandboxScope.SESSION, tools_allow=("Read",), tools_deny=("Bash",)
    )
    assert SandboxPolicy.from_mapping(pol.to_mapping()) == pol


def test_to_mapping_omits_empty_tool_lists() -> None:
    assert SandboxPolicy(scope=SandboxScope.TOOL).to_mapping() == {"scope": "tool"}


# ─── __post_init__ coercion ───────────────────────────────────────────


def test_post_init_coerces_str_scope_to_enum() -> None:
    pol = SandboxPolicy(scope="session")  # type: ignore[arg-type]
    assert pol.scope is SandboxScope.SESSION


def test_post_init_coerces_list_tool_collections_to_tuples() -> None:
    pol = SandboxPolicy(tools_allow=["Read"], tools_deny=["Bash"])  # type: ignore[arg-type]
    assert pol.tools_allow == ("Read",) and pol.tools_deny == ("Bash",)


def test_post_init_rejects_unknown_scope_string() -> None:
    with pytest.raises(ValueError):
        SandboxPolicy(scope="bogus")  # type: ignore[arg-type]


# ─── scope_key ────────────────────────────────────────────────────────


def test_scope_key_none_and_tool_are_unique_per_call() -> None:
    for scope in (SandboxScope.NONE, SandboxScope.TOOL):
        pol = SandboxPolicy(scope=scope)
        assert scope_key(pol) != scope_key(pol)


def test_scope_key_shared_is_constant() -> None:
    pol = SandboxPolicy(scope=SandboxScope.SHARED)
    assert scope_key(pol) == scope_key(pol) == "shared"


def test_scope_key_session_is_deterministic_hash_of_session_id() -> None:
    pol = SandboxPolicy(scope=SandboxScope.SESSION)
    ctx = SandboxScopeContext(session_id="sess-abc")
    expected = "session-" + hashlib.sha256(b"sess-abc").hexdigest()[:12]
    assert scope_key(pol, ctx) == scope_key(pol, ctx) == expected


def test_scope_key_agent_keys_on_agent_id() -> None:
    pol = SandboxPolicy(scope=SandboxScope.AGENT)
    k1 = scope_key(pol, SandboxScopeContext(agent_id="researcher"))
    k2 = scope_key(pol, SandboxScopeContext(agent_id="researcher"))
    k3 = scope_key(pol, SandboxScopeContext(agent_id="writer"))
    assert k1 == k2 and k1 != k3 and k1.startswith("agent-")


def test_scope_key_missing_id_falls_back_to_unique() -> None:
    # session/agent scope with no id must NOT collapse unrelated runs.
    pol = SandboxPolicy(scope=SandboxScope.SESSION)
    assert scope_key(pol) != scope_key(pol)


def test_scope_key_is_docker_name_safe() -> None:
    pol = SandboxPolicy(scope=SandboxScope.SESSION)
    key = scope_key(pol, SandboxScopeContext(session_id="weird/id with spaces"))
    assert all(c.isalnum() or c == "-" for c in key) and len(key) <= 20


# ─── run_sandboxed scope plumbing + SandboxConfig.container_key ───────


def test_sandbox_config_container_key_defaults_none() -> None:
    assert SandboxConfig().container_key is None
    assert SandboxConfig(container_key="session-abc").container_key == "session-abc"


def test_docker_explain_uses_container_key_when_set() -> None:
    strat = DockerStrategy()
    keyed = strat.explain(["echo", "hi"], config=SandboxConfig(container_key="shared"))
    assert "oc-sandbox-shared" in keyed
    plain = strat.explain(["echo", "hi"], config=SandboxConfig())
    assert "oc-sandbox-explain" in plain


async def test_run_sandboxed_accepts_policy_and_runs() -> None:
    # The "none" strategy needs no host sandbox binary — exercises the
    # policy / scope_ctx plumbing end-to-end without a container.
    result = await run_sandboxed(
        ["echo", "scoped"],
        config=SandboxConfig(strategy="none"),
        policy=SandboxPolicy(scope=SandboxScope.SHARED),
        scope_ctx=SandboxScopeContext(session_id="s1"),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "scoped"


async def test_run_sandboxed_without_policy_is_unchanged() -> None:
    result = await run_sandboxed(["echo", "plain"], config=SandboxConfig(strategy="none"))
    assert result.exit_code == 0 and result.stdout.strip() == "plain"


# ─── config round-trip — the sandbox: block ───────────────────────────


def _load(text: str) -> object:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        path.write_text(text)
        return load_config(path)


def test_default_config_has_disabled_sandbox_policy() -> None:
    cfg = _load("timezone: UTC\n")
    assert cfg.sandbox == SandboxPolicy()  # type: ignore[attr-defined]


def test_sandbox_block_round_trips_through_save_and_load() -> None:
    import dataclasses

    base = load_config(Path("/nonexistent/config.yaml"))  # → defaults
    want = SandboxPolicy(scope=SandboxScope.SESSION, tools_deny=("Bash",))
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        save_config(dataclasses.replace(base, sandbox=want), path)
        assert load_config(path).sandbox == want


def test_default_sandbox_policy_not_serialized() -> None:
    base = load_config(Path("/nonexistent/config.yaml"))
    assert "sandbox" not in _to_yaml_dict(base)


def test_invalid_sandbox_scope_in_config_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="sandbox"):
        _load("sandbox:\n  scope: bogus\n")
