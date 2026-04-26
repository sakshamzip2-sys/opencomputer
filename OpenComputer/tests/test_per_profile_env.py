"""Per-profile credential isolation (Round 4 Item 5).

Closes the gap where credentials lived only in `~/.opencomputer/.env`
(global). Now per-profile `.env` files take precedence, with global
as fallback so existing single-profile users keep working unchanged.

Resolution order (first hit per key wins):
  1. <OPENCOMPUTER_HOME>/profiles/<name>/.env  (when profile is named)
  2. <OPENCOMPUTER_HOME>/.env                  (global fallback)
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_oc_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "oc-home"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))


def _write_env(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_loads_global_env_for_default_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default profile sees global ~/.opencomputer/.env."""
    from opencomputer.security.env_loader import load_for_profile

    home = Path(monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False) or
                tmp_path / "oc-home")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))
    home.mkdir(exist_ok=True)
    _write_env(home / ".env", "ANTHROPIC_API_KEY=sk-global\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_for_profile(None, apply_to_environ=False)
    assert loaded == {"ANTHROPIC_API_KEY": "sk-global"}


def test_profile_specific_env_takes_precedence_over_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same key in both files → profile wins."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    _write_env(home / ".env", "ANTHROPIC_API_KEY=sk-global\n")
    _write_env(
        home / "profiles" / "work" / ".env",
        "ANTHROPIC_API_KEY=sk-work\n",
    )

    loaded = load_for_profile("work", apply_to_environ=False)
    assert loaded["ANTHROPIC_API_KEY"] == "sk-work", (
        "profile-local must override global"
    )


def test_profile_specific_falls_back_to_global_for_missing_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key only in global is still visible to a named profile."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    _write_env(
        home / ".env",
        "ANTHROPIC_API_KEY=sk-global\nGITHUB_TOKEN=gh-global\n",
    )
    _write_env(
        home / "profiles" / "work" / ".env",
        "ANTHROPIC_API_KEY=sk-work\n",  # only one key set
    )

    loaded = load_for_profile("work", apply_to_environ=False)
    assert loaded["ANTHROPIC_API_KEY"] == "sk-work"
    assert loaded["GITHUB_TOKEN"] == "gh-global", (
        "profile-local must FALL BACK to global for unset keys"
    )


def test_default_profile_skips_profiles_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default profile never reads ``profiles/default/.env`` even if
    it exists — default IS the root."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    _write_env(home / ".env", "K=global\n")
    _write_env(home / "profiles" / "default" / ".env", "K=should-be-ignored\n")

    loaded = load_for_profile("default", apply_to_environ=False)
    assert loaded == {"K": "global"}, "default profile must use global only"


def test_apply_to_environ_does_not_clobber_shell_set_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shell-exported vars beat file-loaded ones (dotenv convention)."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    _write_env(home / ".env", "ANTHROPIC_API_KEY=sk-from-file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")

    load_for_profile(None, apply_to_environ=True)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-shell", (
        "shell-set values must win over file-loaded ones"
    )


def test_apply_to_environ_populates_unset_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Apply mode actually puts new keys into os.environ."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    _write_env(home / ".env", "BRAND_NEW_KEY=hello\n")
    monkeypatch.delenv("BRAND_NEW_KEY", raising=False)

    load_for_profile(None, apply_to_environ=True)
    assert os.environ.get("BRAND_NEW_KEY") == "hello"


def test_returns_empty_when_no_files_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Brand-new install has neither global nor profile .env → empty dict,
    no exception."""
    from opencomputer.security.env_loader import load_for_profile

    loaded = load_for_profile("work", apply_to_environ=False)
    assert loaded == {}


def test_loose_perms_on_global_env_does_not_crash_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A loose-perm .env (likely from manual chmod) must not crash CLI
    startup — env_loader fails closed; load_for_profile should swallow
    the exception, log at debug, and proceed with whatever else loaded."""
    import os

    from opencomputer.security.env_loader import load_for_profile

    home = Path(os.environ["OPENCOMPUTER_HOME"])
    bad = home / ".env"
    bad.write_text("K=loose\n", encoding="utf-8")
    bad.chmod(0o644)  # group/other readable — fail-closed in env_loader

    with caplog.at_level("DEBUG"):
        loaded = load_for_profile(None, apply_to_environ=False)

    assert loaded == {}, "loose-perm file must not be loaded"
    # Test passes as long as no exception escaped.
