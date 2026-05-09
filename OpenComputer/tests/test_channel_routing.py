"""Per-channel routing tests (v1.1 plan-3 M10)."""

from __future__ import annotations

import pytest

from opencomputer.agent.channel_routing import (
    ChannelRoutingConfig,
    ChannelRoutingDefault,
    ChannelRoutingMatch,
    ChannelRoutingRule,
    load_routing_config,
    match_route,
)

# ─── basic match semantics ──────────────────────────────────────────


def test_empty_config_returns_default() -> None:
    cfg = ChannelRoutingConfig()
    route = match_route(cfg, {"platform": "telegram", "peer": "12345"})
    assert route.agent == "default"
    assert route.profile is None
    assert route.matched_rule_index is None


def test_single_rule_match() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack", channel="#alerts"),
                agent="security-reviewer",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "slack", "channel": "#alerts"})
    assert route.agent == "security-reviewer"
    assert route.matched_rule_index == 0


def test_no_match_falls_to_default() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack"),
                agent="slack-agent",
            ),
        ),
        default=ChannelRoutingDefault(agent="fallback"),
    )
    route = match_route(cfg, {"platform": "telegram"})
    assert route.agent == "fallback"
    assert route.matched_rule_index is None


def test_partial_match_does_not_fire() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack", channel="#sec"),
                agent="r",
            ),
        ),
    )
    # Same platform but different channel → does NOT match
    route = match_route(cfg, {"platform": "slack", "channel": "#general"})
    assert route.matched_rule_index is None


# ─── precedence / specificity ───────────────────────────────────────


def test_more_specific_rule_wins_over_less_specific() -> None:
    """Per-channel rule beats per-platform rule for the same event."""
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack"),
                agent="generic-slack",
            ),
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack", channel="#sec"),
                agent="security",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "slack", "channel": "#sec"})
    assert route.agent == "security"
    assert route.matched_rule_index == 1


def test_peer_match_beats_channel_match() -> None:
    """Exact peer (chat_id) is the most specific."""
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="telegram", channel="general"),
                agent="general-agent",
            ),
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="telegram", peer="123"),
                agent="vip",
            ),
        ),
    )
    route = match_route(
        cfg,
        {"platform": "telegram", "channel": "general", "peer": "123"},
    )
    assert route.agent == "vip"


def test_guild_plus_role_beats_guild_alone() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(guild="myguild"),
                agent="member-agent",
            ),
            ChannelRoutingRule(
                match=ChannelRoutingMatch(guild="myguild", role="admin"),
                agent="admin-agent",
            ),
        ),
    )
    route = match_route(
        cfg,
        {"platform": "discord", "guild": "myguild", "role": "admin"},
    )
    assert route.agent == "admin-agent"


def test_first_rule_wins_on_specificity_tie() -> None:
    """Same specificity → first defined wins (deterministic ordering)."""
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack"),
                agent="first",
            ),
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack"),
                agent="second",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "slack"})
    assert route.agent == "first"
    assert route.matched_rule_index == 0


def test_parent_peer_inheritance() -> None:
    """A rule on parent_peer is more specific than channel/guild."""
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="slack", channel="#sec"),
                agent="channel-agent",
            ),
            ChannelRoutingRule(
                match=ChannelRoutingMatch(parent_peer="thread-456"),
                agent="thread-agent",
            ),
        ),
    )
    route = match_route(
        cfg,
        {"platform": "slack", "channel": "#sec", "parent_peer": "thread-456"},
    )
    assert route.agent == "thread-agent"


# ─── profile re-bind ───────────────────────────────────────────────


def test_rule_with_profile_returns_profile_in_route() -> None:
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="telegram", peer="ceo"),
                agent="executive-assistant",
                profile="executive",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "telegram", "peer": "ceo"})
    assert route.agent == "executive-assistant"
    assert route.profile == "executive"


def test_default_profile_propagates() -> None:
    cfg = ChannelRoutingConfig(
        default=ChannelRoutingDefault(agent="default", profile="personal"),
    )
    route = match_route(cfg, {"platform": "telegram"})
    assert route.profile == "personal"


# ─── load_routing_config (strict validation) ────────────────────────


def test_load_empty_returns_empty_config() -> None:
    cfg = load_routing_config(None)
    assert cfg.rules == ()
    assert cfg.default.agent == "default"


def test_load_minimal_config() -> None:
    cfg = load_routing_config(
        {
            "rules": [
                {"match": {"platform": "slack", "channel": "#sec"}, "agent": "r"},
            ],
            "default": {"agent": "fallback"},
        }
    )
    assert len(cfg.rules) == 1
    assert cfg.rules[0].agent == "r"
    assert cfg.default.agent == "fallback"


