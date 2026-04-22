"""Phase 12d.1 — bundled `dev-tools` plugin: Diff + Browser + Fal.

Tests are network-free and git-aware:
- DiffTool exercises a real `git diff` in a tmp repo (git is required for
  any dev box; if it's missing on CI we skip the affected tests).
- BrowserTool tests the optional-import path (Playwright absent → friendly
  error) — we don't require Playwright on CI.
- FalTool mocks httpx for happy-path + 401 / 422 / timeout.

Plus a plugin-loader integration test that confirms `register(api)` lands
all three tools in a fake API surface (mirrors how PluginAPI is shaped).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from plugin_sdk.core import ToolCall

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "extensions" / "dev-tools"


def _call(name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id="tc-1", name=name, arguments=args)


def _import_plugin_module(module_filename: str):
    """Load a plugin-local module by file path under a unique synthetic name.

    Mirrors `opencomputer.plugins.loader._load_plugin`'s import incantation
    so tests don't pollute sys.modules across files."""
    name = f"_test_devtools_{module_filename.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / module_filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    spec.loader.exec_module(module)
    return module


# ─── manifest + plugin.py registration ──────────────────────────────────


def test_plugin_manifest_is_valid_json_with_required_fields() -> None:
    import json

    data = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert data["id"] == "dev-tools"
    assert data["kind"] == "tool"
    assert data["entry"] == "plugin"
    assert data["version"]
    assert data["name"]


def test_register_function_lands_all_three_named_tools() -> None:
    """Plugin's register(api) must add exactly Diff + Browser + Fal.

    Set equality proves both presence AND count in one assertion — preferred
    over a separate `len() == 3` check (which is a brittle regression-guard
    idiom that the project's hook explicitly flags)."""

    class _FakeAPI:
        def __init__(self) -> None:
            self.registered: list[Any] = []

        def register_tool(self, tool: Any) -> None:
            self.registered.append(tool)

    plugin_mod = _import_plugin_module("plugin.py")
    api = _FakeAPI()
    plugin_mod.register(api)
    names = {t.schema.name for t in api.registered}
    assert names == {"Diff", "Browser", "Fal"}


# ─── DiffTool ───────────────────────────────────────────────────────────


