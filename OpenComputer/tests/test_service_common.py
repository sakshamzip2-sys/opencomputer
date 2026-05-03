"""Common helpers shared across all service backends."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_resolve_executable_finds_oc_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service._common import resolve_executable

    monkeypatch.delenv("OC_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/oc" if name == "oc" else None,
    )
    assert resolve_executable() == "/usr/local/bin/oc"


def test_resolve_executable_falls_back_to_opencomputer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service._common import resolve_executable

    monkeypatch.delenv("OC_EXECUTABLE", raising=False)

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/opencomputer" if name == "opencomputer" else None

    monkeypatch.setattr("shutil.which", fake_which)
    assert resolve_executable() == "/usr/local/bin/opencomputer"


def test_resolve_executable_searches_fallbacks_when_path_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opencomputer.service import _common

    monkeypatch.delenv("OC_EXECUTABLE", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    fake_oc = tmp_path / "oc"
    fake_oc.write_text("#!/bin/sh\n")
    fake_oc.chmod(0o755)

    monkeypatch.setattr(_common, "_FALLBACK_PATHS", [fake_oc])
    assert _common.resolve_executable() == str(fake_oc)


def test_resolve_executable_raises_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _common

    monkeypatch.delenv("OC_EXECUTABLE", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_common, "_FALLBACK_PATHS", [])

    with pytest.raises(RuntimeError, match="could not find oc executable"):
        _common.resolve_executable()


def test_resolve_executable_honors_oc_executable_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opencomputer.service._common import resolve_executable

    fake_oc = tmp_path / "oc"
    fake_oc.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OC_EXECUTABLE", str(fake_oc))
    # shutil.which would still find something else, but the env override wins
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/oc")
    assert resolve_executable() == str(fake_oc)


def test_log_paths_returns_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import log_paths

    out, err = log_paths("default")
    assert out == tmp_path / ".opencomputer" / "default" / "logs" / "gateway.stdout.log"
    assert err == tmp_path / ".opencomputer" / "default" / "logs" / "gateway.stderr.log"
    assert out.parent.exists()  # logs dir created


def test_workdir_creates_profile_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import workdir

    wd = workdir("myprofile")
    assert wd == tmp_path / ".opencomputer" / "myprofile"
    assert wd.exists()


def test_workdir_rejects_path_traversal_in_profile_name() -> None:
    from opencomputer.service._common import workdir

    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("../../etc")
    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("foo/bar")
    with pytest.raises(ValueError, match="invalid profile name"):
        workdir("")


def test_workdir_accepts_dashes_dots_underscores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from opencomputer.service._common import workdir

    for ok in ("p1", "my-profile", "my_profile", "my.profile", "p"):
        wd = workdir(ok)
        assert wd.exists()


def test_tail_lines_returns_last_n(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    f = tmp_path / "log"
    f.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    out = tail_lines(f, 5)
    assert out == ["line 15", "line 16", "line 17", "line 18", "line 19"]


def test_tail_lines_handles_missing_file(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    out = tail_lines(tmp_path / "does-not-exist", 5)
    assert out == []


def test_tail_lines_handles_short_file(tmp_path: Path) -> None:
    from opencomputer.service._common import tail_lines

    f = tmp_path / "log"
    f.write_text("only\ntwo lines\n")
    out = tail_lines(f, 5)
    assert out == ["only", "two lines"]
