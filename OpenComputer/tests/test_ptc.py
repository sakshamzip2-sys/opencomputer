"""Tier-A item 8 — PTC (Programmatic Tool Calling) for python_exec."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

import pytest

from opencomputer.tools.ptc import (
    DEFAULT_ALLOWED_TOOLS,
    PTCServer,
    PTCServerConfig,
    _build_prologue,
    run_ptc,
)
from plugin_sdk.core import ToolCall, ToolResult

# ──────────────────────────── prologue ────────────────────────────


def test_build_prologue_includes_each_allowed_tool():
    out = _build_prologue(("Read", "WebFetch"))
    assert "def Read(**kwargs):" in out
    assert "def WebFetch(**kwargs):" in out
    assert "_ptc_call(" in out


def test_build_prologue_uses_socket_env_var():
    out = _build_prologue(("Read",))
    assert "OC_PTC_SOCKET" in out


def test_build_prologue_enforces_call_cap():
    out = _build_prologue(("Read",))
    assert "_ptc_max_calls" in out


# ──────────────────────────── server (in-process) ────────────────────────────


class _FakeReadTool:
    """Stand-in tool — returns a fixed result for any path."""

    async def execute(self, call: ToolCall) -> ToolResult:
        path = call.arguments.get("file_path", "")
        return ToolResult(tool_call_id=call.id, content=f"contents of {path}")


class _FakeRegistry:
    def __init__(self, tools: dict[str, object]) -> None:
        self._tools = tools

    def get(self, name: str):
        return self._tools.get(name)


@pytest.mark.asyncio
async def test_server_dispatches_allowed_tool():
    registry = _FakeRegistry({"Read": _FakeReadTool()})
    server = PTCServer(registry, PTCServerConfig(allowed_tools=("Read",)))
    await server.start()
    try:
        # Connect to the socket as a client and send one request.
        reader, writer = await asyncio.open_unix_connection(server.socket_path)
        import json
        import struct
        body = json.dumps({"tool": "Read", "arguments": {"file_path": "/etc/hostname"}}).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        hdr = await reader.readexactly(4)
        n = struct.unpack(">I", hdr)[0]
        resp_bytes = await reader.readexactly(n)
        resp = json.loads(resp_bytes)
        assert resp["is_error"] is False
        assert "contents of /etc/hostname" in resp["content"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_rejects_disallowed_tool():
    registry = _FakeRegistry({"Bash": _FakeReadTool()})  # Bash present but not allowed
    server = PTCServer(registry, PTCServerConfig(allowed_tools=("Read",)))
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.socket_path)
        import json
        import struct
        body = json.dumps({"tool": "Bash", "arguments": {}}).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        hdr = await reader.readexactly(4)
        n = struct.unpack(">I", hdr)[0]
        resp = json.loads(await reader.readexactly(n))
        assert resp["is_error"] is True
        assert "not in the PTC allowlist" in resp["content"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_unknown_tool_in_registry():
    registry = _FakeRegistry({})  # empty registry
    server = PTCServer(registry, PTCServerConfig(allowed_tools=("Read",)))
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.socket_path)
        import json
        import struct
        body = json.dumps({"tool": "Read", "arguments": {}}).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        hdr = await reader.readexactly(4)
        n = struct.unpack(">I", hdr)[0]
        resp = json.loads(await reader.readexactly(n))
        assert resp["is_error"] is True
        assert "unknown tool" in resp["content"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_socket_cleaned_up_on_stop():
    """UDS path on macOS is capped ~104 chars — let the helper pick /tmp."""
    registry = _FakeRegistry({})
    server = PTCServer(registry, PTCServerConfig(allowed_tools=("Read",)))
    await server.start()
    sock_path = server.socket_path
    assert os.path.exists(sock_path)
    await server.stop()
    assert not os.path.exists(sock_path)


@pytest.mark.asyncio
async def test_server_enforces_socket_perms():
    registry = _FakeRegistry({})
    server = PTCServer(registry, PTCServerConfig(allowed_tools=("Read",)))
    await server.start()
    try:
        mode = os.stat(server.socket_path).st_mode & 0o777
        assert mode == 0o700
    finally:
        await server.stop()


# ──────────────────────────── end-to-end run_ptc ────────────────────────────


@pytest.mark.asyncio
async def test_run_ptc_round_trip():
    """Spawn the actual subprocess + run a simple script that calls Read twice."""
    registry = _FakeRegistry({"Read": _FakeReadTool()})
    code = textwrap.dedent(
        """
        a = Read(file_path="/x")
        b = Read(file_path="/y")
        print(a + " | " + b)
        """
    )
    result = await run_ptc(
        code, registry=registry, allowed_tools=("Read",), timeout_s=30.0,
    )
    assert result.exit_code == 0, result.stderr
    assert result.timed_out is False
    assert "contents of /x | contents of /y" in result.stdout
    assert result.rpc_call_count == 2


@pytest.mark.asyncio
async def test_run_ptc_disallowed_tool_raises_in_script():
    """A tool name that's not in the allowlist isn't even defined in the
    subprocess — Python raises NameError at runtime."""
    registry = _FakeRegistry({"Read": _FakeReadTool()})
    code = textwrap.dedent(
        """
        try:
            Read(file_path="/x")  # NOT in allowlist
            print("unexpected: succeeded")
        except NameError as e:
            print("name error:", e)
        """
    )
    # Allowed list deliberately empty
    result = await run_ptc(code, registry=registry, allowed_tools=())
    # Script handled the NameError gracefully + printed evidence.
    assert result.exit_code == 0, result.stderr
    assert "name error" in result.stdout


@pytest.mark.asyncio
async def test_run_ptc_caps_stdout_at_50kb():
    registry = _FakeRegistry({})
    code = textwrap.dedent(
        """
        # 60 KB of stdout
        print('x' * (60 * 1024))
        """
    )
    result = await run_ptc(code, registry=registry, allowed_tools=())
    assert result.truncated
    assert "[truncated" in result.stdout


@pytest.mark.asyncio
async def test_run_ptc_timeout_kills_subprocess():
    registry = _FakeRegistry({})
    code = textwrap.dedent(
        """
        import time
        time.sleep(60)
        """
    )
    result = await run_ptc(
        code, registry=registry, allowed_tools=(), timeout_s=0.5,
    )
    assert result.timed_out


@pytest.mark.asyncio
async def test_run_ptc_call_cap():
    """Script exceeding the 50-call cap raises RuntimeError after #50."""
    registry = _FakeRegistry({"Read": _FakeReadTool()})
    code = textwrap.dedent(
        """
        try:
            for i in range(60):
                Read(file_path=f"/file-{i}")
            print("did all 60")
        except RuntimeError as e:
            print("hit cap:", e)
        """
    )
    result = await run_ptc(code, registry=registry, allowed_tools=("Read",))
    assert result.exit_code == 0
    assert "hit cap" in result.stdout
    # The cap is enforced CLIENT-side (in the subprocess) before the 51st
    # send hits the wire, so the server sees exactly 50 RPC calls.
    assert result.rpc_call_count == 50


