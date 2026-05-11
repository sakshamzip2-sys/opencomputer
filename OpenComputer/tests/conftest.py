"""pytest conftest — test infrastructure for all tests in this directory.

This file registers module aliases so hyphenated extension directories can be
imported with underscores in test code:

1.  extensions.coding_harness → extensions/coding-harness/  (lets tests
    import the introspection sub-package and any other coding-harness
    extension modules via the underscore form Python requires.)
2.  extensions.aws_bedrock_provider → extensions/aws-bedrock-provider/
3.  extensions.browser_bridge → extensions/browser-bridge/
4.  extensions.ambient_sensors → extensions/ambient-sensors/
5.  extensions.skill_evolution → extensions/skill-evolution/
6.  extensions.voice_mode → extensions/voice-mode/

The aliases are injected into sys.modules BEFORE any test module is collected.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

# Project root (parent of tests/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_CH_DIR = _EXT_DIR / "coding-harness"
_BEDROCK_DIR = _EXT_DIR / "aws-bedrock-provider"
_AMBIENT_DIR = _EXT_DIR / "ambient-sensors"
_SKILL_EVO_DIR = _EXT_DIR / "skill-evolution"
_VOICE_MODE_DIR = _EXT_DIR / "voice-mode"
_BROWSER_CONTROL_DIR = _EXT_DIR / "browser-control"
_ADAPTER_RUNNER_DIR = _EXT_DIR / "adapter-runner"
_AFFECT_INJECTION_DIR = _EXT_DIR / "affect-injection"
_OPENAI_PROVIDER_DIR = _EXT_DIR / "openai-provider"
_GEMINI_PROVIDER_DIR = _EXT_DIR / "gemini-provider"
_ANTHROPIC_PROVIDER_DIR = _EXT_DIR / "anthropic-provider"
_SCREEN_AWARENESS_DIR = _EXT_DIR / "screen-awareness"
_OLLAMA_PROVIDER_DIR = _EXT_DIR / "ollama-provider"
_GROQ_PROVIDER_DIR = _EXT_DIR / "groq-provider"


def _ensure_extensions_pkg() -> None:
    """Synthesise a namespace package for 'extensions' if not already registered."""
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg


def _register_extension_alias(
    underscore_name: str,
    ext_dir: Path,
    submodules: tuple[str, ...] = (),
    *,
    exec_modules: bool = True,
    bind_on_parent: bool = True,
) -> None:
    """Generic plugin-dir → underscore-package alias registration.

    Plugin directories are hyphenated on disk (``extensions/openai-provider/``)
    but Python module names must use underscores. This helper synthesises
    ``sys.modules["extensions.<underscore>"]`` pointing at the hyphenated
    directory, then optionally registers each submodule the tests need.

    Parameters
    ----------
    underscore_name
        e.g. ``"openai_provider"`` — becomes ``extensions.openai_provider``.
    ext_dir
        Filesystem path of the hyphenated plugin directory.
    submodules
        Sibling ``.py`` filenames (no extension) to register. Empty tuple
        skips the loop entirely (namespace-package mode for plugins with
        their own ``__init__.py``).
    exec_modules
        ``True`` (default) — fully exec each submodule via importlib so
        tests can import freely. ``False`` — register the spec but do
        NOT exec; tests must pop+reload (``aws_bedrock_provider`` style).
    bind_on_parent
        ``True`` (default) — also do ``setattr(parent, sub, mod)`` so
        pytest's ``monkeypatch.setattr("extensions.X.Y", ...)`` resolver
        can find submodules via ``getattr``. ``False`` for older
        registrations (``browser_bridge``, ``ambient_sensors``) that
        worked without binding and we don't want to behavior-change.

    Idempotent — re-calling does nothing if the alias is already in
    ``sys.modules``. Missing ``ext_dir`` or missing per-sub ``.py``
    files are silently skipped so this stays correct as plugins grow.
    """
    _ensure_extensions_pkg()

    if not ext_dir.exists():
        return

    # Mirror the production loader (opencomputer/plugins/loader.py) which
    # inserts the plugin directory at sys.path[0] before importing the
    # entry module. Without this, plugins that use bare sibling imports
    # (``from browser import ...``, ``from tools import ...``) ImportError
    # under pytest even though they work in production.
    ext_path_str = str(ext_dir.resolve())
    if ext_path_str not in sys.path:
        sys.path.insert(0, ext_path_str)

    full_pkg_name = f"extensions.{underscore_name}"
    if full_pkg_name not in sys.modules:
        mod = types.ModuleType(full_pkg_name)
        mod.__path__ = [str(ext_dir)]
        mod.__package__ = full_pkg_name
        sys.modules[full_pkg_name] = mod
        if bind_on_parent:
            setattr(sys.modules["extensions"], underscore_name, mod)

    if not submodules:
        return  # namespace-only — nothing more to do

    parent = sys.modules[full_pkg_name]
    for sub in submodules:
        full_name = f"{full_pkg_name}.{sub}"
        if full_name in sys.modules:
            if bind_on_parent:
                setattr(parent, sub, sys.modules[full_name])
            continue
        init = ext_dir / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = full_pkg_name
        sys.modules[full_name] = sub_mod
        # Class-identity alias: production loader puts the plugin dir on
        # sys.path so a sibling does ``from browser import BrowserError``;
        # the resulting module lands in sys.modules under the bare name
        # ``browser``. If the conftest only registers
        # ``extensions.browser_control.browser`` and lets a downstream
        # module re-import via the bare name, Python re-execs the file
        # → two BrowserError CLASSES → ``except BrowserError:`` misses
        # the test's instance. Aliasing the bare name to the same object
        # keeps class identity intact across both import paths.
        sys.modules.setdefault(sub, sub_mod)
        if exec_modules:
            spec.loader.exec_module(sub_mod)
        if bind_on_parent:
            setattr(parent, sub, sub_mod)


# ─── per-plugin alias functions ──────────────────────────────────────
# Each thin wrapper calls _register_extension_alias with the per-plugin
# flags. The underlying behavior is unchanged from when each function
# had its own copy-pasted body — same submodules, same exec/bind flags.

def _register_coding_harness_alias() -> None:
    """Namespace-only — coding-harness has its own ``__init__.py`` files,
    so Python's normal import machinery resolves sub-packages
    (``introspection/``, etc.) against the parent ``__path__``.
    No per-submodule registration needed; ``bind_on_parent=False`` because
    the historical version didn't bind."""
    _register_extension_alias(
        "coding_harness", _CH_DIR,
        submodules=(),
        bind_on_parent=False,
    )


