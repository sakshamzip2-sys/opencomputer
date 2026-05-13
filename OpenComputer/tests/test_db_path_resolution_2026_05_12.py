"""Regression tests for the 2026-05-12 docs-audit fixes.

The docs audit (against an in-flight MEMORY-DB-ONBOARDING.md) surfaced
two real code bugs alongside the doc drift:

1. ``extensions/api-server/adapter.py`` hardcoded the wrong sessions.db
   path. It read ``OPENCOMPUTER_PROFILE`` (an env var that is never
   set — ``oc -p <name>`` sets ``OPENCOMPUTER_HOME``, not
   ``OPENCOMPUTER_PROFILE``) and built ``~/.opencomputer/<profile>/
   sessions.db`` — a path scheme that matches NEITHER the default
   profile (``~/.opencomputer/sessions.db``) NOR named profiles
   (``~/.opencomputer/profiles/<name>/sessions.db``).

2. ``opencomputer/kanban/db.py`` ``kanban_home()`` documented itself
   as walking up to the cross-profile root, but actually returned
   ``_home()`` verbatim. With ``OPENCOMPUTER_HOME=~/.opencomputer/
   profiles/<name>``, this silently forked the kanban board off the
   dispatcher's view of it — breaking the cross-profile coordination
   contract that ``oc -p worker chat`` depends on.

These tests fail against the pre-fix code and pass after the fix. The
test names spell out the regression so a future bisect points right
at the audit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────
# (1) api-server adapter — sessions.db path resolution
# ────────────────────────────────────────────────────────────────────


def _load_adapter_module():
    """Load extensions/api-server/adapter.py by path (hyphenated dir)."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "api-server"
        / "adapter.py"
    )
    key = "api_server_adapter_for_path_tests"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def adapter_mod():
    return _load_adapter_module()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin OPENCOMPUTER_HOME_ROOT + Path.home() to a tmpdir.

    The adapter resolver consults both ``OPENCOMPUTER_HOME_ROOT`` (test
    override) and ``Path.home() / ".opencomputer"`` (production
    default). Pinning both ensures tests work regardless of the host
    user's real home layout.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_PROFILE", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_default_profile_sessions_db_at_root(adapter_mod, fake_home):
    """Default profile's sessions.db lives at <root>/sessions.db.

    Pre-fix this resolved to ``<root>/default/sessions.db`` (wrong).
    """
    db_path = adapter_mod._resolve_sessions_db_path()
    assert db_path == fake_home / "sessions.db"


def test_default_profile_explicit_argument(adapter_mod, fake_home):
    """Explicit ``profile='default'`` matches the no-arg behavior."""
    db_path = adapter_mod._resolve_sessions_db_path("default")
    assert db_path == fake_home / "sessions.db"


def test_named_profile_via_explicit_argument(adapter_mod, fake_home):
    """Named profile resolves to <root>/profiles/<name>/sessions.db.

    Pre-fix this resolved to ``<root>/<name>/sessions.db`` (wrong —
    missing the ``profiles/`` segment that
    ``opencomputer.profiles.get_profile_dir`` produces).
    """
    db_path = adapter_mod._resolve_sessions_db_path("work")
    assert db_path == fake_home / "profiles" / "work" / "sessions.db"


def test_named_profile_via_opencomputer_home_env(
    adapter_mod, fake_home, monkeypatch
):
    """``oc -p <name>`` sets OPENCOMPUTER_HOME — the adapter must honor it.

    Pre-fix the adapter ignored OPENCOMPUTER_HOME entirely and built a
    bespoke wrong path from a never-set ``OPENCOMPUTER_PROFILE``.
    """
    profile_home = fake_home / "profiles" / "coder"
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))
    db_path = adapter_mod._resolve_sessions_db_path()
    assert db_path == profile_home / "sessions.db"


def test_invalid_profile_name_falls_through(adapter_mod, fake_home):
    """Path-traversal-y or pattern-violating names get ignored silently.

    The adapter must never raise for a bad ``X-OC-Profile`` header — the
    request profile resolver upstream already silently drops malformed
    headers, so the path resolver mirrors that policy.
    """
    db_path = adapter_mod._resolve_sessions_db_path("../etc/passwd")
    # Fell through to default-profile resolution; no traversal occurred.
    assert db_path == fake_home / "sessions.db"

    db_path = adapter_mod._resolve_sessions_db_path("UPPER_CASE")
    assert db_path == fake_home / "sessions.db"

    db_path = adapter_mod._resolve_sessions_db_path("")
    assert db_path == fake_home / "sessions.db"


