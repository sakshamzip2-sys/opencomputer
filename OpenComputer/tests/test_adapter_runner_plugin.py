"""Tests for the adapter-runner plugin's register() — discovery + tool registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class _FakeApi:
    def __init__(self):
        self.tools: list[Any] = []
        self.doctor_contributions: list[Any] = []

    def register_tool(self, tool: Any) -> None:
        self.tools.append(tool)

    def register_doctor_contribution(self, contribution: Any) -> None:
        self.doctor_contributions.append(contribution)


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_register_discovers_bundled_pack_and_registers_tools():
    """The 8 bundled adapters should each become a synthetic tool."""
    from extensions.adapter_runner.plugin import register

    api = _FakeApi()
    register(api)

    tool_names = {t.schema.name for t in api.tools}
    expected = {
        "HackernewsTop",
        "ArxivSearch",
        "RedditHot",
        "GithubNotifications",
        "ApplePodcastsSearch",
        "AmazonTrackPrice",
        "CursorAppRecentFiles",
        "ChatgptAppNewChat",
    }
    missing = expected - tool_names
    assert not missing, f"missing tools: {missing}"


def test_register_adds_doctor_row():
    from extensions.adapter_runner.plugin import register

    api = _FakeApi()
    register(api)
    assert any(c.id == "adapter-runner" for c in api.doctor_contributions)


def test_register_adapter_pack_helper(tmp_path: Path):
    """``register_adapter_pack`` walks just the given dir + registers tools."""
    from extensions.adapter_runner import register_adapter_pack

    pack_root = tmp_path / "my-pack"
    site_dir = pack_root / "adapters" / "thirdparty"
    site_dir.mkdir(parents=True)
    (site_dir / "thing.py").write_text(
        '''
from extensions.adapter_runner import adapter, Strategy

@adapter(site="thirdparty", name="thing", description="d", domain="e.com",
         strategy=Strategy.PUBLIC, columns=["x"])
async def run(args, ctx):
    return [{"x": 1}]
'''
    )

    api = _FakeApi()
    register_adapter_pack(api, adapters_dir=pack_root / "adapters")
    names = {t.schema.name for t in api.tools}
    assert "ThirdpartyThing" in names
