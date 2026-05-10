"""Honest-audit follow-ups to the Hermes v2 parity work (PR #510).

Closes three gaps that were missed or under-scoped in the first pass:

- ``.zprofile`` (and zlogin/zshenv/bash_login) added to blocked basenames.
- Path-traversal protection: refuse references that resolve outside the
  workspace root.
- Binary-file detection: extension allowlist + null-byte sniff.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.at_references import (
    AtRefContext,
    expand,
    is_path_blocked,
)

# ─── ctx helper ───────────────────────────────────────────────────


def _ctx(cwd: Path, home: Path | None = None) -> AtRefContext:
    return AtRefContext(
        cwd=str(cwd),
        home=str(home or cwd),
        context_window_chars=200_000,
    )


# ─── .zprofile + siblings (Hermes v2 missed in PR #510) ───────────


def test_zprofile_basename_is_blocked(tmp_path: Path):
    fake_zprofile = tmp_path / ".zprofile"
    fake_zprofile.write_text("export ANTHROPIC_API_KEY=sk-ant-secret\n")
    assert is_path_blocked(fake_zprofile, home=tmp_path) is True


def test_zlogin_basename_is_blocked(tmp_path: Path):
    fake = tmp_path / ".zlogin"
    fake.write_text("export FOO=bar\n")
    assert is_path_blocked(fake, home=tmp_path) is True


def test_zshenv_basename_is_blocked(tmp_path: Path):
    fake = tmp_path / ".zshenv"
    fake.write_text("export FOO=bar\n")
    assert is_path_blocked(fake, home=tmp_path) is True


def test_bash_login_basename_is_blocked(tmp_path: Path):
    fake = tmp_path / ".bash_login"
    fake.write_text("export FOO=bar\n")
    assert is_path_blocked(fake, home=tmp_path) is True


def test_at_file_blocks_zprofile(tmp_path: Path):
    secret = tmp_path / ".zprofile"
    secret.write_text("export ANTHROPIC_API_KEY=sk-ant-secret\n")
    out = expand(f"@file:{secret}", ctx=_ctx(tmp_path))
    assert "[blocked path:" in out
    assert "sk-ant-secret" not in out


# ─── path-traversal protection (Hermes v2 spec) ───────────────────


def test_at_file_refuses_path_outside_workspace(tmp_path: Path):
    # /tmp/<this test's tmpdir> = workspace_root; /etc/hosts is outside.
    out = expand("@file:/etc/hosts", ctx=_ctx(tmp_path))
    assert "[blocked:" in out and "outside workspace" in out


def test_at_file_refuses_dotdot_traversal(tmp_path: Path):
    # cwd = tmp_path/inner; @file:../sibling.txt resolves outside inner.
    inner = tmp_path / "inner"
    inner.mkdir()
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("secret\n")
    out = expand(f"@file:{sibling}", ctx=_ctx(inner))
    assert "[blocked:" in out and "outside workspace" in out
    assert "secret" not in out


def test_at_file_allows_path_inside_workspace(tmp_path: Path):
    inside = tmp_path / "ok.txt"
    inside.write_text("content-inside\n")
    out = expand(f"@file:{inside}", ctx=_ctx(tmp_path))
    assert "content-inside" in out


# ─── binary-file detection (Hermes v2 spec) ───────────────────────


def test_at_file_refuses_png_extension(tmp_path: Path):
    img = tmp_path / "logo.png"
    # Real PNG header bytes — the null byte AND the extension both trip the check.
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = expand(f"@file:{img}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" in out


def test_at_file_refuses_null_bytes_unknown_extension(tmp_path: Path):
    weird = tmp_path / "data.unknown"  # ext not in allowlist
    weird.write_bytes(b"some text\x00binary\x00\x01\x02\n")
    out = expand(f"@file:{weird}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" in out


def test_at_file_allows_normal_text_with_unicode(tmp_path: Path):
    text = tmp_path / "notes.md"
    text.write_text("# Notes\n\nE = mc²\nÉpée → français\n", encoding="utf-8")
    out = expand(f"@file:{text}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" not in out
    assert "Épée" in out


# ─── D2: text-extension bypass for null-byte sniff ────────────────


def test_md_with_literal_null_is_not_flagged_binary(tmp_path: Path):  # noqa: N802
    """A .md file containing a literal NUL is still text per Hermes v2.

    Real-world case: PlantUML diagram embeds, JSON dumps inside fenced
    code blocks, encoded text. Without the text-extension bypass the
    null-byte sniff would wrongly reject these.
    """
    text = tmp_path / "diagram.md"
    text.write_text("# Notes\n\nDiagram: \x00encoded\x00bytes\n", encoding="utf-8")
    out = expand(f"@file:{text}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" not in out


def test_py_file_passes_even_with_unusual_bytes(tmp_path: Path):
    """A .py file is text by extension contract; bypass null sniff."""
    src = tmp_path / "weird.py"
    src.write_bytes(b"# coding: utf-8\nVALUE = 'has\\x00null'\n")
    out = expand(f"@file:{src}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" not in out


def test_yaml_json_toml_are_text(tmp_path: Path):
    for name, body in [
        ("config.yaml", "key: value\n"),
        ("config.json", '{"key": "value"}\n'),
        ("config.toml", 'key = "value"\n'),
    ]:
        f = tmp_path / name
        f.write_text(body, encoding="utf-8")
        out = expand(f"@file:{f}", ctx=_ctx(tmp_path))
        assert "[binary file not supported:" not in out
        assert "value" in out


def test_zip_extension_blocks_without_io(tmp_path: Path):
    """Binary-extension fast path doesn't even open the file."""
    archive = tmp_path / "data.zip"
    # Empty file — would have no null bytes if we sniffed; extension
    # alone must catch it.
    archive.write_bytes(b"")
    out = expand(f"@file:{archive}", ctx=_ctx(tmp_path))
    assert "[binary file not supported:" in out
