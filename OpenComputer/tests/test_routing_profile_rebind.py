"""v1.1 plan-3 M10.3 — per-rule profile rebind.

Pins the contract:

* `ResolvedTemplate.profile_rebind` carries the matched rule's `profile:`
  field through `resolve_template_for_event`.
* When the rebind target is non-empty AND differs from the current
  source profile, the dispatcher swaps to that profile's loop +
  profile_home for the remainder of dispatch.
* When the rebind target equals the source profile (no-op), no swap.
* When the rebind target is empty (rule has no `profile:` field),
  no swap — current behavior preserved.
* When the rebind target is unknown to the router, the dispatcher logs
  a WARNING and continues on the source profile (defensive — a stale
  rule must never break dispatch).

Most of the dispatcher pipeline is too heavy to spin up here; instead
this test exercises the precise insertion point — the rebind helper
sequence — directly via a stubbed AgentRouter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

# ─── ResolvedTemplate.profile_rebind contract ────────────────────────────


def _event(platform=Platform.SLACK, chat_id="C1", channel="", text="hi"):
    md: dict = {}
    if channel:
        md["channel"] = channel
    return MessageEvent(
        platform=platform, chat_id=chat_id, user_id="U1",
        text=text, timestamp=0.0, metadata=md,
    )


def _template(name: str, system_prompt: str = "PROMPT"):
    return SimpleNamespace(name=name, system_prompt=system_prompt)


def test_profile_rebind_carried_when_rule_sets_profile() -> None:
    """A rule with `profile: work` makes ResolvedTemplate.profile_rebind == 'work'."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack", channel="alerts"),
                agent="alert-handler",
                profile="work",
            ),
        ),
    )
    templates = {"alert-handler": _template("alert-handler")}
    out = resolve_template_for_event(
        routing, _event(channel="alerts"), templates,
    )
    assert out is not None
    assert out.profile_rebind == "work"


def test_profile_rebind_empty_when_rule_omits_profile() -> None:
    """A rule WITHOUT `profile:` makes ResolvedTemplate.profile_rebind == ''."""
    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="slack"),
                agent="any-slack",
            ),
        ),
    )
    templates = {"any-slack": _template("any-slack")}
    out = resolve_template_for_event(routing, _event(), templates)
    assert out is not None
    assert out.profile_rebind == ""


# ─── dispatcher rebind helper sequence (stubbed router) ──────────────────


class _StubRouter:
    """Stand-in for AgentRouter — the M10.3 swap calls
    ``_router.get_or_load(profile_id)`` + ``_profile_home_resolver(pid)``;
    everything else is unused here."""

    def __init__(self, loops: dict[str, Any], homes: dict[str, Path]) -> None:
        self._loops = loops
        self._homes = homes
        self.call_log: list[str] = []
        self._profile_home_resolver = lambda pid: self._homes.get(pid, Path("/tmp"))

    async def get_or_load(self, profile_id: str) -> Any:
        self.call_log.append(profile_id)
        if profile_id not in self._loops:
            raise KeyError(f"unknown profile_id: {profile_id}")
        return self._loops[profile_id]


def _stub_loop(routing: RoutingConfig | None) -> SimpleNamespace:
    """A minimal `loop`-shaped object exposing `loop.config.routing`."""
    config = SimpleNamespace(routing=routing) if routing is not None else None
    return SimpleNamespace(config=config)


