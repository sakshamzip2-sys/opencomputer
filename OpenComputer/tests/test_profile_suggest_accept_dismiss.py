"""Plan 3 Task 6 — /profile-suggest accept|dismiss subcommand tests."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_accept_creates_profile_and_seeded_soul(tmp_path, monkeypatch):
    """/profile-suggest accept work → profile dir + seeded SOUL.md exist."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))

    # Pre-populate the cache with a 'work' suggestion
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create",
            name="work",
            persona="coding",
            rationale="18 sessions coding 9am-6pm",
            command="/profile-suggest accept work",
        )],
        dismissed=[],
    )

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={"user_name": "TestUser"})

    result = await cmd.execute("accept work", rt)

    assert "created" in result.output.lower()
    profile_dir = tmp_path / "profiles" / "work"
    assert profile_dir.exists()
    soul = profile_dir / "SOUL.md"
    assert soul.exists()
    assert "work-mode agent for TestUser" in soul.read_text()


@pytest.mark.asyncio
async def test_accept_without_cache_errors(tmp_path, monkeypatch):
    """/profile-suggest accept work with no cache → friendly error."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("accept work", rt)
    assert "no suggestion cache" in result.output.lower()


@pytest.mark.asyncio
async def test_accept_unknown_name_errors(tmp_path, monkeypatch):
    """/profile-suggest accept unknown_name with cache but no matching suggestion."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import save_cache
    save_cache(suggestions=[], dismissed=[])

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("accept unknown_name", rt)
    assert "no pending suggestion" in result.output.lower()


@pytest.mark.asyncio
async def test_accept_without_name_shows_usage(tmp_path, monkeypatch):
    """/profile-suggest accept (no name) → usage message."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("accept", rt)
    assert "usage" in result.output.lower()


@pytest.mark.asyncio
async def test_dismiss_records_in_cache(tmp_path, monkeypatch):
    """/profile-suggest dismiss work → cache shows 'work' dismissed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import save_cache
    save_cache(suggestions=[], dismissed=[])

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("dismiss work", rt)
    assert "dismissed" in result.output.lower()

    from opencomputer.profile_analysis_daily import is_dismissed
    assert is_dismissed("work") is True


@pytest.mark.asyncio
async def test_dismiss_without_name_shows_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("dismiss", rt)
    assert "usage" in result.output.lower()
