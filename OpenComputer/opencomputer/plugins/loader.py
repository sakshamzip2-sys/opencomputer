"""
Plugin loader — Phase 2 of the two-phase pattern.

Given a PluginCandidate (from discovery.py), lazily import the entry
module and call its register() function. Plugins register their tools,
channel adapters, provider adapters, and hooks with the core registries.

Plugins declare their entry module in plugin.json via the `entry` field
(e.g. `"entry": "src.plugin"`). We import that module — it must export
a `register(api)` function where `api` exposes the plugin-facing registries.
"""

from __future__ import annotations

import atexit
import importlib
import importlib.util
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.plugins.discovery import PluginCandidate
from plugin_sdk.core import SingleInstanceError

logger = logging.getLogger("opencomputer.plugins.loader")


# Common short names plugins use for their sibling files. Clearing these
# between plugin loads prevents two plugins (both with a top-level
# `provider.py`, say) from sharing the first-loaded module.
_PLUGIN_LOCAL_NAMES = ("provider", "adapter", "plugin", "handlers", "hooks")


def _clear_plugin_local_cache() -> None:
    for name in _PLUGIN_LOCAL_NAMES:
        sys.modules.pop(name, None)


# ─── single_instance lock (Phase 12b.2, Task B6) ──────────────────────

# Locks we acquired in THIS process. atexit iterates this and deletes
# only what we own — never a lock held by some other process. Guarded by
# _OWNED_LOCKS_LOCK so concurrent load_plugin calls don't race on the set.
_OWNED_LOCKS: set[Path] = set()
_OWNED_LOCKS_LOCK = threading.Lock()

# Bounded retry: if `os.rename` keeps failing during steal, give up after
# this many attempts rather than looping forever. Three is enough to
# survive a legitimate race; more than that means something is badly
# wrong and we should raise.
_STEAL_MAX_ATTEMPTS = 3


def _locks_dir() -> Path:
    """Return ``~/.opencomputer/.locks/`` (creating parent on demand).

    Uses the same ``_home()`` source as the rest of the config layer so
    OPENCOMPUTER_HOME overrides (tests, profile isolation) just work.
    """
    from opencomputer.agent.config import _home

    return _home() / ".locks"


def _pid_is_running(pid: int) -> bool:
    """Return True if the given PID is currently alive.

    Uses ``os.kill(pid, 0)`` — sends no actual signal but raises:
      - ``ProcessLookupError`` if the process does not exist (→ False).
      - ``PermissionError`` if the process exists but isn't ours; treat
        this as ALIVE (safer default — we can't prove it's dead, so we
        refuse to steal).
      - ``OSError`` on other failures — treat as alive for safety.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Running but we can't signal it — don't steal.
        return True
    except OSError:
        # Unknown kernel state — be conservative.
        return True
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    """Read the PID from an existing lock file. Returns None on any error."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _try_atomic_create(lock_path: Path) -> bool:
    """Attempt atomic creation with O_EXCL; write our PID.

    Returns True iff we won the race and own the lock. Returns False if
    the file already existed (caller must decide: steal or surrender).
    Any other OSError propagates.
    """
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)
    with _OWNED_LOCKS_LOCK:
        _OWNED_LOCKS.add(lock_path)
    return True


def _try_steal_stale(lock_path: Path, holder_pid: int) -> bool:
    """Atomically steal a stale lock.

    Rename the lock file to ``<lock_path>.stale``. Rename is atomic on
    POSIX, so exactly ONE concurrent stealer wins the rename; the losers
    get ``OSError`` and must restart the acquire loop. The winner then
    deletes the .stale file and returns True so the caller can retry
    ``O_EXCL`` creation.

    Returns False if the rename failed (another process beat us or the
    file no longer exists).
    """
    stale_path = lock_path.with_suffix(".lock.stale")
    try:
        os.rename(str(lock_path), str(stale_path))
    except OSError:
        # Someone else moved/deleted it, or rename failed — retry the
        # acquire loop from scratch.
        logger.debug(
            "steal rename failed for %s (holder pid=%s) — retrying",
            lock_path,
            holder_pid,
        )
        return False
    # We own the stale file now; clean it up.
    try:
        stale_path.unlink()
    except OSError:
        # Best-effort; .stale shrapnel won't block anyone.
        pass
    return True


