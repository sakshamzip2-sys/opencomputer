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
from plugin_sdk.core import (
    VALID_ACTIVATION_SOURCES,
    PluginActivationSource,
    SingleInstanceError,
)

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


# ─── runtime contract validation (Task I.5) ───────────────────────────


@dataclass(slots=True)
class _RegistrationSnapshot:
    """Point-in-time view of registered items on a ``PluginAPI``.

    Used by ``_validate_runtime_contract`` to compute the delta produced
    by a single plugin's ``register(api)`` call. The snapshot is cheap:
    sets of tool/provider/channel/slash-command names, a count of hooks
    and a boolean for the currently-exclusive memory provider slot.

    Matches OpenClaw's loader-side contract check
    (``sources/openclaw/src/plugins/loader.ts``) which snapshots before
    and diffs after ``plugin.register()``.
    """

    tool_names: set[str]
    provider_names: set[str]
    channel_names: set[str]
    slash_names: set[str]
    hook_count: int
    memory_provider_present: bool


def _snapshot_registrations(api: PluginAPI) -> _RegistrationSnapshot:
    """Capture the currently-registered items on ``api`` for before/after diff.

    Duck-typed on purpose: tests routinely pass ``_Noop`` stubs for the
    tool registry and hook engine when they only care about the loader's
    lock/import paths. ``getattr`` with sensible defaults keeps the
    contract check a best-effort diagnostic that never breaks those
    stub-based tests.
    """
    names_iter = getattr(api.tools, "names", None)
    tool_names = set(names_iter()) if callable(names_iter) else set()
    hooks_dict = getattr(api.hooks, "_hooks", None)
    hook_count = (
        sum(len(specs) for specs in hooks_dict.values())
        if isinstance(hooks_dict, dict)
        else 0
    )
    return _RegistrationSnapshot(
        tool_names=tool_names,
        provider_names=set(api.providers.keys()),
        channel_names=set(api.channels.keys()),
        slash_names=set(api.slash_commands.keys()),
        hook_count=hook_count,
        memory_provider_present=(api.memory_provider is not None),
    )


def _validate_runtime_contract(
    manifest: Any,
    before: _RegistrationSnapshot,
    after: _RegistrationSnapshot,
) -> None:
    """Compare post-``register()`` deltas against manifest claims.

    Emits WARNINGs only — never raises, never blocks load. Matches
    OpenClaw's ``manifest.contracts`` field + loader-side validation:
    a plugin declaring ``kind=provider`` but registering zero providers
    is almost certainly a drift bug (refactored away but manifest not
    updated). Logging early means ``opencomputer doctor`` and CI smoke
    tests surface the drift before it blows up at dispatch time.

    The mapping is intentionally BROAD for ``kind=provider``: a memory
    provider also satisfies the claim (this matches the bundled
    ``memory-honcho`` plugin which declares ``kind=provider`` and
    registers via ``register_memory_provider``). ``kind=skill`` skips
    the check entirely — skill plugins contribute markdown files, not
    runtime registrations.

    Separately, if ``manifest.tool_names`` is a non-empty tuple, at
    least one newly-registered tool schema name must match (full set
    equality is enforced by a separate drift-guard test on bundled
    extensions; the loader only needs partial-match here so a plugin
    advertising multiple variants doesn't falsely warn on partial load).
    """
    kind = getattr(manifest, "kind", "mixed")
    plugin_id = getattr(manifest, "id", "<unknown>")

    # Compute the per-kind delta.
    new_tools = after.tool_names - before.tool_names
    new_providers = after.provider_names - before.provider_names
    new_channels = after.channel_names - before.channel_names
    new_slash = after.slash_names - before.slash_names
    added_hooks = after.hook_count - before.hook_count
    added_memory = (
        after.memory_provider_present and not before.memory_provider_present
    )

    def _warn(reason: str) -> None:
        # Wording deliberately matches the I.5 spec so downstream
        # log-scrapers can recognise the event. Don't change without
        # updating the I.5 tests.
        logger.warning(
            "Plugin %r declared kind=%r but registered no %s. "
            "Manifest claim may be wrong.",
            plugin_id,
            kind,
            reason,
        )

    # ── kind claim check ───────────────────────────────────────────
    if kind == "provider":
        # Broad: either an LLM provider or a memory provider counts.
        if not new_providers and not added_memory:
            _warn("provider")
    elif kind == "channel":
        if not new_channels:
            _warn("channel")
    elif kind == "tool":
        if not new_tools:
            _warn("tool")
    elif kind == "memory":
        # ``memory`` is not currently in the PluginKind literal, but
        # keep the branch so a future schema expansion Just Works.
        if not added_memory:
            _warn("memory")
    elif kind == "mixed" and (
        not new_tools
        and not new_providers
        and not new_channels
        and not new_slash
        and added_hooks == 0
        and not added_memory
    ):
        _warn("mixed")
    # kind == "skill": skill plugins typically register no runtime
    # items (they contribute markdown files via the skills directory).
    # Skip the check entirely.

    # ── tool_names claim check ─────────────────────────────────────
    declared_tool_names = getattr(manifest, "tool_names", ()) or ()
    if declared_tool_names and not any(
        name in new_tools for name in declared_tool_names
    ):
        logger.warning(
            "Plugin %r declared tool_names=%r but registered tools %r — "
            "at least one declared name must match a registered tool.",
            plugin_id,
            list(declared_tool_names),
            sorted(new_tools),
        )


