"""Profile handoff — auto + manual cross-profile context transfer.

Surfaces:
  - Auto-trigger: classifier-driven swap (3-of-3 turns at confidence ≥0.8)
  - Manual: ``/handoff <target>`` slash command
  - Both produce a ``handoff.md`` per the universal handoff-protocol v2.0,
    written to ``<target-profile-home>/inbox/`` and consumed by an
    injection provider on the next turn after the swap.

Public surface — anything importable from outside opencomputer/agent/handoff
re-exports here so call-sites don't reach into submodules.
"""
from __future__ import annotations

from opencomputer.agent.handoff.audit import HandoffAuditLogger, SwapAuditEvent
from opencomputer.agent.handoff.auto_swap import (
    AutoSwapTrigger,
    SwapDecision,
    SwapDecisionReason,
)
from opencomputer.agent.handoff.generator import (
    HandoffGenerationError,
    HandoffGenerator,
)
from opencomputer.agent.handoff.inbox import (
    HandoffInbox,
    HandoffParseError,
    InboxIOError,
)
from opencomputer.agent.handoff.injector import HandoffInjectionProvider
from opencomputer.agent.handoff.models import (
    HandoffDocument,
    HandoffMetadata,
    HandoffWarranted,
)
from opencomputer.agent.handoff.orchestrator import (
    AutoSwapResult,
    ProviderAdapter,
    run_auto_swap_pipeline,
)
from opencomputer.agent.handoff.protocol_v2 import (
    PROTOCOL_VERSION,
    render_handoff_prompt,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AutoSwapResult",
    "AutoSwapTrigger",
    "HandoffAuditLogger",
    "HandoffDocument",
    "HandoffGenerationError",
    "HandoffGenerator",
    "HandoffInbox",
    "HandoffInjectionProvider",
    "HandoffMetadata",
    "HandoffParseError",
    "HandoffWarranted",
    "InboxIOError",
    "ProviderAdapter",
    "SwapAuditEvent",
    "SwapDecision",
    "SwapDecisionReason",
    "render_handoff_prompt",
    "run_auto_swap_pipeline",
]
