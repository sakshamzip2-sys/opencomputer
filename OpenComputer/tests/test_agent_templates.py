"""
III.5 — subagent templates as ``.md`` files.

Tests the three-tier filesystem discovery (bundled → plugin → profile),
the parser's error handling, and DelegateTool integration:
- ``agent`` parameter looks up a named template
- template ``tools`` become the III.1 allowlist
- template ``system_prompt`` flows through as ``system_prompt_override``
- an explicit ``allowed_tools`` argument beats the template's tools
- unknown agent names return a clean error listing available templates

Mirrors Claude Code's subagent-definition layout
(``sources/claude-code/plugins/<plugin>/agents/*.md``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.agent_templates import (
    AgentTemplate,
    discover_agents,
    parse_agent_template,
)
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.core import ToolCall

# ─── parse_agent_template ──────────────────────────────────────────────


def test_parse_agent_template_minimal(tmp_path: Path) -> None:
    """Frontmatter with only name + description; body becomes system_prompt."""
    p = tmp_path / "minimal.md"
    p.write_text(
        "---\n"
        "name: minimal\n"
        "description: A minimal template\n"
        "---\n\n"
        "This is the system prompt.\n",
        encoding="utf-8",
    )
    tpl = parse_agent_template(p, source="bundled")
    assert tpl.name == "minimal"
    assert tpl.description == "A minimal template"
    assert tpl.tools == ()
    assert tpl.model is None
    assert tpl.system_prompt == "This is the system prompt."
    assert tpl.source_path == p
    assert tpl.source == "bundled"


def test_parse_agent_template_full(tmp_path: Path) -> None:
    """All frontmatter fields populated — tools as comma-sep string + model override."""
    p = tmp_path / "full.md"
    p.write_text(
        "---\n"
        "name: reviewer\n"
        "description: Reviews code carefully\n"
        "tools: Read, Grep, Bash\n"
        "model: sonnet\n"
        "---\n\n"
        "You are a careful reviewer.\n\nBe thorough.",
        encoding="utf-8",
    )
    tpl = parse_agent_template(p, source="plugin")
    assert tpl.name == "reviewer"
    assert tpl.description == "Reviews code carefully"
    assert tpl.tools == ("Read", "Grep", "Bash")
    assert tpl.model == "sonnet"
    assert "careful reviewer" in tpl.system_prompt
    assert "Be thorough" in tpl.system_prompt
    assert tpl.source == "plugin"


def test_parse_agent_template_tools_as_yaml_list(tmp_path: Path) -> None:
    """``tools:`` accepted as a YAML list too, not just comma-sep."""
    p = tmp_path / "yaml_list.md"
    p.write_text(
        "---\n"
        "name: yamltest\n"
        "description: YAML list form\n"
        "tools:\n"
        "  - Read\n"
        "  - Grep\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    tpl = parse_agent_template(p, source="bundled")
    assert tpl.tools == ("Read", "Grep")


def test_parse_agent_template_missing_frontmatter(tmp_path: Path) -> None:
    """A file without the opening ``---`` fence raises ValueError."""
    p = tmp_path / "no_fm.md"
    p.write_text("just a body, no frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing frontmatter"):
        parse_agent_template(p, source="bundled")


def test_parse_agent_template_missing_name(tmp_path: Path) -> None:
    """Frontmatter missing ``name`` raises ValueError."""
    p = tmp_path / "no_name.md"
    p.write_text(
        "---\n"
        "description: No name field\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'name' is required"):
        parse_agent_template(p, source="bundled")


def test_parse_agent_template_missing_description(tmp_path: Path) -> None:
    """Frontmatter missing ``description`` raises ValueError."""
    p = tmp_path / "no_desc.md"
    p.write_text(
        "---\n"
        "name: nodesc\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'description' is required"):
        parse_agent_template(p, source="bundled")


def test_parse_agent_template_invalid_source(tmp_path: Path) -> None:
    """``source`` must be one of the four known tiers."""
    p = tmp_path / "ok.md"
    p.write_text(
        "---\nname: ok\ndescription: ok\n---\n\nBody.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="source must be one of"):
        parse_agent_template(p, source="bogus")


# ─── discover_agents ───────────────────────────────────────────────────


def _write_tpl(
    dir_path: Path, name: str, body: str = "body", tools: str = ""
) -> Path:
    """Helper: write a minimal template to ``dir_path / f'{name}.md'``."""
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / f"{name}.md"
    tools_line = f"tools: {tools}\n" if tools else ""
    p.write_text(
        f"---\nname: {name}\ndescription: desc for {name}\n{tools_line}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def test_discover_bundled(tmp_path: Path) -> None:
    """Files under ``bundled_root/*.md`` parse and land in the result."""
    bundled = tmp_path / "bundled"
    _write_tpl(bundled, "a", body="a-body")
    _write_tpl(bundled, "b", body="b-body")

    templates = discover_agents(
        profile_root=tmp_path / "no_profile",  # absent
        bundled_root=bundled,
    )
    assert set(templates.keys()) == {"a", "b"}
    assert templates["a"].source == "bundled"
    assert "a-body" in templates["a"].system_prompt


def test_discover_profile_overrides_bundled(tmp_path: Path) -> None:
    """Same-name file in profile tier beats bundled tier."""
    bundled = tmp_path / "bundled"
    _write_tpl(bundled, "shared", body="bundled-version")
    profile_root = tmp_path / "profile"
    _write_tpl(profile_root / "home" / "agents", "shared", body="profile-version")

    templates = discover_agents(profile_root=profile_root, bundled_root=bundled)
    assert templates["shared"].source == "profile"
    assert "profile-version" in templates["shared"].system_prompt


def test_discover_plugin_between_bundled_and_profile(tmp_path: Path) -> None:
    """Precedence order: bundled < plugin < profile."""
    bundled = tmp_path / "bundled"
    _write_tpl(bundled, "shared", body="bundled-version")
    _write_tpl(bundled, "bundled-only", body="bundled-unique")
    plugin_root = tmp_path / "plugin1"
    _write_tpl(plugin_root / "agents", "shared", body="plugin-version")
    _write_tpl(plugin_root / "agents", "plugin-only", body="plugin-unique")
    profile_root = tmp_path / "profile"
    _write_tpl(profile_root / "home" / "agents", "shared", body="profile-version")

    templates = discover_agents(
        bundled_root=bundled,
        plugin_roots=[plugin_root],
        profile_root=profile_root,
    )
    # Union of all names
    assert set(templates.keys()) == {"shared", "bundled-only", "plugin-only"}
    # Profile wins for 'shared'
    assert templates["shared"].source == "profile"
    assert "profile-version" in templates["shared"].system_prompt
    # Tier-specific entries keep their source
    assert templates["bundled-only"].source == "bundled"
    assert templates["plugin-only"].source == "plugin"


def test_discover_malformed_file_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed ``.md`` file is logged + skipped; good siblings survive."""
    bundled = tmp_path / "bundled"
    _write_tpl(bundled, "good", body="fine")
    # Malformed: no frontmatter at all.
    bad = bundled / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("no frontmatter here\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.agent_templates"):
        templates = discover_agents(
            profile_root=tmp_path / "no_profile",
            bundled_root=bundled,
        )
    assert set(templates.keys()) == {"good"}
    assert any("bad.md" in rec.getMessage() for rec in caplog.records)


def test_discover_empty_dir(tmp_path: Path) -> None:
    """No templates anywhere returns an empty dict — no error."""
    templates = discover_agents(
        profile_root=tmp_path / "no_profile",
        bundled_root=tmp_path / "no_bundled",
    )
    assert templates == {}


# ─── DelegateTool integration ──────────────────────────────────────────


class _FakeLoop:
    """Captures kwargs passed to ``run_conversation`` for assertions."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured
        self.allowed_tools: frozenset[str] | None = None
        # Minimal config shape — DelegateTool checks ``dataclasses.is_dataclass``
        # before touching this, so a fake with no config still works.
        self.config = None

    async def run_conversation(
        self,
        user_message: str,
        runtime: Any = None,
        system_prompt_override: str | None = None,
        **kw: Any,
    ) -> Any:
        self._captured["allowed_tools"] = self.allowed_tools
        self._captured["system_prompt_override"] = system_prompt_override
        self._captured["user_message"] = user_message

        class _R:
            class final_message:
                content = "ok"

            session_id = "sub"

        return _R()


def _register_tpl(name: str, tools: tuple[str, ...] = (), prompt: str = "SP") -> AgentTemplate:
    """Helper: build an AgentTemplate and push it onto DelegateTool."""
    tpl = AgentTemplate(
        name=name,
        description=f"desc-{name}",
        tools=tools,
        model=None,
        system_prompt=prompt,
        source_path=Path(f"/virtual/{name}.md"),
        source="bundled",
    )
    DelegateTool.set_templates({name: tpl})
    return tpl


def test_delegate_with_agent_name() -> None:
    """`agent='code-reviewer'` → template's system_prompt + tools applied."""
    _register_tpl(
        "code-reviewer",
        tools=("Read", "Grep"),
        prompt="You are a reviewer.",
    )
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "review this", "agent": "code-reviewer"},
            )
        )
    )
    assert not result.is_error, result.content
    assert captured["allowed_tools"] == frozenset({"Read", "Grep"})
    assert captured["system_prompt_override"] == "You are a reviewer."
    assert captured["user_message"] == "review this"


def test_delegate_with_unknown_agent() -> None:
    """Unknown agent name → error result listing registered names."""
    _register_tpl("known", tools=("Read",))
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "go", "agent": "nonexistent"},
            )
        )
    )
    assert result.is_error
    assert "unknown agent" in result.content.lower()
    assert "known" in result.content  # available names listed
    # Child loop was NOT invoked.
    assert captured == {}


def test_delegate_unknown_agent_with_empty_registry() -> None:
    """Unknown agent + empty registry → clean error message."""
    DelegateTool.set_templates({})
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "go", "agent": "anything"},
            )
        )
    )
    assert result.is_error
    assert "no templates registered" in result.content


def test_delegate_allowed_tools_beats_template() -> None:
    """Explicit ``allowed_tools`` argument beats the template's tools list."""
    _register_tpl("code-reviewer", tools=("Read", "Grep"), prompt="SP")
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={
                    "task": "go",
                    "agent": "code-reviewer",
                    "allowed_tools": ["Bash"],
                },
            )
        )
    )
    assert not result.is_error, result.content
    # Explicit argument wins: Bash only, NOT the template's (Read, Grep).
    assert captured["allowed_tools"] == frozenset({"Bash"})
    # But the system prompt still comes from the template.
    assert captured["system_prompt_override"] == "SP"


def test_delegate_without_agent_preserves_existing_behavior() -> None:
    """No ``agent`` arg → system_prompt_override=None (template-free path)."""
    _register_tpl("code-reviewer", tools=("Read",))
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "go"},
            )
        )
    )
    assert not result.is_error
    assert captured["allowed_tools"] is None
    assert captured["system_prompt_override"] is None


def test_delegate_template_with_empty_tools_inherits() -> None:
    """A template with ``tools=()`` does NOT set an allowlist — inherit parent."""
    _register_tpl("inherit-tools", tools=(), prompt="SP")
    captured: dict[str, Any] = {}
    DelegateTool.set_factory(lambda: _FakeLoop(captured))

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "go", "agent": "inherit-tools"},
            )
        )
    )
    assert not result.is_error
    # Empty template tools → no allowlist change (None, the parent-inherit
    # semantic).
    assert captured["allowed_tools"] is None
    assert captured["system_prompt_override"] == "SP"