def _register_aws_bedrock_provider_alias() -> None:
    """Lazy-spec — registers submodule specs but does NOT exec them.
    Tests pop+reload to control when each module's body runs."""
    _register_extension_alias(
        "aws_bedrock_provider", _BEDROCK_DIR,
        submodules=("transport", "provider", "plugin"),
        exec_modules=False,
        bind_on_parent=False,
    )


def _register_browser_bridge_alias() -> None:
    """Eager-exec, no parent-binding (historical browser-bridge pattern)."""
    _register_extension_alias(
        "browser_bridge", _EXT_DIR / "browser-bridge",
        submodules=("adapter", "plugin"),
        bind_on_parent=False,
    )


def _register_ambient_sensors_alias() -> None:
    """Eager-exec, no parent-binding (historical ambient-sensors pattern)."""
    _register_extension_alias(
        "ambient_sensors", _AMBIENT_DIR,
        submodules=("foreground", "sensitive_apps", "pause_state", "daemon", "plugin"),
        bind_on_parent=False,
    )


def _register_skill_evolution_alias() -> None:
    """Eager-exec + parent-binding for ``monkeypatch.setattr`` resolution."""
    _register_extension_alias(
        "skill_evolution", _SKILL_EVO_DIR,
        submodules=(
            "pattern_detector", "skill_extractor", "candidate_store",
            "subscriber", "session_metrics",
        ),
    )


def _register_voice_mode_alias() -> None:
    """Eager-exec + parent-binding."""
    _register_extension_alias(
        "voice_mode", _VOICE_MODE_DIR,
        submodules=(
            "audio_capture", "vad", "stt", "tts", "tts_playback",
            "playback", "orchestrator", "voice_mode", "plugin",
        ),
    )


def _register_openai_provider_alias() -> None:
    """Eager-exec + parent-binding. Realtime voice port (2026-04-29)
    needs underscore-form imports for realtime / realtime_helpers /
    plugin / provider submodules."""
    _register_extension_alias(
        "openai_provider", _OPENAI_PROVIDER_DIR,
        submodules=("provider", "realtime", "realtime_helpers", "plugin"),
    )


