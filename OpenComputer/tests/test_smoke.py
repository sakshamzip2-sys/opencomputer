"""Smoke tests — verify the package imports and core pieces wire up."""

from __future__ import annotations

import tempfile
from pathlib import Path


def test_package_imports() -> None:
    import opencomputer

    assert opencomputer.__version__ == "0.0.1"


def test_cli_module_imports() -> None:
    from opencomputer import cli

    assert hasattr(cli, "main")
    assert hasattr(cli, "app")


def test_plugin_sdk_imports() -> None:
    import plugin_sdk

    assert plugin_sdk.__version__ == "0.1.0"
    # Verify the public surface
    assert plugin_sdk.BaseTool is not None
    assert plugin_sdk.BaseProvider is not None
    assert plugin_sdk.BaseChannelAdapter is not None
    assert plugin_sdk.MessageEvent is not None
    assert plugin_sdk.StopReason is not None


def test_tool_registry_registers_and_dispatches() -> None:
    import asyncio

    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.tool_contract import BaseTool, ToolSchema

    class EchoTool(BaseTool):
        parallel_safe = True

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name="Echo",
                description="Echo input back",
                parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            )

        async def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=call.id, content=str(call.arguments.get("msg", ""))
            )

    reg = ToolRegistry()
    reg.register(EchoTool())
    assert "Echo" in reg.names()

    result = asyncio.run(reg.dispatch(ToolCall(id="1", name="Echo", arguments={"msg": "hi"})))
    assert result.content == "hi"
    assert not result.is_error


def test_session_db_create_and_search() -> None:
    import asyncio  # noqa: F401 (kept for parity with test patterns)

    from opencomputer.agent.state import SessionDB
    from plugin_sdk.core import Message

    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        db.create_session("s1", platform="cli")
        db.append_message("s1", Message(role="user", content="hello nginx world"))
        db.append_message("s1", Message(role="assistant", content="goodbye"))
        hits = db.search("nginx")
        assert len(hits) == 1
        assert "nginx" in hits[0]["snippet"].lower()


def test_prompt_builder_renders() -> None:
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    out = pb.build(skills=[])
    assert "OpenComputer" in out
    assert "Current working directory" in out


def test_memory_manager_writes_skill() -> None:
    from opencomputer.agent.memory import MemoryManager

    with tempfile.TemporaryDirectory() as tmp:
        # Pass explicit empty bundled_skills_paths to isolate from the bundled skills
        mm = MemoryManager(
            declarative_path=Path(tmp) / "MEMORY.md",
            skills_path=Path(tmp) / "skills",
            bundled_skills_paths=[],
        )
        mm.write_skill(
            skill_id="debug-nginx",
            description="Use when nginx 502s",
            body="Steps:\n1. check logs\n",
        )
        found = mm.list_skills()
        assert len(found) == 1
        assert found[0].id == "debug-nginx"
        assert "nginx" in found[0].description
