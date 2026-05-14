"""Tests for the hookify auto-loading rule engine.

Covers the rule engine evaluation, the rule loader (with synthetic
``.md`` files in tmp_path), and the hook handler contract end-to-end.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "hookify"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rule_mod = _load("hookify_rule_test_only", PLUGIN_DIR / "rule.py")
sys.modules["rule"] = rule_mod
Condition = rule_mod.Condition
Rule = rule_mod.Rule

rule_engine_mod = _load(
    "hookify_rule_engine_test_only", PLUGIN_DIR / "rule_engine.py"
)
sys.modules["rule_engine"] = rule_engine_mod
RuleEngine = rule_engine_mod.RuleEngine

rule_loader_mod = _load(
    "hookify_rule_loader_test_only", PLUGIN_DIR / "rule_loader.py"
)
sys.modules["rule_loader"] = rule_loader_mod
load_rules = rule_loader_mod.load_rules

plugin_mod = _load("hookify_plugin_test_only", PLUGIN_DIR / "plugin.py")
_make_handler = plugin_mod._make_handler


# ─── rule construction ─────────────────────────────────────────────────────


def test_rule_from_simple_pattern_bash():
    r = Rule.from_frontmatter(
        {"name": "x", "enabled": True, "event": "bash", "pattern": "rm -rf"},
        "danger",
    )
    assert r.name == "x"
    assert r.event == "bash"
    assert len(r.conditions) == 1
    assert r.conditions[0].field == "command"
    assert r.conditions[0].operator == "regex_match"
    assert r.conditions[0].pattern == "rm -rf"


def test_rule_from_explicit_conditions():
    r = Rule.from_frontmatter(
        {
            "name": "x",
            "enabled": True,
            "event": "file",
            "conditions": [
                {
                    "field": "file_path",
                    "operator": "ends_with",
                    "pattern": ".env",
                }
            ],
        },
        "blocked",
    )
    assert len(r.conditions) == 1
    assert r.conditions[0].operator == "ends_with"
    assert r.conditions[0].pattern == ".env"


# ─── engine evaluation ────────────────────────────────────────────────────


def test_engine_blocks_on_match():
    rule = Rule(
        name="block-rm",
        enabled=True,
        event="bash",
        action="block",
        conditions=(Condition("command", "regex_match", r"rm\s+-rf"),),
        message="dangerous rm",
    )
    decision = RuleEngine().evaluate(
        [rule],
        tool_name="Bash",
        tool_input={"command": "rm -rf /"},
    )
    assert decision.decision == "block"
    assert "block-rm" in decision.reason
    assert "dangerous rm" in decision.reason


def test_engine_warns_then_passes():
    rule = Rule(
        name="warn-eval",
        enabled=True,
        event="file",
        action="warn",
        conditions=(Condition("new_text", "contains", "eval("),),
        message="watch eval",
    )
    decision = RuleEngine().evaluate(
        [rule],
        tool_name="Edit",
        tool_input={"new_string": "x = eval(payload)"},
    )
    assert decision.decision == "pass"
    assert "warn-eval" in decision.reason


def test_engine_no_match_passes_silently():
    rule = Rule(
        name="never-fires",
        enabled=True,
        event="bash",
        action="warn",
        conditions=(Condition("command", "regex_match", r"^never"),),
        message="x",
    )
    decision = RuleEngine().evaluate(
        [rule],
        tool_name="Bash",
        tool_input={"command": "ls -la"},
    )
    assert decision.decision == "pass"
    assert decision.reason == ""


def test_engine_blocks_win_over_warns():
    block = Rule(
        name="b",
        enabled=True,
        event="bash",
        action="block",
        conditions=(Condition("command", "contains", "rm"),),
        message="block msg",
    )
    warn = Rule(
        name="w",
        enabled=True,
        event="bash",
        action="warn",
        conditions=(Condition("command", "contains", "rm"),),
        message="warn msg",
    )
    decision = RuleEngine().evaluate(
        [warn, block],
        tool_name="Bash",
        tool_input={"command": "rm /tmp/x"},
    )
    assert decision.decision == "block"
    # Block message present, warn message NOT (blocking rules win exclusively)
    assert "block msg" in decision.reason
    assert "warn msg" not in decision.reason


def test_engine_tool_matcher_filters():
    rule = Rule(
        name="bash-only",
        enabled=True,
        event="all",
        action="warn",
        tool_matcher="Bash",
        conditions=(Condition("command", "contains", "x"),),
        message="m",
    )
    # Wrong tool: should not match
    decision = RuleEngine().evaluate(
        [rule],
        tool_name="Read",
        tool_input={"command": "x"},
    )
    assert decision.decision == "pass"


def test_engine_multi_edit_combines_new_strings():
    rule = Rule(
        name="r",
        enabled=True,
        event="file",
        action="warn",
        conditions=(Condition("new_text", "contains", "innerHTML"),),
        message="m",
    )
    decision = RuleEngine().evaluate(
        [rule],
        tool_name="MultiEdit",
        tool_input={
            "edits": [
                {"new_string": "const a = 1"},
                {"new_string": "el.innerHTML = userText"},
            ]
        },
    )
    assert decision.decision == "pass"
    assert "innerHTML" not in decision.reason  # message is "m", not the value
    assert decision.reason  # warn fired


# ─── loader (synthetic .md files) ─────────────────────────────────────────


def test_loader_reads_profile_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    hookify_dir = tmp_path / "hookify"
    hookify_dir.mkdir()
    (hookify_dir / "warn-rm.md").write_text(
        "---\n"
        "name: warn-rm\n"
        "enabled: true\n"
        "event: bash\n"
        "pattern: 'rm -rf'\n"
        "---\n"
        "danger\n"
    )
    rules = load_rules(event="bash")
    assert len(rules) == 1
    assert rules[0].name == "warn-rm"


def test_loader_skips_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    hookify_dir = tmp_path / "hookify"
    hookify_dir.mkdir()
    (hookify_dir / "off.md").write_text(
        "---\n"
        "name: off\n"
        "enabled: false\n"
        "event: bash\n"
        "pattern: 'rm'\n"
        "---\n"
        "x\n"
    )
    rules = load_rules()
    assert rules == []


def test_loader_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    # Directory doesn't exist — must not crash, just return [].
    assert load_rules() == []


def test_loader_event_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    hookify_dir = tmp_path / "hookify"
    hookify_dir.mkdir()
    (hookify_dir / "bash.md").write_text(
        "---\nname: a\nenabled: true\nevent: bash\npattern: x\n---\n\n"
    )
    (hookify_dir / "file.md").write_text(
        "---\nname: b\nenabled: true\nevent: file\npattern: y\n---\n\n"
    )
    bash_rules = load_rules(event="bash")
    file_rules = load_rules(event="file")
    assert {r.name for r in bash_rules} == {"a"}
    assert {r.name for r in file_rules} == {"b"}


# ─── handler contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_blocks_via_loaded_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    hookify_dir = tmp_path / "hookify"
    hookify_dir.mkdir()
    (hookify_dir / "block.md").write_text(
        "---\n"
        "name: block-rm\n"
        "enabled: true\n"
        "event: bash\n"
        "action: block\n"
        "pattern: 'rm -rf'\n"
        "---\n"
        "no.\n"
    )
    handler = _make_handler("bash")
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="x", name="Bash", arguments={"command": "rm -rf /"}),
    )
    decision = await handler(ctx)
    assert decision.decision == "block"
    assert "block-rm" in decision.reason


@pytest.mark.asyncio
async def test_handler_passes_when_no_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    handler = _make_handler("bash")
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="x", name="Bash", arguments={"command": "ls"}),
    )
    decision = await handler(ctx)
    assert decision.decision == "pass"
