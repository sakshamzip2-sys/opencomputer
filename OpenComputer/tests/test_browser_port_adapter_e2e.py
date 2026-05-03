"""End-to-end smoke tests for Wave 4 (BLUEPRINT §13 acceptance criteria).

Two real flows the BLUEPRINT calls out:

  1. Inline import + direct call of the bundled ``hackernews/top`` adapter
     against a fake fetcher; verify ≥5 stories returned with the expected
     columns. We don't hit the network — the test stubs ``ctx.fetch`` so
     the test is hermetic.

  2. Browser(action="adapter_new", site="test", name="probe") →
     adapter_validate → instantiate the generated tool spec → execute
     → verify the discovered tool surfaces in a registry walk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


# ─── Smoke 1: bundled hackernews/top via stubbed fetcher ────────────


class _StubHttpClient:
    """Minimal httpx.AsyncClient drop-in for offline testing."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **_kw):
        self.calls.append(url)
        body = self._mapping.get(url, [])
        return _StubResp(body)

    async def aclose(self):  # pragma: no cover - trivial
        return None


class _StubResp:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body

    @property
    def text(self):
        import json

        return json.dumps(self._body)

    def raise_for_status(self):
        return None


def test_smoke_hackernews_top_against_stubbed_http(tmp_path: Path):
    """BLUEPRINT §13 acceptance — bundled adapter returns ≥5 stories with
    expected columns."""
    # Discover the bundled pack — that imports hackernews/top.py and
    # registers it.
    from extensions.adapter_runner._discovery import discover_adapters
    from extensions.adapter_runner._runner import run_adapter

    ext_root = Path(__file__).resolve().parent.parent / "extensions"
    discover_adapters(profile_home=None, extensions_root=ext_root)

    from extensions.adapter_runner import get_adapter

    spec = get_adapter("hackernews", "top")
    assert spec is not None

    # Stub the HN endpoints so the test is hermetic.
    ids_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    fixture_ids = list(range(1000, 1015))
    mapping = {ids_url: fixture_ids}
    for i in fixture_ids:
        mapping[f"https://hacker-news.firebaseio.com/v0/item/{i}.json"] = {
            "id": i,
            "title": f"Story {i}",
            "score": i,
            "by": "alice",
            "descendants": 7,
            "url": f"https://example.com/{i}",
        }

    client = _StubHttpClient(mapping)
    result = asyncio.run(
        run_adapter(
            spec,
            arguments={"limit": 5},
            profile_home=tmp_path,
            http_client=client,
        )
    )
    assert not result.is_error, result.content
    import json

    rows = json.loads(result.content)
    assert isinstance(rows, list)
    assert len(rows) >= 5
    # Expected columns from the spec
    expected_cols = {"rank", "title", "score", "author", "comments", "url"}
    assert expected_cols.issubset(set(rows[0].keys()))


# ─── Smoke 2: adapter_new → adapter_validate → execute ──────────────


def test_smoke_adapter_new_validate_execute_cycle(tmp_path: Path):
    """End-to-end: scaffold → validate → execute generated tool.

    Produces a hardcoded ``run`` body via ``adapter_save`` (since
    ``adapter_new``'s default stub raises NotImplementedError), then
    runs it through the runner."""
    from extensions.browser_control._tool import Browser

    from plugin_sdk.core import ToolCall

    browser = Browser(actions=_NoopActions())
    adapters_root = tmp_path / "adapters"

    # 1) adapter_save (writes a stub with our run body)
    res_save = asyncio.run(
        browser.execute(
            ToolCall(
                id="1",
                name="Browser",
                arguments={
                    "action": "adapter_save",
                    "site": "test",
                    "name": "probe",
                    "path": str(adapters_root / "test" / "probe.py"),
                    "strategy": "public",
                    "run_body": (
                        "return [{'id': 1, 'msg': 'hello'}, "
                        "{'id': 2, 'msg': 'world'}]"
                    ),
                },
            )
        )
    )
    assert not res_save.is_error, res_save.content

    # 2) adapter_validate
    src_path = adapters_root / "test" / "probe.py"
    assert src_path.is_file()

    res_val = asyncio.run(
        browser.execute(
            ToolCall(
                id="2",
                name="Browser",
                arguments={
                    "action": "adapter_validate",
                    "path": str(src_path),
                },
            )
        )
    )
    import json

    payload = json.loads(res_val.content)
    assert payload["ok"] is True, payload
    assert payload["tool_name"] == "TestProbe"

    # 3) Execute the generated tool through the runner
    from extensions.adapter_runner import get_adapter
    from extensions.adapter_runner._runner import run_adapter

    spec = get_adapter("test", "probe")
    assert spec is not None
    result = asyncio.run(
        run_adapter(spec, arguments={}, profile_home=tmp_path)
    )
    assert not result.is_error, result.content
    rows = json.loads(result.content)
    assert len(rows) == 2
    assert rows[0]["msg"] == "hello"


class _NoopActions:
    """Doesn't need any real browser ops for adapter_new / adapter_validate."""

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
