"""Sandbox scope policy — agent / session / shared container scoping.

Milestone 1 of the Hermes + OpenClaw parity plan
(``docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/``).

The strategies in this package (``docker`` / ``linux`` / ``macos`` /
``ssh`` / ``none``) each run a *single* sandboxed invocation. They have
no notion of *which* container an invocation belongs to. This module
adds that notion — the **scope** — ported from OpenClaw's
``agents.defaults.sandbox.scope``.

Scope answers "how many containers exist, and what shares one":

* ``none``    — sandboxing off; run on the host. This is the current
  default, so upgrading users see no behavior change until they opt in.
* ``tool``    — one transient container per tool call (no sharing). OC's
  pre-scope behavior, named explicitly. No OpenClaw equivalent.
* ``session`` — one container per ``SessionDB`` session.
* ``agent``   — one container per agent id.
* ``shared``  — one container shared by every sandboxed invocation.

:class:`SandboxPolicy` is the persisted, per-profile policy object (the
``sandbox:`` block of the profile config). :func:`scope_key` turns a
policy + :class:`SandboxScopeContext` into the stable container key a
backend uses to decide whether two invocations share a container.

Container *reuse* itself (keep-alive containers, ``oc sandbox list`` /
``recreate`` / prune) is intentionally out of Milestone 1 — this module
ships the policy object and the key; the Milestone 2 backend resolver
consumes them.

Milestone 2 (2026-05-16) widens :class:`SandboxPolicy` with two more
keys of the same ``sandbox:`` block — ``backend`` (the default sandbox
backend name a tool call routes to) and ``fallback`` (what happens when
that backend is unreachable). The per-tool-call resolver
(:func:`opencomputer.sandbox.resolver.resolve_backend`) reads both. They
live on the M1 policy object rather than a separate config block so the
spec's ``sandbox.backend`` / ``sandbox.fallback`` key names are literally
true.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import Enum


class SandboxScope(str, Enum):
    """Container-scoping mode. See the module docstring for semantics.

    A plain ``(str, Enum)`` rather than ``StrEnum`` — matches the rest of
    the codebase; see the ``UP042`` ignore in ``pyproject.toml``.
    """

    NONE = "none"
    TOOL = "tool"
    SESSION = "session"
    AGENT = "agent"
    SHARED = "shared"


#: ``sandbox.fallback`` value — fail loud when the chosen backend is
#: unreachable. The default; OC never silently downgrades containment.
SANDBOX_FALLBACK_ERROR = "error"

#: ``sandbox.fallback`` value — run on the host (no sandbox) with a
#: logged WARNING when the chosen backend is unreachable.
SANDBOX_FALLBACK_LOCAL = "local"

#: The two accepted ``sandbox.fallback`` values. A config typo outside
#: this set fails loud in :meth:`SandboxPolicy.__post_init__` — the same
#: "a typo fails, not silently disables" principle ``scope`` follows.
_VALID_FALLBACKS: frozenset[str] = frozenset(
    {SANDBOX_FALLBACK_ERROR, SANDBOX_FALLBACK_LOCAL}
)


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a config value into a tuple of stripped, non-empty strings.

    Accepts a list/tuple of strings (the expected YAML shape) or a bare
    string (treated as a single-element list). Anything else yields an
    empty tuple. Non-string members are skipped rather than raising — a
    malformed allow/deny entry must not be able to wedge sandbox start-up.
    """
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        return ()
    return tuple(s.strip() for s in items if isinstance(s, str) and s.strip())


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Per-profile sandbox policy: scope + tool allow/deny + backend.

    Persisted under the ``sandbox:`` key of the profile config. The
    default — ``scope=NONE``, empty allow/deny, ``backend`` unset,
    ``fallback="error"`` — is exact current behavior (sandbox off, no
    tool restrictions, sandboxing not opted into), so an upgrading user
    sees zero change until they run ``oc sandbox enable`` / set a
    backend.
    """

    scope: SandboxScope = SandboxScope.NONE
    tools_allow: tuple[str, ...] = ()
    """Tools permitted when sandboxed. Empty = all permitted; a non-empty
    allow-list implicitly denies everything not listed."""
    tools_deny: tuple[str, ...] = ()
    """Tools forbidden when sandboxed. ``deny`` always beats ``allow``."""
    backend: str | None = None
    """M2 — default sandbox backend for tool calls: a strategy name such
    as ``"e2b"`` or ``"docker"``. ``None`` (default) = no backend
    configured: sandboxing is not opted into and the per-tool-call
    resolver runs every ordinary tool un-sandboxed (pre-M2 behavior).
    Persisted as the ``sandbox.backend`` config key."""
    fallback: str = SANDBOX_FALLBACK_ERROR
    """M2 — what happens when the chosen backend is unreachable (e.g. an
    E2B ``create()`` raises). ``"error"`` (default) fails the call loud —
    OC never silently downgrades containment. ``"local"`` runs the call
    on the host with a logged WARNING. Default ``"error"`` is deliberate:
    opting into host fallback is an explicit choice. Persisted as the
    ``sandbox.fallback`` config key."""

    def __post_init__(self) -> None:
        """Coerce loosely-typed inputs so the policy is always valid.

        ``oc config set sandbox.scope=session`` and the generic config
        override walker both hand the constructor a bare ``str`` scope
        (and ``list`` tool collections). Normalise them here — a frozen
        dataclass needs :func:`object.__setattr__` to write during
        ``__post_init__``. An unrecognised ``scope`` *or* ``fallback``
        string surfaces as a :class:`ValueError` rather than a silently
        broken policy — a config typo fails loud, not silently.
        """
        if not isinstance(self.scope, SandboxScope):
            object.__setattr__(self, "scope", SandboxScope(self.scope))
        if isinstance(self.tools_allow, list):
            object.__setattr__(self, "tools_allow", tuple(self.tools_allow))
        if isinstance(self.tools_deny, list):
            object.__setattr__(self, "tools_deny", tuple(self.tools_deny))
        if self.fallback not in _VALID_FALLBACKS:
            valid = ", ".join(sorted(_VALID_FALLBACKS))
            raise ValueError(
                f"invalid sandbox.fallback {self.fallback!r}; "
                f"valid values: {valid}"
            )

    @property
    def enabled(self) -> bool:
        """True when sandboxing is active (any scope other than ``none``)."""
        return self.scope is not SandboxScope.NONE

    def tool_allowed(self, tool_name: str) -> bool:
        """Whether ``tool_name`` may run inside the sandbox.

        OpenClaw semantics (``sandbox-vs-tool-policy-vs-elevated``):
        ``deny`` always wins; a non-empty ``allow`` blocks everything not
        listed; an all-empty policy permits every tool. Matching is by
        exact tool name — ``group:*`` shorthands are not in Milestone 1.
        """
        if tool_name in self.tools_deny:
            return False
        if self.tools_allow:
            return tool_name in self.tools_allow
        return True

    @classmethod
    def from_mapping(cls, data: object) -> SandboxPolicy:
        """Build a policy from a config mapping (the ``sandbox:`` block).

        A non-mapping (or ``None``) yields the default policy. An
        unrecognised ``scope`` *or* ``fallback`` raises :class:`ValueError`
        — a typo in ``config.yaml`` should fail loudly, not silently
        disable the sandbox or downgrade containment.

        Reads the M2 ``backend`` / ``fallback`` keys alongside the M1
        ``scope`` / ``tools`` keys: this is the integration point through
        which :mod:`opencomputer.agent.config_store` populates the new
        keys (``Config.sandbox`` is built here).
        """
        if not isinstance(data, dict):
            return cls()
        raw_scope = data.get("scope", SandboxScope.NONE.value)
        try:
            scope = SandboxScope(raw_scope)
        except ValueError as exc:
            valid = ", ".join(s.value for s in SandboxScope)
            raise ValueError(
                f"invalid sandbox.scope {raw_scope!r}; valid values: {valid}"
            ) from exc
        tools = data.get("tools")
        tools = tools if isinstance(tools, dict) else {}
        # ``backend``: an empty / whitespace-only string counts as unset
        # so a blank ``backend:`` line in YAML does not opt into a
        # nonexistent ""-named backend.
        raw_backend = data.get("backend")
        backend: str | None = None
        if isinstance(raw_backend, str) and raw_backend.strip():
            backend = raw_backend.strip()
        # ``fallback``: default to ``error``; ``__post_init__`` validates
        # the value and raises on anything outside the accepted set.
        raw_fallback = data.get("fallback", SANDBOX_FALLBACK_ERROR)
        return cls(
            scope=scope,
            tools_allow=_coerce_str_tuple(tools.get("allow")),
            tools_deny=_coerce_str_tuple(tools.get("deny")),
            backend=backend,
            fallback=raw_fallback,
        )

    def to_mapping(self) -> dict[str, object]:
        """Serialise back to the ``sandbox:`` config block.

        Round-trips :meth:`from_mapping`. Default-valued fields — empty
        allow/deny lists, an unset ``backend``, the default ``error``
        ``fallback`` — are omitted so a freshly-enabled config stays
        minimal (mirrors how the empty ``tools`` dict is omitted).
        """
        out: dict[str, object] = {"scope": self.scope.value}
        tools: dict[str, list[str]] = {}
        if self.tools_allow:
            tools["allow"] = list(self.tools_allow)
        if self.tools_deny:
            tools["deny"] = list(self.tools_deny)
        if tools:
            out["tools"] = tools
        if self.backend is not None:
            out["backend"] = self.backend
        if self.fallback != SANDBOX_FALLBACK_ERROR:
            out["fallback"] = self.fallback
        return out


@dataclass(frozen=True, slots=True)
class SandboxScopeContext:
    """Identifiers a scope key is derived from.

    ``session_id`` keys ``SESSION`` scope; ``agent_id`` keys ``AGENT``
    scope. Both are optional: when the active scope needs an id that is
    absent, :func:`scope_key` falls back to a unique per-call key, so a
    missing id can never collapse two unrelated runs into one container.
    """

    session_id: str | None = None
    agent_id: str | None = None


def scope_key(policy: SandboxPolicy, ctx: SandboxScopeContext | None = None) -> str:
    """Return the stable container key implied by ``policy``'s scope.

    A backend uses this key to decide whether two invocations share a
    container. ``none`` means sandboxing is off — there is no container,
    so it returns the empty string (a falsy "no key" sentinel, never a
    valid container token). ``tool`` gets a fresh random key every call
    (no sharing — its correct behavior is a transient container per
    invocation). ``shared`` returns a constant; ``session`` / ``agent``
    hash the relevant id so repeat calls in the same scope collide onto
    one key. A non-empty result is always a Docker-name-safe token
    (``[a-z0-9-]``, ≤ 20 chars).
    """
    ctx = ctx or SandboxScopeContext()
    scope = policy.scope

    if scope is SandboxScope.NONE:
        # Sandboxing off — no container exists, so derive no key. An empty
        # string is the falsy "no container" sentinel; returning a random
        # uuid here would imply a phantom per-call container.
        return ""
    if scope is SandboxScope.TOOL:
        return uuid.uuid4().hex[:12]
    if scope is SandboxScope.SHARED:
        return "shared"

    if scope is SandboxScope.SESSION:
        ident, prefix = ctx.session_id, "session"
    else:  # SandboxScope.AGENT
        ident, prefix = ctx.agent_id, "agent"

    if not ident:
        # No id for a scope that needs one — fall back to a unique key
        # rather than merging unrelated runs into a single container.
        return uuid.uuid4().hex[:12]
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def pool_key_for(
    policy: SandboxPolicy, ctx: SandboxScopeContext | None = None
) -> str | None:
    """Stable container-reuse key for ``policy`` / ``ctx`` — or ``None``
    when the call is not poolable.

    Unlike :func:`scope_key`, this NEVER returns a per-call uuid: a
    ``None`` result means "use a transient container, do not pool". A
    call is poolable iff its scope is ``shared``, or ``session`` /
    ``agent`` AND the keying id (``session_id`` / ``agent_id``) is
    present. ``none`` / ``tool`` scope — and ``session`` / ``agent``
    scope with the id absent — are NOT poolable: pooling on a per-call
    key would create a fresh, never-reused container on every call.

    This is the gate the per-tool-call resolver (via the agent loop) and
    :func:`opencomputer.sandbox.runner.run_sandboxed` use to decide
    whether to thread a ``container_key`` onto :class:`SandboxConfig`.
    """
    ctx = ctx or SandboxScopeContext()
    scope = policy.scope
    if scope is SandboxScope.SHARED:
        return scope_key(policy, ctx)
    if scope is SandboxScope.SESSION and ctx.session_id:
        return scope_key(policy, ctx)
    if scope is SandboxScope.AGENT and ctx.agent_id:
        return scope_key(policy, ctx)
    return None
