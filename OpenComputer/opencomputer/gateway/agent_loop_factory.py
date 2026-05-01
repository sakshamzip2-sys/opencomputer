"""Production factory for per-profile AgentLoops.

Phase 2 Task 2.4 of the profile-as-agent multi-routing plan. Every
``AgentLoop`` the gateway hands to ``Dispatch`` flows through this
function. The single contract is:

    Inside this call, ``current_profile_home`` is set to
    ``profile_home``, so ``Config`` field-factories capture the right
    paths.

Audit fixes covered:

* G1 (HIGH) — ``set_profile`` wraps the entire construction so
  ``Config.session.db_path``, ``Config.memory.declarative_path`` etc.
  bind to ``profile_home``, not the process default.
* G2 (HIGH) — the loop's ``allowed_tools`` allowlist is derived from
  the profile's ``plugins.enabled`` list via
  :meth:`PluginRegistry.tools_provided_by`.
* G3 (HIGH) — each loop carries a per-instance ``DelegateTool`` whose
  factory closure binds ``(profile_id, profile_home)`` so a child agent
  spawned from this loop runs under the same profile.
* F1 (MEDIUM, Pass-2) — ``AgentLoop`` constructor signature is
  ``(provider, config, …)`` — provider FIRST. Verified against
  ``opencomputer.agent.loop:307-321`` and every existing call site in
  ``opencomputer/cli.py``.
* F7 (MEDIUM, Pass-2) — each per-profile loop has its own
  ``ConsentGate`` slot (``loop._consent_gate``). The factory does NOT
  register the channel-side prompt handler; that wiring lands in Task
  2.5 because the factory has no handle to the gateway's ``Dispatch``
  instance.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from plugin_sdk.profile_context import set_profile

if TYPE_CHECKING:
    from opencomputer.agent.loop import AgentLoop

logger = logging.getLogger("opencomputer.gateway.agent_loop_factory")


def build_agent_loop_for_profile(
    profile_id: str, profile_home: Path
) -> AgentLoop:
    """Construct a fresh AgentLoop bound to ``profile_home``.

    All construction happens inside ``set_profile(profile_home)`` so
    the new loop's ``Config`` (which uses ``_home()`` in its field
    factories) captures the correct paths.

    The returned loop has:

    * ``Config`` with profile-correct paths (``sessions.db``,
      ``MEMORY.md``, …)
    * ``allowed_tools`` allowlist matching the profile's
      ``plugins.enabled`` list (or ``None`` when the profile is in
      wildcard mode)
    * a ``tools`` attribute (test-only inspection surface) holding
      per-instance tool objects whose factories close over
      ``(profile_id, profile_home)`` — currently one fresh
      ``DelegateTool`` per loop. ``run_conversation`` itself still
      dispatches through the global tool registry; production dispatch
      goes through ``loop._consent_gate``, NOT through this list.

    Caller responsibility (Pass-2 F7): if the loop's ``_consent_gate``
    is non-None, the caller must register the channel-side prompt
    handler on it. The factory cannot do this — it doesn't know which
    ``Dispatch`` instance owns the gateway. Task 2.5 wires that.
    """
    from opencomputer.agent.config import load_config_for_profile
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.profile_config import load_profile_config
    from opencomputer.plugins.registry import registry as plugin_registry
    from opencomputer.tools.delegate import DelegateTool

    profile_home.mkdir(parents=True, exist_ok=True)

    with set_profile(profile_home):
        # 1. Load this profile's config.yaml + profile.yaml under the
        #    correct ContextVar binding so field-factories pick up the
        #    profile-rooted paths.
        cfg = load_config_for_profile(profile_home)

        # 2. Resolve the provider per profile config. Plugins register
        #    the CLASS — instantiate with defaults (matches
        #    ``cli.py::_resolve_provider``). When the registry holds a
        #    pre-instantiated provider, pass it through.
        provider_cls = plugin_registry.providers.get(cfg.model.provider)
        if provider_cls is None:
            installed = list(plugin_registry.providers.keys()) or ["none"]
            raise RuntimeError(
                f"profile {profile_id!r}: provider {cfg.model.provider!r} "
                f"is not registered. Installed: {installed}. "
                f"Install or enable the provider plugin."
            )
        provider = (
            provider_cls() if isinstance(provider_cls, type) else provider_cls
        )

        # 3. Resolve the profile's enabled plugins → tool allowlist.
        #    ``"*"`` means "no filter" (legacy single-profile shape);
        #    a concrete frozenset becomes the allowed-tools allowlist.
        prof_cfg = load_profile_config(profile_home)
        allowed_tools: frozenset[str] | None
        if prof_cfg.enabled_plugins == "*":
            allowed_tools = None  # unrestricted (all loaded tools)
        else:
            assert isinstance(prof_cfg.enabled_plugins, frozenset)
            allowed_tools = frozenset(
                tool_name
                for plugin_id in prof_cfg.enabled_plugins
                for tool_name in plugin_registry.tools_provided_by(plugin_id)
            )

        # 4. Construct the loop — Pass-2 F1: ``(provider, config)`` —
        #    provider FIRST. ``allowed_tools`` is consumed at dispatch
        #    + schema-list time inside the loop.
        loop = AgentLoop(
            provider=provider,
            config=cfg,
            allowed_tools=allowed_tools,
        )

        # 5. Per-instance ``DelegateTool`` (G3 + F7). The closure
        #    captures ``profile_id`` and ``profile_home`` so a child
        #    agent spawned via this loop's delegate runs under the
        #    same profile. Default-args bind the captured values
        #    eagerly (mypy-friendly, also avoids late-binding gotchas
        #    if someone mutates the outer ``profile_home`` reference).
        delegate = DelegateTool()

        # TODO(perf): each delegate invocation rebuilds a full
        # AgentLoop + Config + provider + plugin filter. AgentRouter
        # already caches per-profile loops, so this delegate factory
        # could route through `agent_router.get_or_load(profile_id)`
        # instead of recursing into build_agent_loop_for_profile.
        # Acceptable for v1; revisit if profiling shows hot delegate paths.
        def _delegate_factory(
            _pid: str = profile_id, _ph: Path = profile_home,
        ) -> AgentLoop:
            return build_agent_loop_for_profile(_pid, _ph)

        DelegateTool.set_factory(_delegate_factory, instance=delegate)

        # 6. Per-loop tools list. ``AgentLoop`` itself reaches into the
        #    global tool registry for dispatch (see
        #    ``opencomputer.agent.loop._dispatch_tool_calls``); the
        #    production dispatch path goes through ``loop._consent_gate``,
        #    NOT through this list.
        #
        #    NOTE (test-only inspection surface): nothing in production
        #    code reads ``loop.tools``. This attribute exists exclusively
        #    so tests can retrieve the per-instance ``DelegateTool`` and
        #    verify its factory closure (audit G3). Do not gate production
        #    behavior on it — walk ``_consent_gate`` or the tool registry
        #    for that purpose.
        loop.tools = [delegate]  # type: ignore[attr-defined]

    logger.info(
        "agent_loop_factory: built loop for profile_id=%s home=%s",
        profile_id, profile_home,
    )
    return loop


__all__ = ["build_agent_loop_for_profile"]
