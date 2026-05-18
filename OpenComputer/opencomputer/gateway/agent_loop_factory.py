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

Performance — per-profile resolution cache (2026-05-17)
-------------------------------------------------------

The expensive parts of construction (``load_config_for_profile`` YAML
reads, ``load_profile_config``, provider lookup + instantiation,
``allowed_tools`` frozenset materialisation) are deterministic given
``profile_home`` + ``model_override`` and a stable plugin registry.
They were previously re-executed on every delegate dispatch because a
child loop calls back into this factory.

We cache the resolved ``(provider, cfg, allowed_tools)`` tuple keyed
by ``(profile_id, model_override)``, gated on a snapshot signature of
the plugin registry's provider set. A fresh ``AgentLoop`` +
``DelegateTool`` is still constructed per call (correctness: two
concurrent delegates must not share message state), but the cached
prework keeps the hot-path delegate cost in the sub-millisecond range
instead of the multi-tens-of-ms YAML + plugin-walk cost.

Cache invalidation triggers:

* ``OPENCOMPUTER_AGENT_LOOP_FACTORY_NOCACHE=1`` → bypass entirely.
* Provider set in the plugin registry changes → all entries discarded
  (a re-discovery may have added or removed providers).
* :func:`invalidate_cache` → manual flush (one profile or all). Used
  by the hot-reload path (port plan Recipe 6).

The cache is process-scoped (not persisted) and bounded only by the
number of distinct ``(profile_id, model_override)`` pairs seen — in
practice ≤ N_profiles × small_constant.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from plugin_sdk.profile_context import set_profile

if TYPE_CHECKING:
    from opencomputer.agent.loop import AgentLoop

logger = logging.getLogger("opencomputer.gateway.agent_loop_factory")


# ---------------------------------------------------------------------------
# Per-profile resolution cache (perf TODO closed 2026-05-17)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _ResolvedProfile:
    """Immutable snapshot of the expensive resolution work.

    Cached per ``(profile_id, model_override)``. The cached tuple is fed
    into a fresh ``AgentLoop`` per call so message-state isolation is
    preserved — only the YAML loads, provider lookup, and allowlist
    derivation are skipped on cache hits.
    """

    cfg: Any                         # opencomputer.agent.config.Config
    provider: Any                    # plugin_sdk.provider_contract.BaseProvider
    allowed_tools: frozenset[str] | None
    provider_set_signature: int


_RESOLUTION_CACHE: dict[tuple[str, str], _ResolvedProfile] = {}
_RESOLUTION_LOCK = threading.Lock()


