"""Broadcast fan-out tests (v1.1 plan-3 M11.4)."""

from __future__ import annotations

import pytest

from opencomputer.agent.channel_routing import (
    BroadcastTarget,
    ChannelRoutingConfig,
    ChannelRoutingMatch,
    ChannelRoutingRule,
    load_routing_config,
    match_route,
)


def test_broadcast_target_dataclass() -> None:
    t = BroadcastTarget(agent="x", profile="y")
    assert t.agent == "x"
    assert t.profile == "y"
    # Default profile is None
    t2 = BroadcastTarget(agent="x")
    assert t2.profile is None


def test_match_returns_broadcast_targets_when_set() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack", channel="#review"),
                agent="<broadcast>",
                broadcast_to=(
                    BroadcastTarget(agent="code-reviewer", profile="coder"),
                    BroadcastTarget(agent="security-reviewer", profile="audit"),
                ),
            ),
        ),
    )
    route = match_route(cfg, {"platform": "slack", "channel": "#review"})
    assert route.is_broadcast
    assert len(route.broadcast_targets) == 2
    assert route.broadcast_targets[0].agent == "code-reviewer"
    assert route.broadcast_targets[0].profile == "coder"
    assert route.broadcast_targets[1].agent == "security-reviewer"


def test_non_broadcast_route_has_empty_broadcast_targets() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack"),
                agent="single",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "slack"})
    assert not route.is_broadcast
    assert route.broadcast_targets == ()


# ─── YAML loader for broadcast ──────────────────────────────────────


def test_load_broadcast_with_string_targets() -> None:
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack", "channel": "#review"},
                    "broadcast_to": ["code-reviewer", "security-reviewer"],
                },
            ],
        }
    )
    assert len(cfg.rules) == 1
    rule = cfg.rules[0]
    assert rule.broadcast_to == (
        BroadcastTarget(agent="code-reviewer"),
        BroadcastTarget(agent="security-reviewer"),
    )


def test_load_broadcast_with_dict_targets() -> None:
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack", "channel": "#review"},
                    "broadcast_to": [
                        {"agent": "code-reviewer", "profile": "coder"},
                        {"agent": "security-reviewer", "profile": "audit"},
                    ],
                },
            ],
        }
    )
    rule = cfg.rules[0]
    assert rule.broadcast_to == (
        BroadcastTarget(agent="code-reviewer", profile="coder"),
        BroadcastTarget(agent="security-reviewer", profile="audit"),
    )


def test_load_broadcast_alias_key() -> None:
    """``broadcast:`` is accepted as an alias for ``broadcast_to:``."""
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack"},
                    "broadcast": ["a", "b"],
                },
            ],
        }
    )
    assert len(cfg.rules[0].broadcast_to) == 2


def test_load_broadcast_default_agent_sentinel_when_omitted() -> None:
    """When broadcast_to is set + agent: omitted, the parser
    auto-fills the sentinel value '<broadcast>'."""
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack"},
                    "broadcast_to": ["a"],
                },
            ],
        }
    )
    assert cfg.rules[0].agent == "<broadcast>"


def test_load_broadcast_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="broadcast_to must be non-empty"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "broadcast_to": []},
                ],
            }
        )


def test_load_broadcast_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="broadcast_to must be a list"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "broadcast_to": "x"},
                ],
            }
        )


def test_load_broadcast_rejects_empty_target_string() -> None:
    with pytest.raises(ValueError, match="non-empty agent name"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "broadcast_to": [""]},
                ],
            }
        )


def test_load_broadcast_rejects_dict_target_without_agent() -> None:
    with pytest.raises(ValueError, match="agent.*non-empty string"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "broadcast_to": [{"profile": "p"}]},
                ],
            }
        )


def test_load_broadcast_rejects_invalid_target_type() -> None:
    with pytest.raises(ValueError, match="must be a string or a mapping"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "broadcast_to": [42]},
                ],
            }
        )


def test_broadcast_with_per_target_profiles_end_to_end() -> None:
    """End-to-end: YAML -> config -> match -> broadcast targets with
    distinct profiles."""
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack", "channel": "#urgent"},
                    "broadcast_to": [
                        {"agent": "code-reviewer", "profile": "coder"},
                        {"agent": "security-reviewer", "profile": "audit"},
                        "perf-reviewer",  # uses default profile
                    ],
                },
            ],
        }
    )
    route = match_route(cfg, {"platform": "slack", "channel": "#urgent"})
    assert route.is_broadcast
    assert len(route.broadcast_targets) == 3
    profiles = [t.profile for t in route.broadcast_targets]
    assert profiles == ["coder", "audit", None]
