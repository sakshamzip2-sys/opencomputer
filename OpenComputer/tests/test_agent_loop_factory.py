"""Production AgentLoop factory — builds a per-profile loop under set_profile.

Phase 2 Task 2.4 of the profile-as-agent multi-routing plan.

The factory ``build_agent_loop_for_profile`` is the keystone of the
gateway's per-profile dispatch path: every AgentLoop the gateway hands
to ``Dispatch`` flows through it. Three audit fixes are exercised here:

* G1 (HIGH) — construction is wrapped in ``set_profile(profile_home)``
  so ``Config`` field-factories capture the right paths.
* G2 (HIGH) — ``allowed_tools`` is derived from this profile's
  ``plugins.enabled`` list via ``PluginRegistry.tools_provided_by``.
* G3 (HIGH) — each loop's ``DelegateTool`` factory is per-instance and
  closes over ``(profile_id, profile_home)`` so a child agent spawned
  from this loop runs under the same profile.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.gateway.agent_loop_factory import build_agent_loop_for_profile
from opencomputer.plugins.registry import registry as plugin_registry
from plugin_sdk.provider_contract import BaseProvider


class _StubProvider(BaseProvider):
    """Minimal provider stub — no network, no env-var dependency.

    The factory only needs ``provider_cls()`` to succeed; nothing in
    these tests actually drives a turn. We avoid the real
    ``AnthropicProvider`` because it would gripe about a missing
    ``ANTHROPIC_API_KEY``.
    """

    name = "anthropic"

    async def complete(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError("stub")

    async def stream_complete(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError("stub")

    def cost_per_token(self, *args, **kwargs) -> tuple[float, float]:  # pragma: no cover
        return (0.0, 0.0)


@pytest.fixture(autouse=True)
def _registered_anthropic_provider():
    """Register a stub ``anthropic`` provider for the duration of each
    test so the factory's provider lookup succeeds without standing
    up real plugin discovery.

    Restores the prior provider mapping (and any tracked plugin tool
    set) on teardown to keep tests isolated.
    """
    prior_provider = plugin_registry.providers.get("anthropic")
    plugin_registry.providers["anthropic"] = _StubProvider
    prior_tools = plugin_registry._tools_by_plugin.get("anthropic-provider")
    # anthropic-provider provides zero tools — record an explicit empty
    # set so ``tools_provided_by("anthropic-provider")`` returns ()
    # without needing the plugin loaded.
    plugin_registry._tools_by_plugin["anthropic-provider"] = set()
    try:
        yield
    finally:
        if prior_provider is None:
            plugin_registry.providers.pop("anthropic", None)
        else:
            plugin_registry.providers["anthropic"] = prior_provider
        if prior_tools is None:
            plugin_registry._tools_by_plugin.pop("anthropic-provider", None)
        else:
            plugin_registry._tools_by_plugin["anthropic-provider"] = prior_tools


def test_factory_builds_under_set_profile(tmp_path: Path) -> None:
    """Config paths inside the loop must reflect profile_home, not the
    process-default. This is the audit-G1 correctness contract."""
    profile_home = tmp_path / "p1"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )

    loop = build_agent_loop_for_profile("p1", profile_home)
    assert loop.config.session.db_path.parent == profile_home
    assert loop.config.memory.declarative_path.parent == profile_home


def test_factory_per_profile_plugin_filter(tmp_path: Path) -> None:
    """Audit G2: each AgentLoop's tool registry filter reflects the
    profile's plugins.enabled list (not the global registry)."""
    profile_home = tmp_path / "p1"
    profile_home.mkdir()
    (profile_home / "profile.yaml").write_text(
        "plugins:\n  enabled: ['anthropic-provider']\n"
    )
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )

    loop = build_agent_loop_for_profile("p1", profile_home)
    # The loop carries an `allowed_tools` allowlist derived from enabled
    # plugins. Tools provided by NON-enabled plugins are excluded.
    assert loop.allowed_tools is not None  # opt-in filter active
    # When only anthropic-provider (which provides 0 tools) is enabled,
    # allowed_tools should be empty.
    assert loop.allowed_tools == frozenset()


def test_factory_delegate_factory_closes_over_profile(tmp_path: Path) -> None:
    """Audit G3: a delegate spawned from this loop must build its child
    under THIS profile's home, not whatever was last set globally."""
    p1 = tmp_path / "p1"
    p1.mkdir()
    p2 = tmp_path / "p2"
    p2.mkdir()
    for h in (p1, p2):
        (h / "config.yaml").write_text(
            "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
        )

    loop1 = build_agent_loop_for_profile("p1", p1)
    loop2 = build_agent_loop_for_profile("p2", p2)
    # Each loop's DelegateTool instance has its own factory closing
    # over its own profile_home.
    from opencomputer.tools.delegate import DelegateTool
    delegate_a = next(
        t for t in loop1.tools if isinstance(t, DelegateTool)
    )
    delegate_b = next(
        t for t in loop2.tools if isinstance(t, DelegateTool)
    )
    # Calling each delegate's factory should produce a loop bound to
    # the right profile_home.
    spawned_a = delegate_a._factory()
    spawned_b = delegate_b._factory()
    assert spawned_a.config.session.db_path.parent == p1
    assert spawned_b.config.session.db_path.parent == p2
