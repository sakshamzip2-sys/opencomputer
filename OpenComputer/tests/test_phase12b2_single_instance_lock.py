"""Phase 12b.2 — Sub-project B, Task B6.

Tests for atomic single_instance lock enforcement. Closes the Phase 14.C
stub: ``PluginManifest.single_instance`` was declared but never enforced.

The lock lives at ``~/.opencomputer/.locks/<plugin-id>.lock`` and holds a
PID written atomically via ``os.open(O_CREAT|O_EXCL|O_WRONLY)``.

Correctness requirements (adversarial-review flagged):
  - Use ``os.open(..., O_EXCL)``, NOT check-then-write.
  - Stale-lock steal uses ``os.rename`` as the atomicity gate — NEVER
    unlink-then-create.
  - Retry is bounded (3 attempts) — no infinite loops.
  - ``atexit`` cleanup only removes OUR lock (not someone else's).
  - ``PermissionError`` from ``os.kill`` is treated as "running" (safer
    default — don't steal a lock we can't prove is dead).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.loader import (
    PluginAPI,
    load_plugin,
)
from opencomputer.plugins.loader import (
    SingleInstanceError as _LoaderSIE,  # proves alias/re-export path
)
from opencomputer.plugins.registry import PluginRegistry
from plugin_sdk.core import PluginManifest

# ─── helpers ─────────────────────────────────────────────────────────


def _make_plugin(
    tmp_path: Path,
    plugin_id: str = "si-demo",
    *,
    single_instance: bool = True,
    entry: str = "plugin",
) -> PluginCandidate:
    """Write a minimal plugin on disk and return a PluginCandidate for it."""
    root = tmp_path / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text("{}")  # content irrelevant; we pass manifest directly
    (root / f"{entry}.py").write_text(
        "def register(api):\n    return None\n",
        encoding="utf-8",
    )
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        kind="tool",
        entry=entry,
        single_instance=single_instance,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )


def _fresh_api() -> PluginAPI:
    """A throwaway PluginAPI whose registries are local to the call."""
    return PluginAPI(
        tool_registry=_Noop(),
        hook_engine=_Noop(),
        provider_registry={},
        channel_registry={},
        injection_engine=_Noop(),
        doctor_contributions=[],
    )


class _Noop:
    def register(self, *_a, **_kw) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect OPENCOMPUTER_HOME so .locks/ lives in tmp_path."""
    home = tmp_path / ".opencomputer"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))
    # Clear any lingering atexit callbacks from prior loads in the same
    # process (the test runner shares the module cache).
    from opencomputer.plugins import loader as loader_mod

    if hasattr(loader_mod, "_OWNED_LOCKS"):
        loader_mod._OWNED_LOCKS.clear()
    return home


def _locks_dir(home: Path) -> Path:
    return home / ".locks"


# ─── Tests 1-12 ──────────────────────────────────────────────────────


