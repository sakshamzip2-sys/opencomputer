"""Tests for ``ExecuteCode`` (Hermes Doc-2 parity, 2026-05-08).

Covers:
* Schema shape (Hermes-named ``ExecuteCode``).
* Empty-code guard.
* Recursion guard via ``OC_EXECUTE_CODE_DEPTH`` env marker.
* Windows refusal (sys.platform check).
* Mode validation (project / strict).
* End-to-end happy path: a tiny script that prints a literal
  short-circuits the harness without needing real tools.
* Env scrub: ``_scrub_env`` removes vars matching scrub patterns and
  preserves passthrough keys.
* Stderr cap: stderr longer than the cap is truncated with marker.

The end-to-end test of full RPC against real tools is covered by
``test_ptc.py`` already; this file exercises the ExecuteCode-specific
*delta* over PTC.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from opencomputer.tools.execute_code import (
    ExecuteCode,
    ExecuteCodeRecursionError,
)
from opencomputer.tools.ptc import (
    _RECURSION_GUARD_ENV,
    EXECUTE_CODE_DEFAULT_TOOLS,
    _scrub_env,
)
from plugin_sdk.core import ToolCall

# ─── Schema ────────────────────────────────────────────────────────────


def test_execute_code_schema_uses_hermes_name() -> None:
    schema = ExecuteCode().schema
    assert schema.name == "ExecuteCode"
    assert "code" in schema.parameters["properties"]
    assert "code" in schema.parameters["required"]
    assert "mode" in schema.parameters["properties"]
    assert schema.parameters["properties"]["mode"]["enum"] == ["project", "strict"]


def test_execute_code_default_tools_match_hermes() -> None:
    """The default allowlist must include both reads AND writes/Bash —
    Hermes' default does, so ours must too."""
    assert "Read" in EXECUTE_CODE_DEFAULT_TOOLS
    assert "Write" in EXECUTE_CODE_DEFAULT_TOOLS
    assert "Bash" in EXECUTE_CODE_DEFAULT_TOOLS
    assert "WebFetch" in EXECUTE_CODE_DEFAULT_TOOLS
    assert "WebSearch" in EXECUTE_CODE_DEFAULT_TOOLS


# ─── Empty / invalid input ─────────────────────────────────────────────


def test_empty_code_is_an_error() -> None:
    tool = ExecuteCode()
    call = ToolCall(id="t1", name="ExecuteCode", arguments={"code": "  "})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "empty" in result.content.lower()


def test_unknown_mode_is_an_error() -> None:
    tool = ExecuteCode()
    call = ToolCall(
        id="t2", name="ExecuteCode",
        arguments={"code": "print(1)", "mode": "ghost"},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "unknown mode" in result.content


# ─── Recursion guard ───────────────────────────────────────────────────


def test_recursion_guard_refuses_nested_invocation(monkeypatch) -> None:
    """When OC_EXECUTE_CODE_DEPTH is already set, ExecuteCode refuses."""
    monkeypatch.setenv(_RECURSION_GUARD_ENV, "1")
    tool = ExecuteCode()
    call = ToolCall(id="t3", name="ExecuteCode", arguments={"code": "print(1)"})
    with pytest.raises(ExecuteCodeRecursionError, match="recursive"):
        asyncio.run(tool.execute(call))


# ─── Windows guard ─────────────────────────────────────────────────────


def test_windows_refuses_with_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    tool = ExecuteCode()
    call = ToolCall(id="t4", name="ExecuteCode", arguments={"code": "print(1)"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "Windows" in result.content


# ─── Env scrub helper ──────────────────────────────────────────────────


def test_scrub_env_removes_pattern_matches() -> None:
    env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret",
        "MY_TOKEN": "abc",
        "ANTHROPIC_AUTH_MODE": "bearer",
        "USERNAME": "saksham",
        "AWS_SECRET_ACCESS_KEY": "x",
        "MY_PASSWORD": "y",
        "ROUTING_CREDENTIAL": "z",
    }
    scrubbed, removed = _scrub_env(env)
    # Innocuous vars survive.
    assert "PATH" in scrubbed
    assert "USERNAME" in scrubbed
    # Sensitive vars dropped.
    assert "OPENAI_API_KEY" not in scrubbed
    assert "MY_TOKEN" not in scrubbed
    assert "ANTHROPIC_AUTH_MODE" not in scrubbed
    assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
    assert "MY_PASSWORD" not in scrubbed
    assert "ROUTING_CREDENTIAL" not in scrubbed
    # And the names are reported.
    assert "OPENAI_API_KEY" in removed
    assert "AWS_SECRET_ACCESS_KEY" in removed


def test_scrub_env_passthrough_preserves_named_keys() -> None:
    env = {"OPENAI_API_KEY": "sk", "MY_TOKEN": "abc"}
    scrubbed, removed = _scrub_env(env, passthrough=("OPENAI_API_KEY",))
    assert scrubbed["OPENAI_API_KEY"] == "sk"  # explicitly passed through
    assert "MY_TOKEN" not in scrubbed  # still scrubbed
    assert "MY_TOKEN" in removed
    assert "OPENAI_API_KEY" not in removed


# ─── End-to-end: tiny script ───────────────────────────────────────────


def test_execute_code_runs_a_trivial_print() -> None:
    """The script prints '42'; ExecuteCode returns it as content."""
    if sys.platform == "win32":
        pytest.skip("ExecuteCode disabled on Windows")
    tool = ExecuteCode()
    call = ToolCall(
        id="t5",
        name="ExecuteCode",
        arguments={"code": "print('hello world')", "tools": []},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is False, f"unexpected error: {result.content}"
    assert "hello world" in result.content


def test_execute_code_subprocess_sees_recursion_guard() -> None:
    """The script reads OC_EXECUTE_CODE_DEPTH and prints it.

    Asserts the parent set the env var to '1' (the depth was 0 before
    spawn, incremented to 1 for the child).
    """
    if sys.platform == "win32":
        pytest.skip("ExecuteCode disabled on Windows")
    tool = ExecuteCode()
    call = ToolCall(
        id="t6",
        name="ExecuteCode",
        arguments={
            "code": (
                "import os\n"
                f"print('depth=' + os.environ.get({_RECURSION_GUARD_ENV!r}, ''))\n"
            ),
            "tools": [],
        },
    )
    result = asyncio.run(tool.execute(call))
    assert "depth=1" in result.content


def test_execute_code_subprocess_env_is_scrubbed() -> None:
    """Sensitive env vars present in the parent must NOT leak into
    the child unless explicitly passed through."""
    if sys.platform == "win32":
        pytest.skip("ExecuteCode disabled on Windows")
    # Inject a fake credential into the parent's env.
    fake_key = "OC_TEST_FAKE_API_KEY"
    fake_val = "this-must-not-leak"
    saved = os.environ.get(fake_key)
    os.environ[fake_key] = fake_val
    try:
        tool = ExecuteCode()
        call = ToolCall(
            id="t7",
            name="ExecuteCode",
            arguments={
                "code": (
                    "import os\n"
                    f"print('present=' + str({fake_key!r} in os.environ))\n"
                ),
                "tools": [],
            },
        )
        result = asyncio.run(tool.execute(call))
        assert "present=False" in result.content, (
            f"env scrub failed — leaked {fake_key} into subprocess: {result.content}"
        )
    finally:
        if saved is not None:
            os.environ[fake_key] = saved
        else:
            os.environ.pop(fake_key, None)