def _provider_set_signature() -> int:
    """Cheap fingerprint of the plugin registry's provider keyset.

    Used to invalidate the resolution cache when a plugin registers or
    unregisters a provider (which would change ``allowed_tools`` and
    potentially the ``provider`` class for affected profiles). Avoids
    importing the registry at module top-level (circularity-safe).
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    return hash(tuple(sorted(plugin_registry.providers.keys())))


def invalidate_cache(profile_id: str | None = None) -> None:
    """Drop cached resolutions.

    With ``profile_id=None`` (default) flushes every entry — used by
    the plugin loader after a hot-reload. With a specific id, flushes
    only matching entries (any ``model_override`` variant).
    """
    with _RESOLUTION_LOCK:
        if profile_id is None:
            _RESOLUTION_CACHE.clear()
            return
        # k is (profile_id, profile_home_str, model_override_str)
        to_drop = [k for k in _RESOLUTION_CACHE if k[0] == profile_id]
        for k in to_drop:
            _RESOLUTION_CACHE.pop(k, None)


def _cache_disabled() -> bool:
    """Honor the env-var test escape hatch."""
    val = os.environ.get("OPENCOMPUTER_AGENT_LOOP_FACTORY_NOCACHE", "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _resolve_profile_uncached(
    profile_id: str,
    profile_home: Path,
    model_override: str | None,
) -> _ResolvedProfile:
    """The legacy hot-path body. Pulled into a helper so the cached
    wrapper can be a thin pre/post around it. Caller MUST already be
    inside ``set_profile(profile_home)``."""
    from opencomputer.agent.config import load_config_for_profile
    from opencomputer.agent.profile_config import load_profile_config
    from opencomputer.plugins.registry import registry as plugin_registry

    # 1. Load this profile's config.yaml + profile.yaml under the
    #    correct ContextVar binding so field-factories pick up the
    #    profile-rooted paths.
    cfg = load_config_for_profile(profile_home)

    # 1b. Per-request model override (webui model-dropdown). ``Config``
    #     and ``ModelConfig`` are frozen+slots dataclasses, so the
    #     override is applied by rebuilding both immutably — direct
    #     assignment would raise FrozenInstanceError. Only the model id
    #     changes; ``provider`` is left intact so the step-2 provider
    #     lookup below still resolves the same plugin.
    if model_override and model_override != cfg.model.model:
        cfg = dataclasses.replace(
            cfg,
            model=dataclasses.replace(cfg.model, model=model_override),
        )

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
    #
    #    M3 #2 fix (gateway-vs-CLI parity): ``gateway.tool_filter``
    #    overrides this. ``profile`` (default) keeps the behaviour
    #    below; ``wildcard`` forces ``allowed_tools = None`` so the
    #    gateway sees the full tool registry exactly like the CLI
    #    (which never sets an allowlist).
    prof_cfg = load_profile_config(profile_home)
    allowed_tools: frozenset[str] | None
    tool_filter = getattr(getattr(cfg, "gateway", None), "tool_filter", "profile")
    if tool_filter == "wildcard":
        allowed_tools = None
    elif prof_cfg.enabled_plugins == "*":
        allowed_tools = None  # unrestricted (all loaded tools)
    else:
        assert isinstance(prof_cfg.enabled_plugins, frozenset)
        allowed_tools = frozenset(
            tool_name
            for plugin_id in prof_cfg.enabled_plugins
            for tool_name in plugin_registry.tools_provided_by(plugin_id)
        )

    return _ResolvedProfile(
        cfg=cfg,
        provider=provider,
        allowed_tools=allowed_tools,
        provider_set_signature=_provider_set_signature(),
    )


def _resolve_profile(
    profile_id: str,
    profile_home: Path,
    model_override: str | None,
) -> _ResolvedProfile:
    """Cached resolution wrapper. Invalidates on provider-set drift."""
    # Include profile_home in the key — in production profile_id maps
    # 1:1 to a home dir, but tests reuse profile_id across tmp dirs, and
    # nothing in the contract forbids a future caller from re-rooting a
    # profile. Cheap to include; eliminates a class of stale-cache bugs.
    cache_key = (profile_id, str(profile_home), model_override or "")

    if _cache_disabled():
        return _resolve_profile_uncached(profile_id, profile_home, model_override)

    with _RESOLUTION_LOCK:
        hit = _RESOLUTION_CACHE.get(cache_key)
        if hit is not None and hit.provider_set_signature == _provider_set_signature():
            return hit

    # Resolve outside the lock — the YAML reads are slow and we don't
    # want to serialize unrelated profiles. A racing build for the
    # same key is acceptable: both writes produce equivalent values,
    # last-writer-wins.
    fresh = _resolve_profile_uncached(profile_id, profile_home, model_override)

    with _RESOLUTION_LOCK:
        _RESOLUTION_CACHE[cache_key] = fresh

    return fresh


def build_agent_loop_for_profile(
    profile_id: str,
    profile_home: Path,
    *,
    model_override: str | None = None,
) -> AgentLoop:
    """Construct a fresh AgentLoop bound to ``profile_home``.

    All construction happens inside ``set_profile(profile_home)`` so
    the new loop's ``Config`` (which uses ``_home()`` in its field
    factories) captures the correct paths.

    ``model_override`` (optional) pins this loop to a specific model id,
    overriding the profile's default. The OpenAI-compat webui surface
    (``openai_compat._run_agent_completion``) passes the per-request
    model the user picked from the dropdown. The override is applied at
    construction time via ``dataclasses.replace`` because ``Config`` and
    ``ModelConfig`` are ``@dataclass(frozen=True, slots=True)`` — direct
    attribute assignment raises ``FrozenInstanceError``. When ``None``
    or empty (the default — the gateway dispatch path never passes it),
    behaviour is byte-identical to a loop built from the profile config
    unchanged.

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
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.delegate import DelegateTool

    profile_home.mkdir(parents=True, exist_ok=True)

    with set_profile(profile_home):
        # Cached resolution of the expensive parts: YAML loads, provider
        # instantiation, allowlist frozenset. See the module docstring
        # for the invalidation contract.
        resolved = _resolve_profile(profile_id, profile_home, model_override)

        # 4. Construct the loop — Pass-2 F1: ``(provider, config)`` —
        #    provider FIRST. ``allowed_tools`` is consumed at dispatch
        #    + schema-list time inside the loop.
        loop = AgentLoop(
            provider=resolved.provider,
            config=resolved.cfg,
            allowed_tools=resolved.allowed_tools,
        )

        # 5. Per-instance ``DelegateTool`` (G3 + F7). The closure
        #    captures ``profile_id`` and ``profile_home`` so a child
        #    agent spawned via this loop's delegate runs under the
        #    same profile. Default-args bind the captured values
        #    eagerly (mypy-friendly, also avoids late-binding gotchas
        #    if someone mutates the outer ``profile_home`` reference).
        delegate = DelegateTool()

        # PERF (2026-05-17, closes the old `TODO(perf)` here): the
        # delegate factory now reuses the cached resolution of
        # ``(provider, cfg, allowed_tools)`` instead of repeating the
        # YAML + plugin-registry walk. A fresh AgentLoop + DelegateTool
        # are still built per invocation so concurrent delegates don't
        # share message state — only the deterministic prework is
        # amortised.
        #
        # We deliberately don't route through
        # ``AgentRouter.get_or_load(profile_id)`` (the obvious-looking
        # fix): that returns the SAME AgentLoop instance to every
        # caller, which corrupts message-state isolation when two
        # concurrent delegates run under the same profile.
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


__all__ = [
    "build_agent_loop_for_profile",
    "invalidate_cache",
]
