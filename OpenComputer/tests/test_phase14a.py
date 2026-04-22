"""Phase 14.A — per-profile directory + pre-import -p flag routing."""

from __future__ import annotations

from pathlib import Path


class TestProfileValidation:
    def test_default_profile_lives_at_home_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
        from opencomputer.agent.config import _home

        assert _home() == tmp_path

    def test_validate_profile_name_accepts_valid(self):
        from opencomputer.profiles import validate_profile_name

        validate_profile_name("coder")
        validate_profile_name("stocks_v2")
        validate_profile_name("side-hustle")
        validate_profile_name("my123")

    def test_validate_profile_name_rejects_uppercase(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("UPPER")

    def test_validate_profile_name_rejects_spaces(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("has space")

    def test_validate_profile_name_rejects_empty(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("")

    def test_validate_profile_name_rejects_dots(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("dot.profile")

    def test_validate_profile_name_rejects_reserved(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        for name in ["default", "presets", "wrappers", "plugins", "profiles", "skills"]:
            with pytest.raises(ProfileNameError):
                validate_profile_name(name)

    def test_get_profile_dir_default_returns_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import get_profile_dir

        assert get_profile_dir(None) == tmp_path
        assert get_profile_dir("default") == tmp_path

    def test_get_profile_dir_named(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import get_profile_dir

        assert get_profile_dir("coder") == tmp_path / "profiles" / "coder"

    def test_list_profiles_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import list_profiles

        assert list_profiles() == []

    def test_list_profiles_shows_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "profiles" / "stocks").mkdir(parents=True)
        from opencomputer.profiles import list_profiles

        assert list_profiles() == ["coder", "stocks"]

    def test_active_profile_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import read_active_profile, write_active_profile

        assert read_active_profile() is None
        write_active_profile("coder")
        assert read_active_profile() == "coder"
        write_active_profile(None)
        assert read_active_profile() is None

    def test_write_active_profile_default_clears(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import read_active_profile, write_active_profile

        write_active_profile("coder")
        write_active_profile("default")  # clears
        assert read_active_profile() is None


class TestProfileFlagRouting:
    """Tests for ``_apply_profile_override``.

    Phase 14.A originally ran this function as a module-level side effect
    at import time, so tests reloaded ``opencomputer.cli`` to re-run it.
    After the Phase 14-followup refactor, the function runs inside
    ``main()`` and is a normal callable — tests just invoke it directly.
    """

    def _run_override(self):
        from opencomputer.cli import _apply_profile_override

        _apply_profile_override()

    def test_p_flag_sets_opencomputer_home(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])
        self._run_override()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_long_form_profile_flag(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile=stocks", "chat"])
        self._run_override()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "stocks")

    def test_long_form_profile_flag_spaced(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile", "stocks", "chat"])
        self._run_override()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "stocks")

    def test_sticky_active_profile_applied_when_no_flag(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        monkeypatch.setattr(sys, "argv", ["opencomputer", "chat"])
        self._run_override()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_flag_beats_sticky(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "personal", "chat"])
        self._run_override()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "personal")

    def test_flag_stripped_from_argv(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat", "--plan"])
        self._run_override()
        assert sys.argv == ["opencomputer", "chat", "--plan"]

    def test_invalid_profile_name_falls_back_to_default(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "BAD NAME", "chat"])
        self._run_override()
        import os

        # Bad name = fallback to default = no OPENCOMPUTER_HOME set (or unchanged)
        assert "OPENCOMPUTER_HOME" not in os.environ or os.environ["OPENCOMPUTER_HOME"] == str(
            tmp_path
        )

    def test_p_flag_missing_value_strips_flag(self, tmp_path, monkeypatch):
        """Issue 2 regression: -p as last arg must strip flag, not leak to Typer."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p"])
        self._run_override()
        import os

        assert "-p" not in sys.argv
        assert "OPENCOMPUTER_HOME" not in os.environ

    def test_explicit_flag_beats_parent_env_var(self, tmp_path, monkeypatch):
        """Issue 5 regression: -p flag must override OPENCOMPUTER_HOME pre-set by parent."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        # Simulate parent process having OPENCOMPUTER_HOME set to something unrelated
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "some-other-place"))
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])
        self._run_override()
        import os

        # Flag must win, even though OPENCOMPUTER_HOME was pre-set
        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_profile_empty_value_is_treated_as_default(self, tmp_path, monkeypatch):
        """Issue 3 regression: --profile= (empty value) must not leak as falsy profile."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile=", "chat"])
        self._run_override()
        import os

        # Empty value → fallback to default → no OPENCOMPUTER_HOME set
        assert "OPENCOMPUTER_HOME" not in os.environ


class TestProfileRoutingLazyInvariant:
    """Guard the invariant the Phase 14-followup refactor depends on.

    The pre-import ``_apply_profile_override()`` block was removed from
    the top of ``opencomputer/cli.py`` and moved into ``main()``. That
    move is only safe as long as NO module captures ``OPENCOMPUTER_HOME``
    at import time (which would freeze a stale path before ``main()``
    ever runs). If someone ever writes::

        HOME = _home()                    # at module top
        DEFAULT_DB_PATH = HOME / "sessions.db"
        class Tool: config_path = _home() / "tool.yaml"   # class-body

    the refactor silently breaks: ``opencomputer -p coder`` sets the
    env var correctly inside ``main()`` but the frozen constant still
    points at ``~/.opencomputer/``. These two tests catch that pattern
    — the static one at write-time, the runtime one end-to-end.
    """

    def test_no_module_level_home_capture(self):
        """Static AST scan — no module-top assignment calls ``_home()``.

        Scans every .py file under ``opencomputer/`` and asserts no
        *module-level* assignment statement has ``_home()`` or
        ``environ[...OPENCOMPUTER_HOME...]`` on the RHS. Assignments
        inside functions, class bodies, and ``default_factory=lambda:``
        closures are fine — this deliberately only catches code that
        runs at import.
        """
        import ast

        pkg_root = Path(__file__).resolve().parent.parent / "opencomputer"
        offenders: list[str] = []
        for py_file in pkg_root.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            # tree.body only contains module-level statements — exactly
            # what we want; nested assignments are skipped by design.
            for node in tree.body:
                if not isinstance(node, ast.Assign | ast.AnnAssign):
                    continue
                rhs = node.value
                if rhs is None:
                    continue  # bare annotation with no value
                rhs_src = ast.unparse(rhs)
                if (
                    "_home()" in rhs_src
                    or 'environ.get("OPENCOMPUTER_HOME"' in rhs_src
                    or 'environ["OPENCOMPUTER_HOME"]' in rhs_src
                ):
                    rel = py_file.relative_to(pkg_root.parent)
                    offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")

        assert not offenders, (
            "Module-level code captures OPENCOMPUTER_HOME / _home() at import. "
            "This breaks profile routing: `opencomputer -p coder` sets the env "
            "inside main(), but your frozen constant stays pointing at the old "
            "HOME. Use `field(default_factory=lambda: _home() / ...)` on a "
            "dataclass, or call `_home()` inside a function/method instead.\n"
            "Offenders:\n  " + "\n  ".join(offenders)
        )

    def test_override_after_full_import_updates_paths(self, tmp_path, monkeypatch):
        """End-to-end: import everything first, THEN route, verify paths.

        Simulates the real main()-time ordering. If any module silently
        froze HOME during imports (something the AST check missed), the
        post-override ``default_config()`` produces stale paths and
        this assertion fires.
        """
        import importlib
        import sys

        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))

        # Force a fresh import of cli.py (which pulls in default_config,
        # agent/state, agent/memory, plugins/*, tools/*, plugin_sdk/*).
        # This is the WORST CASE for the lazy-invariant: every module is
        # loaded BEFORE _apply_profile_override() ever runs.
        import opencomputer.cli as cli_module

        importlib.reload(cli_module)

        # Now route to a named profile (as main() would).
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])
        cli_module._apply_profile_override()

        # Every path-bearing field in default_config() must reflect the
        # new HOME. If any is still pointing at tmp_path (the root) or
        # ~/.opencomputer/, some module froze it at import time.
        from opencomputer.agent.config import default_config
        from opencomputer.agent.config_store import config_file_path

        cfg = default_config()
        expected_home = tmp_path / "profiles" / "coder"
        assert cfg.home == expected_home, (
            f"cfg.home frozen at import — got {cfg.home}, want {expected_home}"
        )
        assert cfg.session.db_path == expected_home / "sessions.db"
        assert cfg.memory.declarative_path == expected_home / "MEMORY.md"
        assert cfg.memory.user_path == expected_home / "USER.md"
        assert cfg.memory.skills_path == expected_home / "skills"
        assert config_file_path() == expected_home / "config.yaml"
