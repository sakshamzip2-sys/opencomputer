"""Phase gap-closure §3.3: the session prompt-snapshot cache is LRU-bounded.

In gateway mode a single AgentLoop instance serves all sessions on the box.
Without eviction, `_prompt_snapshots` grows monotonically — one entry per
lifetime session, forever. These tests assert LRU semantics: the cache
never exceeds the configured cap, the oldest entry is evicted first, and
access refreshes "recently used".

Note on scope: hermes also runs a separate `auxiliary_client` cache keyed
by (provider, base_url, api_key, …). That layer does NOT apply to
OpenComputer because providers here are plugin-singletons instantiated
once per process — there is nothing to cache. See
/Users/saksham/.claude/plans/what-all-do-you-misty-cookie.md §3.3.
"""

from __future__ import annotations

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
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage


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


def _endturn_provider() -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )
    )
    return p


async def test_lru_cap_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_endturn_provider(),
        config=cfg,
        compaction_disabled=True,
        prompt_snapshot_cache_max=3,
    )
    for i in range(5):
        await loop.run_conversation(user_message="hi", session_id=f"s-{i}")

    # Only the last 3 session snapshots survive.
    assert len(loop._prompt_snapshots) == 3
    assert list(loop._prompt_snapshots.keys()) == ["s-2", "s-3", "s-4"]


async def test_access_marks_session_as_recently_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accessing an existing snapshot should move it to the most-recent end
    so it isn't evicted when a new session arrives."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_endturn_provider(),
        config=cfg,
        compaction_disabled=True,
        prompt_snapshot_cache_max=3,
    )
    await loop.run_conversation(user_message="hi", session_id="s-a")
    await loop.run_conversation(user_message="hi", session_id="s-b")
    await loop.run_conversation(user_message="hi", session_id="s-c")
    # Touch s-a — it should now be the most-recently-used.
    await loop.run_conversation(user_message="hi", session_id="s-a")
    # New session arrives — s-b (least recently used) should be evicted,
    # s-a should survive.
    await loop.run_conversation(user_message="hi", session_id="s-d")

    assert "s-a" in loop._prompt_snapshots
    assert "s-b" not in loop._prompt_snapshots
    assert set(loop._prompt_snapshots.keys()) == {"s-a", "s-c", "s-d"}


async def test_system_override_does_not_pollute_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_endturn_provider(),
        config=cfg,
        compaction_disabled=True,
        prompt_snapshot_cache_max=2,
    )
    # 3 override sessions, 2 natural sessions — cap is 2, so cache should
    # end up with only the 2 natural ones.
    for i in range(3):
        await loop.run_conversation(
            user_message="hi",
            session_id=f"ovr-{i}",
            system_override="x",
        )
    await loop.run_conversation(user_message="hi", session_id="nat-a")
    await loop.run_conversation(user_message="hi", session_id="nat-b")

    assert set(loop._prompt_snapshots.keys()) == {"nat-a", "nat-b"}
    for i in range(3):
        assert f"ovr-{i}" not in loop._prompt_snapshots
