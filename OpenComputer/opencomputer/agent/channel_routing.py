"""Per-channel agent-template routing (v1.1 plan-3 M10).

Lets a single OpenComputer gateway daemon serve multiple distinct
conversation surfaces with different agent personalities — for example,
a Slack ``#security-alerts`` channel routes to the ``security-reviewer``
agent template, while a personal Telegram chat routes to
``executive-assistant`` running under the ``executive`` profile.

Configured under ``routing:`` in profile config.yaml::

    routing:
      rules:
        - match: {platform: slack, channel: "#security-alerts"}
          agent: security-reviewer
        - match: {platform: telegram, peer: "<chat_id>"}
          agent: executive-assistant
          profile: executive
        - match: {platform: discord, guild: "myguild", role: "admin"}
          agent: admin-agent
      default:
        agent: default

Most-specific-wins precedence (matches OpenClaw):

    1. exact peer match
    2. parent peer (thread inheritance)
    3. guild + roles (more keys = more specific)
    4. guild
    5. team
    6. account
    7. channel
    8. (default rule, if any)

This module supplies:

- :class:`ChannelRoutingConfig` / :class:`ChannelRoutingRule` /
  :class:`ChannelRoutingMatch` — the typed configuration shape.
- :class:`ResolvedRoute` — the output of :func:`match_route`.
- :func:`match_route` — pure routing-decision function (no IO).
- :func:`load_routing_config` — parses the ``routing:`` block from
  the profile YAML and validates it strictly (unknown match keys
  raise so typos surface at config-load, not at first inbound).

The gateway integration (``opencomputer.gateway.dispatch.handle_message``)
calls :func:`match_route` once per inbound MessageEvent before invoking
the agent loop.  When a rule matches and sets ``profile:``, the gateway
re-binds its per-message context to that profile (memory + creds +
agent templates) before dispatching — see follow-up dispatch wiring
PR for details.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("opencomputer.agent.channel_routing")


# Match keys supported.  Adding a new key requires updating both the
# specificity scoring in _specificity() and the strict-validation set
# below.  Anything else raises ValueError at config load.
_VALID_MATCH_KEYS: frozenset[str] = frozenset({
    "platform",
    "peer",        # specific chat_id (highest specificity for that platform)
    "parent_peer", # thread parent (inherited routing)
    "channel",     # named channel like #security-alerts
    "guild",       # discord guild / matrix space
    "team",        # slack team / mattermost workspace
    "account",     # account id (e.g. discord user id)
    "role",        # role name (typically combined with guild)
})


@dataclass(frozen=True, slots=True)
class ChannelRoutingMatch:
    """One ``match:`` block in a routing rule.

    All fields are optional.  An empty match block matches everything
    (effectively a wildcard); typically you'd use the ``routing.default``
    section for that instead.
    """

    platform: str | None = None
    peer: str | None = None
    parent_peer: str | None = None
    channel: str | None = None
    guild: str | None = None
    team: str | None = None
    account: str | None = None
    role: str | None = None

    def specificity(self) -> int:
        """Higher = more specific.  Used for tie-breaking among rules.

        Specificity favors exact identity (peer) over coarser
        scoping (channel, then guild, then team, then platform).
        Multiple keys add up — a rule that matches both guild AND role
        is more specific than guild alone.
        """
        score = 0
        if self.peer is not None:
            score += 1000
        if self.parent_peer is not None:
            score += 500
        if self.role is not None:
            score += 200
        if self.guild is not None:
            score += 100
        if self.team is not None:
            score += 80
        if self.account is not None:
            score += 60
        if self.channel is not None:
            score += 40
        if self.platform is not None:
            score += 1
        return score

    def matches(self, event: dict[str, Any]) -> bool:
        """Return True if every set field equals the corresponding
        value on the event.  Unset fields don't constrain the match.
        """
        for key in _VALID_MATCH_KEYS:
            expected = getattr(self, key)
            if expected is None:
                continue
            actual = event.get(key)
            if actual is None or str(actual) != str(expected):
                return False
        return True


@dataclass(frozen=True, slots=True)
class BroadcastTarget:
    """One destination in a broadcast fan-out (v1.1 plan-3 M11.4).

    Each ``BroadcastTarget`` is dispatched as a separate concurrent
    AgentLoop run.  ``profile`` is optional per-target so different
    targets can run under different profiles (different memory + creds
    + agent templates) — e.g. one inbound triggers ``code-reviewer``
    in the ``coder`` profile AND ``security-reviewer`` in the
    ``audit`` profile simultaneously.
    """

    agent: str
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelRoutingRule:
    """One rule mapping a match-pattern to an agent template (and
    optionally a different profile).

    For broadcast fan-out (M11.4), set ``broadcast_to`` to a tuple of
    :class:`BroadcastTarget`; the gateway then dispatches to every
    target in parallel.  When ``broadcast_to`` is non-empty, the
    ``agent`` field is treated as a sentinel (convention:
    ``"<broadcast>"``) and is not used for dispatch.
    """

    match: ChannelRoutingMatch
    agent: str
    profile: str | None = None
    """Optional cross-profile re-bind for the SINGLE-target case.
    When set, the gateway swaps its per-message MemoryManager +
    credential pool + agent-template registry to the named profile
    before dispatching.  Ignored when ``broadcast_to`` is non-empty."""
    broadcast_to: tuple[BroadcastTarget, ...] = ()
    """v1.1 plan-3 M11.4 — broadcast fan-out.  When non-empty, the
    gateway dispatches the inbound message to EVERY target in
    parallel (one AgentLoop per target).  ``agent`` and ``profile``
    are ignored when ``broadcast_to`` is set."""


@dataclass(frozen=True, slots=True)
class ChannelRoutingDefault:
    """The ``default:`` block — used when no rule matches."""

    agent: str = "default"
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelRoutingConfig:
    """Full ``routing:`` block from profile config.yaml."""

    rules: tuple[ChannelRoutingRule, ...] = ()
    default: ChannelRoutingDefault = field(default_factory=ChannelRoutingDefault)


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """Output of :func:`match_route`.

    Single-target case: ``broadcast_targets`` is empty, ``agent`` and
    ``profile`` carry the destination.

    Broadcast case (v1.1 plan-3 M11.4): ``broadcast_targets`` is
    non-empty; the gateway dispatches to every target in parallel.
    ``agent`` is the sentinel ``"<broadcast>"`` and ``profile`` is
    ``None``.
    """

    agent: str
    profile: str | None
    matched_rule_index: int | None
    """0-indexed position of the matching rule in the config; None when
    the default rule fired."""
    broadcast_targets: tuple[BroadcastTarget, ...] = ()
    """Empty for single-target dispatch; non-empty when the matched
    rule was a broadcast rule (M11.4)."""

    @property
    def is_broadcast(self) -> bool:
        return bool(self.broadcast_targets)


def match_route(
    config: ChannelRoutingConfig,
    event: dict[str, Any],
) -> ResolvedRoute:
    """Resolve an agent template + optional profile for an inbound event.

    Pure function: no IO, no side effects.  ``event`` is a dict carrying
    the match-relevant fields from the gateway's MessageEvent
    (typically ``platform``, ``peer``, optionally ``parent_peer``,
    ``channel``, ``guild``, ``team``, ``account``, ``role``).

    Behavior:
    - Walks all rules; among those whose ``match`` succeeds, the most
      specific wins (higher :meth:`ChannelRoutingMatch.specificity`).
    - Ties at the same specificity score are broken by FIRST rule order
      (deterministic; lets users put earlier-defined rules ahead).
    - If no rule matches, returns the ``default`` agent + profile.
    """
    best: tuple[int, int, ChannelRoutingRule] | None = None  # (specificity, -idx, rule)
    for idx, rule in enumerate(config.rules):
        if not rule.match.matches(event):
            continue
        score = rule.match.specificity()
        # FIRST rule wins on tie — implement by preferring the lower
        # index, which means we use a `< score or (== score and lower idx)`
        # comparison.  Track best as (score, -idx, rule) so the standard
        # max-pair comparison works.
        candidate = (score, -idx, rule)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        return ResolvedRoute(
            agent=config.default.agent,
            profile=config.default.profile,
            matched_rule_index=None,
        )

    score, neg_idx, rule = best
    return ResolvedRoute(
        agent=rule.agent,
        profile=rule.profile,
        matched_rule_index=-neg_idx,
        broadcast_targets=rule.broadcast_to,
    )


# ─── config loader (strict validation) ────────────────────────────


def load_routing_config(raw: Any) -> ChannelRoutingConfig:
    """Parse the ``routing:`` block from profile config.yaml.

    Raises ``ValueError`` on schema violations (unknown match keys,
    missing ``agent``, malformed types).  This way a typo in
    ``platfrom:`` surfaces at config-load time, not at first inbound
    message.
    """
    if raw is None:
        return ChannelRoutingConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"routing: must be a mapping, got {type(raw).__name__}"
        )

    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ValueError(
            f"routing.rules must be a list, got {type(rules_raw).__name__}"
        )

    rules: list[ChannelRoutingRule] = []
    for i, rule_raw in enumerate(rules_raw):
        if not isinstance(rule_raw, dict):
            raise ValueError(
                f"routing.rules[{i}] must be a mapping, "
                f"got {type(rule_raw).__name__}"
            )
        rules.append(_parse_rule(rule_raw, index=i))

    default_raw = raw.get("default") or {}
    if not isinstance(default_raw, dict):
        raise ValueError(
            f"routing.default must be a mapping, got {type(default_raw).__name__}"
        )
    default = _parse_default(default_raw)

    return ChannelRoutingConfig(rules=tuple(rules), default=default)


def _parse_rule(raw: dict[str, Any], *, index: int) -> ChannelRoutingRule:
    match_raw = raw.get("match") or {}
    if not isinstance(match_raw, dict):
        raise ValueError(
            f"routing.rules[{index}].match must be a mapping, "
            f"got {type(match_raw).__name__}"
        )
    # Strict validation: reject unknown match keys so typos surface here.
    unknown = set(match_raw.keys()) - _VALID_MATCH_KEYS
    if unknown:
        raise ValueError(
            f"routing.rules[{index}].match has unknown key(s) "
            f"{sorted(unknown)!r}; valid keys are {sorted(_VALID_MATCH_KEYS)!r}"
        )
    match = ChannelRoutingMatch(
        platform=_str_or_none(match_raw.get("platform")),
        peer=_str_or_none(match_raw.get("peer")),
        parent_peer=_str_or_none(match_raw.get("parent_peer")),
        channel=_str_or_none(match_raw.get("channel")),
        guild=_str_or_none(match_raw.get("guild")),
        team=_str_or_none(match_raw.get("team")),
        account=_str_or_none(match_raw.get("account")),
        role=_str_or_none(match_raw.get("role")),
    )
    agent = raw.get("agent")
    # Detect broadcast key presence separately from truthiness so that
    # an explicit `broadcast_to: []` raises "must be non-empty" rather
    # than silently falling through to the agent-required check.
    if "broadcast_to" in raw:
        broadcast_raw: Any = raw.get("broadcast_to")
        broadcast_present = True
    elif "broadcast" in raw:
        broadcast_raw = raw.get("broadcast")
        broadcast_present = True
    else:
        broadcast_raw = None
        broadcast_present = False

    # If broadcast is set, agent is allowed to be the sentinel
    # "<broadcast>" or any non-empty string (the gateway ignores it).
    # If broadcast is NOT set, agent is required.
    if broadcast_present:
        broadcast_targets = _parse_broadcast(broadcast_raw, index=index)
        # agent has loose validation in broadcast mode — just needs to be
        # a string (sentinel "<broadcast>" by convention).
        if agent is None:
            agent = "<broadcast>"
        if not isinstance(agent, str):
            raise ValueError(
                f"routing.rules[{index}].agent must be a string"
            )
    else:
        broadcast_targets = ()
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError(
                f"routing.rules[{index}].agent must be a non-empty string"
            )
        agent = agent.strip()
    profile = raw.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise ValueError(
            f"routing.rules[{index}].profile must be a non-empty string when set"
        )
    return ChannelRoutingRule(
        match=match,
        agent=agent,
        profile=profile,
        broadcast_to=broadcast_targets,
    )


def _parse_broadcast(
    raw: Any, *, index: int
) -> tuple[BroadcastTarget, ...]:
    """Parse the ``broadcast_to:`` (or legacy ``broadcast:``) list.

    Each element is either a bare string (just an agent name) or
    a dict ``{agent: ..., profile: ...}``.  Strict validation on
    types/empties matches the rest of the loader's posture.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"routing.rules[{index}].broadcast_to must be a list, "
            f"got {type(raw).__name__}"
        )
    if not raw:
        raise ValueError(
            f"routing.rules[{index}].broadcast_to must be non-empty when set"
        )
    targets: list[BroadcastTarget] = []
    for j, entry in enumerate(raw):
        if isinstance(entry, str):
            agent_name = entry.strip()
            if not agent_name:
                raise ValueError(
                    f"routing.rules[{index}].broadcast_to[{j}] must be "
                    f"a non-empty agent name"
                )
            targets.append(BroadcastTarget(agent=agent_name))
        elif isinstance(entry, dict):
            agent_name = entry.get("agent")
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise ValueError(
                    f"routing.rules[{index}].broadcast_to[{j}].agent "
                    f"must be a non-empty string"
                )
            target_profile = entry.get("profile")
            if target_profile is not None and (
                not isinstance(target_profile, str)
                or not target_profile.strip()
            ):
                raise ValueError(
                    f"routing.rules[{index}].broadcast_to[{j}].profile "
                    f"must be a non-empty string when set"
                )
            targets.append(
                BroadcastTarget(agent=agent_name.strip(), profile=target_profile)
            )
        else:
            raise ValueError(
                f"routing.rules[{index}].broadcast_to[{j}] must be a string "
                f"or a mapping, got {type(entry).__name__}"
            )
    return tuple(targets)


def _parse_default(raw: dict[str, Any]) -> ChannelRoutingDefault:
    agent = raw.get("agent", "default")
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("routing.default.agent must be a non-empty string")
    profile = raw.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise ValueError(
            "routing.default.profile must be a non-empty string when set"
        )
    return ChannelRoutingDefault(agent=agent.strip(), profile=profile)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


__all__ = [
    "BroadcastTarget",
    "ChannelRoutingConfig",
    "ChannelRoutingDefault",
    "ChannelRoutingMatch",
    "ChannelRoutingRule",
    "ResolvedRoute",
    "load_routing_config",
    "match_route",
]