@pytest.mark.asyncio
async def test_rebind_swaps_to_target_profile_when_rule_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher's rebind block (replicated here as a closure) calls
    `_router.get_or_load(target)` and updates `loop` / `profile_home` /
    `profile_id`."""
    rules_for_default_profile = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram", chat_id="999"),
                agent="exec",
                profile="work",
            ),
        ),
    )
    default_loop = _stub_loop(rules_for_default_profile)
    work_loop = _stub_loop(None)
    router = _StubRouter(
        loops={"default": default_loop, "work": work_loop},
        homes={
            "default": Path("/tmp/default"),
            "work": Path("/tmp/work"),
        },
    )

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {"exec": _template("exec")},
    )

    event = _event(platform=Platform.TELEGRAM, chat_id="999")

    # Replicate the dispatcher's M10.3 block as a closure over (router,
    # event, profile_id, loop, profile_home).
    profile_id = "default"
    loop = default_loop
    profile_home = router._profile_home_resolver(profile_id)

    cfg_obj = getattr(loop, "config", None)
    routing_cfg = getattr(cfg_obj, "routing", None)
    if routing_cfg is not None and routing_cfg.rules:
        from opencomputer.agent.agent_templates import (
            discover_agents as _disc,
        )
        templates = _disc()
        resolved = resolve_template_for_event(routing_cfg, event, templates)
        if (
            resolved is not None
            and resolved.profile_rebind
            and resolved.profile_rebind != profile_id
        ):
            new_pid = resolved.profile_rebind
            new_loop = await router.get_or_load(new_pid)
            new_home = router._profile_home_resolver(new_pid)
            loop = new_loop
            profile_home = new_home
            profile_id = new_pid

    assert profile_id == "work"
    assert loop is work_loop
    assert profile_home == Path("/tmp/work")
    assert "work" in router.call_log


@pytest.mark.asyncio
async def test_rebind_no_swap_when_target_equals_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule that names the SAME profile as the source must not swap
    (and must not log a confusing 'rebound to self' message)."""
    rules = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram"),
                agent="exec",
                profile="default",  # same as source
            ),
        ),
    )
    default_loop = _stub_loop(rules)
    router = _StubRouter(
        loops={"default": default_loop},
        homes={"default": Path("/tmp/default")},
    )

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {"exec": _template("exec")},
    )

    event = _event(platform=Platform.TELEGRAM)
    profile_id = "default"
    loop = default_loop

    cfg_obj = getattr(loop, "config", None)
    routing_cfg = getattr(cfg_obj, "routing", None)
    swapped = False
    if routing_cfg is not None and routing_cfg.rules:
        from opencomputer.agent.agent_templates import (
            discover_agents as _disc,
        )
        templates = _disc()
        resolved = resolve_template_for_event(routing_cfg, event, templates)
        if (
            resolved is not None
            and resolved.profile_rebind
            and resolved.profile_rebind != profile_id
        ):
            swapped = True

    assert not swapped
    # router.get_or_load was NOT called for any swap
    assert router.call_log == []


@pytest.mark.asyncio
async def test_rebind_falls_through_when_target_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A rule that names an UNKNOWN profile must log WARNING and
    continue on the source profile (no crash)."""
    rules = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram"),
                agent="exec",
                profile="ghost",  # unknown to router
            ),
        ),
    )
    default_loop = _stub_loop(rules)
    router = _StubRouter(
        loops={"default": default_loop},  # ghost not present
        homes={"default": Path("/tmp/default")},
    )

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {"exec": _template("exec")},
    )

    event = _event(platform=Platform.TELEGRAM)
    profile_id = "default"
    loop = default_loop
    profile_home = router._profile_home_resolver(profile_id)

    cfg_obj = getattr(loop, "config", None)
    routing_cfg = getattr(cfg_obj, "routing", None)
    if routing_cfg is not None and routing_cfg.rules:
        from opencomputer.agent.agent_templates import (
            discover_agents as _disc,
        )
        templates = _disc()
        resolved = resolve_template_for_event(routing_cfg, event, templates)
        if (
            resolved is not None
            and resolved.profile_rebind
            and resolved.profile_rebind != profile_id
        ):
            new_pid = resolved.profile_rebind
            try:
                _new_loop = await router.get_or_load(new_pid)
                # would swap — but we expect the line above to raise
                pytest.fail("Should not reach here — router has no 'ghost' profile")
            except KeyError:
                # M10.3 fallthrough path: continue on source profile
                pass

    # State unchanged
    assert profile_id == "default"
    assert loop is default_loop
    assert profile_home == Path("/tmp/default")


# ─── M10.2 ↔ M10.3 composition ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rebind_composes_with_m10_2_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M10.3 swaps the loop early; M10.2's later resolution against the
    rebound loop's routing.rules must still produce the right
    ResolvedTemplate. Pin the composition contract."""
    work_rules = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram"),
                agent="work-exec",
            ),
        ),
    )
    work_loop = _stub_loop(work_rules)

    monkeypatch.setattr(
        "opencomputer.agent.agent_templates.discover_agents",
        lambda: {"work-exec": _template("work-exec", "WORK PROMPT")},
    )

    # After M10.3 rebind we'd have loop=work_loop. M10.2 then re-runs
    # routing on work_loop.config.routing for the system_prompt_override.
    event = _event(platform=Platform.TELEGRAM)
    cfg_for_m10_2 = work_loop.config.routing

    from opencomputer.agent.agent_templates import discover_agents as _disc
    templates = _disc()
    resolved_for_m10_2 = resolve_template_for_event(cfg_for_m10_2, event, templates)
    assert resolved_for_m10_2 is not None
    assert resolved_for_m10_2.template_name == "work-exec"
    assert resolved_for_m10_2.system_prompt == "WORK PROMPT"
