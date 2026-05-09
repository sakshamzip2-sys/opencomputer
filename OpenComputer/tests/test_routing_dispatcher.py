"""v1.1 plan-3 M10.2 — gateway dispatcher integration with routing rules.

Tests the resolution helper :func:`opencomputer.agent.routing.resolve_template_for_event`
that ``Dispatch.handle_message`` calls before ``loop.run_conversation``.

Stubs the AgentTemplate (loose-typed via ``object``) so this stays a unit
test on the resolver — full end-to-end dispatcher coverage lives in the
gateway integration suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencomputer.agent.config import (
    RoutingConfig,
    RoutingDefault,
    RoutingMatch,
    RoutingRule,
)
from opencomputer.agent.routing import (
    ResolvedTemplate,
    resolve_template_for_event,
)
from plugin_sdk.core import MessageEvent, Platform


def _event(
    platform: Platform = Platform.SLACK,
    chat_id: str = "C123",
    channel: str = "",
    guild: str = "",
    role: str = "",
    text: str = "hi",
) -> MessageEvent:
    md: dict = {}
    if channel:
        md["channel"] = channel
    if guild:
        md["guild"] = guild
    if role:
        md["role"] = role
    return MessageEvent(
        platform=platform,
        chat_id=chat_id,
        user_id="U1",
        text=text,
        timestamp=0.0,
        metadata=md,
    )


def _template(name: str, system_prompt: str = "You are SYSTEM."):
    """Loose AgentTemplate-shaped object with the only attribute we read."""
    return SimpleNamespace(name=name, system_prompt=system_prompt)


# ─── matched rule + registered template ───────────────────────────────────


def test_matched_rule_with_registered_template_returns_resolved() -> None:
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack", channel="security-alerts"),
                agent="security-reviewer",
            ),
        ),
    )
    templates = {"security-reviewer": _template("security-reviewer", "Be careful.")}
    out = resolve_template_for_event(
        routing,
        _event(platform=Platform.SLACK, channel="security-alerts"),
        templates,
    )
    assert out is not None
    assert out.template_name == "security-reviewer"
    assert out.system_prompt == "Be careful."
    assert out.profile_rebind == ""


def test_matched_rule_carries_profile_rebind() -> None:
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram", chat_id="12345"),
                agent="executive",
                profile="work",
            ),
        ),
    )
    templates = {"executive": _template("executive", "Exec brief.")}
    out = resolve_template_for_event(
        routing,
        _event(platform=Platform.TELEGRAM, chat_id="12345"),
        templates,
    )
    assert out is not None
    assert out.profile_rebind == "work"


# ─── matched rule + missing template → None (caller falls through) ───────


def test_matched_rule_unknown_template_returns_none() -> None:
    """Honest scope: routing names a template, but discover_agents()
    didn't find one with that name. Caller falls through to default
    dispatch (with a WARNING in the dispatcher itself)."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack"),
                agent="ghost",
            ),
        ),
    )
    out = resolve_template_for_event(routing, _event(platform=Platform.SLACK), {})
    assert out is None


def test_matched_rule_template_with_empty_system_prompt_returns_none() -> None:
    """A registered template with an empty system_prompt is treated as
    no-match — same fall-through as missing template (an empty prompt
    would be worse than the default behavior)."""
    routing = RoutingConfig(
        rules=(RoutingRule(match=RoutingMatch(platform="slack"), agent="bare"),),
    )
    templates = {"bare": _template("bare", "")}
    out = resolve_template_for_event(routing, _event(platform=Platform.SLACK), templates)
    assert out is None


# ─── no rules → None ─────────────────────────────────────────────────────


def test_no_rules_returns_none() -> None:
    routing = RoutingConfig()
    out = resolve_template_for_event(routing, _event(), {})
    assert out is None


def test_default_only_returns_none() -> None:
    """Bare default (no rules) — caller dispatches with no template override."""
    routing = RoutingConfig(rules=(), default=RoutingDefault(agent="fallback"))
    templates = {"fallback": _template("fallback", "Default brief.")}
    out = resolve_template_for_event(routing, _event(), templates)
    assert out is None


def test_no_rule_matches_returns_none() -> None:
    """Rules present but none match → None (caller default-dispatches)."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(match=RoutingMatch(platform="slack"), agent="slack-handler"),
        ),
        default=RoutingDefault(agent="fallback"),
    )
    templates = {
        "slack-handler": _template("slack-handler"),
        "fallback": _template("fallback"),
    }
    out = resolve_template_for_event(
        routing,
        _event(platform=Platform.TELEGRAM),  # doesn't match the slack-only rule
        templates,
    )
    assert out is None


# ─── most-specific-wins survives integration ─────────────────────────────


def test_most_specific_template_wins() -> None:
    """The integration must honor sort_rules_by_specificity ordering."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="myguild"),
                agent="guild-default",
            ),
            RoutingRule(
                match=RoutingMatch(
                    platform="discord", guild="myguild", role="admin"
                ),
                agent="admin-only",
            ),
        ),
    )
    templates = {
        "guild-default": _template("guild-default", "GUILD"),
        "admin-only": _template("admin-only", "ADMIN"),
    }
    out = resolve_template_for_event(
        routing,
        _event(platform=Platform.DISCORD, guild="myguild", role="admin"),
        templates,
    )
    assert out is not None
    assert out.template_name == "admin-only"
    assert out.system_prompt == "ADMIN"


def test_event_metadata_extraction() -> None:
    """Channel / guild / role come from event.metadata; chat_id is direct.

    Pin: if MessageEvent.metadata's keys ever change (channel → channel_id
    etc.), this test fails fast.
    """
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="discord", guild="g1", role="mod"),
                agent="modteam",
            ),
        ),
    )
    templates = {"modteam": _template("modteam")}

    # With matching metadata
    matched = resolve_template_for_event(
        routing,
        _event(platform=Platform.DISCORD, guild="g1", role="mod"),
        templates,
    )
    assert matched is not None

    # Wrong role → no match
    miss = resolve_template_for_event(
        routing,
        _event(platform=Platform.DISCORD, guild="g1", role="member"),
        templates,
    )
    assert miss is None
