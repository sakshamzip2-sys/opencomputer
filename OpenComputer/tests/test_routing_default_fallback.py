"""v1.1 plan-3 M10.1 — fallback to RoutingConfig.default when no rule matches."""

from __future__ import annotations

from opencomputer.agent.config import (
    RoutingConfig,
    RoutingDefault,
    RoutingMatch,
    RoutingRule,
)
from opencomputer.agent.routing import resolve_routing_rule_by_fields


def _fields(**overrides: str) -> dict[str, str]:
    base = {
        "platform": "", "chat_id": "", "peer": "", "channel": "",
        "guild": "", "team": "", "account": "", "role": "",
    }
    base.update(overrides)
    return base


def test_no_rules_fires_default() -> None:
    rc = RoutingConfig(rules=(), default=RoutingDefault(agent="my-default"))
    out = resolve_routing_rule_by_fields(rc, _fields(platform="slack"))
    assert out.matched_default is True
    assert out.agent == "my-default"
    assert out.rule is None


def test_rules_present_but_none_match_fires_default() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(match=RoutingMatch(platform="slack"), agent="slack"),
            RoutingRule(match=RoutingMatch(platform="discord"), agent="discord"),
        ),
        default=RoutingDefault(agent="catchall", profile="general"),
    )
    out = resolve_routing_rule_by_fields(rc, _fields(platform="matrix"))
    assert out.matched_default is True
    assert out.agent == "catchall"
    assert out.profile == "general"


def test_default_default_agent_is_default_string() -> None:
    """Bare RoutingConfig() — default agent is 'default'."""
    rc = RoutingConfig()
    out = resolve_routing_rule_by_fields(rc, _fields(platform="slack"))
    assert out.matched_default is True
    assert out.agent == "default"
    assert out.profile == ""
