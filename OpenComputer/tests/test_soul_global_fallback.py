"""D4: Global SOUL.md fallback (Hermes v2 HERMES_HOME parity).

Per-profile SOUL.md takes precedence; if missing or whitespace-only,
``read_soul`` falls back to the global ``~/.opencomputer/SOUL.md``
(or ``$OPENCOMPUTER_HOME/SOUL.md``). This restores HERMES_HOME-style
identity sharing across profiles for users who want one voice
everywhere.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import MemoryManager


def _make_memory(profile_home: Path, global_home: Path) -> MemoryManager:
    return MemoryManager(
        declarative_path=profile_home / "MEMORY.md",
        skills_path=profile_home / "skills",
        soul_path=profile_home / "SOUL.md",
        global_soul_path=global_home / "SOUL.md",
    )


def test_per_profile_soul_wins_when_present(tmp_path: Path):
    profile = tmp_path / "p1"
    profile.mkdir()
    glob = tmp_path / "global"
    glob.mkdir()

    (profile / "SOUL.md").write_text("# Profile soul\nProfile voice\n", encoding="utf-8")
    (glob / "SOUL.md").write_text("# Global soul\nGlobal voice\n", encoding="utf-8")

    mem = _make_memory(profile, glob)
    out = mem.read_soul()
    assert "Profile voice" in out
    assert "Global voice" not in out


def test_global_soul_fallback_when_profile_missing(tmp_path: Path):
    profile = tmp_path / "p1"
    profile.mkdir()
    glob = tmp_path / "global"
    glob.mkdir()
    (glob / "SOUL.md").write_text("# Global soul\nShared voice\n", encoding="utf-8")

    mem = _make_memory(profile, glob)
    out = mem.read_soul()
    assert "Shared voice" in out


def test_global_soul_fallback_when_profile_whitespace_only(tmp_path: Path):
    profile = tmp_path / "p1"
    profile.mkdir()
    glob = tmp_path / "global"
    glob.mkdir()
    (profile / "SOUL.md").write_text("   \n\n\t  \n", encoding="utf-8")
    (glob / "SOUL.md").write_text("# Global soul\nShared voice\n", encoding="utf-8")

    mem = _make_memory(profile, glob)
    out = mem.read_soul()
    assert "Shared voice" in out


def test_empty_string_when_neither_exists(tmp_path: Path):
    profile = tmp_path / "p1"
    profile.mkdir()
    glob = tmp_path / "global"
    glob.mkdir()

    mem = _make_memory(profile, glob)
    assert mem.read_soul() == ""


def test_global_soul_whitespace_returns_empty(tmp_path: Path):
    profile = tmp_path / "p1"
    profile.mkdir()
    glob = tmp_path / "global"
    glob.mkdir()
    (glob / "SOUL.md").write_text("    \n", encoding="utf-8")

    mem = _make_memory(profile, glob)
    assert mem.read_soul() == ""


def test_default_global_soul_path_uses_opencomputer_home(tmp_path: Path, monkeypatch):  # noqa: N802
    """No explicit global_soul_path → derived from OPENCOMPUTER_HOME."""
    profile = tmp_path / "alt-profile"
    profile.mkdir()
    fake_home = tmp_path / "alt-home"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    (fake_home / "SOUL.md").write_text("# From env home\nVoice\n", encoding="utf-8")

    mem = MemoryManager(
        declarative_path=profile / "MEMORY.md",
        skills_path=profile / "skills",
        soul_path=profile / "SOUL.md",
        # global_soul_path NOT provided — should derive from env.
    )
    assert "From env home" in mem.read_soul()
