"""v1.1 plan-3 M10.1 — most-specific-wins precedence chain.

Pins every step of the precedence chain documented in
:mod:`opencomputer.agent.routing`:

    exact peer  → parent peer  → guild + roles → guild → team →
    account     → channel      → default
"""

from __future__ import annotations

from opencomputer.agent.config import (
    RoutingConfig,
    RoutingDefault,
    RoutingMatch,
    RoutingRule,
)
from opencomputer.agent.routing import (
    _match_specificity,
    resolve_routing_rule_by_fields,
    sort_rules_by_specificity,
)


def _fields(**overrides: str) -> dict[str, str]:
    """Return a fully-populated match-fields dict (everything else = '')."""
    base = {
        "platform": "",
        "chat_id": "",
        "peer": "",
        "channel": "",
        "guild": "",
        "team": "",
        "account": "",
        "role": "",
    }
    base.update(overrides)
    return base


# ─── exact peer beats everything ─────────────────────────────────────────


def test_exact_chat_id_beats_guild_and_channel() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild", channel="general"),
                agent="guild-channel",
            ),
            RoutingRule(
                match=RoutingMatch(platform="discord", chat_id="12345"),
                agent="dm-handler",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="discord", chat_id="12345", guild="myguild", channel="general"),
    )
    assert out.agent == "dm-handler"


def test_peer_alias_treated_as_chat_id() -> None:
    """OpenClaw `peer` and our `chat_id` must match identically."""
    rc = RoutingConfig(
        rules=(RoutingRule(match=RoutingMatch(platform="telegram", peer="abc"), agent="exec"),),
    )
    out = resolve_routing_rule_by_fields(rc, _fields(platform="telegram", peer="abc", chat_id="abc"))
    assert out.agent == "exec"


# ─── guild + roles beats guild alone ─────────────────────────────────────


def test_guild_plus_role_beats_guild_alone() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild"),
                agent="guild-default",
            ),
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild", role="admin"),
                agent="admin-only",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="discord", guild="myguild", role="admin"),
    )
    assert out.agent == "admin-only"


def test_guild_alone_fires_when_role_does_not_match() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild"),
                agent="guild-default",
            ),
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild", role="admin"),
                agent="admin-only",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="discord", guild="myguild", role="member"),
    )
    assert out.agent == "guild-default"


# ─── team / account / channel chain ──────────────────────────────────────


def test_channel_beats_platform_alone() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack"),
                agent="any-slack",
            ),
            RoutingRule(
                match=RoutingMatch(platform="slack", channel="security-alerts"),
                agent="security-channel",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="slack", channel="security-alerts"),
    )
    assert out.agent == "security-channel"


def test_team_beats_channel() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack", channel="general"),
                agent="channel-handler",
            ),
            RoutingRule(
                match=RoutingMatch(platform="slack", team="team123"),
                agent="team-handler",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="slack", channel="general", team="team123"),
    )
    assert out.agent == "team-handler"


def test_account_beats_channel() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack", channel="general"),
                agent="channel-handler",
            ),
            RoutingRule(
                match=RoutingMatch(platform="slack", account="bot-A"),
                agent="account-handler",
            ),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="slack", channel="general", account="bot-A"),
    )
    assert out.agent == "account-handler"


def test_guild_beats_team_beats_account() -> None:
    rc = RoutingConfig(
        rules=(
            RoutingRule(match=RoutingMatch(platform="discord", account="x"), agent="account"),
            RoutingRule(match=RoutingMatch(platform="discord", team="t"), agent="team"),
            RoutingRule(match=RoutingMatch(platform="discord", guild="g"), agent="guild"),
        ),
    )
    out = resolve_routing_rule_by_fields(
        rc, _fields(platform="discord", account="x", team="t", guild="g"),
    )
    assert out.agent == "guild"


# ─── precedence sort is order-independent ───────────────────────────────


def test_rules_sorted_so_author_order_is_irrelevant() -> None:
    """Authors can list rules in any order in YAML; the parser sorts."""
    less_specific = RoutingRule(
        match=RoutingMatch(platform="slack"), agent="any-slack",
    )
    more_specific = RoutingRule(
        match=RoutingMatch(platform="slack", channel="security"), agent="security",
    )
    out_a = sort_rules_by_specificity((less_specific, more_specific))
    out_b = sort_rules_by_specificity((more_specific, less_specific))
    assert out_a == out_b
    assert out_a[0] is more_specific
    assert out_a[1] is less_specific


def test_specificity_score_ranks_dimensions_correctly() -> None:
    """chat_id should score highest; platform alone, lowest."""
    chat_only = RoutingMatch(chat_id="x")
    platform_only = RoutingMatch(platform="slack")
    role_in_guild = RoutingMatch(platform="discord", guild="g", role="admin")

    assert _match_specificity(chat_only) > _match_specificity(role_in_guild)
    assert _match_specificity(role_in_guild) > _match_specificity(platform_only)


# ─── stable sort ─────────────────────────────────────────────────────────


def test_equal_specificity_preserves_author_order() -> None:
    """When two rules have identical specificity, author order wins."""
    a = RoutingRule(match=RoutingMatch(platform="slack", channel="alpha"), agent="A")
    b = RoutingRule(match=RoutingMatch(platform="slack", channel="beta"), agent="B")
    sorted_ab = sort_rules_by_specificity((a, b))
    sorted_ba = sort_rules_by_specificity((b, a))
    assert sorted_ab[0].agent == "A"
    assert sorted_ba[0].agent == "B"
