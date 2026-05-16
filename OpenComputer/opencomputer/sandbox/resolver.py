"""Per-tool-call sandbox backend resolver — Milestone 2 (T2.4).

The strategies in this package each run a *single* argv inside a single
backend. They have no notion of *which* backend a given tool call should
use. This module adds that decision — :func:`resolve_backend` looks at
one tool, the active config, and a small context object, and answers:

* a :class:`~plugin_sdk.SandboxStrategy` — "route this call's sandboxed
  work through this backend"; or
* ``None`` — "no sandbox: run the tool exactly as it would run with no
  sandbox configured at all".

This generalizes :func:`opencomputer.sandbox.auto.auto_strategy` (which
only ever picks a *local* host backend and ignores its ``config`` arg):
the resolver is the piece that finally consumes the config to route a
call to the cloud (``e2b``) or keep it local — per tool call, not once
at process start (Hermes routes via a single ``TERMINAL_ENV`` env var
read at startup; OC's resolver is per-invocation, which is the
M2-original part of the design).

Decision order (plain branching — no magic; see :func:`resolve_backend`):

1. The tool sets ``sandbox_preference == "skip"`` → ``None``. A tool that
   opts out is never sandboxed, regardless of config.
2. Sandboxing is disabled globally (no backend configured / scope
   ``none``) → ``None`` for an ordinary tool. A tool that sets
   ``sandbox_preference == "required"`` is the one exception: its claim
   is honored — a backend is resolved, or the call fails, per the
   ``sandbox.fallback`` policy on
   :class:`~opencomputer.sandbox.policy.SandboxPolicy`.
3. The tool sets a ``sandbox_backend_hint`` and that named backend's
   :meth:`~plugin_sdk.SandboxBackend.is_available` is ``True`` → use the
   hinted backend.
4. Otherwise → the user's configured default backend.

**No-op guarantee.** With the default config (no ``sandbox.backend``
configured, scope ``none``) and a tool that sets no preference,
:func:`resolve_backend` returns ``None`` at branch 2 — so a caller that
unconditionally calls the resolver before every tool sees byte-identical
behavior to never having called it. Sandboxing is strictly opt-in.

The fallback policy (T2.9) governs what happens when a backend is asked
for but is *unreachable*. ``error`` (the default) fails loud — OC never
silently downgrades containment. ``local`` runs on the host with a
logged WARNING. The unreachable-at-*creation* case (E2B ``create()``
raising) is detected at run time by the caller, which compares against
:data:`SANDBOX_FALLBACK_ERROR` / :data:`SANDBOX_FALLBACK_LOCAL`; this
module also raises :class:`~plugin_sdk.SandboxUnavailable` eagerly when a
``required`` tool's backend cannot even be *named*.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opencomputer.sandbox.runner import _named_strategy
from plugin_sdk.sandbox import SandboxStrategy, SandboxUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opencomputer.agent.config import Config
    from plugin_sdk.tool_contract import BaseTool

_log = logging.getLogger("opencomputer.sandbox.resolver")

#: ``sandbox.fallback`` value — fail loud when the chosen backend is
#: unreachable. The default; OC never silently downgrades containment.
SANDBOX_FALLBACK_ERROR = "error"

#: ``sandbox.fallback`` value — run on the host (no sandbox) with a
#: logged WARNING when the chosen backend is unreachable.
SANDBOX_FALLBACK_LOCAL = "local"


def _sandbox_policy(config: Config | None) -> object | None:
    """Return the :class:`~opencomputer.sandbox.policy.SandboxPolicy` off ``config``.

    The M2 ``backend`` / ``fallback`` keys live on the same M1
    ``SandboxPolicy`` object that carries ``scope``, persisted as the
    ``sandbox:`` config block.

    ``None`` ``config`` — or a ``config`` without the field — yields
    ``None``, which every caller treats as "sandboxing disabled". Read
    defensively (``getattr``) so a partially-constructed test ``Config``
    can't crash the resolver.
    """
    if config is None:
        return None
    return getattr(config, "sandbox", None)


def _configured_backend_name(config: Config | None) -> str | None:
    """Return the user-configured default backend name, or ``None``.

    ``None`` means the user has not opted into sandboxing — there is no
    default backend. An empty / whitespace-only string is treated the
    same as unset. Reads the ``backend`` key of ``config.sandbox``.
    """
    policy = _sandbox_policy(config)
    if policy is None:
        return None
    raw = getattr(policy, "backend", None)
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def fallback_policy(config: Config | None) -> str:
    """Return the configured fallback policy — ``"error"`` or ``"local"``.

    Reads the ``fallback`` key of ``config.sandbox``. ``"error"`` is the
    default and the value returned for any unrecognised / missing
    setting: an unreachable backend should fail loud unless the operator
    has *explicitly* opted into host fallback.
    """
    policy = _sandbox_policy(config)
    if policy is None:
        return SANDBOX_FALLBACK_ERROR
    raw = getattr(policy, "fallback", SANDBOX_FALLBACK_ERROR)
    if raw == SANDBOX_FALLBACK_LOCAL:
        return SANDBOX_FALLBACK_LOCAL
    return SANDBOX_FALLBACK_ERROR


def _resolve_named(name: str) -> SandboxStrategy | None:
    """Resolve a backend by name; ``None`` if it is not available here.

    Wraps :func:`opencomputer.sandbox.runner._named_strategy`, which
    raises :class:`~plugin_sdk.SandboxUnavailable` both for an unknown
    name and for a known-but-unavailable backend. The resolver treats
    *both* as "this backend cannot be used" and returns ``None`` — the
    caller decides whether that is fatal (a ``required`` tool) or simply
    means "run un-sandboxed" (an ordinary tool).
    """
    try:
        return _named_strategy(name)
    except SandboxUnavailable as exc:
        _log.debug("sandbox backend %r unavailable: %s", name, exc)
        return None


def resolve_backend(
    tool: BaseTool,
    config: Config | None,
    ctx: object | None = None,
) -> SandboxStrategy | None:
    """Pick the sandbox backend for one tool call — or ``None`` for no sandbox.

    ``tool`` is the :class:`~plugin_sdk.tool_contract.BaseTool` about to
    run; its ``sandbox_preference`` / ``sandbox_backend_hint`` class
    fields steer the decision. ``config`` is the active
    :class:`~opencomputer.agent.config.Config`; its ``sandbox`` block
    (the M1 :class:`~opencomputer.sandbox.policy.SandboxPolicy`) carries
    the user's default ``backend`` + ``fallback`` policy. ``ctx`` is
    reserved for future scope-aware routing (session / agent id) —
    accepted, currently unused; pass it for forward-compatibility.

    Returns a :class:`~plugin_sdk.SandboxStrategy` to route the call's
    sandboxed work through, or ``None`` to run the tool with no sandbox
    (exactly as it runs when sandboxing is not configured at all).

    Raises :class:`~plugin_sdk.SandboxUnavailable` only in one case: the
    tool declares ``sandbox_preference == "required"`` AND no usable
    backend can be resolved AND the fallback policy is ``error``. A
    ``required`` tool under the ``local`` fallback policy returns
    ``None`` (run on host) with a logged WARNING instead.

    See the module docstring for the full decision order and the no-op
    guarantee for the default (un-opted-in) config.
    """
    del ctx  # reserved for scope-aware routing; see docstring.

    preference = getattr(tool, "sandbox_preference", "default")
    backend_hint = getattr(tool, "sandbox_backend_hint", None)
    tool_name = type(tool).__name__

    # --- Branch 1: the tool opted out. Never sandbox a "skip" tool. -------
    if preference == "skip":
        return None

    required = preference == "required"
    default_name = _configured_backend_name(config)

    # --- Branch 2: sandboxing is disabled globally (no default backend). --
    # An ordinary tool runs un-sandboxed — this is the no-op path that
    # keeps the default config byte-identical to pre-M2 behavior. A
    # ``required`` tool is the sole exception: honor its claim.
    if default_name is None and not required:
        return None

    # --- Branch 3: honor a tool's backend hint when it is available. -----
    if isinstance(backend_hint, str) and backend_hint.strip():
        hinted = _resolve_named(backend_hint.strip())
        if hinted is not None and hinted.is_available():
            return hinted
        _log.debug(
            "tool %s requested sandbox backend hint %r but it is "
            "unavailable; falling back to the configured default",
            tool_name,
            backend_hint,
        )

    # --- Branch 4: the user's configured default backend. ----------------
    if default_name is not None:
        resolved = _resolve_named(default_name)
        if resolved is not None and resolved.is_available():
            return resolved
        # The configured default exists in config but cannot run here.
        return _handle_unreachable_default(
            tool_name=tool_name,
            backend_name=default_name,
            required=required,
            config=config,
        )

    # No default configured, and we only reach here for a ``required``
    # tool (branch 2 returned for the ordinary case). It demands a
    # sandbox but the operator never named one.
    return _handle_unreachable_default(
        tool_name=tool_name,
        backend_name=None,
        required=required,
        config=config,
    )


def _handle_unreachable_default(
    *,
    tool_name: str,
    backend_name: str | None,
    required: bool,
    config: Config | None,
) -> SandboxStrategy | None:
    """Apply the fallback policy when no usable backend could be resolved.

    Called from :func:`resolve_backend`'s branch 4 when the configured
    default backend is unreachable, or when a ``required`` tool has no
    configured default at all.

    * ``required`` tool + ``error`` policy → raise
      :class:`~plugin_sdk.SandboxUnavailable` (fail loud).
    * ``required`` tool + ``local`` policy → ``None`` (run on host) with
      a logged WARNING — the operator explicitly opted into host
      fallback.
    * an ordinary tool → ``None`` (run un-sandboxed); a missing optional
      backend must not break a tool that only *prefers* a sandbox.
    """
    if not required:
        # Ordinary tool: a configured-but-unreachable backend simply
        # means "no sandbox for this call". No warning — the operator
        # configured the backend; the per-run failure (if any) surfaces
        # at the backend's own layer.
        return None

    target = repr(backend_name) if backend_name is not None else "<none configured>"
    if fallback_policy(config) == SANDBOX_FALLBACK_LOCAL:
        _log.warning(
            "sandbox: tool %s requires a sandbox but backend %s is "
            "unreachable; sandbox.fallback=local — running on the HOST "
            "without containment",
            tool_name,
            target,
        )
        return None

    raise SandboxUnavailable(
        f"sandbox: tool {tool_name} declares sandbox_preference='required' "
        f"but backend {target} is unavailable, and sandbox.fallback='error' "
        "(the default). Configure a reachable sandbox.backend, or set "
        "sandbox.fallback=local to permit running on the host."
    )


__all__ = [
    "SANDBOX_FALLBACK_ERROR",
    "SANDBOX_FALLBACK_LOCAL",
    "fallback_policy",
    "resolve_backend",
]
