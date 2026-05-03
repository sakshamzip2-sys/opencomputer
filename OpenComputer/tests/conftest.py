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
import sys
import types
from pathlib import Path

# Project root (parent of tests/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_CH_DIR = _EXT_DIR / "coding-harness"
_BEDROCK_DIR = _EXT_DIR / "aws-bedrock-provider"
_AMBIENT_DIR = _EXT_DIR / "ambient-sensors"
_SKILL_EVO_DIR = _EXT_DIR / "skill-evolution"
_VOICE_MODE_DIR = _EXT_DIR / "voice-mode"
_BROWSER_CONTROL_DIR = _EXT_DIR / "browser-control"
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


_register_coding_harness_alias()
_register_aws_bedrock_provider_alias()
_register_browser_bridge_alias()
_register_ambient_sensors_alias()
_register_skill_evolution_alias()
_register_voice_mode_alias()
_register_openai_provider_alias()
_register_gemini_provider_alias()
_register_anthropic_provider_alias()
_register_browser_control_alias()
_register_affect_injection_alias()
_register_screen_awareness_alias()
_register_ollama_provider_alias()
_register_groq_provider_alias()
