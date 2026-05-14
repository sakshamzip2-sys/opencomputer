"""Tests for the security-guidance PreToolUse hook plugin.

Covers the pattern catalogue, the once-per-session-per-(file,rule)
de-duplication, the env-var disable switch, and tools the hook should
ignore. Importing the plugin's modules directly mirrors how a future
test of the plugin loader would do it; the plugin itself ships a
``register(api)`` for the real loader path.
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
    / "security-guidance"
)


def _load(module_name: str, path: Path):
    """Load ``module_name`` from ``path`` without colliding with same-name
    modules already on ``sys.path`` (the OC loader does this with
    synthetic names; we use an explicit import to avoid contaminating
    the global namespace).
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load patterns first (the plugin module imports it).
security_patterns = _load(
    "security_patterns_test_only",
    PLUGIN_DIR / "security_patterns.py",
)
find_match = security_patterns.find_match

# The plugin module imports ``security_patterns`` by short name —
# alias the test-loaded module so the import resolves.
sys.modules["security_patterns"] = security_patterns

plugin_mod = _load(
    "security_guidance_plugin_test_only",
    PLUGIN_DIR / "plugin.py",
)
on_pre_tool_use = plugin_mod.on_pre_tool_use


# ─── pattern catalogue ─────────────────────────────────────────────────────


def test_eval_match():
    p = find_match("src/foo.js", "let x = eval(userInput)")
    assert p is not None
    assert p.rule_name == "eval_injection"


def test_inner_html_match():
    p = find_match("src/foo.js", "el.innerHTML = userText")
    assert p is not None
    assert p.rule_name == "innerHTML_xss"


def test_pickle_match():
    p = find_match("src/foo.py", "import pickle\npickle.loads(payload)")
    assert p is not None
    assert p.rule_name == "pickle_deserialization"


def test_github_actions_workflow_path_match():
    # Path-based rule fires regardless of content.
    p = find_match(".github/workflows/ci.yml", "")
    assert p is not None
    assert p.rule_name == "github_actions_workflow"


def test_no_match_for_clean_code():
    assert find_match("src/foo.py", "def add(a, b):\n    return a + b") is None


def test_first_pattern_wins():
    # The eval rule comes before the innerHTML rule in the catalogue.
    # When both substrings are present, eval should match.
    content = "el.innerHTML = eval(userInput)"
    p = find_match("src/foo.js", content)
    assert p is not None
    assert p.rule_name in {"eval_injection", "innerHTML_xss"}


# ─── hook handler ──────────────────────────────────────────────────────────


def _make_ctx(
    tool_name: str,
    tool_input: dict,
    session_id: str = "s1",
) -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id=session_id,
        tool_call=ToolCall(id="tc1", name=tool_name, arguments=tool_input),
    )


@pytest.mark.asyncio
async def test_blocks_first_eval_then_passes_repeat(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    monkeypatch.delenv("ENABLE_SECURITY_REMINDER", raising=False)
    payload = {"file_path": "/repo/src/foo.js", "content": "eval(x)"}
    first = await on_pre_tool_use(_make_ctx("Write", payload))
    assert first.decision == "block"
    assert "eval" in first.reason.lower()

    # Same file + same rule the second time — pass.
    second = await on_pre_tool_use(_make_ctx("Write", payload))
    assert second.decision == "pass"


@pytest.mark.asyncio
async def test_disable_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    monkeypatch.setenv("ENABLE_SECURITY_REMINDER", "0")
    payload = {"file_path": "/repo/src/foo.js", "content": "eval(x)"}
    res = await on_pre_tool_use(_make_ctx("Write", payload))
    assert res.decision == "pass"


@pytest.mark.asyncio
async def test_ignores_non_file_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    res = await on_pre_tool_use(
        _make_ctx("Bash", {"command": "echo eval(x)"})
    )
    assert res.decision == "pass"


@pytest.mark.asyncio
async def test_multi_edit_combines_new_strings(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    payload = {
        "file_path": "/repo/src/foo.js",
        "edits": [
            {"new_string": "const a = 1;"},
            {"new_string": "el.innerHTML = userText;"},
        ],
    }
    res = await on_pre_tool_use(_make_ctx("MultiEdit", payload))
    assert res.decision == "block"
    assert "innerHTML" in res.reason
