"""ConsentGate — the authority that allows/denies privileged tool calls.

Invoked by AgentLoop BEFORE any PreToolUse hook fires. This is THE
security primitive — plugins cannot pre-empt it.

Resolution order for matching a claim against the ConsentStore:
    1. Try exact match on (capability_id, scope)       — e.g., grant was for "/Users/x/foo.py"
    2. Try global grant (capability_id, scope=None)    — grant covers any scope
    3. Try prefix match against any active grant's scope_filter
       — e.g., grant was for "/Users/x/Projects", call is "/Users/x/Projects/foo.py"
    4. If no match, deny.

Every call (allow or deny) is audit-logged. The decision's audit_event_id
points to the resulting row.

## Pending-approval registry (round 2a P-5)

When AgentLoop hits a Tier-2 (PER_ACTION) deny on a session that has a
channel adapter bound (e.g., Telegram), the loop calls
:meth:`request_approval` to block waiting for a user click. The channel
adapter's callback handler then calls :meth:`resolve_pending` with the
clicked decision (allow once, allow always, or deny). The two are joined
through an :class:`asyncio.Event` keyed on ``(session_id, capability_id)``
so the loop wakes up the moment the user taps a button.

Auto-deny on timeout (per L3 of the round-2a plan): if no callback
arrives within ``timeout_s`` (default 300s = 5 min), the gate logs a
warning, writes an audit event, and returns a deny decision so the loop
can proceed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
from opencomputer.agent.consent.store import ConsentStore
from plugin_sdk import CapabilityClaim, ConsentDecision, ConsentGrant, ConsentTier

if TYPE_CHECKING:
    from opencomputer.security.approvals import ApprovalsConfig as _ApprovalsConfig

_log = logging.getLogger("opencomputer.agent.consent.gate")

# Round 2a P-5 — type alias for the channel-side prompt sender. The
# handler is responsible for delivering the user-facing prompt (e.g.,
# Telegram inline buttons) and arranging for the eventual button click
# to call back into :meth:`ConsentGate.resolve_pending`. It returns
# True if a prompt was successfully dispatched (so the gate should
# block waiting for the user); False if no channel is available for
# this session (so the gate should immediately auto-deny).
PromptHandler = Callable[
    [str, CapabilityClaim, "str | None"], Awaitable[bool]
]


def render_prompt_message(claim: CapabilityClaim, scope: str | None) -> str:
    """Build the user-facing prompt for a Tier-2 (PER_ACTION) consent ask.

    F1 2.B.2 — when a scope is available, the prompt names the specific
    resource being accessed instead of just the capability class. This
    is what AgentLoop / TUI / wire clients show to a user when a
    capability requires per-action approval.

    Examples:
        render_prompt_message(claim, "/Users/x/foo.py")
        → "Allow read_files.metadata on /Users/x/foo.py? [y/N/session/always]"
        render_prompt_message(claim, None)
        → "Allow read_files.metadata? [y/N/session/always]"
    """
    cap = claim.capability_id
    if scope:
        return f"Allow {cap} on {scope}? [y/N/session/always]"
    return f"Allow {cap}? [y/N/session/always]"


#: Sentinel value for ``request_approval(timeout_s=...)`` so we can
#: distinguish "caller didn't pass anything" (resolve from config) vs
#: "caller passed an explicit float" (honour their value).
_TIMEOUT_FROM_CONFIG: float = -1.0


class ConsentGate:
    def __init__(self, *, store: ConsentStore, audit: AuditLogger) -> None:
        self._store = store
        self._audit = audit
        # Round 2a P-5 — pending-approval registry. Key is
        # ``(session_id, capability_id)``. The Event is set by
        # :meth:`resolve_pending` once the user clicks; the decision
        # 3-tuple ``(allowed, persist, session_scoped)`` carries the
        # click meaning back to the caller. Hermes-parity 4-verb
        # encoding:
        #   (True,  False, False) -> allow_once
        #   (True,  False, True)  -> allow_session (in-memory only)
        #   (True,  True,  False) -> allow_always (persistent grant)
        #   (False, _,     _)     -> deny
        self._pending_requests: dict[tuple[str, str], asyncio.Event] = {}
        self._pending_decisions: dict[
            tuple[str, str], tuple[bool, bool, bool]
        ] = {}
        # Hermes parity: session-scoped grants. Cleared on
        # SESSION_FINALIZE via :meth:`on_session_finalize`. NOT persisted
        # to ConsentStore — a session that ends loses its grants.
        self._session_grants: dict[tuple[str, str], ConsentGrant] = {}
        # Channel-side prompt handler (set by the gateway / dispatch
        # when a channel adapter is available). When None,
        # :meth:`request_approval` immediately auto-denies because there
        # is no surface to ask the user on.
        self._prompt_handler: PromptHandler | None = None
        # Cached approvals config (mode + timeout). Lazy-loaded on first
        # use to avoid hitting the YAML loader at construction time
        # (gates are constructed early in CLI startup before the active
        # profile is necessarily resolved). Refresh via
        # :meth:`refresh_approvals_config` when the operator changes the
        # config file.
        self._approvals_config: _ApprovalsConfig | None = None

    def _get_approvals_config(self) -> _ApprovalsConfig:
        """Lazy-load + cache the active profile's ``security.approvals`` config.

        Imports inside the function so a circular-import on cold load
        (security/__init__.py → approvals → profiles → ...) doesn't
        break gate construction. Always returns a usable
        :class:`ApprovalsConfig`; never raises.
        """
        if self._approvals_config is not None:
            return self._approvals_config
        try:
            from opencomputer.security.approvals import (
                ApprovalsConfig,
                load_approvals_from_active_config,
            )

            self._approvals_config = load_approvals_from_active_config()
            return self._approvals_config
        except Exception:  # noqa: BLE001
            from opencomputer.security.approvals import ApprovalsConfig

            self._approvals_config = ApprovalsConfig()
            return self._approvals_config

    def refresh_approvals_config(self) -> None:
        """Force a re-read of ``security.approvals`` config on next use.

        Call this after the operator edits ``config.yaml`` so the gate
        picks up the new mode/timeout without restarting the daemon.
        """
        self._approvals_config = None

    def set_prompt_handler(self, handler: PromptHandler | None) -> None:
        """Register (or clear) the channel-side prompt sender.

        See :data:`PromptHandler` for the contract. Re-registration
        replaces; pass ``None`` to disable prompt-aware approval.
        """
        self._prompt_handler = handler

    @staticmethod
    def render_prompt(
        claim: CapabilityClaim, scope: str | None,
    ) -> str:
        """Public alias for :func:`render_prompt_message`.

        Surfaced as a method so callers (TUI, wire server, AgentLoop's
        consent-prompt path) can ask the gate to format the prompt
        without importing the module-level helper directly.
        """
        return render_prompt_message(claim, scope)

    def check(
        self,
        claim: CapabilityClaim,
        *,
        scope: str | None,
        session_id: str | None,
    ) -> ConsentDecision:
        # Hermes-parity: when ``security.approvals.mode == off`` the
        # operator has explicitly opted into auto-allow for all consent
        # prompts (equivalent to per-session ``--auto``). Hardline
        # patterns are NEVER affected by this — they fire at tool entry
        # before the consent gate ever runs. Audit-log every auto-allow
        # so the trail is intact.
        approvals_cfg = self._get_approvals_config()
        if approvals_cfg.auto_allow:
            audit_id = self._audit.append(AuditEvent(
                session_id=session_id, actor="hook",
                action="check_auto_allow",
                capability_id=claim.capability_id,
                tier=int(claim.tier_required),
                scope=scope,
                decision="allow",
                reason="security.approvals.mode=off (operator opt-in auto-allow)",
            ))
            return ConsentDecision(
                allowed=True,
                reason="security.approvals.mode=off (operator opt-in)",
                tier_matched=claim.tier_required,
                audit_event_id=audit_id,
            )

        # Hermes parity: session-scoped grant short-circuits before the
        # persistent-store lookup. Session grants live in-memory only
        # and are cleared on SESSION_FINALIZE via on_session_finalize.
        if session_id is not None:
            sg = self._session_grants.get((session_id, claim.capability_id))
            if sg is not None and sg.tier >= claim.tier_required:
                audit_id = self._audit.append(AuditEvent(
                    session_id=session_id, actor="hook",
                    action="check_session_grant",
                    capability_id=claim.capability_id,
                    tier=int(sg.tier),
                    scope=scope,
                    decision="allow",
                    reason="session_grant matched",
                ))
                return ConsentDecision(
                    allowed=True,
                    reason="session_grant matched",
                    tier_matched=sg.tier,
                    audit_event_id=audit_id,
                )

        grant = None
        # 1. Exact scope match (if caller has a concrete scope)
        if scope is not None:
            grant = self._store.get(claim.capability_id, scope)
        # 2. Global grant
        if grant is None:
            grant = self._store.get(claim.capability_id, None)
        # 3. Path-anchored prefix match against any active scope_filter.
        #
        # CRITICAL: a plain `startswith` would let grant=`/Users/saksham/Projects`
        # allow a call on `/Users/saksham/Projects-secret/.env` — scope escape.
        # Anchored check requires the scope to equal the filter OR start with
        # filter + '/'. Trailing slash on the filter is normalized away.
        if grant is None and scope is not None:
            for g in self._store.list_active():
                if (
                    g.capability_id == claim.capability_id
                    and g.scope_filter is not None
                ):
                    anchor = g.scope_filter.rstrip("/")
                    if scope == anchor or scope.startswith(anchor + "/"):
                        grant = g
                        break

        if grant is None:
            decision_bool = False
            # 2.B.2 — name the resource in the deny reason when we have one
            # so callers surfacing this string to the user see "no grant for
            # capability — would prompt: Allow X on /path? ..." rather than
            # the bare capability class.
            reason = "no grant for capability"
            if scope:
                reason = (
                    f"{reason} (would prompt: "
                    f"{render_prompt_message(claim, scope)})"
                )
            tier: ConsentTier | None = None
        elif grant.tier < claim.tier_required:
            decision_bool = False
            reason = (
                f"grant tier {grant.tier.name} insufficient "
                f"(need {claim.tier_required.name})"
            )
            if scope:
                reason = (
                    f"{reason} (would prompt: "
                    f"{render_prompt_message(claim, scope)})"
                )
            tier = grant.tier
        else:
            decision_bool = True
            reason = "grant matched"
            tier = grant.tier

        audit_id = self._audit.append(AuditEvent(
            session_id=session_id, actor="hook", action="check",
            capability_id=claim.capability_id,
            tier=int(tier) if tier is not None else int(claim.tier_required),
            scope=scope,
            decision="allow" if decision_bool else "deny",
            reason=reason,
        ))
        return ConsentDecision(
            allowed=decision_bool, reason=reason,
            tier_matched=tier, audit_event_id=audit_id,
        )

    # ─── Round 2a P-5: pending-approval registry ───────────────────

    async def request_approval(
        self,
        *,
        claim: CapabilityClaim,
        scope: str | None,
        session_id: str,
        timeout_s: float = _TIMEOUT_FROM_CONFIG,
    ) -> ConsentDecision:
        """Block until a channel callback resolves the request, or timeout.

        Used by AgentLoop when ``check`` denied a Tier-2 claim AND a
        channel adapter is bound to ``session_id``. The adapter's
        ``send_approval_request(...)`` posts inline buttons to the user;
        when the user clicks one, the adapter calls
        :meth:`resolve_pending` with the decision, which sets the
        backing ``asyncio.Event`` and unblocks this coroutine.

        On timeout (default 300s = 5 min, per the round-2a L3 lock), the
        request auto-denies: a warning is logged, an audit event is
        written, and a deny ``ConsentDecision`` is returned. The pending
        slot is cleaned up so a late callback finds nothing to resolve.

        On ``allow_always`` (``persist=True``), this method writes a
        non-expiring grant via :class:`ConsentStore` BEFORE returning so
        future ``check`` calls succeed immediately. ``allow_once``
        leaves the store untouched — the in-memory grant covers only
        this dispatch.

        ``timeout_s`` defaults to the value of
        ``security.approvals.timeout`` from the active profile's
        ``config.yaml`` (300s if unset). Pass an explicit float to
        override per call.
        """
        # Resolve the config-driven default lazily so callers that don't
        # pass a timeout get the operator's configured value.
        if timeout_s == _TIMEOUT_FROM_CONFIG:
            timeout_s = self._get_approvals_config().timeout_s

        # Hermes-parity: ``mode == off`` skips the prompt entirely.
        # Already handled in ``check`` for the no-grant path, but a
        # caller that explicitly invokes ``request_approval`` (e.g.
        # AgentLoop bypassing the cached check decision) still gets the
        # honest auto-allow.
        approvals_cfg = self._get_approvals_config()
        if approvals_cfg.auto_allow:
            audit_id = self._audit.append(AuditEvent(
                session_id=session_id, actor="hook",
                action="approval_auto_allow",
                capability_id=claim.capability_id,
                tier=int(claim.tier_required),
                scope=scope,
                decision="allow",
                reason="security.approvals.mode=off (operator opt-in auto-allow)",
            ))
            return ConsentDecision(
                allowed=True,
                reason="security.approvals.mode=off (operator opt-in)",
                tier_matched=claim.tier_required,
                audit_event_id=audit_id,
            )

        # Hermes-parity smart mode: when an aux-LLM verdict is
        # available we let it short-circuit prompt or deny without
        # bothering the user. Uncertain / medium → manual fallthrough.
        # The aux call is best-effort — any failure inside
        # ``assess_risk`` returns a fallback verdict that defers to the
        # manual path. Hardline patterns NEVER reach this code path —
        # they fire at tool entry.
        if approvals_cfg.mode == "smart":
            try:
                from opencomputer.security.smart_mode import assess_risk

                verdict = await assess_risk(
                    capability_id=claim.capability_id,
                    scope=scope,
                    command=claim.human_description or claim.capability_id,
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "smart-mode assess_risk crashed for session=%s "
                    "capability=%s — falling back to manual prompt",
                    session_id, claim.capability_id, exc_info=True,
                )
                verdict = None
            if verdict is not None:
                if verdict.auto_allow:
                    audit_id = self._audit.append(AuditEvent(
                        session_id=session_id, actor="smart_mode",
                        action="approval_smart_allow",
                        capability_id=claim.capability_id,
                        tier=int(claim.tier_required),
                        scope=scope,
                        decision="allow",
                        reason=f"smart-mode low-risk: {verdict.reason}"
                        + (" (LLM-fallback)" if verdict.used_fallback else ""),
                    ))
                    return ConsentDecision(
                        allowed=True,
                        reason=f"smart-mode low-risk: {verdict.reason}",
                        tier_matched=claim.tier_required,
                        audit_event_id=audit_id,
                    )
                if verdict.auto_deny:
                    audit_id = self._audit.append(AuditEvent(
                        session_id=session_id, actor="smart_mode",
                        action="approval_smart_deny",
                        capability_id=claim.capability_id,
                        tier=int(claim.tier_required),
                        scope=scope,
                        decision="deny",
                        reason=f"smart-mode high-risk: {verdict.reason}",
                    ))
                    return ConsentDecision(
                        allowed=False,
                        reason=f"smart-mode high-risk: {verdict.reason}",
                        tier_matched=None,
                        audit_event_id=audit_id,
                    )
                # medium / uncertain → fall through to manual prompt.

        key = (session_id, claim.capability_id)
        event = asyncio.Event()
        # If a request is already pending for this key, the new caller
        # joins the existing Event. The first resolution wakes both.
        # That keeps double-prompts on the same (session, capability)
        # from each consuming a separate user click. Practically rare
        # because AgentLoop awaits in-line, but defensive.
        if key not in self._pending_requests:
            self._pending_requests[key] = event
        else:
            event = self._pending_requests[key]

        # Fire the channel-side prompt FIRST so the user actually sees
        # something. If no handler is registered or the handler reports
        # the channel is unavailable, auto-deny without waiting (we
        # have no surface to ask the user on, so blocking would just
        # burn the timeout for no reason).
        if self._prompt_handler is None:
            self._pending_requests.pop(key, None)
            audit_id = self._audit.append(AuditEvent(
                session_id=session_id, actor="system", action="approval_no_channel",
                capability_id=claim.capability_id,
                tier=int(claim.tier_required),
                scope=scope,
                decision="deny",
                reason="no approval channel bound to session",
            ))
            return ConsentDecision(
                allowed=False,
                reason="no approval channel bound to session",
                tier_matched=None,
                audit_event_id=audit_id,
            )
        # Wave 5 T14 — Hermes-port pre_approval_request hook (30307a980).
        # Observer-only (return value ignored). Plugin crashes are
        # swallowed by the engine. Wrapped defensively because a hook
        # crashing must never block the user prompt.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext as _HookCtx
            from plugin_sdk.hooks import HookEvent as _HookEv

            await _hook_engine.fire_blocking(_HookCtx(
                event=_HookEv.PRE_APPROVAL_REQUEST,
                session_id=session_id,
                surface="gateway",  # request_approval is the gateway path
                command=claim.capability_id,
            ))
        except Exception:  # noqa: BLE001
            pass

        try:
            prompted = await self._prompt_handler(session_id, claim, scope)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "consent prompt handler raised for session=%s capability=%s: %s",
                session_id, claim.capability_id, exc,
            )
            prompted = False
        if not prompted:
            self._pending_requests.pop(key, None)
            audit_id = self._audit.append(AuditEvent(
                session_id=session_id, actor="system", action="approval_no_channel",
                capability_id=claim.capability_id,
                tier=int(claim.tier_required),
                scope=scope,
                decision="deny",
                reason="approval channel did not deliver prompt",
            ))
            return ConsentDecision(
                allowed=False,
                reason="approval channel did not deliver prompt",
                tier_matched=None,
                audit_event_id=audit_id,
            )

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except TimeoutError:
            self._pending_requests.pop(key, None)
            self._pending_decisions.pop(key, None)
            _log.warning(
                "consent.request_approval timeout after %.0fs for "
                "session=%s capability=%s — auto-denying",
                timeout_s, session_id, claim.capability_id,
            )
            audit_id = self._audit.append(AuditEvent(
                session_id=session_id, actor="user", action="approval_timeout",
                capability_id=claim.capability_id,
                tier=int(claim.tier_required),
                scope=scope,
                decision="deny",
                reason=f"approval prompt timed out after {int(timeout_s)}s",
            ))
            return ConsentDecision(
                allowed=False,
                reason=f"approval prompt timed out after {int(timeout_s)}s",
                tier_matched=None,
                audit_event_id=audit_id,
            )

        # 3-tuple migration (Hermes session-verb parity). resolve_pending
        # always writes a 3-tuple; the default fills session_scoped=False.
        decision = self._pending_decisions.pop(key, (False, False, False))
        self._pending_requests.pop(key, None)
        allowed, persist, session_scoped = decision

        if allowed and session_scoped:
            # Hermes parity: session-scoped grant. In-memory only;
            # cleared on SESSION_FINALIZE. Tier == claim's required tier.
            self._session_grants[(session_id, claim.capability_id)] = ConsentGrant(
                capability_id=claim.capability_id,
                tier=claim.tier_required,
                scope_filter=scope,
                granted_at=time.time(),
                expires_at=None,
                granted_by="user",
            )

        if allowed and persist:
            # allow_always — persist a non-expiring grant scoped to this
            # exact resource (or global if scope is None) so future
            # check() calls match without re-prompting.
            self._store.upsert(ConsentGrant(
                capability_id=claim.capability_id,
                tier=claim.tier_required,
                scope_filter=scope,
                granted_at=time.time(),
                expires_at=None,
                granted_by="user",
            ))

        action = (
            "approval_allow_always" if (allowed and persist)
            else "approval_allow_session" if (allowed and session_scoped)
            else "approval_allow_once" if allowed
            else "approval_deny"
        )
        reason = (
            "user clicked allow always" if (allowed and persist)
            else "user clicked allow session" if (allowed and session_scoped)
            else "user clicked allow once" if allowed
            else "user clicked deny"
        )
        audit_id = self._audit.append(AuditEvent(
            session_id=session_id, actor="user", action=action,
            capability_id=claim.capability_id,
            tier=int(claim.tier_required),
            scope=scope,
            decision="allow" if allowed else "deny",
            reason=reason,
        ))
        # Wave 5 T14 — Hermes-port post_approval_response hook (30307a980).
        # Observer-only. Maps action → choice vocab the hook receives:
        # allow_always→"always", allow_once→"once", deny→"deny",
        # timeout (handled in the timeout branch above) writes "timeout".
        _choice = (
            "always" if (allowed and persist)
            else "session" if (allowed and session_scoped)
            else "once" if allowed
            else "deny"
        )
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext as _HookCtx
            from plugin_sdk.hooks import HookEvent as _HookEv

            await _hook_engine.fire_blocking(_HookCtx(
                event=_HookEv.POST_APPROVAL_RESPONSE,
                session_id=session_id,
                surface="gateway",
                command=claim.capability_id,
                choice=_choice,
            ))
        except Exception:  # noqa: BLE001
            pass

        return ConsentDecision(
            allowed=allowed,
            reason=reason,
            tier_matched=claim.tier_required if allowed else None,
            audit_event_id=audit_id,
        )

    def resolve_pending(
        self,
        *,
        session_id: str,
        capability_id: str,
        decision: bool,
        persist: bool,
        session_scoped: bool = False,
    ) -> bool:
        """Mark a pending approval as resolved with the given decision.

        Called by the channel adapter's callback handler when the user
        clicks an inline button. Hermes-parity 4-verb encoding:

        | decision | persist | session_scoped | meaning |
        |----------|---------|----------------|---------|
        | True     | False   | False          | allow_once |
        | True     | False   | True           | allow_session (in-memory only) |
        | True     | True    | False          | allow_always (persistent grant) |
        | False    | _       | _              | deny |

        ``session_scoped`` defaults False so existing callers (telegram /
        slack / matrix dispatch handlers and text-reply path) keep
        working unchanged.

        Returns True if a pending request existed and was resolved;
        False if no matching key was registered (stale callback after
        timeout, or duplicate click after first one already processed).
        Callers should treat False as "ignore — late or duplicate".
        """
        key = (session_id, capability_id)
        event = self._pending_requests.get(key)
        if event is None or event.is_set():
            # Either no pending request (stale callback) or already
            # resolved (double-click). Don't overwrite; signal to the
            # caller so it can log "stale callback ignored".
            return False
        self._pending_decisions[key] = (decision, persist, session_scoped)
        event.set()
        return True

    # ─── Hermes parity: SESSION_FINALIZE cleanup ──────────────────────

    def on_session_finalize(self, *, session_id: str) -> None:
        """Drop session-scoped grants for an ending session.

        Called from the hook engine when ``HookEvent.SESSION_FINALIZE``
        fires. Idempotent on unknown ``session_id`` — a session that
        never created a grant passes through silently.
        """
        keys = [k for k in self._session_grants if k[0] == session_id]
        for k in keys:
            self._session_grants.pop(k, None)

    def register_session_finalize_handler(self) -> None:
        """Subscribe ``on_session_finalize`` to ``HookEvent.SESSION_FINALIZE``.

        Idempotent — safe to call multiple times. Caller is responsible
        for invoking this exactly once (typically from the gate factory
        in the agent loop's __init__ path). Best-effort: hook subscribe
        failures (e.g. test-time engine substitution) are swallowed so
        the gate works without hooks.
        """
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookEvent

            async def _handler(ctx):
                sid = getattr(ctx, "session_id", None)
                if sid:
                    self.on_session_finalize(session_id=sid)

            _hook_engine.subscribe(HookEvent.SESSION_FINALIZE, _handler)
        except Exception:  # noqa: BLE001 — observer-only; gate must work without hooks
            pass

    def has_pending_request(
        self,
        *,
        session_id: str,
        capability_id: str,
    ) -> bool:
        """Whether an unresolved approval request is registered for this key."""
        key = (session_id, capability_id)
        event = self._pending_requests.get(key)
        return event is not None and not event.is_set()
