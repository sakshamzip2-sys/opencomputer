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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencomputer.plugins.discovery import PluginCandidate
from plugin_sdk.core import (
    VALID_ACTIVATION_SOURCES,
    PluginActivationSource,
    SingleInstanceError,
)
from plugin_sdk.runtime_context import RequestContext

logger = logging.getLogger("opencomputer.plugins.loader")


# Sub-project G (openclaw-parity) Task 10 — min_host_version enforcement.


class PluginIncompatibleError(RuntimeError):
    """Raised at load time when a plugin's ``min_host_version`` exceeds
    the running ``opencomputer.__version__``.

    Halts that plugin's load — others continue. Caller (load_plugin)
    catches and logs + returns None so one bad plugin can't break the
    rest of the registry.
    """


def _check_min_host_version(
    *, plugin_id: str, min_host_version: str, host_version: str
) -> None:
    """Compare a plugin's ``min_host_version`` to the running host.

    Empty ``min_host_version`` skips the check (back-compat for v3
    manifests). Otherwise parse with ``packaging.version.Version`` and
    raise :class:`PluginIncompatibleError` on mismatch.

    Sub-project G (openclaw-parity) Task 10. Mirrors openclaw
    ``min-host-version.ts`` semantics.
    """
    if not min_host_version:
        return
    from packaging.version import InvalidVersion, Version

    try:
        required = Version(min_host_version)
        current = Version(host_version)
    except InvalidVersion as e:
        raise PluginIncompatibleError(
            f"plugin {plugin_id!r} declares unparseable min_host_version "
            f"{min_host_version!r}: {e}"
        ) from e
    if current < required:
        raise PluginIncompatibleError(
            f"plugin {plugin_id!r} requires opencomputer >= "
            f"{min_host_version} but host is {host_version}"
        )


# Common short names plugins use for their sibling files. Clearing these
# between plugin loads prevents two plugins (both with a top-level
# ``provider.py``, ``realtime.py``, etc.) from sharing the first-loaded
# module via ``sys.modules`` cache. Adding a new sibling filename used
# by more than one plugin? Add it here too.
_PLUGIN_LOCAL_NAMES = (
    "provider",
    "adapter",
    "plugin",
    "handlers",
    "hooks",
    "realtime",          # openai-provider, gemini-provider, future Anthropic
    "realtime_helpers",  # ditto — pure-helpers sidecar per realtime bridge
)


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
class PluginRegistrations:
    """Exact set of items a single plugin registered on ``PluginAPI``.

    Computed by diffing the snapshots captured before/after
    ``register(api)``. Stored on ``LoadedPlugin`` so teardown (Task I.4)
    knows which entries to remove even when multiple plugins contributed
    to the same shared registry dict.

    Hooks are tracked by object identity (``HookSpec`` instances the
    plugin registered) rather than by name — ``HookEngine`` keys by
    event, and multiple plugins can register handlers on the same
    event, so only identity-match unregister is safe.
    """

    tool_names: tuple[str, ...] = ()
    provider_names: tuple[str, ...] = ()
    channel_names: tuple[str, ...] = ()
    slash_names: tuple[str, ...] = ()
    injection_provider_ids: tuple[str, ...] = ()
    hook_specs: tuple[Any, ...] = ()
    #: How many doctor contributions this plugin added (most recent N).
    doctor_contributions_count: int = 0
    #: True iff this plugin registered the currently-active memory provider.
    registered_memory_provider: bool = False


@dataclass(slots=True)
class LoadedPlugin:
    """Record of an activated plugin.

    ``registrations`` + ``api`` are I.4 teardown hooks — the loader
    captures them so ``PluginRegistry.teardown_plugin`` can remove
    exactly the entries this plugin registered (safely, even when
    multiple plugins contributed to the same registry dict).
    """

    candidate: PluginCandidate
    module: Any
    registrations: PluginRegistrations = field(default_factory=PluginRegistrations)
    api: PluginAPI | None = None


# ─── runtime contract validation (Task I.5) ───────────────────────────


