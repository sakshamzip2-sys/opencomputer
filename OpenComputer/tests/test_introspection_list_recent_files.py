"""tests/test_introspection_list_recent_files.py"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from extensions.coding_harness.introspection.tools import ListRecentFilesTool

from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_files_modified_within_window(tmp_path):
    recent = tmp_path / "recent.txt"
    recent.write_text("recent")

    old = tmp_path / "old.txt"
    old.write_text("old")
    old_mtime = time.time() - 24 * 3600
    os.utime(old, (old_mtime, old_mtime))

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 10},
    ))

    assert not result.is_error
    payload = json.loads(result.content)
    paths = [r["path"] for r in payload]
    assert any("recent.txt" in p for p in paths)
    assert all("old.txt" not in p for p in paths)


@pytest.mark.asyncio
async def test_skips_pycache_and_dot_dirs(tmp_path):
    pyc = tmp_path / "__pycache__"
    pyc.mkdir()
    (pyc / "junk.pyc").write_text("compiled")

    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref")

    real = tmp_path / "real.py"
    real.write_text("source")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 10},
    ))

    payload = json.loads(result.content)
    assert all("__pycache__" not in r["path"] for r in payload)
    assert all(".git" not in r["path"] for r in payload)


@pytest.mark.asyncio
async def test_skips_node_modules_and_venv(tmp_path):
    for d in ("node_modules", ".venv", "venv", "dist"):
        sub = tmp_path / d
        sub.mkdir()
        (sub / "junk.js").write_text("noise")

    real = tmp_path / "src.py"
    real.write_text("real")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 50},
    ))

    payload = json.loads(result.content)
    paths = [r["path"] for r in payload]
    # Use path-segment match (not bare substring) so pytest's tmp_path basename
    # — which echoes the test function name and so embeds 'node_modules'/'venv'
    # — doesn't trip the assertion. We only care that no file was returned from
    # *inside* a skipped dir.
    for d in ("node_modules", ".venv", "venv"):
        assert all(f"/{d}/" not in p for p in paths), f"unexpectedly returned a {d} entry"


@pytest.mark.asyncio
async def test_skips_macos_library_bloat_dirs(tmp_path):
    """macOS Library/Mail, Library/Caches, Library/Containers should be skipped."""
    for d in ("Mail", "Caches", "Containers"):
        sub = tmp_path / d
        sub.mkdir()
        (sub / "junk").write_text("noise")

    real = tmp_path / "doc.txt"
    real.write_text("real")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 50},
    ))

    payload = json.loads(result.content)
    paths = [r["path"] for r in payload]
    for d in ("Mail", "Caches", "Containers"):
        assert all(f"/{d}/" not in p for p in paths), f"unexpectedly returned a {d} entry"


@pytest.mark.asyncio
async def test_limit_caps_results(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 5},
    ))

    payload = json.loads(result.content)
    assert len(payload) == 5


@pytest.mark.asyncio
async def test_results_sorted_newest_first(tmp_path):
    earlier = tmp_path / "earlier.txt"
    earlier.write_text("e")
    e_mtime = time.time() - 30 * 60  # 30min ago
    os.utime(earlier, (e_mtime, e_mtime))

    later = tmp_path / "later.txt"
    later.write_text("l")  # default mtime = now

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 10},
    ))

    payload = json.loads(result.content)
    assert payload[0]["path"].endswith("later.txt")
    assert payload[1]["path"].endswith("earlier.txt")


@pytest.mark.asyncio
async def test_missing_directory_returns_error(tmp_path):
    bogus = tmp_path / "does-not-exist"
    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(bogus), "limit": 10},
    ))

    assert result.is_error
    assert "not found" in result.content.lower()


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ListRecentFilesTool.capability_claims
    assert claims[0].capability_id == "introspection.list_recent_files"


@pytest.mark.asyncio
async def test_default_directory_expands_tilde():
    """Default directory '~' should expand to the user's home, not be passed literally."""
    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": "~", "limit": 1},
    ))
    # Just verify it doesn't error with "~ not found" — we don't assert content shape.
    if result.is_error:
        # If it errors for unrelated reasons (e.g. perm error scanning home), that's fine,
        # but it must not be the "directory not found" path.
        assert "not found" not in result.content.lower()
