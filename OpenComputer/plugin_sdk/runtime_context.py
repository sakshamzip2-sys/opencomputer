"""
RuntimeContext — per-turn flags passed through the agent loop.

The CLI / caller builds this once per invocation. The agent loop passes it
to InjectionProviders (so they can decide whether to fire) and to Hooks
(so they can decide whether to block). `delegate` propagates it to
subagents, so modes like `--plan` apply to the whole subagent tree.

Frozen dataclass — safe to share across tasks / threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Flags that cross-cutting modes read. Immutable per invocation."""

    #: Plan mode — agent should describe what it would do without executing
    #: destructive tools. Enforced both via injection (prompt) and hook (hard-block).
    plan_mode: bool = False

    #: Yolo mode — auto-approve dangerous operations. Mutually exclusive with plan_mode
    #: (we enforce this in the CLI; if both set, plan_mode wins).
    yolo_mode: bool = False

    #: Which agent context this invocation belongs to. ``Literal`` narrows the
    #: allowed values at type-check time so typos like ``"Cron"`` fail mypy
    #: rather than silently no-op-ing the guards downstream. Default
    #: ``"chat"`` preserves existing behaviour. ``"cron"`` and ``"flush"``
    #: short-circuit external memory providers — those batch jobs shouldn't
    #: spin a Docker stack for quick background work (baseline SQLite+FTS5 is
    #: enough). Mirrors Hermes'
    #: ``sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286``.
    agent_context: Literal["chat", "cron", "flush", "review"] = "chat"

    #: Escape hatch for third-party plugins to add their own modes without
    #: forcing an SDK version bump.
    custom: dict[str, Any] = field(default_factory=dict)


#: A sentinel "no flags" default — useful when callers don't care about modes.
DEFAULT_RUNTIME_CONTEXT = RuntimeContext()


__all__ = ["RuntimeContext", "DEFAULT_RUNTIME_CONTEXT"]
