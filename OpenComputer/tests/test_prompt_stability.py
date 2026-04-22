"""Phase gap-closure §3.2: tool-ordering + prompt-cache stability regression.

A silent prefix-cache miss costs ~10× tokens with no error signal, so
CLAUDE.md line 59 treats prompt-cache stability as a *correctness*
concern. These tests are the tripwire: if anyone ever reintroduces a
map/set iteration at request-assembly time, or forgets to call the
sort helper before handing tools to the provider, these tests fail.

Tests:
1. sort_tools_for_request is deterministic under shuffled input.
2. Two registrations of the same tool set in different order produce
   byte-identical tools blocks.
3. AgentLoop wires the sort helper into the provider.complete call
   (turn-to-turn registry mutation that shouldn't matter → same tools).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.tool_ordering import sort_tools_for_request
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage
from plugin_sdk.tool_contract import ToolSchema

# ─── direct helper tests ───────────────────────────────────────────────


def _schema(name: str) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
    )


def _hash_tools(tools: list[ToolSchema]) -> str:
    """Hash the JSON-serialised tools block. This is what Anthropic sees."""
    payload = json.dumps(
        [t.to_anthropic_format() for t in tools], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_sort_tools_is_deterministic_under_shuffled_input() -> None:
    a = [_schema("zeta"), _schema("alpha"), _schema("mu")]
    b = [_schema("mu"), _schema("alpha"), _schema("zeta")]
    assert [t.name for t in sort_tools_for_request(a)] == ["alpha", "mu", "zeta"]
    assert sort_tools_for_request(a) == sort_tools_for_request(b)


def test_hash_is_byte_equal_across_shuffles() -> None:
    a = [_schema("x"), _schema("b"), _schema("alpha"), _schema("mu")]
    b = list(reversed(a))
    h_a = _hash_tools(sort_tools_for_request(a))
    h_b = _hash_tools(sort_tools_for_request(b))
    assert h_a == h_b, "tools hash drifted — prefix cache would miss"


def test_unsorted_registry_hash_would_drift() -> None:
    """Control: confirm the hash *would* differ without the sort — so the
    previous test is actually catching a real invariant."""
    a = [_schema("z"), _schema("a")]
    b = [_schema("a"), _schema("z")]
    assert _hash_tools(a) != _hash_tools(b)


# ─── AgentLoop integration ─────────────────────────────────────────────


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(
            provider="mock", model="mock-model", max_tokens=1024, temperature=0.0
        ),
        loop=LoopConfig(max_iterations=1, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _endturn_provider(capture: list[list[ToolSchema]]) -> MagicMock:
    """Provider that records the tools argument it was handed per call."""

    async def complete(**kwargs):
        capture.append(kwargs["tools"])
        return ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )

    p = MagicMock()
    p.complete = AsyncMock(side_effect=complete)
    return p


async def test_loop_sorts_tools_before_handing_to_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the registry hands back tools in registration order, the
    loop must sort them before calling provider.complete — so the prefix
    cache hits on turn 2 when a new plugin reshuffles the registry."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    from opencomputer.tools.registry import registry

    # Turn 1: registry returns [zeta, alpha, mu]
    turn1_schemas = [_schema("zeta"), _schema("alpha"), _schema("mu")]
    captured: list[list[ToolSchema]] = []
    provider = _endturn_provider(captured)

    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=turn1_schemas))
    await loop.run_conversation(user_message="turn 1", session_id="s-stab")

    # Turn 2: same tools but registry order changed (plugin hot-reload scenario)
    turn2_schemas = [_schema("mu"), _schema("zeta"), _schema("alpha")]
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=turn2_schemas))
    await loop.run_conversation(user_message="turn 2", session_id="s-stab2")

    assert len(captured) == 2
    # The tools block handed to the provider must be byte-identical even
    # though the registry handed back different orders.
    assert _hash_tools(captured[0]) == _hash_tools(captured[1]), (
        "provider got different tool orderings on two turns — prefix cache miss"
    )
    assert [t.name for t in captured[0]] == ["alpha", "mu", "zeta"]