@dataclass(slots=True)
class _RegistrationSnapshot:
    """Point-in-time view of registered items on a ``PluginAPI``.

    Used by ``_validate_runtime_contract`` AND by I.4 teardown: the diff
    between before/after snapshots is the exact set of items a single
    plugin's ``register(api)`` call contributed.

    Sets of names for bulk registries (tools/providers/channels/slash
    commands), a count for hooks (used for contract warning), a tuple
    of the actual ``HookSpec`` identities present (used for teardown),
    a count for doctor contributions (list-append pattern), and a flag
    for the currently-exclusive memory provider slot.

    Matches OpenClaw's loader-side contract check
    (``sources/openclaw/src/plugins/loader.ts``) which snapshots before
    and diffs after ``plugin.register()``.
    """

    tool_names: set[str]
    provider_names: set[str]
    channel_names: set[str]
    slash_names: set[str]
    injection_provider_ids: set[str]
    hook_count: int
    hook_specs: list[Any]
    doctor_contributions_count: int
    memory_provider_present: bool


def _snapshot_registrations(api: PluginAPI) -> _RegistrationSnapshot:
    """Capture the currently-registered items on ``api`` for before/after diff.

    Duck-typed on purpose: tests routinely pass ``_Noop`` stubs for the
    tool registry and hook engine when they only care about the loader's
    lock/import paths. ``getattr`` with sensible defaults keeps the
    contract check a best-effort diagnostic that never breaks those
    stub-based tests.

    ``hook_specs`` captures identities (the actual ``HookSpec`` objects
    currently registered) so teardown can remove exactly the specs a
    plugin added — multiple plugins can register handlers on the same
    event, so name-keyed removal isn't safe.
    """
    names_iter = getattr(api.tools, "names", None)
    tool_names = set(names_iter()) if callable(names_iter) else set()
    hooks_dict = getattr(api.hooks, "_hooks", None)
    hook_count = (
        sum(len(specs) for specs in hooks_dict.values())
        if isinstance(hooks_dict, dict)
        else 0
    )
    hook_specs: list[Any] = []
    if isinstance(hooks_dict, dict):
        for specs in hooks_dict.values():
            for entry in specs:
                # Round 2A P-1: HookEngine now stores entries as
                # ``(priority, seq, spec)`` tuples so it can sort by priority
                # while keeping HookSpec frozen. Extract the spec so the
                # identity-diff in :func:`_compute_plugin_registrations`
                # still matches what the plugin handed in. Older shapes (a
                # bare ``HookSpec``) are handled defensively for stub
                # engines used in tests.
                if isinstance(entry, tuple) and len(entry) == 3:
                    hook_specs.append(entry[2])
                else:
                    hook_specs.append(entry)
    # Injection engine stores providers in ``_providers`` dict keyed by
    # provider_id. Duck-type so stub engines (no ``_providers``) don't
    # break the diagnostic path.
    inj_dict = getattr(api.injection, "_providers", None) if api.injection else None
    injection_ids = set(inj_dict.keys()) if isinstance(inj_dict, dict) else set()
    return _RegistrationSnapshot(
        tool_names=tool_names,
        provider_names=set(api.providers.keys()),
        channel_names=set(api.channels.keys()),
        slash_names=set(api.slash_commands.keys()),
        injection_provider_ids=injection_ids,
        hook_count=hook_count,
        hook_specs=hook_specs,
        doctor_contributions_count=len(api.doctor_contributions),
        memory_provider_present=(api.memory_provider is not None),
    )