def test_load_strict_rejects_unknown_match_key() -> None:
    """A typo in 'platfrom:' must surface at config load, not at first message."""
    with pytest.raises(ValueError, match="unknown key.*'platfrom'"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platfrom": "slack"}, "agent": "r"},
                ],
            }
        )


def test_load_rejects_missing_agent() -> None:
    with pytest.raises(ValueError, match="agent.*non-empty string"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}},
                ],
            }
        )


def test_load_rejects_empty_agent() -> None:
    with pytest.raises(ValueError, match="agent.*non-empty string"):
        load_routing_config(
            {
                "rules": [
                    {"match": {"platform": "slack"}, "agent": "   "},
                ],
            }
        )


def test_load_rejects_non_dict_routing() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        load_routing_config([])


def test_load_rejects_non_list_rules() -> None:
    with pytest.raises(ValueError, match="rules must be a list"):
        load_routing_config({"rules": "not-a-list"})


def test_load_rejects_non_dict_rule() -> None:
    with pytest.raises(ValueError, match=r"rules\[0\] must be a mapping"):
        load_routing_config({"rules": ["not-a-dict"]})


def test_load_rejects_non_dict_match() -> None:
    with pytest.raises(ValueError, match=r"rules\[0\].match must be a mapping"):
        load_routing_config({"rules": [{"match": "x", "agent": "y"}]})


def test_load_rejects_empty_default_agent() -> None:
    with pytest.raises(ValueError, match="default.agent.*non-empty"):
        load_routing_config({"default": {"agent": ""}})


def test_load_full_yaml_shape() -> None:
    """End-to-end: the exact shape from Plan 3 spec."""
    cfg = load_routing_config(
        {
            "rules": [
                {
                    "match": {"platform": "slack", "channel": "#security-alerts"},
                    "agent": "security-reviewer",
                },
                {
                    "match": {"platform": "telegram", "peer": "ceo-chat"},
                    "agent": "executive-assistant",
                    "profile": "executive",
                },
                {
                    "match": {"platform": "discord", "guild": "myguild", "role": "admin"},
                    "agent": "admin-agent",
                },
            ],
            "default": {"agent": "default"},
        }
    )
    assert len(cfg.rules) == 3
    assert cfg.rules[1].profile == "executive"

    # Verify routing decisions on representative events
    r1 = match_route(cfg, {"platform": "slack", "channel": "#security-alerts"})
    assert r1.agent == "security-reviewer"

    r2 = match_route(cfg, {"platform": "telegram", "peer": "ceo-chat"})
    assert r2.agent == "executive-assistant"
    assert r2.profile == "executive"

    r3 = match_route(
        cfg,
        {"platform": "discord", "guild": "myguild", "role": "admin"},
    )
    assert r3.agent == "admin-agent"

    r4 = match_route(cfg, {"platform": "irc"})  # nothing matches
    assert r4.agent == "default"


# ─── specificity ordering ───────────────────────────────────────────


def test_specificity_ranking() -> None:
    """Empirical specificity ordering — pin to defend against future drift."""
    m_peer = ChannelRoutingMatch(platform="x", peer="123")
    m_parent = ChannelRoutingMatch(platform="x", parent_peer="t1")
    m_role_guild = ChannelRoutingMatch(platform="x", guild="g", role="admin")
    m_guild = ChannelRoutingMatch(platform="x", guild="g")
    m_channel = ChannelRoutingMatch(platform="x", channel="#c")
    m_platform = ChannelRoutingMatch(platform="x")
    assert m_peer.specificity() > m_parent.specificity()
    assert m_parent.specificity() > m_role_guild.specificity()
    assert m_role_guild.specificity() > m_guild.specificity()
    assert m_guild.specificity() > m_channel.specificity()
    assert m_channel.specificity() > m_platform.specificity()


# ─── coercion for non-string event values ──────────────────────────


def test_match_coerces_event_values_to_string() -> None:
    """Telegram chat_ids arrive as ints; the matcher should coerce
    both sides to str so '123' == 123 in match-time comparison."""
    cfg = ChannelRoutingConfig(
        rules=(
            ChannelRoutingRule(
                match=ChannelRoutingMatch(platform="telegram", peer="123"),
                agent="vip",
            ),
        ),
    )
    route = match_route(cfg, {"platform": "telegram", "peer": 123})
    assert route.agent == "vip"
