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
"""
from __future__ import annotations

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
from opencomputer.agent.consent.store import ConsentStore
from plugin_sdk import CapabilityClaim, ConsentDecision, ConsentTier


def render_prompt_message(claim: CapabilityClaim, scope: str | None) -> str:
    """Build the user-facing prompt for a Tier-2 (PER_ACTION) consent ask.

    F1 2.B.2 — when a scope is available, the prompt names the specific
    resource being accessed instead of just the capability class. This
    is what AgentLoop / TUI / wire clients show to a user when a
    capability requires per-action approval.

    Examples:
        render_prompt_message(claim, "/Users/x/foo.py")
        → "Allow read_files.metadata on /Users/x/foo.py? [y/N/always]"
        render_prompt_message(claim, None)
        → "Allow read_files.metadata? [y/N/always]"
    """
    cap = claim.capability_id
    if scope:
        return f"Allow {cap} on {scope}? [y/N/always]"
    return f"Allow {cap}? [y/N/always]"


class ConsentGate:
    def __init__(self, *, store: ConsentStore, audit: AuditLogger) -> None:
        self._store = store
        self._audit = audit

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