def _compute_plugin_registrations(
    before: _RegistrationSnapshot,
    after: _RegistrationSnapshot,
) -> PluginRegistrations:
    """Diff two snapshots to produce the exact delta a plugin registered.

    The result is stored on ``LoadedPlugin.registrations`` and consumed
    by ``PluginRegistry.teardown_plugin`` to remove precisely the items
    this plugin added — safe even when multiple plugins contributed to
    the same shared registry dict.

    ``hook_specs`` uses identity diff (``id()``) because ``HookSpec``
    is a frozen dataclass and two plugins could theoretically register
    equal-valued specs; identity-match is the only unambiguous key.
    """
    before_hook_ids = {id(s) for s in before.hook_specs}
    new_hook_specs = tuple(
        s for s in after.hook_specs if id(s) not in before_hook_ids
    )
    added_doctor = max(
        0, after.doctor_contributions_count - before.doctor_contributions_count
    )
    return PluginRegistrations(
        tool_names=tuple(sorted(after.tool_names - before.tool_names)),
        provider_names=tuple(sorted(after.provider_names - before.provider_names)),
        channel_names=tuple(sorted(after.channel_names - before.channel_names)),
        slash_names=tuple(sorted(after.slash_names - before.slash_names)),
        injection_provider_ids=tuple(
            sorted(after.injection_provider_ids - before.injection_provider_ids)
        ),
        hook_specs=new_hook_specs,
        doctor_contributions_count=added_doctor,
        registered_memory_provider=(
            after.memory_provider_present and not before.memory_provider_present
        ),
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
    new_injection = after.injection_provider_ids - before.injection_provider_ids
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
        and not new_injection
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


@dataclass(frozen=True, slots=True)
class _RealtimeBridgeRegistration:
    """Internal record stored by ``register_realtime_bridge``.

    Holds the bridge factory PLUS the metadata the CLI needs to size
    audio I/O and validate environment for that provider — without the
    CLI having to maintain a hardcoded provider table. Frozen so plugins
    can't mutate after registration.
    """

    factory: Any
    env_var: str | None = None
    audio_sink_kwargs: dict[str, Any] = field(default_factory=dict)


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
        outgoing_queue: Any = None,
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
        # Pass-2 F8 fix: ``session_db_path`` and ``profile_home`` are
        # now lazy ``@property`` accessors that read the active profile
        # via ``_home()`` each time. Eager capture at PluginRegistry.api()
        # time was the F8 bug — the boot-time default path leaked into
        # multi-profile dispatch, so a plugin reading it under a different
        # profile would write to the wrong DB. The optional override
        # argument (``session_db_path=...``) is honoured when explicitly
        # provided (e.g. tests that want to pin a path); otherwise the
        # property resolves lazily through ``_home()`` which is
        # ContextVar-aware after Phase 1.
        self._session_db_path_override: Path | None = session_db_path
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
        # Task I.9: per-request scope. ``None`` outside any dispatch;
        # populated by the gateway via ``in_request(ctx)`` around each
        # inbound MessageEvent / wire call. Plugins read via the
        # ``request_context`` property.
        self._request_context: RequestContext | None = None
        # Hermes channel-port (PR 2 / amendment §A.3): outgoing-queue
        # facade. Lets webhook-style plugins enqueue outbound messages
        # without importing ``opencomputer.gateway.outgoing_queue``
        # directly — preserving the plugin_sdk → opencomputer one-way
        # boundary. The accessor is a duck-typed object exposing
        # ``.enqueue(platform=..., chat_id=..., body=..., attachments=...,
        # metadata=...)`` (see :class:`opencomputer.gateway.outgoing_queue.
        # OutgoingQueue`). ``None`` outside the gateway (CLI / tests)
        # — plugins MUST handle the ``None`` case gracefully.
        self._outgoing_queue: Any = outgoing_queue
        # PR #221 follow-up: the live ``Dispatch`` instance the gateway
        # is using. ``None`` outside the gateway (CLI / wire / tests).
        # Plugins read this to query in-flight session locks
        # (``_locks`` dict) so /reset can drop a stuck per-chat lock,
        # and to call into the gateway's approval-callback machinery
        # without coupling to ``opencomputer.gateway.dispatch``. Without
        # the binding, the lock-clear branch in
        # ``extensions/discord/adapter._reset_session`` silently
        # no-ops. Gateway calls ``_bind_dispatch(disp)`` from
        # :meth:`Gateway.__init__` right after constructing ``Dispatch``.
        self._dispatch: Any = None
        # Realtime voice bridge registrations (e.g. OpenAI, Gemini,
        # future Anthropic). Keyed by short provider name ("openai",
        # "gemini"). Each entry is a ``_RealtimeBridgeRegistration``
        # bundling the factory with the env var the CLI must check and
        # any audio-sink kwargs (e.g. 24 kHz output for Gemini).
        # ``opencomputer voice realtime --provider <name>`` looks up the
        # registration here — replaces the older hardcoded if/elif
        # dispatch + ``_PROVIDER_DEFAULTS`` table in cli_voice.py.
        self._realtime_bridge_registrations: dict[str, _RealtimeBridgeRegistration] = {}

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

    @property
    def request_context(self) -> RequestContext | None:
        """Per-request scope, if the gateway has entered one — else ``None``.

        Populated by ``in_request(ctx)`` on this same PluginAPI during
        each dispatch. Plugins reach this from any code that runs
        inside the dispatch (tool handlers, injection providers, hook
        handlers) to learn about the inbound request identity.

        ``None`` outside a scope — the CLI + direct
        ``AgentLoop.run_conversation`` path does not populate a scope,
        so the return is ``None`` there. Plugins MUST handle the
        ``None`` case; it's the common case for offline / CLI runs.

        Mirrors OpenClaw's per-request plugin scope at
        ``sources/openclaw/src/gateway/server-plugins.ts:47-64, 107-144``.
        """
        return self._request_context

    @property
    def session_db_path(self) -> Path | None:
        """Per-profile SQLite session DB path (lazy; profile-aware).

        Pass-2 F8 fix. Plugins that persist per-session state
        (coding-harness ``TodoWrite``, affect-injection scratchpad
        reads, etc.) read this attribute to learn where to write.

        The path is resolved lazily on each access through
        :func:`opencomputer.agent.config._home`, which is ContextVar-aware
        after Phase 1: under ``set_profile(profile_home)``, ``_home()``
        returns the active profile's directory; outside any scope it
        falls back to ``OPENCOMPUTER_HOME`` and finally
        ``~/.opencomputer``. So a plugin reading
        ``api.session_db_path`` under multi-profile dispatch sees the
        right DB even though the ``PluginAPI`` instance was constructed
        once at boot under the default profile.

        If a caller passed an explicit ``session_db_path`` to the
        constructor (typically tests that pin a path) the override is
        honoured and lazy resolution is bypassed.

        Plugins that hold long-lived state bound to this path
        (e.g. an open ``SessionDB`` connection cached on a provider
        instance) must be aware that the path can change between
        dispatches under multi-profile routing — they should either
        re-resolve per call or accept that they're effectively
        single-profile in v1. See the affect-injection + coding-harness
        TodoWrite follow-ups documented in the F8 audit.
        """
        if self._session_db_path_override is not None:
            return self._session_db_path_override
        # Function-local import: ``opencomputer.agent.config`` is fine
        # to import from this module (we live under ``opencomputer/``);
        # this is just symmetry with how ``_home()`` consumers do it.
        from opencomputer.agent.config import _home

        return _home() / "sessions.db"

    @property
    def profile_home(self) -> Path:
        """Active profile's home directory (lazy; profile-aware).

        Pass-2 F8 fix. Returns the directory that holds this profile's
        ``config.yaml`` / ``profile.yaml`` / per-feature subdirs (e.g.
        ``ambient/``, ``screen-awareness/``). Resolved lazily through
        :func:`opencomputer.agent.config._home` so multi-profile
        dispatch sees the right path even though the ``PluginAPI``
        instance was constructed once at boot.

        Plugins that need per-profile feature state (e.g.
        screen-awareness loading ``profile_home/screen-awareness/state.json``)
        should read this on each call rather than caching the value
        from ``register()`` — the underlying value can change between
        dispatches under multi-profile routing.
        """
        from opencomputer.agent.config import _home

        return _home()

    @property
    def outgoing_queue(self) -> Any:
        """Outgoing-message queue facade for plugins that enqueue async sends.

        Hermes channel-port PR 2 / amendment §A.3. Webhook-style plugins
        (HTTP receivers, cron-driven publishers) need to schedule a
        message without holding a live adapter reference. Importing
        ``opencomputer.gateway.outgoing_queue.OutgoingQueue`` directly
        would violate the plugin_sdk → opencomputer one-way boundary, so
        the gateway threads its queue through ``PluginAPI`` and plugins
        access it via ``api.outgoing_queue.enqueue(...)``.

        Returns ``None`` when no queue is bound (CLI / tests / direct
        ``AgentLoop`` runs). Plugin code MUST handle the ``None`` case
        — typically by logging a warning and dropping the message
        rather than raising.

        Pass-2 F8 follow-up: the queue object itself is single-process
        and bound to one ``_home()`` snapshot (the default profile at
        gateway boot). Multi-profile routing of OUTBOUND messages is
        out of scope for this fix — the gateway's outgoing-drainer
        runs once per process, and re-binding the queue per-profile
        would require a queue-per-profile registry. Documented as a
        follow-up; v1 inbound multi-profile dispatch still works
        because inbound paths read ``session_db_path`` lazily.
        """
        return self._outgoing_queue

    def _bind_outgoing_queue(self, queue: Any) -> None:
        """Late-bind the outgoing queue. Called by the gateway after
        ``load_all`` ran but before adapters connect — at construction
        time the queue's SQLite path isn't available yet (it depends on
        per-profile config). Idempotent: replacing an existing binding
        is harmless. Plugin code only ever sees the post-binding value
        because dispatch starts after ``Gateway.start``.
        """
        self._outgoing_queue = queue

    def _bind_dispatch(self, dispatch: Any) -> None:
        """Late-bind the live ``Dispatch`` instance.

        Called by :meth:`Gateway.__init__` immediately after constructing
        ``Dispatch`` so plugin-side helpers (e.g. Discord ``/reset``)
        can reach the live per-chat lock map without importing
        ``opencomputer.gateway.dispatch`` directly.

        Idempotent: replacing an existing binding is harmless. ``None``
        outside the gateway is the documented default — plugins MUST
        handle that path gracefully (typically by no-op).
        """
        self._dispatch = dispatch

    @contextmanager
    def in_request(self, ctx: RequestContext) -> Iterator[None]:
        """Enter a per-request scope for the duration of a dispatch.

        The gateway wraps each inbound channel message's
        ``run_conversation`` call in this context manager so plugins
        running during the dispatch can read ``api.request_context``
        and query the current request identity (for auth gating,
        rate limiting, activation-context branching).

        Scopes do NOT stack on a single PluginAPI — a second entry
        while one is already active raises ``RuntimeError``. This
        matches OpenClaw's server-plugins model: one request in flight
        per scope at a time (fire-per-message channels produce one
        scope per message; the wire server produces one scope per
        method call; concurrent chats go through separate session
        locks at the dispatcher level, not through nested scopes).

        The scope unwinds cleanly even if the wrapped block raises —
        ``request_context`` reverts to its prior value (usually
        ``None``) before the exception propagates.
        """
        if self._request_context is not None:
            raise RuntimeError(
                "PluginAPI is already in a request (request_id="
                f"{self._request_context.request_id!r}) — nested in_request "
                "scopes are not supported; concurrent dispatches must use "
                "separate locks at the dispatcher level."
            )
        self._request_context = ctx
        try:
            yield
        finally:
            self._request_context = None

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

    def register_realtime_bridge(
        self,
        name: str,
        factory: Any,
        *,
        env_var: str | None = None,
        audio_sink_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Register a realtime-voice bridge factory under ``name``.

        ``factory`` is a callable ``factory(*, callbacks, api_key, model,
        voice, instructions, **kwargs) -> BaseRealtimeVoiceBridge``. The
        CLI's ``voice realtime --provider <name>`` looks the factory up
        and invokes it.

        ``env_var`` is the environment variable the CLI checks for the
        provider's API key (e.g. ``"OPENAI_API_KEY"``,
        ``"GEMINI_API_KEY"``). ``None`` means no env validation — the
        plugin is responsible for sourcing credentials another way.

        ``audio_sink_kwargs`` is forwarded to ``LocalAudioIO`` so the
        speaker stream matches the provider's output rate (e.g.
        ``{"output_sample_rate": 24_000}`` for Gemini). Defaults to
        ``{}`` (16 kHz, OpenAI's native rate).

        Re-registering an existing name overwrites the prior entry —
        matches ``register_channel`` semantics. Plugins SHOULD pick
        unique short names ("openai", "gemini", "anthropic"); collisions
        are silent so user-shipped plugins can override bundled defaults.
        """
        self._realtime_bridge_registrations[name] = _RealtimeBridgeRegistration(
            factory=factory,
            env_var=env_var,
            # Defensive copy so plugin code can't mutate the stored entry.
            audio_sink_kwargs=dict(audio_sink_kwargs or {}),
        )

    def get_realtime_bridge_registration(self, name: str) -> _RealtimeBridgeRegistration:
        """Return the full registration for ``name`` or raise KeyError.

        Use this when the caller needs the env var or audio-sink kwargs
        in addition to the factory. The CLI's ``voice realtime`` reads
        all three fields from one lookup.
        """
        try:
            return self._realtime_bridge_registrations[name]
        except KeyError as exc:
            available = sorted(self._realtime_bridge_registrations)
            raise KeyError(
                f"no realtime-voice bridge registered for {name!r}; "
                f"available: {available}"
            ) from exc

    def get_realtime_bridge_factory(self, name: str) -> Any:
        """Return just the factory callable for ``name``.

        Convenience for callers that don't need the env var or audio
        kwargs. Equivalent to
        ``get_realtime_bridge_registration(name).factory``.
        """
        return self.get_realtime_bridge_registration(name).factory

    def realtime_bridge_names(self) -> list[str]:
        """List registered realtime-voice bridge names."""
        return sorted(self._realtime_bridge_registrations)

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

    # Sub-project G (openclaw-parity) Task 10 — enforce min_host_version
    # BEFORE we import the plugin's entry module. A version mismatch
    # never invokes plugin code — fail closed with a clear log line, but
    # don't propagate the exception (other plugins must still load).
    if manifest.min_host_version:
        try:
            import opencomputer

            _check_min_host_version(
                plugin_id=manifest.id,
                min_host_version=manifest.min_host_version,
                host_version=opencomputer.__version__,
            )
        except PluginIncompatibleError as e:
            logger.warning("incompatible plugin '%s': %s", manifest.id, e)
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

    # Task I.4: capture the exact delta THIS plugin added so teardown
    # can surgically remove just those entries. Reusing the I.5 snapshot
    # infrastructure — no extra work at load time beyond one diff call.
    registrations = _compute_plugin_registrations(before_snapshot, after_snapshot)

    # Sub-project G.11 (Tier 2.13): MCP catalog binding. After register()
    # succeeds, install any preset-MCPs the plugin's manifest declared.
    # Idempotent — skips servers already in config.yaml. Never blocks load.
    if manifest.mcp_servers:
        try:
            _install_mcp_servers_from_manifest(manifest)
        except Exception:  # noqa: BLE001 — diagnostic, never block load
            logger.debug(
                "MCP catalog binding raised for plugin '%s'; swallowing",
                manifest.id,
                exc_info=True,
            )

    logger.info("loaded plugin '%s' v%s", manifest.id, manifest.version)
    return LoadedPlugin(
        candidate=candidate,
        module=module,
        registrations=registrations,
        api=api,
    )


def _install_mcp_servers_from_manifest(manifest) -> None:  # type: ignore[no-untyped-def]
    """Resolve manifest ``mcp_servers`` (preset slugs) and add them to config.yaml.

    Idempotent: if a server with the same name already exists in config,
    skip it (don't overwrite — the user may have customised it). Unknown
    preset slugs are logged at WARNING but don't fail the load.

    Sub-project G.11 (Tier 2.13). See ``opencomputer/mcp/presets.py`` for
    the preset registry that slugs resolve against.
    """
    # Late imports — keep loader.py's import surface narrow + avoid circular
    # imports through opencomputer.agent.config_store.
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.agent.config_store import load_config, save_config
    from opencomputer.mcp.presets import get_preset

    cfg = load_config()
    existing_names = {s.name for s in cfg.mcp.servers}
    new_servers: list = []
    for slug in manifest.mcp_servers:
        preset = get_preset(slug)
        if preset is None:
            logger.warning(
                "plugin '%s' declared MCP slug %r but no such preset exists; skipping",
                manifest.id,
                slug,
            )
            continue
        if preset.config.name in existing_names:
            logger.debug(
                "plugin '%s' MCP %r already in config — skipping (user customisation respected)",
                manifest.id,
                preset.config.name,
            )
            continue
        new_servers.append(
            MCPServerConfig(
                name=preset.config.name,
                transport=preset.config.transport,
                command=preset.config.command,
                args=preset.config.args,
                url=preset.config.url,
                env=dict(preset.config.env),
                headers=dict(preset.config.headers),
                enabled=preset.config.enabled,
            )
        )
        existing_names.add(preset.config.name)

    if not new_servers:
        return

    # Replace cfg.mcp with the extended servers tuple via dataclasses.replace.
    import dataclasses

    extended_mcp = dataclasses.replace(
        cfg.mcp,
        servers=(*cfg.mcp.servers, *new_servers),
    )
    new_cfg = dataclasses.replace(cfg, mcp=extended_mcp)
    save_config(new_cfg)
    logger.info(
        "plugin '%s' auto-installed %d MCP server(s): %s",
        manifest.id,
        len(new_servers),
        ", ".join(s.name for s in new_servers),
    )


def teardown_loaded_plugin(
    loaded: LoadedPlugin,
    *,
    api: PluginAPI | None = None,
) -> None:
    """Remove a plugin's registrations + synthetic modules (Task I.4).

    Safe to call once per ``LoadedPlugin``. Never raises — teardown is
    best-effort cleanup; a failed step is logged and the rest of the
    teardown continues.

    Order (mirrors OpenClaw's ``clearPluginLoaderCache`` pattern,
    ``sources/openclaw/src/plugins/loader.ts:222-230``):

    1. Call the plugin's ``cleanup()`` / ``teardown()`` entry-point
       function if present. Plugin-owned cleanup first so the plugin
       can flush caches / close resources while its registrations are
       still reachable.
    2. Unregister the plugin's items from the shared registries
       (tools, providers, channels, slash commands, injection
       providers, hooks, doctor contributions, memory provider).
    3. Drop the plugin's synthetic module name + common sibling names
       from ``sys.modules`` so a later reload imports fresh state.

    ``api`` can override the api on ``loaded`` — used by registry
    callers that want to be explicit about which PluginAPI instance
    owns the registrations.
    """
    plugin_id = loaded.candidate.manifest.id
    module = loaded.module
    target_api = api if api is not None else loaded.api

    # Step 1 — call the plugin's cleanup hook if present.
    cleanup_fn = None
    for hook_name in ("cleanup", "teardown"):
        fn = getattr(module, hook_name, None)
        if callable(fn):
            cleanup_fn = fn
            break
    if cleanup_fn is not None:
        try:
            cleanup_fn()
        except Exception:  # noqa: BLE001
            logger.exception(
                "plugin %r cleanup/teardown raised; continuing teardown",
                plugin_id,
            )

    # Step 2 — remove registrations if we have the owning api.
    regs = loaded.registrations
    if target_api is not None:
        for name in regs.tool_names:
            unregister = getattr(target_api.tools, "unregister", None)
            if callable(unregister):
                try:
                    unregister(name)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "tool unregister failed for %r (plugin %r)",
                        name,
                        plugin_id,
                        exc_info=True,
                    )
        for name in regs.provider_names:
            target_api.providers.pop(name, None)
        for name in regs.channel_names:
            target_api.channels.pop(name, None)
        for name in regs.slash_names:
            target_api.slash_commands.pop(name, None)
        if target_api.injection is not None:
            inj_unreg = getattr(target_api.injection, "unregister", None)
            for pid in regs.injection_provider_ids:
                if callable(inj_unreg):
                    try:
                        inj_unreg(pid)
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "injection unregister failed for %r (plugin %r)",
                            pid,
                            plugin_id,
                            exc_info=True,
                        )
        # Hooks — identity-match remove from each event's list.
        # Round 2A P-1: ``_hooks`` entries are ``(priority, seq, spec)``
        # tuples; match the spec at index 2 against ``regs.hook_specs``
        # (which stored bare specs at snapshot time).
        hooks_dict = getattr(target_api.hooks, "_hooks", None)
        if isinstance(hooks_dict, dict) and regs.hook_specs:
            target_ids = {id(s) for s in regs.hook_specs}
            for event, specs in list(hooks_dict.items()):
                remaining = [
                    s
                    for s in specs
                    if id(
                        s[2] if isinstance(s, tuple) and len(s) == 3 else s
                    )
                    not in target_ids
                ]
                if len(remaining) != len(specs):
                    hooks_dict[event] = remaining
        # Doctor contributions — remove the most recent N entries we added.
        # Best-effort: if the list shrank under us, trim however many
        # are left (never negative).
        if regs.doctor_contributions_count > 0:
            to_drop = min(
                regs.doctor_contributions_count,
                len(target_api.doctor_contributions),
            )
            if to_drop > 0:
                del target_api.doctor_contributions[-to_drop:]
        if regs.registered_memory_provider:
            target_api.memory_provider = None

    # Step 3 — drop the synthetic module + common sibling names from
    # sys.modules so a later reload sees a clean graph. Synthetic name
    # is deterministic (see load_plugin below).
    entry = loaded.candidate.manifest.entry.strip()
    synth_name = f"_opencomputer_plugin_{plugin_id.replace('-', '_')}_{entry}"
    sys.modules.pop(synth_name, None)
    _clear_plugin_local_cache()


__all__ = [
    "PluginAPI",
    "LoadedPlugin",
    "PluginRegistrations",
    "load_plugin",
    "teardown_loaded_plugin",
    "SingleInstanceError",
]