def test_single_instance_false_no_lock_created(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """Plugins with single_instance=False must NOT touch .locks/."""
    cand = _make_plugin(tmp_path, "not-single", single_instance=False)
    result = load_plugin(cand, _fresh_api())
    assert result is not None
    # .locks/ either doesn't exist or is empty — definitely no lock file for this plugin.
    lock_path = _locks_dir(_isolated_home) / "not-single.lock"
    assert not lock_path.exists()


def test_single_instance_true_creates_lock_with_our_pid(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """After a successful load, lock file holds our PID."""
    cand = _make_plugin(tmp_path, "mine", single_instance=True)
    result = load_plugin(cand, _fresh_api())
    assert result is not None
    lock_path = _locks_dir(_isolated_home) / "mine.lock"
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())


def test_single_instance_second_load_same_process_raises(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """Two load_plugin calls with two PluginAPIs for the same single_instance manifest must fail the second."""
    from plugin_sdk import SingleInstanceError

    cand = _make_plugin(tmp_path, "twice", single_instance=True)
    # First acquire succeeds.
    first = load_plugin(cand, _fresh_api())
    assert first is not None
    # Second acquire against same lock path must raise.
    with pytest.raises(SingleInstanceError) as excinfo:
        load_plugin(cand, _fresh_api())
    assert "twice" in str(excinfo.value)


def test_single_instance_second_load_different_process_raises_when_original_running(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """If an existing lock file names a currently-running PID, new load raises."""
    from plugin_sdk import SingleInstanceError

    cand = _make_plugin(tmp_path, "alive", single_instance=True)
    locks = _locks_dir(_isolated_home)
    locks.mkdir(parents=True, exist_ok=True)
    # Write our own PID — we are definitely running.
    lock_path = locks / "alive.lock"
    lock_path.write_text(f"{os.getpid()}\n")

    with pytest.raises(SingleInstanceError) as excinfo:
        load_plugin(cand, _fresh_api())
    assert f"PID {os.getpid()}" in str(excinfo.value) or str(os.getpid()) in str(
        excinfo.value
    )


def test_single_instance_steals_stale_lock_atomically(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """A lock file pointing at a dead PID should be stolen; load succeeds."""
    cand = _make_plugin(tmp_path, "steal", single_instance=True)
    locks = _locks_dir(_isolated_home)
    locks.mkdir(parents=True, exist_ok=True)
    # Write a PID that we'll force os.kill to reject as ProcessLookupError.
    lock_path = locks / "steal.lock"
    lock_path.write_text("99999\n")

    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 99999:
            raise ProcessLookupError(f"No such process: {pid}")
        real_kill(pid, sig)

    with patch("os.kill", fake_kill):
        result = load_plugin(cand, _fresh_api())
    assert result is not None
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())
    # .stale shrapnel should have been cleaned up.
    stale = lock_path.with_suffix(".lock.stale")
    assert not stale.exists()


def test_single_instance_atexit_removes_lock(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """The atexit hook should delete our lock file on simulated process exit."""
    from opencomputer.plugins import loader as loader_mod

    cand = _make_plugin(tmp_path, "ateexit", single_instance=True)
    result = load_plugin(cand, _fresh_api())
    assert result is not None
    lock_path = _locks_dir(_isolated_home) / "ateexit.lock"
    assert lock_path.exists()
    # Directly invoke the cleanup for this lock
    loader_mod._release_owned_lock(lock_path)
    assert not lock_path.exists()


def test_single_instance_error_is_public_sdk_export() -> None:
    """SingleInstanceError must be importable from plugin_sdk and in __all__."""
    import plugin_sdk as sdk
    from plugin_sdk import SingleInstanceError

    assert "SingleInstanceError" in sdk.__all__
    # And it must be a RuntimeError subclass.
    assert issubclass(SingleInstanceError, RuntimeError)
    # The loader's name should be the same class (no duplicate defn).
    assert SingleInstanceError is _LoaderSIE


def test_single_instance_load_all_continues_on_one_failure(
    tmp_path: Path, _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """load_all must log WARNING and keep loading other plugins."""
    # Three plugins; middle one is single_instance with a pre-existing lock.
    a = _make_plugin(tmp_path, "aaa", single_instance=False)
    b = _make_plugin(tmp_path, "bbb", single_instance=True)
    c = _make_plugin(tmp_path, "ccc", single_instance=False)

    # Pre-populate a live lock for "bbb"
    locks = _locks_dir(_isolated_home)
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "bbb.lock").write_text(f"{os.getpid()}\n")

    from opencomputer.plugins import registry as registry_module

    monkeypatch.setattr(registry_module, "discover", lambda _p: [a, b, c])

    reg = PluginRegistry()
    import logging

    with caplog.at_level(logging.WARNING, logger="opencomputer.plugins.registry"):
        reg.load_all([Path("/fake")])
    loaded_ids = {lp.candidate.manifest.id for lp in reg.loaded}
    assert loaded_ids == {"aaa", "ccc"}
    assert any("bbb" in rec.message for rec in caplog.records), (
        f"expected WARNING mentioning 'bbb' but got: {[r.message for r in caplog.records]}"
    )


def test_single_instance_lock_dir_created_on_demand(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """If .locks/ does not exist, load should create it."""
    locks = _locks_dir(_isolated_home)
    assert not locks.exists()
    cand = _make_plugin(tmp_path, "auto-mkdir", single_instance=True)
    result = load_plugin(cand, _fresh_api())
    assert result is not None
    assert locks.exists() and locks.is_dir()
    assert (locks / "auto-mkdir.lock").exists()


def test_steal_lock_retries_bounded(
    tmp_path: Path, _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.rename keeps failing, steal gives up after 3 attempts — no infinite loop."""
    from plugin_sdk import SingleInstanceError

    cand = _make_plugin(tmp_path, "retry-bounded", single_instance=True)
    locks = _locks_dir(_isolated_home)
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / "retry-bounded.lock"
    lock_path.write_text("99999\n")

    # Dead PID path triggers steal.
    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 99999:
            raise ProcessLookupError(f"No such process: {pid}")
        real_kill(pid, sig)

    # os.rename always fails → every steal attempt falls over.
    real_rename = os.rename
    call_count = {"n": 0}

    def flaky_rename(src, dst) -> None:
        call_count["n"] += 1
        raise OSError("simulated rename failure")

    with patch("os.kill", fake_kill), patch("os.rename", flaky_rename):
        with pytest.raises(SingleInstanceError):
            load_plugin(cand, _fresh_api())
    # Should have tried at most 3 steal attempts (bounded).
    assert call_count["n"] <= 3, (
        f"steal retry is not bounded: os.rename called {call_count['n']} times"
    )
    assert call_count["n"] >= 1, "should have attempted at least once"

    # Sanity: ensure restoring monkeypatches didn't break real os.rename
    assert real_rename is os.rename


def test_concurrent_acquire_only_one_wins(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """Five threads race for the same lock; exactly one succeeds."""
    from plugin_sdk import SingleInstanceError

    cand = _make_plugin(tmp_path, "race", single_instance=True)
    barrier = threading.Barrier(5)
    successes: list[bool] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            r = load_plugin(cand, _fresh_api())
            if r is not None:
                with lock:
                    successes.append(True)
        except SingleInstanceError as e:  # expected for 4 of 5
            with lock:
                errors.append(e)
        except Exception as e:  # pragma: no cover — any other error is a real bug
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(successes) == 1, (
        f"expected exactly one winner, got {len(successes)} wins "
        f"and {len(errors)} errors: {errors!r}"
    )
    assert len(errors) == 4
    assert all(isinstance(e, SingleInstanceError) for e in errors), (
        f"non-SingleInstanceError surfaced: {errors!r}"
    )


def test_permission_error_on_os_kill_treated_as_running(
    tmp_path: Path, _isolated_home: Path
) -> None:
    """os.kill raising PermissionError -> 'running but not ours' -> raise."""
    from plugin_sdk import SingleInstanceError

    cand = _make_plugin(tmp_path, "privileged", single_instance=True)
    locks = _locks_dir(_isolated_home)
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "privileged.lock").write_text("1\n")  # PID 1 = init on *nix

    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError("not allowed to signal pid 1")

    with patch("os.kill", fake_kill):
        with pytest.raises(SingleInstanceError):
            load_plugin(cand, _fresh_api())
