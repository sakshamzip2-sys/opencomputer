"""Hot-reload + namespace-bootstrap tests for the Wave 4 hotfix.

Two bugs surfaced when the user tried to author a LearnX adapter
in-session through the adapter-runner:

  1. ``Browser(action="adapter_validate")`` failed with
     ``ModuleNotFoundError: No module named 'extensions.adapter_runner'``
     because the production loader never bootstraps the hyphenated-on-
     disk plugin dir under the underscore alias.
  2. Adapters written during a session weren't visible to the agent
     until restart — the plugin's boot-time discovery walked the
     adapters directory once and never refreshed.

These tests cover the fix:

  - :func:`test_namespace_bootstrap_makes_user_imports_resolve` —
    after the bootstrap helper runs, an adapter file can do
    ``from extensions.adapter_runner import adapter, Strategy``.
  - :func:`test_adapter_save_hot_reload_publishes_synthetic_tool` —
    the killer flow: write a fresh adapter via ``adapter_save``, the
    new ``<Site><Name>`` tool appears on the live ``api`` registry,
    and invoking it returns the adapter's ``run`` output.
  - :func:`test_adapter_save_hot_reload_marks_already_registered` —
    duplicate (site, name) on a second ``adapter_save`` returns
    ``already_registered=True`` rather than raising.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from plugin_sdk.core import ToolCall


class _FakeApi:
    """In-memory ``PluginAPI`` subset — only what the runtime hot-reload
    path touches (``register_tool`` + a tool registry the test reads)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register_tool(self, tool: Any) -> None:
        name = tool.schema.name
        if name in self.tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self.tools[name] = tool


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts with an empty adapter registry + clean LIVE_API."""
    import sys

    from extensions.adapter_runner import clear_registry_for_tests

    def _reset_live_api():
        # ``_LIVE_API`` is stashed on the package namespace (so the two
        # copies of plugin.py — loader-synthetic vs. re-import — share
        # one slot). Wipe both the package slot and the legacy in-module
        # global to keep tests fully isolated.
        pkg = sys.modules.get("extensions.adapter_runner")
        if pkg is not None and hasattr(pkg, "_LIVE_API"):
            pkg._LIVE_API = None
        plugin_mod = sys.modules.get("extensions.adapter_runner.plugin")
        if plugin_mod is not None:
            plugin_mod._LIVE_API = None

    clear_registry_for_tests()
    _reset_live_api()
    yield
    clear_registry_for_tests()
    _reset_live_api()


def test_namespace_bootstrap_makes_user_imports_resolve(tmp_path: Path):
    """Bug 1: dynamic adapter-file import works after the helper runs.

    The helper inside ``_tool.py`` mirrors what the plugin's
    ``register()`` does at boot — it must be self-contained so the
    Browser tool can call it BEFORE the plugin loads (production load
    order isn't guaranteed). Verify that after the helper runs, an
    adapter file with ``from extensions.adapter_runner import adapter,
    Strategy`` imports cleanly via the same dynamic-import path the
    validate action takes.
    """
    # Drop the alias to simulate "fresh process where plugin hasn't loaded"
    import sys

    from extensions.browser_control._tool import _ensure_adapter_runner_namespace

    saved_pkg = sys.modules.pop("extensions.adapter_runner", None)
    try:
        # Bootstrap (the fix under test).
        _ensure_adapter_runner_namespace()
        # The synthesised package now has the public re-exports bound.
        pkg = sys.modules["extensions.adapter_runner"]
        assert hasattr(pkg, "adapter")
        assert hasattr(pkg, "Strategy")
        # And a fresh adapter file's import line resolves.
        f = tmp_path / "good.py"
        f.write_text(
            'from extensions.adapter_runner import adapter, Strategy\n\n'
            '@adapter(site="probe", name="bootcheck", description="d", '
            'domain="e.com", strategy=Strategy.PUBLIC, columns=["x"])\n'
            'async def run(args, ctx):\n'
            '    return [{"x": 1}]\n'
        )
        from extensions.adapter_runner._discovery import _import_adapter_file

        err = _import_adapter_file(f, prefix="bootcheck")
        assert err is None, f"adapter import failed: {err}"
    finally:
        # Don't permanently mutate sys.modules across tests; conftest
        # registers the alias eagerly so restoring is the polite thing.
        if saved_pkg is not None:
            sys.modules["extensions.adapter_runner"] = saved_pkg


def test_adapter_save_hot_reload_publishes_synthetic_tool(tmp_path: Path):
    """Bug 2 — the killer flow.

    Run plugin ``register()`` against an empty extensions root so the
    fake api has zero adapter tools to start, then call
    ``Browser(action="adapter_save", ...)`` for a fresh adapter and
    verify the synthetic ``ProbeTest`` tool appears on the api +
    invoking it returns the adapter's run() output.
    """
    import sys

    from extensions.adapter_runner import plugin as plugin_mod
    from extensions.browser_control._tool import Browser

    api = _FakeApi()
    # Stash the api on the plugin module + the shared package slot —
    # same effect as register(). The plugin code reads via
    # ``sys.modules["extensions.adapter_runner"]._LIVE_API`` so we set
    # both to keep this independent of which slot lookup wins.
    plugin_mod._LIVE_API = api
    pkg = sys.modules.get("extensions.adapter_runner")
    if pkg is not None:
        pkg._LIVE_API = api

    adapters_root = tmp_path / "adapters"
    browser = Browser(actions=_NoopActions())

    res_save = asyncio.run(
        browser.execute(
            ToolCall(
                id="1",
                name="Browser",
                arguments={
                    "action": "adapter_save",
                    "site": "probe",
                    "name": "test",
                    "path": str(adapters_root / "probe" / "test.py"),
                    "strategy": "public",
                    "run_body": "return [{'k': 'v', 'n': 42}]",
                },
            )
        )
    )
    assert not res_save.is_error, res_save.content
    import json

    payload = json.loads(res_save.content)
    assert payload["path"].endswith("probe/test.py")
    hot = payload["hot_reload"]
    assert hot["registered"] is True, hot
    assert hot["tool_name"] == "ProbeTest"

    # Synthetic tool now lives on the live API's registry.
    assert "ProbeTest" in api.tools
    tool = api.tools["ProbeTest"]
    assert tool.schema.name == "ProbeTest"

    # And invoking it executes the adapter's run() body.
    result = asyncio.run(
        tool.execute(ToolCall(id="x", name="ProbeTest", arguments={}))
    )
    assert not result.is_error, result.content
    rows = json.loads(result.content)
    assert rows == [{"k": "v", "n": 42}]


def test_adapter_save_hot_reload_marks_already_registered(tmp_path: Path):
    """A second ``adapter_save`` for the same (site, name) reports the
    duplicate via ``already_registered=True`` instead of raising."""
    import sys

    from extensions.adapter_runner import plugin as plugin_mod
    from extensions.browser_control._tool import Browser

    api = _FakeApi()
    plugin_mod._LIVE_API = api
    pkg = sys.modules.get("extensions.adapter_runner")
    if pkg is not None:
        pkg._LIVE_API = api

    adapters_root = tmp_path / "adapters"
    browser = Browser(actions=_NoopActions())

    args = {
        "action": "adapter_save",
        "site": "probe",
        "name": "test",
        "path": str(adapters_root / "probe" / "test.py"),
        "strategy": "public",
        "run_body": "return [{'k': 'v'}]",
    }
    res1 = asyncio.run(
        browser.execute(ToolCall(id="1", name="Browser", arguments=dict(args)))
    )
    import json

    p1 = json.loads(res1.content)
    assert p1["hot_reload"]["registered"] is True

    # Second save with the same (site, name) — file is overwritten,
    # but the existing tool keeps the registry slot.
    res2 = asyncio.run(
        browser.execute(ToolCall(id="2", name="Browser", arguments=dict(args)))
    )
    p2 = json.loads(res2.content)
    hot2 = p2["hot_reload"]
    assert hot2["registered"] is False
    assert hot2.get("already_registered") is True
    assert hot2["tool_name"] == "ProbeTest"


class _NoopActions:
    """Adapter authoring actions never touch a real browser."""

    async def browser_status(self, **kw):
        return {}

    async def browser_navigate(self, **kw):
        return {}

    async def browser_act(self, request, **kw):
        return {}

    async def browser_requests(self, **kw):
        return {}

    async def browser_response_body(self, **kw):
        return {}
