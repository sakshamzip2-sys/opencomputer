"""Resolve a MessageEvent to a profile_id using bindings.yaml.

Match precedence — most-specific binding wins, ties broken by
``priority`` descending.

Specificity score (higher = more specific):
  peer_id     = 5
  chat_id     = 4
  group_id    = 3
  account_id  = 2
  platform    = 1

A binding's specificity is the SUM of present-and-matching fields.
A binding with ``match: {}`` has specificity 0 — only beats the
``default_profile`` fall-through.

Pass-2 F3 fix
-------------
At construction time the resolver scans the bindings and logs ERROR
for any binding whose match references a field the platform does
NOT surface yet in v1. Without this warning the binding would
silently never match — see the ``_ADAPTER_SUPPORT_MATRIX`` below.
"""
from __future__ import annotations

import logging
from typing import Any

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
)
from plugin_sdk.core import MessageEvent

logger = logging.getLogger("opencomputer.gateway.binding_resolver")

#: Specificity weights for each match field. Higher is more specific.
_FIELD_WEIGHTS: dict[str, int] = {
    "peer_id": 5,
    "chat_id": 4,
    "group_id": 3,
    "account_id": 2,
    "platform": 1,
}

#: Pass-2 F3: which match fields each platform's adapter surfaces in
#: v1 inbound MessageEvents. Used to warn at resolver-construction
#: time when a user binds against a field the platform doesn't fill.
#: Extend as adapters add metadata.
_ADAPTER_SUPPORT_MATRIX: dict[str, frozenset[str]] = {
    "telegram": frozenset({"platform", "chat_id"}),
    "discord":  frozenset({"platform", "chat_id"}),
    "slack":    frozenset({"platform", "chat_id"}),
    "matrix":   frozenset({"platform", "chat_id"}),
    "signal":   frozenset({"platform", "chat_id"}),
    "whatsapp": frozenset({"platform", "chat_id"}),
    "imessage": frozenset({"platform", "chat_id"}),
    "email":    frozenset({"platform", "chat_id"}),
    "webhook":  frozenset({"platform", "chat_id"}),
    "sms":      frozenset({"platform", "chat_id"}),
    "mattermost": frozenset({"platform", "chat_id"}),
}


class BindingResolver:
    """Resolve a ``MessageEvent`` to a ``profile_id``."""

    def __init__(self, cfg: BindingsConfig) -> None:
        self._cfg = cfg
        self._validate(cfg)

    def _validate(self, cfg: BindingsConfig) -> None:
        """Pass-2 F3: log ERROR for bindings that reference unsupported
        match fields. The binding will never match — silent miss is
        the worst outcome."""
        for i, b in enumerate(cfg.bindings):
            platform = b.match.platform
            if platform is None:
                continue  # platform-agnostic; we can't predict per-event support
            supported = _ADAPTER_SUPPORT_MATRIX.get(platform, frozenset())
            for field_name in ("peer_id", "group_id", "account_id"):
                if (
                    getattr(b.match, field_name) is not None
                    and field_name not in supported
                ):
                    logger.error(
                        "binding[%d]: platform=%s does not surface match field "
                        "%s in v1; this binding will never match. Use chat_id "
                        "or platform-only matching.",
                        i, platform, field_name,
                    )

    def resolve(self, event: MessageEvent) -> str:
        """Return the matching profile_id, or ``default_profile`` on miss."""
        platform = event.platform.value if event.platform else None
        meta = event.metadata or {}

        candidates: list[tuple[int, int, Binding]] = []
        for b in self._cfg.bindings:
            score = self._match_score(b.match, event, platform, meta)
            if score is None:
                continue
            candidates.append((score, b.priority, b))

        if not candidates:
            return self._cfg.default_profile

        # Sort by (specificity_score desc, priority desc).
        candidates.sort(key=lambda t: (-t[0], -t[1]))
        return candidates[0][2].profile

    def _match_score(
        self,
        match: BindingMatch,
        event: MessageEvent,
        platform: str | None,
        meta: dict[str, Any],
    ) -> int | None:
        """Return the specificity score, or None if any present field mismatches."""
        score = 0
        if match.platform is not None:
            if platform != match.platform:
                return None
            score += _FIELD_WEIGHTS["platform"]
        if match.chat_id is not None:
            if event.chat_id != match.chat_id:
                return None
            score += _FIELD_WEIGHTS["chat_id"]
        if match.group_id is not None:
            if str(meta.get("group_id", "")) != match.group_id:
                return None
            score += _FIELD_WEIGHTS["group_id"]
        if match.peer_id is not None:
            if str(meta.get("peer_id", "")) != match.peer_id:
                return None
            score += _FIELD_WEIGHTS["peer_id"]
        if match.account_id is not None:
            if str(meta.get("account_id", "")) != match.account_id:
                return None
            score += _FIELD_WEIGHTS["account_id"]
        return score


__all__ = ["BindingResolver"]
