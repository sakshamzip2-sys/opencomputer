"""Tests for the bundled FastMCP authoring skill (G.34 / Tier 4).

The skill is doc-heavy + ships a runnable mini-example. We don't
spawn a real MCP server here (that would couple tests to the `mcp`
package's transport implementation); we verify:

1. The skill folder layout matches what the skill discoverer expects.
2. SKILL.md frontmatter is well-formed.
3. The example python file is syntactically valid.
4. The reference docs cover both transports + lifecycle.
"""

from __future__ import annotations

from pathlib import Path

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "opencomputer"
    / "skills"
    / "fastmcp-authoring"
)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestLayout:
    def test_skill_dir_exists(self) -> None:
        assert _SKILL_DIR.is_dir()

    def test_skill_md_exists(self) -> None:
        assert (_SKILL_DIR / "SKILL.md").is_file()

    def test_examples_dir_with_minimal_server(self) -> None:
        assert (_SKILL_DIR / "examples" / "minimal_server.py").is_file()

    def test_references_cover_transports_and_lifecycle(self) -> None:
        assert (_SKILL_DIR / "references" / "transports.md").is_file()
        assert (_SKILL_DIR / "references" / "lifecycle.md").is_file()


# ---------------------------------------------------------------------------
# SKILL.md frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_has_name_description_version(self) -> None:
        body = (_SKILL_DIR / "SKILL.md").read_text()
        assert body.startswith("---\n"), "SKILL.md must lead with frontmatter"
        # Find the closing fence.
        end = body.index("\n---\n", 4)
        front = body[4:end]
        assert "name: " in front
        assert "description: " in front
        assert "version: " in front

    def test_description_mentions_trigger_phrases(self) -> None:
        # The skill discoverer matches user requests against the
        # description; if the trigger phrases drift away from the
        # likely user vocabulary, the skill stops surfacing.
        body = (_SKILL_DIR / "SKILL.md").read_text()
        # Expected trigger phrases in the description block.
        for phrase in (
            "MCP server",
            "FastMCP",
            "Python",
        ):
            assert phrase in body[: body.index("\n---\n", 4)], (
                f"description should mention {phrase!r}"
            )

    def test_skill_md_links_to_scaffolder(self) -> None:
        # G.30 scaffolder + G.34 skill are designed to compose; the
        # skill should point at the CLI as the fastest path.
        body = (_SKILL_DIR / "SKILL.md").read_text()
        assert "opencomputer mcp scaffold" in body


# ---------------------------------------------------------------------------
# Example compiles
# ---------------------------------------------------------------------------


class TestExampleCompiles:
    def test_minimal_server_is_valid_python(self) -> None:
        path = _SKILL_DIR / "examples" / "minimal_server.py"
        body = path.read_text()
        # ``compile`` raises SyntaxError if the example is broken.
        compile(body, str(path), "exec")

    def test_minimal_server_uses_fastmcp(self) -> None:
        body = (_SKILL_DIR / "examples" / "minimal_server.py").read_text()
        assert "from mcp.server.fastmcp import FastMCP" in body
        assert "@server.tool()" in body
        assert 'def add(a: int, b: int) -> int' in body

    def test_minimal_server_has_main_entry(self) -> None:
        body = (_SKILL_DIR / "examples" / "minimal_server.py").read_text()
        assert 'if __name__ == "__main__":' in body
        assert "main()" in body


# ---------------------------------------------------------------------------
# Reference content
# ---------------------------------------------------------------------------


class TestReferences:
    def test_transports_doc_covers_three_kinds(self) -> None:
        body = (_SKILL_DIR / "references" / "transports.md").read_text()
        assert "stdio" in body
        assert "http" in body
        assert "sse" in body
        # Decision matrix should warn against SSE for new servers.
        assert "legacy" in body.lower() or "deprecated" in body.lower()

    def test_lifecycle_doc_covers_four_phases(self) -> None:
        body = (_SKILL_DIR / "references" / "lifecycle.md").read_text()
        # Initialize / list / call / shutdown are the four phases.
        for phase in ("Initialize", "List tools", "Call tool", "Shutdown"):
            assert phase in body, f"lifecycle doc missing phase: {phase}"
