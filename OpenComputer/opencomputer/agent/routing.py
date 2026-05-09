"""v1.1 plan-3 M10 — per-channel routing engine (schema layer).

This module owns the precedence + match logic for the
:class:`RoutingConfig` schema. It does NOT yet wire into the gateway
dispatcher (that's M10.2). Today the only consumers are:

* :func:`opencomputer.agent.config_store._parse_routing_block` —
  YAML → dataclass round-trip.
* The ``oc routing test`` / ``oc routing list`` CLI commands
  (M10.4) for operator dry-runs.

Precedence chain (most-specific-wins per OpenClaw):

    exact peer  → parent peer  → guild + roles → guild → team →
    account     → channel      → default

Authors can list rules in any order in YAML; rules are sorted by
specificity at parse time so a less-specific rule listed first never
eclipses a more-specific one listed later.
"""

from __future__ import annotations

from dataclasses import dataclass

from opencomputer.agent.config import RoutingConfig, RoutingMatch, RoutingRule
from plugin_sdk.core import MessageEvent

__all__ = [
    "MatchOutcome",
    "_match_specificity",
    "match_rule",
    "resolve_routing_rule",
    "sort_rules_by_specificity",
]


# ─── precedence ──────────────────────────────────────────────────────────


# Specificity weights — most-specific-wins. The numeric values exist
# only to define an ordering. Larger == more specific. The labels match
# the precedence chain in the module docstring.
_DIM_WEIGHTS: dict[str, int] = {
    "chat_id": 1000,  # exact peer / DM identifier
    "peer": 1000,     # OpenClaw alias (treated as `chat_id`)
    "role": 200,      # role membership ranks above plain guild
    "guild": 100,
    "team": 80,
    "account": 60,
    "channel": 40,
    "platform": 1,    # presence alone barely tightens
}


def _match_specificity(match: RoutingMatch) -> int:
    """Return a numeric score representing how specific ``match`` is.

    Authors list rules in any order in YAML; the parser calls
    :func:`sort_rules_by_specificity` so resolution iterates from most-
    to least-specific.
    """
    score = 0
    for dim, weight in _DIM_WEIGHTS.items():
        if getattr(match, dim, ""):
            score += weight
    return score


def sort_rules_by_specificity(rules: tuple[RoutingRule, ...]) -> tuple[RoutingRule, ...]:
    """Return ``rules`` sorted most-specific first.

    Stable: rules with identical specificity preserve author order.
    """
    return tuple(
        sorted(
            rules,
            key=lambda r: _match_specificity(r.match),
            reverse=True,
        )
    )


# ─── match ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """Result of resolving an inbound event against a :class:`RoutingConfig`.

    * ``rule`` is the matched rule, or ``None`` if the default fired.
    * ``agent`` is the resolved agent template name (rule.agent or
      default.agent).
    * ``profile`` is the resolved profile, or empty string if no rebind
      is requested by the matched rule.
    * ``matched_default`` is True when no rule matched.
    """

    agent: str
    profile: str = ""
    rule: RoutingRule | None = None
    matched_default: bool = False


def match_rule(rule: RoutingRule, event_fields: dict[str, str]) -> bool:
    """True if every set field on the rule's match block equals the
    corresponding field on the inbound event.

    Empty fields on the rule are wildcards. ``event_fields`` is the
    flat dict produced by :func:`_event_to_match_fields` — keeps this
    function pure (no MessageEvent dep) so tests can pass synthetic
    dicts directly.
    """
    m = rule.match
    for dim in _DIM_WEIGHTS:
        rule_val = getattr(m, dim, "")
        if not rule_val:
            continue
        event_val = event_fields.get(dim, "")
        if rule_val != event_val:
            return False
    return True


def _event_to_match_fields(event: MessageEvent) -> dict[str, str]:
    """Flatten a :class:`MessageEvent` into the dimensions
    :func:`match_rule` checks.

    Reads:

    * ``platform`` — from ``event.platform.value``.
    * ``chat_id`` / ``peer`` — both populated from ``event.chat_id`` so
      authors can use either label in YAML.
    * ``channel`` / ``guild`` / ``team`` / ``account`` / ``role`` —
      from ``event.metadata`` (channel adapters populate these where
      applicable; a Telegram DM has none, a Discord guild message has
      ``guild`` + ``channel`` + maybe ``role``).
    """
    md = event.metadata or {}
    role_val = md.get("role") or md.get("roles", "")
    if isinstance(role_val, list | tuple):
        # OpenClaw's match-on-any-of behavior: a member with multiple
        # roles matches a rule asking for any one of them. We stringify
        # to the first role here; the rule-vs-event dimension match is
        # exact, so authors document one role per rule. Multi-role
        # match-any can be added when demand surfaces (M10 acceptance
        # criteria don't require it).
        role_val = role_val[0] if role_val else ""
    return {
        "platform": event.platform.value,
        "chat_id": event.chat_id,
        "peer": event.chat_id,
        "channel": str(md.get("channel", "")).lstrip("#"),
        "guild": str(md.get("guild", "")),
        "team": str(md.get("team", "")),
        "account": str(md.get("account", "")),
        "role": str(role_val or ""),
    }


def resolve_routing_rule(
    routing: RoutingConfig, event: MessageEvent
) -> MatchOutcome:
    """Walk ``routing.rules`` (most-specific-first) and return the first
    matching :class:`MatchOutcome`. Falls through to ``routing.default``
    if no rule matches."""
    fields = _event_to_match_fields(event)
    # Rules are already sorted at parse time, but we re-sort defensively
    # to handle programmatically-built RoutingConfigs (tests construct
    # one directly without going through YAML).
    sorted_rules = sort_rules_by_specificity(routing.rules)
    for rule in sorted_rules:
        if match_rule(rule, fields):
            return MatchOutcome(
                agent=rule.agent,
                profile=rule.profile,
                rule=rule,
                matched_default=False,
            )
    return MatchOutcome(
        agent=routing.default.agent,
        profile=routing.default.profile,
        matched_default=True,
    )


def resolve_routing_rule_by_fields(
    routing: RoutingConfig, fields: dict[str, str]
) -> MatchOutcome:
    """Variant of :func:`resolve_routing_rule` that takes raw match
    fields directly — used by the ``oc routing test`` CLI which doesn't
    have a real :class:`MessageEvent` to hand."""
    sorted_rules = sort_rules_by_specificity(routing.rules)
    for rule in sorted_rules:
        if match_rule(rule, fields):
            return MatchOutcome(
                agent=rule.agent,
                profile=rule.profile,
                rule=rule,
                matched_default=False,
            )
    return MatchOutcome(
        agent=routing.default.agent,
        profile=routing.default.profile,
        matched_default=True,
    )