def _acquire_single_instance_lock(plugin_id: str) -> Path:
    """Acquire the ``~/.opencomputer/.locks/<plugin-id>.lock`` lock.

    Returns the lock path on success. Raises SingleInstanceError if the
    lock is held by a running process OR if stale-steal hits the
    bounded-retry ceiling.
    """
    locks_dir = _locks_dir()
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{plugin_id}.lock"

    for attempt in range(_STEAL_MAX_ATTEMPTS):
        # Step 1: try atomic create.
        if _try_atomic_create(lock_path):
            return lock_path

        # Step 2: something exists. Read its PID.
        # Race guard: if holder just won O_EXCL but hasn't yet written its
        # PID, we'll see an empty file. Treating empty as "unparseable →
        # stale → steal" is the bug that lets two threads both win. Retry
        # the read a few times with microsleeps to let the writer catch up.
        # Only after repeated empties do we conclude the lock is truly
        # stale (prior process crashed between O_EXCL and write).
        holder = _read_lock_pid(lock_path)
        if holder is None:
            import time as _time

            for _ in range(10):
                _time.sleep(0.005)  # 50 ms total budget
                holder = _read_lock_pid(lock_path)
                if holder is not None:
                    break
        if holder is None:
            # Still empty/malformed after wait — genuinely stale.
            if _try_steal_stale(lock_path, -1):
                continue
            # Steal failed this attempt — loop.
            continue

        # Step 3: if the holder is running, we lose. Raise.
        if _pid_is_running(holder):
            raise SingleInstanceError(
                f"Plugin {plugin_id!r} already held by PID {holder}"
            )

        # Step 4: holder is dead → steal atomically.
        if _try_steal_stale(lock_path, holder):
            # Rename succeeded, file is gone, retry create on next iter.
            continue
        # Steal failed (another process got there first) — loop.

    raise SingleInstanceError(
        f"Plugin {plugin_id!r} — failed to acquire lock after "
        f"{_STEAL_MAX_ATTEMPTS} steal attempts"
    )


def _release_owned_lock(lock_path: Path) -> None:
    """Delete a lock file IFF we own it. Called by atexit + tests."""
    with _OWNED_LOCKS_LOCK:
        if lock_path not in _OWNED_LOCKS:
            return
        _OWNED_LOCKS.discard(lock_path)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:  # pragma: no cover — best-effort cleanup
        logger.debug("failed to unlink owned lock %s: %s", lock_path, e)


def _atexit_release_all() -> None:
    """Clean up every lock we acquired in this process."""
    with _OWNED_LOCKS_LOCK:
        owned = list(_OWNED_LOCKS)
    for p in owned:
        _release_owned_lock(p)


# Register once per process.
atexit.register(_atexit_release_all)


@dataclass(slots=True)
class LoadedPlugin:
    """Record of an activated plugin."""

    candidate: PluginCandidate
    module: Any


class PluginAPI:
    """Passed to each plugin's register() — the narrow runtime surface."""

    def __init__(
        self,
        tool_registry: Any,
        hook_engine: Any,
        provider_registry: dict[str, Any],
        channel_registry: dict[str, Any],
        injection_engine: Any = None,
        doctor_contributions: list[Any] | None = None,
    ) -> None:
        self.tools = tool_registry
        self.hooks = hook_engine
        self.providers = provider_registry
        self.channels = channel_registry
        self.injection = injection_engine
        # Plugins append to this list via register_doctor_contribution. The core
        # doctor runs every registered contribution after the built-in checks.
        self.doctor_contributions = doctor_contributions if doctor_contributions is not None else []
        # At most one external memory provider can be active at a time
        # (Phase 10f.G). None = built-in memory only.
        self.memory_provider: Any = None

    def register_tool(self, tool: Any) -> None:
        self.tools.register(tool)

    def register_hook(self, spec: Any) -> None:
        self.hooks.register(spec)

    def register_provider(self, name: str, provider: Any) -> None:
        self.providers[name] = provider

    def register_channel(self, name: str, adapter: Any) -> None:
        self.channels[name] = adapter

    def register_injection_provider(self, provider: Any) -> None:
        """Register a DynamicInjectionProvider (plan mode, yolo mode, etc.)."""
        if self.injection is None:
            raise RuntimeError("Injection engine unavailable — plugin-SDK version mismatch?")
        self.injection.register(provider)

    def register_memory_provider(self, provider: Any) -> None:
        """Register an external MemoryProvider (Honcho, Mem0, etc.).

        Only ONE external provider may be active at a time. Registering a
        second one raises ValueError. The built-in MEMORY.md + USER.md
        + FTS5 baseline is always on and unaffected by this call.
        """
        from plugin_sdk.memory import MemoryProvider

        if not isinstance(provider, MemoryProvider):
            raise TypeError(
                f"register_memory_provider requires a MemoryProvider instance; "
                f"got {type(provider).__name__}"
            )
        if self.memory_provider is not None:
            existing_id = getattr(self.memory_provider, "provider_id", "<unknown>")
            raise ValueError(
                f"a memory provider is already registered: {existing_id!r} — "
                "only one external provider is allowed at a time"
            )
        self.memory_provider = provider

    def register_doctor_contribution(self, contribution: Any) -> None:
        """Register a HealthContribution — runs on `opencomputer doctor [--fix]`.

        Each contribution is an async (fix: bool) -> RepairResult callable
        wrapped in a HealthContribution(id, description, run). When the user
        passes --fix, the contribution is expected to repair in place.
        Source: openclaw DoctorHealthContribution.
        """
        self.doctor_contributions.append(contribution)


