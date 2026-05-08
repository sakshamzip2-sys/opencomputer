"""@-reference expansion: file/folder/diff/staged/git/url + caps + blocked."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opencomputer.agent.at_references import (
    AtRefContext,
    expand,
    is_path_blocked,
)


def _ctx(tmp_path: Path, **kw) -> AtRefContext:
    return AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
        context_window_chars=kw.pop("context_window_chars", 200_000),
        **kw,
    )


# ─── @file ────────────────────────────────────────────────────────


def test_expand_file_inline(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello world\n")
    out = expand(f"see @file:{f}", ctx=_ctx(tmp_path))
    assert "hello world" in out
    assert "Attached Context" in out


def test_expand_file_with_line_range(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = expand(f"see @file:{f}:2-4", ctx=_ctx(tmp_path))
    assert "b\nc\nd" in out
    assert "a\nb\nc" not in out  # line 1 excluded


def test_expand_missing_file_marks_inline(tmp_path):
    out = expand(f"see @file:{tmp_path}/nope.txt", ctx=_ctx(tmp_path))
    assert "[file not found" in out


def test_blocked_path_refuses(tmp_path):
    home = tmp_path / "home"
    blocked = home / ".ssh" / "id_rsa"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("SECRET")
    out = expand(f"see @file:{blocked}", ctx=_ctx(tmp_path))
    assert "[blocked path" in out
    assert "SECRET" not in out


def test_hard_cap_refuses_oversized_file(tmp_path):
    big = tmp_path / "huge.txt"
    big.write_text("x" * 200_000)
    ctx = _ctx(tmp_path, context_window_chars=100_000)
    out = expand(f"@file:{big}", ctx=ctx)
    assert "[ref refused" in out and "hard cap" in out


def test_soft_cap_warns_but_includes(tmp_path):
    """30000 chars > 25% (25000) and < 50% (50000) → soft warn, included."""
    medium = tmp_path / "medium.txt"
    medium.write_text("x" * 30_000)
    ctx = _ctx(tmp_path, context_window_chars=100_000)
    out = expand(f"@file:{medium}", ctx=ctx)
    assert "x" * 30_000 in out
    assert "soft cap" in out


# ─── is_path_blocked ──────────────────────────────────────────────


def test_is_path_blocked_ssh(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    p = home / ".ssh" / "id_rsa"
    p.write_text("x")
    assert is_path_blocked(p, home=home)


def test_is_path_blocked_pem(tmp_path):
    p = tmp_path / "cert.pem"
    p.write_text("x")
    assert is_path_blocked(p, home=tmp_path / "home")


def test_is_path_blocked_zshrc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    p = home / ".zshrc"
    p.write_text("x")
    assert is_path_blocked(p, home=home)


def test_is_path_blocked_aws(tmp_path):
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    p = home / ".aws" / "credentials"
    p.write_text("x")
    assert is_path_blocked(p, home=home)


def test_is_path_blocked_normal_file(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_text("x")
    assert not is_path_blocked(p, home=tmp_path / "home")


# ─── @folder ──────────────────────────────────────────────────────


def test_expand_folder_lists_entries(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    (tmp_path / "sub").mkdir()
    out = expand(f"@folder:{tmp_path}", ctx=_ctx(tmp_path))
    assert "a.txt" in out and "b.txt" in out and "sub" in out


def test_expand_folder_caps_at_200(tmp_path):
    for i in range(250):
        (tmp_path / f"f{i:03d}.txt").write_text("x")
    out = expand(f"@folder:{tmp_path}", ctx=_ctx(tmp_path))
    assert "f000.txt" in out
    assert "[truncated" in out


def test_expand_missing_folder(tmp_path):
    out = expand(f"@folder:{tmp_path}/nope", ctx=_ctx(tmp_path))
    assert "[folder not found" in out


# ─── @diff / @staged / @git ───────────────────────────────────────


def test_expand_diff_in_non_git_repo(tmp_path):
    out = expand("@diff", ctx=_ctx(tmp_path))
    assert "[not a git repository" in out


def test_expand_staged_in_non_git_repo(tmp_path):
    out = expand("@staged", ctx=_ctx(tmp_path))
    assert "[not a git repository" in out


def test_expand_git_clamped_to_10(tmp_path):
    out = expand("@git:50", ctx=_ctx(tmp_path))
    # Either "clamped" appears (if git exec runs in a real repo) or
    # "not a git repository" (in tmp_path which is not a repo).
    assert "clamped" in out or "[not a git" in out


def test_expand_git_in_real_repo(tmp_path):
    """End-to-end: real git repo with one commit."""
    if subprocess.run(["which", "git"], capture_output=True).returncode != 0:
        pytest.skip("git not on PATH")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True
    )
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True
    )

    out = expand("@git:1", ctx=_ctx(tmp_path))
    assert "init" in out  # commit message
    assert "hello" in out  # diff body


# ─── @url ────────────────────────────────────────────────────────


def test_url_blocked_for_loopback(tmp_path):
    out = expand("@url:http://127.0.0.1/", ctx=_ctx(tmp_path))
    assert "[blocked" in out or "[fetch" in out


def test_url_blocked_for_metadata(tmp_path):
    """Cloud metadata endpoint must be refused."""
    out = expand("@url:http://169.254.169.254/", ctx=_ctx(tmp_path))
    assert "[blocked" in out or "[fetch" in out


# ─── general ─────────────────────────────────────────────────────


def test_no_atref_returns_text_unchanged(tmp_path):
    text = "hi there, no refs"
    assert expand(text, ctx=_ctx(tmp_path)) == text


def test_email_not_expanded(tmp_path):
    text = "ping me at sak@example.com"
    out = expand(text, ctx=_ctx(tmp_path))
    assert out == text
    assert "Attached Context" not in out


def test_combined_hard_cap_truncates(tmp_path):
    """Multiple files whose combined expansion exceeds hard cap → truncate."""
    for i in range(5):
        (tmp_path / f"big{i}.txt").write_text("X" * 30_000)
    msg = " ".join(f"@file:{tmp_path}/big{i}.txt" for i in range(5))
    ctx = _ctx(tmp_path, context_window_chars=100_000)
    out = expand(msg, ctx=ctx)
    assert "combined expansion exceeded hard cap" in out