def test_explicit_profile_arg_overrides_opencomputer_home(
    adapter_mod, fake_home, monkeypatch
):
    """When both an explicit profile AND OPENCOMPUTER_HOME are set,
    the explicit arg wins (per the docstring's priority order)."""
    monkeypatch.setenv(
        "OPENCOMPUTER_HOME", str(fake_home / "profiles" / "coder")
    )
    db_path = adapter_mod._resolve_sessions_db_path("work")
    # explicit "work" wins, not "coder" from the env
    assert db_path == fake_home / "profiles" / "work" / "sessions.db"


def test_session_count_helpers_accept_profile_argument(
    adapter_mod, fake_home, monkeypatch
):
    """``_count_active_sessions`` + ``_count_total_sessions`` take an
    optional profile arg. ``None`` exercises default-path resolution.

    The actual SQLite query runs only if the DB exists, so this test
    confirms the API shape without needing a real DB on disk.
    """
    assert adapter_mod._count_active_sessions(None) is None
    assert adapter_mod._count_total_sessions(None) is None
    # Named profile with no DB on disk: still None, never raises.
    assert adapter_mod._count_active_sessions("work") is None
    assert adapter_mod._count_total_sessions("work") is None


# ────────────────────────────────────────────────────────────────────
# (2) kanban_home() — walk-up for named profiles
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def kanban_db():
    """Import the kanban db module fresh — it caches the path lookups."""
    import importlib

    import opencomputer.kanban.db as db

    importlib.reload(db)
    return db


@pytest.fixture
def kanban_env(tmp_path, monkeypatch):
    """Clean kanban env for path tests."""
    monkeypatch.delenv("OC_KANBAN_HOME", raising=False)
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_HOME_ROOT", raising=False)
    return tmp_path


def test_kanban_home_default_profile_returns_root(
    kanban_db, kanban_env, monkeypatch
):
    """Default profile: _home() = <root> → kanban_home() returns <root>.

    Default profile lives at the root, so the parent of ``_home()`` is
    NOT named "profiles" — the walk-up does not trigger.
    """
    root = kanban_env
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(root))
    assert kanban_db.kanban_home() == root


def test_kanban_home_named_profile_walks_up_to_root(
    kanban_db, kanban_env, monkeypatch
):
    """Named profile: _home() = <root>/profiles/<name> → kanban_home() walks up.

    This is the regression. Pre-fix, kanban_home() returned
    ``_home()`` verbatim, so the kanban board silently forked per
    profile and broke dispatcher/worker coordination.
    """
    root = kanban_env
    named_home = root / "profiles" / "worker"
    named_home.mkdir(parents=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(named_home))
    assert kanban_db.kanban_home() == root


def test_kanban_home_dispatcher_and_worker_converge(
    kanban_db, kanban_env, monkeypatch
):
    """Dispatcher (default) + worker (named) MUST resolve the same board file.

    This is the cross-profile coordination contract. Without it,
    ``oc -p worker chat`` orphans every task the dispatcher claimed.
    """
    root = kanban_env

    # Dispatcher runs on the default profile.
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(root))
    dispatcher_kanban = kanban_db.kanban_home()

    # Worker runs on a named profile.
    named_home = root / "profiles" / "worker"
    named_home.mkdir(parents=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(named_home))
    worker_kanban = kanban_db.kanban_home()

    assert dispatcher_kanban == worker_kanban == root


def test_kanban_home_docker_style_deployment(
    kanban_db, kanban_env, monkeypatch
):
    """Docker / custom: OPENCOMPUTER_HOME=/opt/oc (outside ~/.opencomputer)
    resolves to /opt/oc directly (no walk-up — parent isn't "profiles").
    """
    docker_root = kanban_env / "opt-oc"
    docker_root.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(docker_root))
    assert kanban_db.kanban_home() == docker_root


def test_kanban_home_docker_named_profile_walks_up(
    kanban_db, kanban_env, monkeypatch
):
    """Docker + named profile: /opt/oc/profiles/worker → /opt/oc."""
    docker_root = kanban_env / "opt-oc"
    named_home = docker_root / "profiles" / "worker"
    named_home.mkdir(parents=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(named_home))
    assert kanban_db.kanban_home() == docker_root


def test_oc_kanban_home_override_bypasses_walk_up(
    kanban_db, kanban_env, monkeypatch
):
    """Explicit OC_KANBAN_HOME wins — even from inside a named profile."""
    root = kanban_env
    named_home = root / "profiles" / "worker"
    named_home.mkdir(parents=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(named_home))

    override = kanban_env / "custom-kanban-root"
    override.mkdir()
    monkeypatch.setenv("OC_KANBAN_HOME", str(override))

    assert kanban_db.kanban_home() == override
