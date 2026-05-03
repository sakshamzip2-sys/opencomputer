"""plugin.json may be JSON5 — comments + trailing commas tolerated.

Sub-project G (openclaw-parity) Task 4. Two-tier parse: json.loads first
(zero overhead for compliant manifests), json5.loads on JSONDecodeError.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.plugins.discovery import _parse_manifest


def _write(tmp: Path, content: str) -> Path:
    p = tmp / "plugin.json"
    p.write_text(content, encoding="utf-8")
    return p


class TestJSON5Tolerance:
    def test_plain_json_still_parses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin","kind":"tool"}',
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_line_comment_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                // line comment
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool"
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_trailing_comma_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool",
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_block_comment_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                /* block
                   comment */
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool"
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None

    def test_garbage_neither_json_nor_json5_returns_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "this is not json")
        m = _parse_manifest(path)
        assert m is None