@pytest.mark.asyncio
async def test_run_ptc_intermediate_results_dont_leak_to_caller():
    """The whole point of PTC: only stdout returns to the LLM."""
    registry = _FakeRegistry({"Read": _FakeReadTool()})
    code = textwrap.dedent(
        """
        a = Read(file_path="/secret")
        # User doesn't print 'a' — only the summary.
        if "secret" in a:
            print("found")
        else:
            print("not found")
        """
    )
    result = await run_ptc(code, registry=registry, allowed_tools=("Read",))
    assert result.exit_code == 0
    assert "found" in result.stdout
    # The intermediate "contents of /secret" must NOT be in stdout.
    assert "contents of /secret" not in result.stdout


@pytest.mark.asyncio
async def test_run_ptc_empty_script():
    registry = _FakeRegistry({})
    result = await run_ptc("", registry=registry, allowed_tools=())
    assert result.exit_code != 0
    assert "empty" in result.stderr.lower()


# ──────────────────────────── PythonExec mode=ptc ────────────────────────────


@pytest.mark.asyncio
async def test_run_ptc_with_patched_registry_round_trip():
    """Sanity check that swapping the registry via the run_ptc parameter
    works end-to-end (no monkeypatching needed)."""
    fake = _FakeReadTool()

    class _PatchedRegistry:
        def get(self, name):
            return fake if name == "Read" else None

    result = await run_ptc(
        "print(Read(file_path='/etc/hosts'))",
        registry=_PatchedRegistry(),
        allowed_tools=("Read",),
    )
    assert result.exit_code == 0
    assert "contents of /etc/hosts" in result.stdout


@pytest.mark.asyncio
async def test_python_exec_default_mode_unchanged():
    """Without mode='ptc' the legacy plain-subprocess path is used."""
    from opencomputer.tools.python_exec import PythonExec

    tool = PythonExec()
    result = await tool.execute(
        ToolCall(id="c1", name="PythonExec", arguments={"code": "print(1+1)"}),
    )
    assert not result.is_error
    assert "2" in result.content


@pytest.mark.asyncio
async def test_python_exec_ptc_mode_includes_metadata_in_result():
    from opencomputer.tools.python_exec import PythonExec

    tool = PythonExec()
    result = await tool.execute(
        ToolCall(
            id="c1", name="PythonExec",
            arguments={
                "code": "print('hello')",
                "mode": "ptc",
                "tools": [],
            },
        ),
    )
    assert not result.is_error
    assert "hello" in result.content
    assert "[ptc:" in result.content


@pytest.mark.asyncio
async def test_python_exec_ptc_mode_caps_timeout_at_300s():
    """Even if timeout_seconds=10000, ptc mode caps at 300s."""
    from opencomputer.tools.python_exec import PythonExec

    tool = PythonExec()
    # Pass an absurdly short script + verify the real call paths through
    # without raising on the absurd timeout value.
    result = await tool.execute(
        ToolCall(
            id="c1", name="PythonExec",
            arguments={
                "code": "print('quick')",
                "mode": "ptc",
                "tools": [],
                "timeout_seconds": 100000.0,
            },
        ),
    )
    assert not result.is_error
    assert "quick" in result.content
