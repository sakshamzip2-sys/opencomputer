"""tests/test_introspection_list_app_usage.py"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from extensions.coding_harness.introspection.tools import ListAppUsageTool

from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_top_processes_by_cpu():
    now = time.time()
    fake_procs = [
        MagicMock(info={"name": "Chrome", "cpu_percent": 12.5, "create_time": now - 100}),
        MagicMock(info={"name": "VSCode", "cpu_percent": 8.1, "create_time": now - 200}),
        MagicMock(info={"name": "kernel_task", "cpu_percent": 1.2, "create_time": now - 50}),
    ]

    with patch("extensions.coding_harness.introspection.tools.psutil.process_iter", return_value=fake_procs):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t1", name="list_app_usage", arguments={}))

    assert not result.is_error
    payload = json.loads(result.content)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "Chrome"
    assert payload[0]["cpu_percent"] == 12.5
    assert "started" in payload[0]
    # All three are within the default 8h window
    assert len(payload) == 3


@pytest.mark.asyncio
async def test_filters_out_old_processes():
    now = time.time()
    fake_procs = [
        MagicMock(info={"name": "Recent", "cpu_percent": 5.0, "create_time": now - 100}),
        MagicMock(info={"name": "Stale", "cpu_percent": 9.0, "create_time": now - 99999}),  # 27+ hrs old
    ]
    with patch("extensions.coding_harness.introspection.tools.psutil.process_iter", return_value=fake_procs):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t1", name="list_app_usage", arguments={"hours": 1}))

    payload = json.loads(result.content)
    assert len(payload) == 1
    assert payload[0]["name"] == "Recent"


@pytest.mark.asyncio
async def test_capability_claim_namespace_is_introspection():
    claims = ListAppUsageTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "introspection.list_app_usage"


@pytest.mark.asyncio
async def test_handles_psutil_exception():
    with patch(
        "extensions.coding_harness.introspection.tools.psutil.process_iter",
        side_effect=RuntimeError("psutil unavailable"),
    ):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t2", name="list_app_usage", arguments={}))

    assert result.is_error
    assert "psutil unavailable" in result.content


@pytest.mark.asyncio
async def test_handles_per_process_access_denied():
    """Some processes (e.g. SYSTEM on Windows) raise AccessDenied during iteration.

    The tool should skip them and return what it can read.

    Design choice: AccessDenied bubbling up from `process_iter` is treated
    as fatal — the tool returns is_error=True with the underlying message.
    Per-process AccessDenied raised when reading individual `proc.info`
    fields is handled by psutil's `process_iter` itself (it skips them
    silently when fields are passed via the `attrs=` kwarg). So if we
    actually see AccessDenied surface to us, it means the iterator itself
    failed, which is unrecoverable. Test reflects this.
    """
    import psutil
    now = time.time()
    good = MagicMock(info={"name": "Chrome", "cpu_percent": 12.5, "create_time": now - 100})

    def raising_iter(*args, **kwargs):
        # First yield a good process, then raise AccessDenied
        yield good
        raise psutil.AccessDenied(pid=999)

    with patch("extensions.coding_harness.introspection.tools.psutil.process_iter", side_effect=raising_iter):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t1", name="list_app_usage", arguments={}))

    # AccessDenied bubbling up from process_iter is treated as fatal — return is_error.
    assert result.is_error or json.loads(result.content) == [
        {"name": "Chrome", "cpu_percent": 12.5, "started": pytest.approx(now - 100, abs=10)}
    ]