def _register_gemini_provider_alias() -> None:
    """Eager-exec + parent-binding. Currently realtime-only (chat
    BaseProvider port pending)."""
    _register_extension_alias(
        "gemini_provider", _GEMINI_PROVIDER_DIR,
        submodules=("realtime", "realtime_helpers", "plugin"),
    )


def _register_anthropic_provider_alias() -> None:
    """Eager-exec + parent-binding for the Anthropic chat-completion provider.
    Tests that exercise multimodal tool_result handling (screenshot + vision
    pipeline) import via ``extensions.anthropic_provider.provider``."""
    _register_extension_alias(
        "anthropic_provider", _ANTHROPIC_PROVIDER_DIR,
        submodules=("provider", "plugin"),
    )


def _register_browser_control_alias() -> None:
    """Eager-exec + parent-binding.

    Browser-control is the OpenClaw browser port (W3 wired Browser
    discriminator + deprecation shims + plugin entry). Submodules use
    ``_`` prefix on the per-tool entry (``_tool``) to avoid sys.path
    collisions with coding-harness's ``tools/`` package — same lesson
    PR #394 burned in for the legacy ``_tools.py`` predecessor.
    """
    _register_extension_alias(
        "browser_control", _BROWSER_CONTROL_DIR,
        submodules=("schema", "_tool", "_dispatcher_bootstrap", "plugin"),
    )


def _register_adapter_runner_alias() -> None:
    """Eager-exec + parent-binding for the Wave 4 adapter-runner plugin.

    Mirrors browser-control's pattern: synthesise the underscore alias
    so adapter modules + tests can do
    ``from extensions.adapter_runner import adapter, Strategy``.
    Submodules use the leading-underscore pattern to avoid sys.modules
    collisions with sibling plugins.
    """
    _register_extension_alias(
        "adapter_runner", _ADAPTER_RUNNER_DIR,
        submodules=(
            "_strategy", "_decorator", "_site_memory", "_ctx",
            "_runner", "_discovery", "_validation", "_trace",
            "_verify", "plugin",
        ),
    )
    # adapter_runner's __init__.py has public re-exports (adapter, Strategy,
    # clear_registry_for_tests, register_adapter_pack). The helper loads
    # named submodules but doesn't execute the package's __init__, so those
    # symbols never get bound on the package. Force-exec it now.
    init_file = _ADAPTER_RUNNER_DIR / "__init__.py"
    if init_file.exists():
        package = sys.modules.get("extensions.adapter_runner")
        if package is not None:
            with open(init_file, encoding="utf-8") as f:
                code = compile(f.read(), str(init_file), "exec")
            exec(code, package.__dict__)


def _register_affect_injection_alias() -> None:
    """Eager-exec + parent-binding."""
    _register_extension_alias(
        "affect_injection", _AFFECT_INJECTION_DIR,
        submodules=("provider", "plugin"),
    )


def _register_screen_awareness_alias() -> None:
    """Eager-exec + parent-binding."""
    _register_extension_alias(
        "screen_awareness", _SCREEN_AWARENESS_DIR,
        submodules=(
            "lock_detect", "sensitive_apps", "diff", "ring_buffer",
            "sensor", "persist", "recall_tool", "injection_provider",
            "state", "plugin",
        ),
    )


def _register_ollama_provider_alias() -> None:
    """Eager-exec + parent-binding for the Ollama local LLM provider."""
    _register_extension_alias(
        "ollama_provider", _OLLAMA_PROVIDER_DIR,
        submodules=("provider", "plugin"),
    )


def _register_groq_provider_alias() -> None:
    """Eager-exec + parent-binding for the Groq fast-inference provider."""
    _register_extension_alias(
        "groq_provider", _GROQ_PROVIDER_DIR,
        submodules=("provider", "plugin"),
    )


def _register_memory_mem0_alias() -> None:
    """Hermes A3 — Mem0 memory backend extension. Eager-exec so tests can
    ``from extensions.memory_mem0.provider import Mem0Provider``.
    """
    _register_extension_alias(
        "memory_mem0", _EXT_DIR / "memory-mem0",
        submodules=("provider", "plugin"),
    )


_register_coding_harness_alias()
_register_aws_bedrock_provider_alias()
_register_browser_bridge_alias()
_register_ambient_sensors_alias()
_register_skill_evolution_alias()
_register_voice_mode_alias()
_register_openai_provider_alias()
_register_gemini_provider_alias()
_register_anthropic_provider_alias()
# browser-control transitively imports fastapi via server/app.py during
# the alias eager-exec. The introspection cross-platform CI job
# deliberately skips installing the [browser] extras, so fastapi is
# missing there — guard the registration so the rest of conftest still
# loads. Tests that actually need browser-control will hit the
# ImportError on their own import statement (which is the right place
# for it).
try:
    _register_browser_control_alias()
