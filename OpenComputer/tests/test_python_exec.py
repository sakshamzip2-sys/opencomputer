"""PythonExec tool — sandboxed Python execution."""
import pytest

from opencomputer.tools.python_exec import PythonExec
from plugin_sdk.core import ToolCall


@pytest.fixture
def tool() -> PythonExec:
    return PythonExec()


@pytest.mark.asyncio
async def test_executes_simple_script(tool):
    call = ToolCall(id="t1", name="PythonExec", arguments={"code": "print(2 + 2)"})
    result = await tool.execute(call)
    assert "4" in result.content
    assert not result.is_error


@pytest.mark.asyncio
async def test_returns_error_on_syntax_error(tool):
    call = ToolCall(id="t2", name="PythonExec", arguments={"code": "def x(:"})
    result = await tool.execute(call)
    assert result.is_error
    assert "SyntaxError" in result.content


@pytest.mark.asyncio
async def test_blocks_unsafe_script(tool):
    call = ToolCall(id="t3", name="PythonExec", arguments={"code": "import os; os.system('rm /')"})
    result = await tool.execute(call)
    assert result.is_error
    assert "denylist" in result.content.lower() or "unsafe" in result.content.lower()


@pytest.mark.asyncio
async def test_captures_stdout(tool):
    call = ToolCall(id="t4", name="PythonExec", arguments={"code": "for i in range(3):\n    print(f'line {i}')"})
    result = await tool.execute(call)
    assert "line 0" in result.content
    assert "line 1" in result.content
    assert "line 2" in result.content


@pytest.mark.asyncio
async def test_captures_stderr(tool):
    call = ToolCall(id="t5", name="PythonExec", arguments={"code": "import sys\nprint('err', file=sys.stderr)"})
    result = await tool.execute(call)
    assert "err" in result.content


@pytest.mark.asyncio
async def test_timeout_returns_error(tool):
    call = ToolCall(
        id="t6", name="PythonExec",
        arguments={"code": "import time\ntime.sleep(60)", "timeout_seconds": 0.5},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "timeout" in result.content.lower() or "timed out" in result.content.lower()
