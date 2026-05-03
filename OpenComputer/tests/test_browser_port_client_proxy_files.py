"""Tests for client/proxy_files.py — recursive path rewriting + persist."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from extensions.browser_control.client.proxy_files import (
    apply_proxy_paths,
    persist_proxy_files,
)


class TestApplyProxyPaths:
    def test_top_level_path_field(self):
        node = {"path": "/remote/file.png", "other": "leave-alone"}
        apply_proxy_paths(node, {"/remote/file.png": "/local/file.png"})
        assert node["path"] == "/local/file.png"
        assert node["other"] == "leave-alone"

    def test_image_path_alias(self):
        node = {"imagePath": "/remote/img.png"}
        apply_proxy_paths(node, {"/remote/img.png": "/local/img.png"})
        assert node["imagePath"] == "/local/img.png"

    def test_recursive_walk_dict(self):
        """Fix vs OpenClaw shallow walk — must descend into nested dicts."""
        node = {
            "result": {
                "download": {
                    "path": "/remote/dl.zip",
                },
            },
        }
        apply_proxy_paths(
            node,
            {"/remote/dl.zip": "/local/dl.zip"},
        )
        assert node["result"]["download"]["path"] == "/local/dl.zip"

    def test_recursive_walk_list(self):
        node = {
            "downloads": [
                {"path": "/remote/a.zip", "size": 100},
                {"path": "/remote/b.zip", "size": 200},
            ],
        }
        apply_proxy_paths(
            node,
            {
                "/remote/a.zip": "/local/a.zip",
                "/remote/b.zip": "/local/b.zip",
            },
        )
        assert node["downloads"][0]["path"] == "/local/a.zip"
        assert node["downloads"][1]["path"] == "/local/b.zip"

    def test_deep_nesting_4_levels(self):
        node = {
            "level1": {
                "level2": {
                    "level3": {
                        "level4": {
                            "path": "/remote/deep.png",
                        }
                    }
                }
            }
        }
        apply_proxy_paths(node, {"/remote/deep.png": "/local/deep.png"})
        assert node["level1"]["level2"]["level3"]["level4"]["path"] == "/local/deep.png"

    def test_unmapped_paths_untouched(self):
        node = {"path": "/remote/x.png"}
        apply_proxy_paths(node, {"/remote/y.png": "/local/y.png"})
        assert node["path"] == "/remote/x.png"

    def test_empty_mapping_no_op(self):
        node = {"path": "/remote/x.png"}
        apply_proxy_paths(node, {})
        assert node["path"] == "/remote/x.png"

    def test_non_path_string_field_untouched(self):
        # Even if a string field happens to match a key, only the listed
        # path-bearing field names are rewritten — by design, to avoid
        # rewriting arbitrary user data.
        node = {"description": "/remote/x.png"}
        apply_proxy_paths(node, {"/remote/x.png": "/local/x.png"})
        assert node["description"] == "/remote/x.png"

    def test_cycles_tolerated(self):
        a: dict = {"path": "/remote/x.png"}
        a["self"] = a
        # Should not infinite-loop
        apply_proxy_paths(a, {"/remote/x.png": "/local/x.png"})
        assert a["path"] == "/local/x.png"


class TestPersistProxyFiles:
    @pytest.mark.asyncio
    async def test_persists_files_and_returns_mapping(self, tmp_path: Path):
        payload = b"hello world"
        files = [
            {
                "path": "/remote/hello.txt",
                "base64": base64.b64encode(payload).decode("ascii"),
                "mimeType": "text/plain",
            },
        ]
        mapping = await persist_proxy_files(files, media_root=tmp_path / "media")
        assert "/remote/hello.txt" in mapping
        local = Path(mapping["/remote/hello.txt"])
        assert local.exists()
        assert local.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_handles_empty_input(self, tmp_path: Path):
        assert await persist_proxy_files(None, media_root=tmp_path) == {}
        assert await persist_proxy_files([], media_root=tmp_path) == {}

    @pytest.mark.asyncio
    async def test_disambiguates_collisions(self, tmp_path: Path):
        media_root = tmp_path / "m"
        # First file
        await persist_proxy_files(
            [{"path": "/r/a.txt", "base64": base64.b64encode(b"v1").decode()}],
            media_root=media_root,
        )
        # Second file with the same basename
        mapping = await persist_proxy_files(
            [{"path": "/r/a.txt", "base64": base64.b64encode(b"v2").decode()}],
            media_root=media_root,
        )
        local = Path(mapping["/r/a.txt"])
        # Disambiguated to a-1.txt
        assert "a-1" in local.name or local.name != "a.txt"
        assert local.read_bytes() == b"v2"

    @pytest.mark.asyncio
    async def test_skips_malformed_records(self, tmp_path: Path):
        files = [
            {"path": ""},  # blank path
            {"base64": "abc"},  # missing path
            "not a dict",
            {"path": "/r/x.txt", "base64": "@@invalid base64@@"},  # b64 fail
        ]
        mapping = await persist_proxy_files(files, media_root=tmp_path)  # type: ignore[arg-type]
        assert mapping == {}