except ModuleNotFoundError as _e:
    if "fastapi" not in str(_e):
        raise
_register_adapter_runner_alias()
_register_affect_injection_alias()
_register_screen_awareness_alias()
_register_ollama_provider_alias()
_register_groq_provider_alias()
_register_memory_mem0_alias()


# ── Test-isolation fixtures (added 2026-05-10) ───────────────────────
# Three documented full-suite-only flakes existed on origin/main as of
# 2026-05-08:
#   1. tests/test_phase12b1_honcho_default.py::
#      test_agent_loop_multi_turn_snapshot_stays_identical_across_different_prefetches
#   2. tests/test_typed_event_bus.py::
#      test_apublish_runs_async_handlers_concurrently
#   3. tests/test_voice_mode_stt.py::test_openai_api_when_key_set
#      (segfault — pycares + asyncio cross-loop)
#   4. tests/test_browser_control_extension_daemon.py::
#      test_command_timeout_when_extension_silent
#
# Root cause across all four: tests that mutate global singletons
# (TypedEventBus default_bus, asyncio loops left with running tasks,
# aiohttp client sessions never explicitly closed) leak state into
# subsequent tests. The flakes don't always fire — depends on which
# polluter ran first AND whether GC happened to clean up.
#
# These fixtures defend against those patterns at the harness level
# without skipping any test or modifying production code. They are:
#   * function-scoped + cheap (≤1 ms aggregate per test)
#   * autouse=True so every test inherits them — no opt-in needed
#   * silent in the success case; emit a single WARN once per session
#     when they had to clean something up (so a real polluter still
#     surfaces in CI logs)

_iso_log = logging.getLogger("conftest.test_isolation")
_polluter_warnings_shown: set[str] = set()


def _warn_once(category: str, detail: str) -> None:
    """Emit a WARN line at most once per pytest session per category.

    Avoids drowning a 13k-test run in noise while still surfacing a real
    polluter the first time it shows up.
    """
    if category in _polluter_warnings_shown:
        return
    _polluter_warnings_shown.add(category)
    _iso_log.warning("test pollution detected (%s): %s", category, detail)


@pytest.fixture(autouse=True)
def _reset_event_bus_singleton() -> Iterator[None]:
    """Clear bus subscribers between tests; preserve the singleton identity.

    Subscribers added by one test would otherwise fire on events
    published by later tests, causing:
      * non-deterministic timing assertions (concurrent-handlers test)
      * accidental cross-test event coupling

    We CLEAR the existing bus's handlers in-place rather than swapping
    the singleton for a new instance — that preserves the identity
    relied on by ``test_default_bus_is_singleton`` (imported at module
    load time and asserted ``is`` against ``get_default_bus()``).
    """
    yield
    try:
        from opencomputer.ingestion.bus import default_bus as _existing
    except ImportError:
        return  # bus module not built — nothing to reset

    if _existing is None:
        return
    handlers = getattr(_existing, "_handlers", None)
    if not handlers:
        return
    handler_count = sum(len(h) for h in handlers.values())
    if handler_count > 0:
        _warn_once(
            "event_bus_handlers_leak",
            f"default_bus carried {handler_count} unsubscribed handlers "
            f"into teardown — a test forgot to unsubscribe",
        )
        # Clear in-place — preserves singleton identity.
        for v in handlers.values():
            v.clear()


# Session-scoped warnings filter — set once, cheap per-test.
# Filters the pycares/aiohttp ResourceWarnings that have caused native
# aborts when surfaced from a background thread mid-teardown of the
# NEXT test's event loop. The warnings are advisory (the underlying
# socket gets closed by GC eventually), so suppressing the noise is
# safe; real aiohttp errors still propagate as test failures.
def pytest_configure(config) -> None:  # noqa: D401 — pytest hook
    """Filter known-noisy ResourceWarnings that have caused native aborts."""
    warnings.filterwarnings(
        "ignore",
        message=r".*unclosed transport.*pycares.*",
        category=ResourceWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r".*unclosed.*<aiohttp.connector\..*",
        category=ResourceWarning,
    )