def _init_git_repo(root: Path) -> None:
    """Spin up a minimal repo for diff tests."""
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    (root / "a.txt").write_text("alpha\n")
    subprocess.run(["git", "add", "a.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial"], cwd=root, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_diff_working_changes_in_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    DiffTool = diff_mod.DiffTool

    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\nbravo\n")
    monkeypatch.chdir(tmp_path)

    res = await DiffTool().execute(_call("Diff", {}))
    assert not res.is_error
    assert "+bravo" in res.content
    assert "-alpha" not in res.content  # alpha unchanged, not removed


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_diff_clean_repo_says_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    res = await diff_mod.DiffTool().execute(_call("Diff", {}))
    assert not res.is_error
    assert "no changes" in res.content


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_diff_staged_only_shows_index_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\nbravo\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    # Add a SECOND change unstaged — staged diff should NOT include it
    (tmp_path / "a.txt").write_text("alpha\nbravo\ncharlie\n")
    monkeypatch.chdir(tmp_path)

    staged_res = await diff_mod.DiffTool().execute(_call("Diff", {"staged": True}))
    assert not staged_res.is_error
    assert "+bravo" in staged_res.content
    assert "+charlie" not in staged_res.content


async def test_diff_rejects_staged_and_against_both_set() -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    res = await diff_mod.DiffTool().execute(_call("Diff", {"staged": True, "against": "HEAD"}))
    assert res.is_error
    assert "mutually exclusive" in res.content


async def test_diff_handles_missing_git_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    monkeypatch.setattr(
        diff_mod,
        "shutil",
        type("S", (), {"which": staticmethod(lambda *_a, **_k: None)}),
    )
    res = await diff_mod.DiffTool().execute(_call("Diff", {}))
    assert res.is_error
    assert "git" in res.content


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_diff_truncates_to_max_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diff_mod = _import_plugin_module("diff_tool.py")
    _init_git_repo(tmp_path)
    # `git diff` (working) only shows changes to TRACKED files. Commit the
    # big file first, then mutate every line so the diff is large enough
    # to exercise the truncation path.
    (tmp_path / "big.txt").write_text("\n".join(f"line{i}" for i in range(500)) + "\n")
    subprocess.run(["git", "add", "big.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "add big"], cwd=tmp_path, check=True)
    (tmp_path / "big.txt").write_text("\n".join(f"changed{i}" for i in range(500)) + "\n")
    monkeypatch.chdir(tmp_path)

    res = await diff_mod.DiffTool().execute(_call("Diff", {"max_lines": 50}))
    assert not res.is_error
    assert "[truncated" in res.content
    line_count = len(res.content.splitlines())
    assert line_count <= 60  # 50 + ~few lines for the truncation marker


# ─── BrowserTool ────────────────────────────────────────────────────────


async def test_browser_returns_friendly_error_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forces the not-installed branch even when Playwright happens to be on
    the dev box. Prevents test outcome from depending on the dev's environment."""
    browser_mod = _import_plugin_module("browser_tool.py")
    monkeypatch.setattr(browser_mod, "_PLAYWRIGHT_AVAILABLE", False)
    res = await browser_mod.BrowserTool().execute(_call("Browser", {"url": "https://example.com"}))
    assert res.is_error
    assert "Playwright" in res.content
    assert "pip install playwright" in res.content


async def test_browser_rejects_empty_url(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_mod = _import_plugin_module("browser_tool.py")
    monkeypatch.setattr(browser_mod, "_PLAYWRIGHT_AVAILABLE", True)
    res = await browser_mod.BrowserTool().execute(_call("Browser", {"url": ""}))
    assert res.is_error
    assert "url is required" in res.content


async def test_browser_rejects_non_http_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_mod = _import_plugin_module("browser_tool.py")
    monkeypatch.setattr(browser_mod, "_PLAYWRIGHT_AVAILABLE", True)
    res = await browser_mod.BrowserTool().execute(_call("Browser", {"url": "ftp://example.com"}))
    assert res.is_error
    assert "must start with http" in res.content


def test_browser_schema_declares_required_fields() -> None:
    browser_mod = _import_plugin_module("browser_tool.py")
    schema = browser_mod.BrowserTool().schema
    assert schema.name == "Browser"
    assert "url" in schema.parameters["properties"]
    assert schema.parameters["required"] == ["url"]


# ─── FalTool ────────────────────────────────────────────────────────────


async def test_fal_missing_key_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.delenv("FAL_KEY", raising=False)
    res = await fal_mod.FalTool().execute(
        _call("Fal", {"model": "fal-ai/flux/schnell", "payload": {"prompt": "x"}})
    )
    assert res.is_error
    assert "FAL_KEY" in res.content
    assert "fal.ai" in res.content


async def test_fal_missing_model_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "test-key")
    res = await fal_mod.FalTool().execute(_call("Fal", {"payload": {}}))
    assert res.is_error
    assert "model is required" in res.content


async def test_fal_payload_must_be_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "test-key")
    res = await fal_mod.FalTool().execute(
        _call("Fal", {"model": "fal-ai/flux/schnell", "payload": "not a dict"})
    )
    assert res.is_error
    assert "payload" in res.content


def _fake_async_client_factory(captured: dict, response: MagicMock):
    class _FakeClient:
        def __init__(self, *a, **kw):
            captured["init"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return response

    return _FakeClient


async def test_fal_happy_path_image_response_formats_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "test-key")
    captured: dict[str, Any] = {}
    fake_resp = MagicMock(status_code=200, text="")
    fake_resp.json = MagicMock(
        return_value={
            "images": [{"url": "https://fal.media/files/abc.png"}],
            "seed": 42,
        }
    )
    monkeypatch.setattr(
        fal_mod.httpx, "AsyncClient", _fake_async_client_factory(captured, fake_resp)
    )
    res = await fal_mod.FalTool().execute(
        _call(
            "Fal",
            {"model": "fal-ai/flux/schnell", "payload": {"prompt": "an apple"}},
        )
    )
    assert not res.is_error
    assert "https://fal.media/files/abc.png" in res.content
    assert captured["url"].endswith("fal-ai/flux/schnell")
    assert captured["headers"]["Authorization"] == "Key test-key"
    assert captured["json"] == {"prompt": "an apple"}


async def test_fal_401_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "bad-key")
    captured: dict[str, Any] = {}
    fake_resp = MagicMock(status_code=401, text="unauthorized")
    monkeypatch.setattr(
        fal_mod.httpx, "AsyncClient", _fake_async_client_factory(captured, fake_resp)
    )
    res = await fal_mod.FalTool().execute(_call("Fal", {"model": "fal-ai/whisper", "payload": {}}))
    assert res.is_error
    assert "401" in res.content


async def test_fal_422_validation_includes_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "test-key")
    captured: dict[str, Any] = {}
    fake_resp = MagicMock(status_code=422, text="")
    fake_resp.json = MagicMock(
        return_value={"detail": [{"loc": ["body", "prompt"], "msg": "field required"}]}
    )
    monkeypatch.setattr(
        fal_mod.httpx, "AsyncClient", _fake_async_client_factory(captured, fake_resp)
    )
    res = await fal_mod.FalTool().execute(
        _call("Fal", {"model": "fal-ai/flux/schnell", "payload": {}})
    )
    assert res.is_error
    assert "422" in res.content
    assert "field required" in res.content


async def test_fal_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    monkeypatch.setenv("FAL_KEY", "test-key")

    class _TimeoutClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(fal_mod.httpx, "AsyncClient", _TimeoutClient)
    res = await fal_mod.FalTool().execute(
        _call(
            "Fal",
            {
                "model": "fal-ai/flux/schnell",
                "payload": {"prompt": "x"},
                "timeout_s": 1,
            },
        )
    )
    assert res.is_error
    assert "timed out" in res.content


def test_fal_schema_required_fields() -> None:
    fal_mod = _import_plugin_module("fal_tool.py")
    schema = fal_mod.FalTool().schema
    assert schema.name == "Fal"
    assert set(schema.parameters["required"]) == {"model", "payload"}
