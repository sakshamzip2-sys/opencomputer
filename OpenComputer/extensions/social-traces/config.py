"""Config schema for the social-traces plugin.

Read from the active profile's ``config.yaml`` under the ``social_traces:``
top-level key. Missing keys fall back to the defaults below; a missing
section means "use all defaults" (and the plugin still respects the
on-disk enabled flag managed by :mod:`extensions.social_traces.state`).

Frozen dataclass so plugin code can pass it around without worrying
about mutation, and so config-validation errors surface at parse time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Default soft timeout on the pre-task network query. Anything slower
#: falls through to the explore path — agent never paralyzed by network.
DEFAULT_QUERY_TIMEOUT_S: float = 1.0

#: Default top-K returned from the network.
DEFAULT_TOP_K: int = 3

#: Below this score the trace is treated as "no good match" — agent
#: explores instead of injecting. Network's own ranker should do most of
#: the work; this is a final client-side gate.
DEFAULT_RELEVANCE_THRESHOLD: float = 0.6

#: Cap on outbox size so a long network outage can't fill the disk with
#: queued submissions. Excess submissions are dropped with a WARN log.
DEFAULT_MAX_OUTBOX: int = 100

#: Cost cap for the post-task novelty judge + distiller pipeline. Three
#: Haiku calls per session at ~$0.005 typical → 0.05 leaves margin for
#: longer transcripts. Per-session, not cumulative.
DEFAULT_COST_GUARD_USD: float = 0.05

Backend = Literal["local", "http"]


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    """Privacy redaction toggles. All on by default; the network must
    never see raw user data, so loosening these is a deliberate act."""

    redact_paths: bool = True
    redact_hostnames: bool = True
    #: Names of additional caller-registered redactor callables. Other
    #: plugins may publish their own redactors via the plugin SDK; this
    #: field opts the social-traces plugin into them by id.
    extra_redactors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NoveltyJudgeConfig:
    """Rule (d) configuration: when a trace was used at pre-task time, run
    one Haiku call at session-end to judge whether the agent improved on
    it. ``enabled=False`` collapses to rule (a) — never emit when a trace
    was used."""

    enabled: bool = True
    cost_guard_usd_per_session: float = DEFAULT_COST_GUARD_USD


@dataclass(frozen=True, slots=True)
class QueryConfig:
    """Pre-task network-query knobs."""

    soft_timeout_s: float = DEFAULT_QUERY_TIMEOUT_S
    top_k: int = DEFAULT_TOP_K
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD


@dataclass(frozen=True, slots=True)
class OutboxConfig:
    """Local submission queue knobs."""

    max_pending: int = DEFAULT_MAX_OUTBOX


@dataclass(frozen=True, slots=True)
class SocialTracesConfig:
    """Top-level config for the plugin.

    ``backend`` selects the :class:`plugin_sdk.TraceNetworkClient`
    implementation:

    * ``local`` — :class:`LocalFileTraceNetworkClient` (dev stub, reads/
      writes JSON in ``<profile_home>/traces/{inbox,outbox}/``).
    * ``http`` — :class:`HttpTraceNetworkClient` (talks to OpenHub at
      ``endpoint``).

    The on-disk enabled flag (managed by
    :mod:`extensions.social_traces.state`) is checked SEPARATELY and
    takes precedence — even with ``enabled: true`` here the plugin stays
    silent until ``oc traces enable`` writes the state file.
    """

    enabled: bool = False
    backend: Backend = "local"
    endpoint: str = "http://localhost:8000"

    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    novelty_judge: NoveltyJudgeConfig = field(default_factory=NoveltyJudgeConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    outbox: OutboxConfig = field(default_factory=OutboxConfig)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return default


def from_config_dict(raw: Any) -> SocialTracesConfig:
    """Parse a raw ``social_traces:`` dict from config.yaml.

    Tolerates missing or partial sections — every key falls back to the
    documented default. Returns the canonical dataclass.
    """
    if not isinstance(raw, dict):
        return SocialTracesConfig()

    privacy_raw = raw.get("privacy", {}) or {}
    privacy = PrivacyConfig(
        redact_paths=_coerce_bool(privacy_raw.get("redact_paths"), True),
        redact_hostnames=_coerce_bool(privacy_raw.get("redact_hostnames"), True),
        extra_redactors=tuple(privacy_raw.get("extra_redactors") or ()),
    )

    nj_raw = raw.get("novelty_judge", {}) or {}
    novelty = NoveltyJudgeConfig(
        enabled=_coerce_bool(nj_raw.get("enabled"), True),
        cost_guard_usd_per_session=float(
            nj_raw.get("cost_guard_usd_per_session", DEFAULT_COST_GUARD_USD)
        ),
    )

    q_raw = raw.get("query", {}) or {}
    query = QueryConfig(
        soft_timeout_s=float(q_raw.get("soft_timeout_s", DEFAULT_QUERY_TIMEOUT_S)),
        top_k=int(q_raw.get("top_k", DEFAULT_TOP_K)),
        relevance_threshold=float(
            q_raw.get("relevance_threshold", DEFAULT_RELEVANCE_THRESHOLD)
        ),
    )

    o_raw = raw.get("outbox", {}) or {}
    outbox = OutboxConfig(
        max_pending=int(o_raw.get("max_pending", DEFAULT_MAX_OUTBOX)),
    )

    return SocialTracesConfig(
        enabled=_coerce_bool(raw.get("enabled"), False),
        backend=raw.get("backend", "local"),
        endpoint=str(raw.get("endpoint", "http://localhost:8000")),
        privacy=privacy,
        novelty_judge=novelty,
        query=query,
        outbox=outbox,
    )


__all__ = [
    "Backend",
    "DEFAULT_COST_GUARD_USD",
    "DEFAULT_MAX_OUTBOX",
    "DEFAULT_QUERY_TIMEOUT_S",
    "DEFAULT_RELEVANCE_THRESHOLD",
    "DEFAULT_TOP_K",
    "NoveltyJudgeConfig",
    "OutboxConfig",
    "PrivacyConfig",
    "QueryConfig",
    "SocialTracesConfig",
    "from_config_dict",
]