@pytest.fixture(autouse=True)
def _drop_orphan_browser_daemon_singleton() -> Iterator[None]:
    """Clear the browser-control shared-daemon singleton reference.

    Symptom this defends against: ``test_command_timeout_when_extension_silent``
    failing intermittently because a prior test left
    ``control_driver._shared_daemon`` pointing at a daemon that's
    bound to a stale port + has stale extension-connection state.

    We DON'T await ``daemon.stop()`` (that needs an event loop, which
    risks deadlocks across pytest-asyncio's per-test loops). We just
    NULL the singleton — the daemon will be GC'd when its references
    drop, and the next test that needs one creates a fresh instance.
    """
    yield
    try:
        from extensions.browser_control import control_driver as _cd
    except ImportError:
        return
    if getattr(_cd, "_shared_daemon", None) is not None:
        _cd._shared_daemon = None


def _resolve_real_profile_home() -> Path:
    """Resolve the same home path ``opencomputer.agent.config._home()`` uses.

    Kept in sync with production by mirroring the same env-var precedence
    and default location. Returning a ``Path`` rather than a string so
    callers can use ``.glob`` / ``.unlink`` directly. Symlinks are NOT
    resolved here — production code passes the symlinked path to
    ``record_rate_limit`` and we need to clean files at the same logical
    location, not the resolved one.
    """
    raw = os.environ.get("OPENCOMPUTER_HOME") or str(Path.home() / ".opencomputer")
    return Path(raw)


def _purge_rate_limit_state_files(home: Path) -> list[str]:
    """Delete every ``<home>/rate_limits/*.json`` file, return names removed.

    Race-safe: uses ``Path.unlink(missing_ok=True)`` so concurrent
    pytest-xdist workers don't trip on each other. ``OSError`` from
    permission denied / device errors is logged but never raised — a
    test polluter is not worth blowing up the whole test session over.

    Returns the list of file names that EXISTED at glob time. The
    caller uses this list to emit a single WARN per session via
    ``_warn_once``. An empty return value means "no pollution detected".
    """
    rate_dir = home / "rate_limits"
    if not rate_dir.exists():
        return []

    # Glob first, then unlink. If a concurrent worker raced us between
    # glob and unlink, the missing_ok=True will swallow the result.
    leaked_paths = list(rate_dir.glob("*.json"))
    leaked_names: list[str] = []
    for path in leaked_paths:
        leaked_names.append(path.name)
        try:
            path.unlink(missing_ok=True)
        except OSError as err:
            # Permission denied, read-only filesystem, etc. Don't abort
            # the test run — log via the well-known iso_log channel so
            # the source of the failure surfaces in CI without wedging.
            _iso_log.warning(
                "could not delete leaked rate-limit state %s: %s", path, err
            )
    return leaked_names


@pytest.fixture(autouse=True)
def _clear_provider_rate_limit_pollution() -> Iterator[None]:
    """Delete leaked ``rate_limits/<provider>.json`` state files before each test.

    Symptom this defends against: a test path that instantiates a
    provider plugin (anthropic, openai, …) without first setting
    ``OPENCOMPUTER_HOME`` can call ``record_rate_limit`` against the
    user's REAL ``~/.opencomputer/rate_limits/<provider>.json``.
    The persisted ``reset_at`` then makes subsequent tests'
    ``rate_limit_remaining`` calls return a non-None cooldown for
    ~5 minutes, causing them to skip outbound calls and assert wrong
    state — the "10 anthropic-provider full-sweep flakes" pattern.

    We DON'T globally redirect ``OPENCOMPUTER_HOME`` (some tests
    legitimately set it themselves, and a blanket redirect would
    surprise tests that probe production-path behaviour). Instead we
    nuke the known state files at the resolved home BEFORE each test
    so leakage is bounded to one test's window. Pollution still has
    to come from somewhere — flagged as a separate WARN via
    ``_warn_once`` when a stale file is found, so a real leak source
    surfaces in CI.

    The cleanup logic is factored into ``_purge_rate_limit_state_files``
    so it can be unit-tested without invoking pytest's autouse machinery
    (see ``tests/test_conftest_rate_limit_pollution_fixture.py``).
    """
    home = _resolve_real_profile_home()
    leaked = _purge_rate_limit_state_files(home)
    if leaked:
        _warn_once(
            "rate_limit_state_leak",
            f"deleted stale {leaked} before test (a prior test wrote "
            f"rate-limit state to the real OPENCOMPUTER_HOME={home})",
        )
    yield
