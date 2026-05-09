"""v1.1 plan-3 M10.1 — `routing:` block YAML parser + round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config_store import (
    _parse_routing_block,
    load_config,
    save_config,
)


def test_parse_minimal_block() -> None:
    block = {
        "rules": [
            {"match": {"platform": "slack"}, "agent": "any-slack"},
        ],
    }
    rc = _parse_routing_block(block)
    assert rc is not None
    assert len(rc.rules) == 1
    assert rc.rules[0].agent == "any-slack"
    assert rc.rules[0].match.platform == "slack"
    # Default's default
    assert rc.default.agent == "default"


def test_parse_full_block_with_default() -> None:
    block = {
        "rules": [
            {"match": {"platform": "slack", "channel": "#alerts"}, "agent": "alert-handler"},
            {"match": {"platform": "telegram", "peer": "12345"}, "agent": "exec", "profile": "work"},
        ],
        "default": {"agent": "fallback", "profile": "default"},
    }
    rc = _parse_routing_block(block)
    assert rc is not None
    assert len(rc.rules) == 2
    assert rc.default.agent == "fallback"
    assert rc.default.profile == "default"

    # Most-specific-first ordering at parse time
    # (chat_id-tagged peer rule has spec 1001, channel has spec 41)
    # so peer rule comes first.
    assert rc.rules[0].agent == "exec"
    assert rc.rules[1].agent == "alert-handler"


def test_empty_block_returns_none() -> None:
    """Empty / missing routing block leaves Config.routing at default."""
    assert _parse_routing_block(None) is None
    assert _parse_routing_block({}) is None


def test_skip_rule_missing_agent(caplog: pytest.LogCaptureFixture) -> None:
    block = {
        "rules": [
            {"match": {"platform": "slack"}},  # no agent
            {"match": {"platform": "telegram"}, "agent": "tele"},
        ],
    }
    with caplog.at_level("WARNING"):
        rc = _parse_routing_block(block)
    assert rc is not None
    assert len(rc.rules) == 1
    assert rc.rules[0].agent == "tele"
    assert "missing required `agent`" in caplog.text


def test_skip_unknown_match_dimension_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    block = {
        "rules": [
            {
                "match": {"platform": "slack", "moonphase": "waxing"},
                "agent": "lunar",
            },
        ],
    }
    with caplog.at_level("WARNING"):
        rc = _parse_routing_block(block)
    assert rc is not None
    assert len(rc.rules) == 1  # rule kept; unknown dim dropped
    assert rc.rules[0].agent == "lunar"
    assert "unknown match dimension 'moonphase'" in caplog.text


def test_channel_normalization_strips_leading_hash() -> None:
    """`#foo` and `foo` both match an event with channel='foo'."""
    rc1 = _parse_routing_block(
        {"rules": [{"match": {"channel": "#alpha"}, "agent": "a"}]}
    )
    rc2 = _parse_routing_block(
        {"rules": [{"match": {"channel": "alpha"}, "agent": "a"}]}
    )
    assert rc1 is not None and rc2 is not None
    assert rc1.rules[0].match.channel == "alpha" == rc2.rules[0].match.channel


def test_full_yaml_round_trip(tmp_path: Path) -> None:
    """Write a Config with routing rules → reload → routing matches."""
    from opencomputer.agent.config import (
        Config,
        RoutingConfig,
        RoutingDefault,
        RoutingMatch,
        RoutingRule,
    )

    cfg = Config(
        routing=RoutingConfig(
            rules=(
                RoutingRule(
                    match=RoutingMatch(platform="slack", channel="security-alerts"),
                    agent="security-reviewer",
                ),
                RoutingRule(
                    match=RoutingMatch(platform="telegram", chat_id="12345"),
                    agent="exec-asst",
                    profile="work",
                ),
            ),
            default=RoutingDefault(agent="fallback"),
        ),
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)

    # Reload and verify
    reloaded = load_config(path)
    assert len(reloaded.routing.rules) == 2
    assert reloaded.routing.default.agent == "fallback"
    # chat_id-rule should sort first by specificity
    assert reloaded.routing.rules[0].agent == "exec-asst"
    assert reloaded.routing.rules[0].profile == "work"
    assert reloaded.routing.rules[1].agent == "security-reviewer"


def test_load_config_with_no_routing_block_is_default(tmp_path: Path) -> None:
    """A config.yaml without `routing:` leaves Config.routing at default."""
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  model: claude-opus-4-7\n")
    cfg = load_config(path)
    assert cfg.routing.rules == ()
    assert cfg.routing.default.agent == "default"