def load_plugin(candidate: PluginCandidate, api: PluginAPI) -> LoadedPlugin | None:
    """Import a candidate's entry module and call its register(api) function.

    Uses importlib.util.spec_from_file_location with a unique synthetic module
    name per plugin (based on plugin id). This avoids Python's module cache
    returning the same module for multiple plugins that happen to share an
    `entry` value (e.g. all three plugins use "plugin" as their entry).

    Also adds the plugin root to sys.path so the entry module's own sibling
    imports (e.g. `from adapter import X`) resolve correctly.

    If ``candidate.manifest.single_instance`` is True, acquires an atomic
    PID lock at ``~/.opencomputer/.locks/<plugin-id>.lock`` BEFORE running
    any plugin code. Raises :class:`SingleInstanceError` if the lock is
    held by another running process.
    """
    manifest = candidate.manifest
    entry = manifest.entry.strip()
    if not entry:
        logger.warning("plugin '%s' has no 'entry' field in manifest", manifest.id)
        return None

    # Single-instance enforcement (Task B6). Acquire BEFORE import so we
    # don't run plugin code twice in parallel profiles.
    if manifest.single_instance:
        _acquire_single_instance_lock(manifest.id)
        # Lock release is handled by the module-level atexit hook.

    plugin_root = candidate.root_dir.resolve()
    plugin_root_str = str(plugin_root)
    if plugin_root_str not in sys.path:
        sys.path.insert(0, plugin_root_str)

    entry_path = plugin_root / f"{entry}.py"
    if not entry_path.exists():
        logger.warning(
            "plugin '%s' entry file not found: %s (expected at %s)",
            manifest.id,
            entry,
            entry_path,
        )
        return None

    # Clear common sibling module names from sys.modules so this plugin sees
    # its OWN siblings (not another plugin's cached 'provider' or 'adapter').
    # Without this, two plugins that both have a top-level 'provider' module
    # would share the one that loaded first.
    _clear_plugin_local_cache()

    # Unique module name so sys.modules doesn't collide between plugins
    synthetic_name = f"_opencomputer_plugin_{manifest.id.replace('-', '_')}_{entry}"

    try:
        spec = importlib.util.spec_from_file_location(synthetic_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no spec for {entry_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[synthetic_name] = module
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to import plugin '%s' (entry=%s): %s", manifest.id, entry, e)
        return None

    register_fn = getattr(module, "register", None)
    if register_fn is None:
        logger.warning(
            "plugin '%s' has no register() function in entry module %s",
            manifest.id,
            entry,
        )
        return None

    try:
        register_fn(api)
    except Exception as e:  # noqa: BLE001
        logger.exception("plugin '%s' register() raised: %s", manifest.id, e)
        return None

    logger.info("loaded plugin '%s' v%s", manifest.id, manifest.version)
    return LoadedPlugin(candidate=candidate, module=module)


__all__ = [
    "PluginAPI",
    "LoadedPlugin",
    "load_plugin",
    "SingleInstanceError",
]