# ─── provider config-schema validation (Task I.6) ─────────────────────


def _validate_provider_config(name: str, provider: Any) -> None:
    """Validate a provider's ``config`` against its declared ``config_schema``.

    Mirror of OpenClaw's ``normalizeRegisteredProvider``
    (``sources/openclaw/src/plugins/provider-validation.ts``) — catch bad
    config at registration instead of at first-use.

    Rules:
      - If ``provider`` is a class (not an instance), skip. The instance
        doesn't exist yet; construction errors surface at resolve time.
      - If the provider's type has ``config_schema = None`` (the
        default), skip. Backwards compat with pre-I.6 providers.
      - If the provider exposes ``self.config``:
          * already a pydantic BaseModel → ensure it's an instance of
            ``config_schema`` (or re-validate via dump + model_validate
            to catch unrelated models).
          * dict → parse via ``config_schema(**config)``.
          * anything else → parse via ``config_schema(**config.__dict__)``
            (tolerates dataclass-style configs).
      - If the provider declares ``config_schema`` but has no ``config``
        attribute, skip — the provider hasn't opted into validation yet.

    Raises:
        ValueError: config fails pydantic validation. Message names the
            provider and includes the pydantic error for debuggability.
    """
    # Class-registered providers: no instance to validate. The class's
    # own config_schema attr stays available for future instances.
    if isinstance(provider, type):
        return

    schema = getattr(type(provider), "config_schema", None)
    if schema is None:
        return

    config = getattr(provider, "config", None)
    if config is None:
        return

    from pydantic import BaseModel as _PydanticBaseModel
    from pydantic import ValidationError

    try:
        if isinstance(config, _PydanticBaseModel):
            if isinstance(config, schema):
                # Already the right shape. Fastest path.
                return
            # Different pydantic model — re-validate via dump.
            schema.model_validate(config.model_dump())
            return
        if isinstance(config, dict):
            schema.model_validate(config)
            return
        # Tolerate dataclass-style or namespace-style configs.
        schema.model_validate(vars(config))
    except ValidationError as e:
        raise ValueError(
            f"provider {name!r} config failed schema validation: {e}"
        ) from e
    else:
        logger.debug(
            "provider %r config validated against schema %s",
            name,
            schema.__name__,
        )


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
        session_db_path: Path | None = None,
        slash_commands: dict[str, Any] | None = None,
        activation_source: PluginActivationSource = "bundled",
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
        # Per-profile SQLite session DB path. Plugins that persist per-session
        # state (coding-harness TodoWrite, scratchpads, etc.) get this via
        # api.session_db_path instead of importing opencomputer.agent.config —
        # preserves the plugin→SDK boundary for third-party plugins that don't
        # have opencomputer in their import path.
        self.session_db_path: Path | None = session_db_path
        # Phase 12b.6 Task D8: plugin-authored slash commands. Shared dict
        # threaded in from PluginRegistry so all plugins register into the
        # same table. Keyed by command name (no leading slash).
        self.slash_commands: dict[str, Any] = (
            slash_commands if slash_commands is not None else {}
        )
        # Task I.7: why this plugin was activated. Exposed to plugin code
        # via the ``activation_source`` property so ``register(api)`` can
        # branch on the origin (user-enabled → verbose logging;
        # auto-enabled → quiet). Validated here because the Literal type
        # is erased at runtime — without the check, typos silently pass.
        if activation_source not in VALID_ACTIVATION_SOURCES:
            raise ValueError(
                f"activation_source must be one of "
                f"{sorted(VALID_ACTIVATION_SOURCES)!r}; got {activation_source!r}"
            )
        self._activation_source: PluginActivationSource = activation_source

    @property
    def activation_source(self) -> PluginActivationSource:
        """Why this plugin was activated — see ``PluginActivationSource``.

        Plugins can read this inside ``register(api)`` and adapt. For
        example, a noisy onboarding message only makes sense the first
        time a user explicitly enables the plugin::

            def register(api):
                if api.activation_source == "user_enable":
                    api.hooks.notify("thanks for enabling <plugin>!")
        """
        return self._activation_source

    def register_tool(self, tool: Any) -> None:
        self.tools.register(tool)

    def register_hook(self, spec: Any) -> None:
        self.hooks.register(spec)

    def register_provider(self, name: str, provider: Any) -> None:
        """Register an LLM provider under ``name``.

        ``provider`` may be either a provider INSTANCE or a provider
        CLASS. Plugins typically register the class (existing pattern)
        and the CLI instantiates it on demand in ``_resolve_provider``.

        Task I.6 — config-schema validation. If the provider's type (or
        the provider itself, if it's a class) declares a
        ``config_schema`` class attribute AND the object is an
        instance with a ``config`` attribute, the registry validates
        ``config`` against the schema using pydantic and raises
        ``ValueError`` on mismatch. This catches malformed config at
        plugin load rather than at first-use.

        Providers without ``config_schema`` (the default) skip
        validation entirely — backwards compatible with every pre-I.6
        provider. When ``provider`` is a class (not an instance), we
        skip validation too; the instance doesn't exist yet, and the
        CLI path will surface construction errors naturally.
        """
        _validate_provider_config(name, provider)
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

    def register_slash_command(self, cmd: Any) -> None:
        """Register a slash command instance.

        Accepts either a ``plugin_sdk.SlashCommand`` subclass instance
        OR a duck-typed object with ``name``, ``description``, and
        ``execute(args, runtime)`` attributes (Phase 6f legacy compat).

        Raises ``ValueError`` on missing/invalid name or name collision.
        """
        name = getattr(cmd, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError(
                f"slash command must have a str 'name' attribute; "
                f"got {type(cmd).__name__}"
            )
        if name in self.slash_commands:
            raise ValueError(
                f"slash command '{name}' is already registered"
            )
        self.slash_commands[name] = cmd

    def register_doctor_contribution(self, contribution: Any) -> None:
        """Register a HealthContribution — runs on `opencomputer doctor [--fix]`.

        Each contribution is an async (fix: bool) -> RepairResult callable
        wrapped in a HealthContribution(id, description, run). When the user
        passes --fix, the contribution is expected to repair in place.
        Source: openclaw DoctorHealthContribution.
        """
        self.doctor_contributions.append(contribution)


def load_plugin(
    candidate: PluginCandidate,
    api: PluginAPI,
    activation_source: PluginActivationSource | None = None,
) -> LoadedPlugin | None:
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

    Task I.7: ``activation_source`` lets callers describe WHY this plugin
    is being activated (e.g. ``"user_enable"`` from the CLI, vs the
    ``"bundled"`` default for ``extensions/*``). When supplied, the value
    is pushed onto the shared ``api`` for the duration of the plugin's
    ``register()`` call so plugin code can read ``api.activation_source``
    and branch on it. ``None`` (the default) leaves ``api``'s existing
    source untouched — backwards compatible with every pre-I.7 caller.
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

    # Task I.7: temporarily override the shared api's activation source
    # for this specific plugin's register() call. Save + restore so
    # sibling plugins loaded through the same api see their own source
    # (or the original baseline if this was a one-off override).
    prior_source: PluginActivationSource | None = None
    if activation_source is not None:
        if activation_source not in VALID_ACTIVATION_SOURCES:
            raise ValueError(
                f"activation_source must be one of "
                f"{sorted(VALID_ACTIVATION_SOURCES)!r}; got {activation_source!r}"
            )
        prior_source = api._activation_source
        api._activation_source = activation_source

    # Task I.5: snapshot registrations BEFORE calling into the plugin so
    # we can diff after and catch manifest-vs-runtime drift. Snapshot is
    # cheap (set copies + int count); cost is paid once per plugin load.
    before_snapshot = _snapshot_registrations(api)

    try:
        register_fn(api)
    except Exception as e:  # noqa: BLE001
        logger.exception("plugin '%s' register() raised: %s", manifest.id, e)
        return None
    finally:
        if prior_source is not None:
            api._activation_source = prior_source

    # Task I.5: compare post-register state against manifest claims.
    # Emits WARNINGs on mismatch — never blocks load. Intentionally
    # non-fatal: the plugin's register() may have had side effects we
    # don't want to abort on mid-way.
    after_snapshot = _snapshot_registrations(api)
    try:
        _validate_runtime_contract(manifest, before_snapshot, after_snapshot)
    except Exception:  # noqa: BLE001
        # Contract validation is diagnostics — never break load for it.
        logger.debug(
            "runtime contract validation raised for plugin '%s'; swallowing",
            manifest.id,
            exc_info=True,
        )

    logger.info("loaded plugin '%s' v%s", manifest.id, manifest.version)
    return LoadedPlugin(candidate=candidate, module=module)


__all__ = [
    "PluginAPI",
    "LoadedPlugin",
    "load_plugin",
    "SingleInstanceError",
]
