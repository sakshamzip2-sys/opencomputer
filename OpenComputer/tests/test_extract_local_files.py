"""Tests for BaseChannelAdapter.extract_local_files (Hermes PR 2 Task 2.4 + §A.8).

Code-region-aware bare-path extraction. Validates path exists. Per
amendment §A.8, paths outside the allowlist are NOT extracted (prevents
``/etc/passwd`` exfiltration even if the file exists).
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform


class _A(BaseChannelAdapter):
    platform = Platform.CLI

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, *a, **kw):
        return None


# ─── Basic extraction (allowlist permitting) ─────────────────────────


def test_extract_local_files_bare_image_path(tmp_path: Path) -> None:
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    text = f"check this: {f}"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert files == [f]
    assert "foo.png" not in cleaned


def test_extract_local_files_ignores_inside_code_block(tmp_path: Path) -> None:
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = f"```\n{f}\n```"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert files == []
    assert str(f) in cleaned


def test_extract_local_files_ignores_inside_inline_code(tmp_path: Path) -> None:
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = f"`{f}`"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert files == []


def test_extract_local_files_nonexistent_passed_through(tmp_path: Path) -> None:
    text = "/nonexistent/path/foo.png"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert files == []
    assert "foo.png" in cleaned


def test_extract_local_files_relative_not_extracted(
    tmp_path: Path, monkeypatch
) -> None:
    """Relative paths NOT extracted (only absolute paths via /-prefix or ~/-prefix)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.png").write_bytes(b"\x89PNG")
    text = "see: rel.png"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert files == []


def test_extract_local_files_empty_returns_empty() -> None:
    cleaned, files = _A({}).extract_local_files("")
    assert cleaned == ""
    assert files == []


def test_extract_local_files_multiple_paths(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"\x89PNG")
    b.write_bytes(b"\xff\xd8\xff")
    text = f"two files: {a} and {b}"
    cleaned, files = _A({}).extract_local_files(text, allowed_dirs=[tmp_path])
    assert set(files) == {a, b}


# ─── §A.8 allowlist (security) ───────────────────────────────────────


def test_extract_local_files_outside_allowlist_rejected(tmp_path: Path) -> None:
    """Per amendment §A.8: paths outside allowlist are NOT extracted.

    Real risk: agent emits ``/etc/passwd`` (absolute, exists) → adapter
    attaches → leaks via chat. The allowlist breaks that path even if
    ``/etc/passwd`` is genuinely on disk.
    """
    a = _A({})
    text = "leak this: /etc/passwd"
    cleaned, files = a.extract_local_files(
        text, allowed_dirs=[tmp_path]  # /etc not in allowlist
    )
    # /etc/passwd exists on macOS / Linux, but it's outside the
    # allowlist → must not be extracted.
    assert files == []
    # Path stays in the cleaned text (the agent's words are unmodified).
    assert "/etc/passwd" in cleaned


def test_extract_local_files_default_allowlist_is_documents_and_tmp(
    tmp_path: Path, monkeypatch
) -> None:
    """Default allowlist: ~/Documents + /tmp. /var, /etc, /usr are blocked."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    docs = fake_home / "Documents"
    docs.mkdir()
    f = docs / "report.pdf"
    f.write_bytes(b"%PDF-1.4")
    a = _A({})
    cleaned, files = a.extract_local_files(f"see {f}")
    assert files == [f]


def test_extract_local_files_tmp_is_in_default_allowlist() -> None:
    """/tmp paths are extractable by default — common scratch dir for the agent.

    Use ``/tmp`` directly (not ``tempfile``) because on macOS ``$TMPDIR``
    points at ``/var/folders/...`` which isn't on the default allowlist.
    """
    import os
    import uuid

    name = f"oc_test_{uuid.uuid4().hex}.png"
    path = f"/tmp/{name}"
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        a = _A({})
        cleaned, files = a.extract_local_files(f"image: {path}")
        assert len(files) == 1
        # Resolve both sides since /tmp is a symlink on macOS.
        assert files[0].resolve() == Path(path).resolve()
    finally:
        os.unlink(path)


def test_extract_local_files_path_traversal_blocked(tmp_path: Path) -> None:
    """``..`` traversal cannot escape the allowlist — resolve() normalises."""
    a = _A({})
    # Pretend the allowlist is /tmp/allowed; agent emits a traversal
    # path that resolves outside it.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = tmp_path / "secret.txt"
    target.write_bytes(b"hush")
    sneaky = f"{allowed}/../secret.txt"
    cleaned, files = a.extract_local_files(
        f"see: {sneaky}", allowed_dirs=[allowed]
    )
    # secret.txt resolves to tmp_path/secret.txt which is OUTSIDE allowed/.
    assert files == []
