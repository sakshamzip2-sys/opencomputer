"""M7.1 — path-glob rules loader + matcher + injection provider.

Pins the contract added 2026-05-09:

* :class:`Rule` dataclass + :func:`load_rules` + :func:`merged_rules`
* :func:`active_rules_for` matching with priority ordering
* :func:`format_rules_block` system-prompt addendum shape
* :func:`extract_paths_from_tool_call` for each path-touching tool
* :class:`PathGlobRulesProvider` injection-engine integration
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.path_rules_injection import PathGlobRulesProvider
from opencomputer.agent.rules_loader import (
    MAX_RULE_BODY_BYTES,
    PATH_TOUCHING_TOOLS,
    Rule,
    active_rules_for,
    extract_paths_from_tool_call,
    format_rules_block,
    load_rules,
    merged_rules,
)
from plugin_sdk.injection import InjectionContext

# ─── load_rules: file → Rule list ─────────────────────────────────────────


class TestLoadRules:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_rules(tmp_path / "absent") == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        assert load_rules(d) == []

    def test_well_formed_rule(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "python.md").write_text(
            "---\npaths:\n  - '**/*.py'\npriority: 50\n---\nUse type hints."
        )
        rules = load_rules(d)
        assert len(rules) == 1
        r = rules[0]
        assert r.name == "python"
        assert r.paths == ("**/*.py",)
        assert r.priority == 50
        assert "type hints" in r.body

    def test_string_paths_field_coerces_to_tuple(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "single.md").write_text(
            "---\npaths: '**/*.tsx'\n---\nUse React.FC."
        )
        rules = load_rules(d)
        assert rules[0].paths == ("**/*.tsx",)

    def test_missing_paths_loads_with_empty_globs(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "no-paths.md").write_text("---\npriority: 10\n---\nGuide.")
        rules = load_rules(d)
        assert rules[0].paths == ()  # warned about, but loaded

    def test_invalid_priority_defaults_to_100(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "bad-prio.md").write_text(
            "---\npaths: ['**/*.py']\npriority: 'high'\n---\nGuide."
        )
        rules = load_rules(d)
        assert rules[0].priority == 100

    def test_malformed_yaml_skips_rule(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "ok.md").write_text("---\npaths: ['**/*.py']\n---\nA")
        (d / "bad.md").write_text("---\npaths: [unclosed\n---\nB")
        rules = load_rules(d)
        names = {r.name for r in rules}
        assert "ok" in names
        assert "bad" not in names

    def test_body_truncated_at_cap(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        big_body = "x" * (MAX_RULE_BODY_BYTES + 100)
        (d / "huge.md").write_text(
            f"---\npaths: ['*.py']\n---\n{big_body}"
        )
        rules = load_rules(d)
        assert "rule body truncated" in rules[0].body
        assert len(rules[0].body.encode("utf-8")) <= MAX_RULE_BODY_BYTES + 200

    def test_sort_order_priority_then_name(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "z.md").write_text("---\npaths: ['*']\npriority: 10\n---\nZ")
        (d / "a.md").write_text("---\npaths: ['*']\npriority: 50\n---\nA")
        (d / "b.md").write_text("---\npaths: ['*']\npriority: 10\n---\nB")
        rules = load_rules(d)
        # priority asc, then name asc
        assert [r.name for r in rules] == ["b", "z", "a"]


# ─── merged_rules: workspace shadows profile ──────────────────────────────


class TestMergedRules:
    def test_workspace_shadows_profile(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        prof = tmp_path / "prof"
        ws.mkdir()
        prof.mkdir()
        (prof / "shared.md").write_text(
            "---\npaths: ['**/*.py']\n---\nProfile version."
        )
        (ws / "shared.md").write_text(
            "---\npaths: ['**/*.py']\n---\nWorkspace version."
        )
        rules = merged_rules(ws, prof)
        assert len(rules) == 1
        assert "Workspace version" in rules[0].body

    def test_workspace_unique_and_profile_unique_both_kept(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        prof = tmp_path / "prof"
        ws.mkdir()
        prof.mkdir()
        (ws / "frontend.md").write_text("---\npaths: ['**/*.tsx']\n---\nW")
        (prof / "global.md").write_text("---\npaths: ['*']\n---\nP")
        rules = merged_rules(ws, prof)
        names = {r.name for r in rules}
        assert names == {"frontend", "global"}


# ─── active_rules_for: glob matching ──────────────────────────────────────


class TestActiveRulesFor:
    def _rule(self, name: str, paths: tuple[str, ...], pri: int = 100) -> Rule:
        return Rule(name=name, paths=paths, priority=pri, body="body")

    def test_simple_glob_matches(self) -> None:
        rules = [self._rule("py", ("*.py",))]
        assert active_rules_for(rules, ["foo.py"]) == rules
        assert active_rules_for(rules, ["foo.txt"]) == []

    def test_no_paths_returns_empty(self) -> None:
        rules = [self._rule("any", ("*",))]
        assert active_rules_for(rules, []) == []

    def test_no_rules_returns_empty(self) -> None:
        assert active_rules_for([], ["foo.py"]) == []

    def test_multiple_globs_one_matches(self) -> None:
        rules = [self._rule("multi", ("*.py", "*.pyi"))]
        assert active_rules_for(rules, ["foo.pyi"]) == rules

    def test_multiple_paths_any_match(self) -> None:
        rules = [self._rule("py", ("*.py",))]
        assert active_rules_for(rules, ["a.txt", "b.py"]) == rules

    def test_priority_order_preserved_after_load(self, tmp_path: Path) -> None:
        d = tmp_path / "rules"
        d.mkdir()
        (d / "low.md").write_text("---\npaths: ['*']\npriority: 10\n---\nA")
        (d / "high.md").write_text("---\npaths: ['*']\npriority: 90\n---\nB")
        loaded = load_rules(d)
        matched = active_rules_for(loaded, ["any.txt"])
        # priority 10 before 90
        assert [r.name for r in matched] == ["low", "high"]


# ─── format_rules_block ──────────────────────────────────────────────────


class TestFormatRulesBlock:
    def test_empty_returns_empty_string(self) -> None:
        assert format_rules_block([]) == ""

    def test_single_rule_block(self) -> None:
        block = format_rules_block(
            [Rule(name="py", paths=("*.py",), priority=10, body="Use types.")]
        )
        assert block.startswith("[Active Rules]")
        assert "### py" in block
        assert "(*.py)" in block
        assert "Use types." in block

    def test_multiple_rules_separated(self) -> None:
        block = format_rules_block(
            [
                Rule(name="a", paths=("*",), priority=10, body="A"),
                Rule(name="b", paths=("*",), priority=20, body="B"),
            ]
        )
        assert "### a" in block
        assert "### b" in block


# ─── extract_paths_from_tool_call ────────────────────────────────────────


class TestExtractPaths:
    def test_non_path_touching_tool_returns_empty(self) -> None:
        assert extract_paths_from_tool_call("Bash", {"command": "ls"}) == []

    def test_read_path(self) -> None:
        assert extract_paths_from_tool_call("Read", {"file_path": "foo.py"}) == [
            "foo.py"
        ]

    def test_edit_path(self) -> None:
        assert extract_paths_from_tool_call("Edit", {"file_path": "x.py"}) == [
            "x.py"
        ]

    def test_glob_pattern_only(self) -> None:
        # Glob's `pattern` is NOT a target file
        assert extract_paths_from_tool_call("Glob", {"pattern": "**/*.py"}) == []

    def test_multiedit_nested_edits(self) -> None:
        out = extract_paths_from_tool_call(
            "MultiEdit",
            {
                "edits": [
                    {"file_path": "a.py", "old": "x", "new": "y"},
                    {"file_path": "b.py", "old": "x", "new": "y"},
                ]
            },
        )
        assert out == ["a.py", "b.py"]

    def test_paths_list(self) -> None:
        out = extract_paths_from_tool_call("Read", {"paths": ["a.py", "b.py"]})
        assert out == ["a.py", "b.py"]

    def test_path_touching_tools_constant(self) -> None:
        # Pin so adding a new path-touching tool stays intentional
        expected = frozenset(
            {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit"}
        )
        assert PATH_TOUCHING_TOOLS == expected  # noqa: SIM300


# ─── PathGlobRulesProvider — injection integration ───────────────────────


@dataclass
class _FakeToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class _FakeMessage:
    role: str
    content: str = ""
    tool_calls: list[_FakeToolCall] = field(default_factory=list)


def _ctx(messages: list[_FakeMessage]) -> InjectionContext:
    # InjectionContext expects tuple[Message, ...]; the provider only
    # reads role + tool_calls + .arguments so duck-typing is safe.
    from plugin_sdk.runtime_context import RuntimeContext

    return InjectionContext(
        messages=tuple(messages),
        runtime=RuntimeContext(),
        session_id="test",
        turn_index=1,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestPathGlobRulesProvider:
    def test_no_rules_returns_none(self) -> None:
        provider = PathGlobRulesProvider(rules=[])
        ctx = _ctx(
            [
                _FakeMessage(
                    role="assistant",
                    tool_calls=[
                        _FakeToolCall(name="Read", arguments={"file_path": "foo.py"})
                    ],
                ),
            ]
        )
        assert _run(provider.collect(ctx)) is None

    def test_no_assistant_tool_calls_returns_none(self) -> None:
        provider = PathGlobRulesProvider(
            rules=[Rule(name="py", paths=("*.py",), priority=50, body="x")]
        )
        ctx = _ctx([_FakeMessage(role="user", content="hi")])
        assert _run(provider.collect(ctx)) is None

    def test_path_touched_returns_block(self) -> None:
        rule = Rule(name="py", paths=("*.py",), priority=50, body="Use types.")
        provider = PathGlobRulesProvider(rules=[rule])
        ctx = _ctx(
            [
                _FakeMessage(
                    role="assistant",
                    tool_calls=[
                        _FakeToolCall(name="Edit", arguments={"file_path": "src.py"})
                    ],
                ),
            ]
        )
        block = _run(provider.collect(ctx))
        assert block is not None
        assert "[Active Rules]" in block
        assert "### py" in block
        assert "Use types." in block

    def test_only_last_assistant_turn_considered(self) -> None:
        """Walking backwards stops at the first assistant turn with paths."""
        rule = Rule(name="py", paths=("*.py",), priority=50, body="P")
        provider = PathGlobRulesProvider(rules=[rule])
        ctx = _ctx(
            [
                _FakeMessage(
                    role="assistant",
                    tool_calls=[
                        _FakeToolCall(name="Read", arguments={"file_path": "OLD.py"})
                    ],
                ),
                _FakeMessage(role="user", content="continue"),
                _FakeMessage(
                    role="assistant",
                    tool_calls=[
                        _FakeToolCall(name="Read", arguments={"file_path": "NEW.py"})
                    ],
                ),
            ]
        )
        # Provider sees the LAST assistant turn (NEW.py); rule matches; block emitted
        block = _run(provider.collect(ctx))
        assert block is not None

    def test_provider_id_is_path_glob_rules(self) -> None:
        provider = PathGlobRulesProvider(rules=[])
        assert provider.provider_id == "path_glob_rules"

    def test_priority_default(self) -> None:
        provider = PathGlobRulesProvider(rules=[])
        assert provider.priority == 60


# ─── load_rules_for_active_profile ────────────────────────────────────────


class TestLoadRulesForActiveProfile:
    def test_picks_up_workspace_and_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opencomputer.agent import path_rules_injection

        # Workspace = a fake cwd
        ws = tmp_path / "workspace"
        (ws / ".opencomputer" / "rules").mkdir(parents=True)
        (ws / ".opencomputer" / "rules" / "py.md").write_text(
            "---\npaths: ['**/*.py']\n---\nWorkspace py rule."
        )
        # Profile = a fake home
        prof = tmp_path / "profile"
        (prof / "rules").mkdir(parents=True)
        (prof / "rules" / "global.md").write_text(
            "---\npaths: ['*']\n---\nGlobal."
        )

        monkeypatch.chdir(ws)
        monkeypatch.setattr(
            "opencomputer.agent.config._home", lambda: prof
        )
        rules = path_rules_injection.load_rules_for_active_profile()
        names = {r.name for r in rules}
        assert names == {"py", "global"}
