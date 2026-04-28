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


def _ensure_extensions_pkg() -> None:
    """Synthesise a namespace package for 'extensions' if not already registered."""
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg


def _register_coding_harness_alias() -> None:
    """Register extensions.coding_harness → extensions/coding-harness/ in sys.modules.

    The parent package is registered as a synthetic namespace module with
    ``__path__`` pointing at the hyphenated directory; Python's standard
    import machinery then resolves sub-packages (e.g. ``introspection/``)
    against that path automatically. No explicit per-submodule registration
    is required for sub-packages that have their own ``__init__.py``.
    """
    _ensure_extensions_pkg()

    if "extensions.coding_harness" not in sys.modules:
        # coding-harness is a plugin dir — no __init__.py at the root; treat as namespace.
        ch_mod = types.ModuleType("extensions.coding_harness")
        ch_mod.__path__ = [str(_CH_DIR)]
        ch_mod.__package__ = "extensions.coding_harness"
        sys.modules["extensions.coding_harness"] = ch_mod


def _register_aws_bedrock_provider_alias() -> None:
    """Register extensions.aws_bedrock_provider → extensions/aws-bedrock-provider/.

    PR-C: allows test_bedrock_provider.py to import via the underscore form
    (Python module name) while the directory keeps the canonical hyphenated name.
    Mirrors the pattern used for coding_harness above.
    """
    _ensure_extensions_pkg()

    if "extensions.aws_bedrock_provider" not in sys.modules:
        mod = types.ModuleType("extensions.aws_bedrock_provider")
        mod.__path__ = [str(_BEDROCK_DIR)]
        mod.__package__ = "extensions.aws_bedrock_provider"
        sys.modules["extensions.aws_bedrock_provider"] = mod

    # Register transport.py and provider.py as importable sub-modules
    for sub in ("transport", "provider", "plugin"):
        full_name = f"extensions.aws_bedrock_provider.{sub}"
        if full_name not in sys.modules:
            init = _BEDROCK_DIR / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                full_name,
                str(init),
            )
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.aws_bedrock_provider"
            sys.modules[full_name] = sub_mod
            # Do NOT exec yet — tests control when the module loads


def _register_browser_bridge_alias() -> None:
    """Register extensions.browser_bridge → extensions/browser-bridge/.

    Mirrors the pattern used for ``extensions.aws_bedrock_provider`` —
    plugins live in hyphenated dirs, but Python modules need underscores.
    Layered Awareness MVP T10: lets tests import the adapter / plugin
    Python modules from the hyphenated ``browser-bridge/`` directory.

    We register the parent package (with ``__path__`` pointing at the
    hyphenated dir) so Python's standard import machinery resolves
    ``extensions.browser_bridge.adapter`` against ``adapter.py`` in
    that directory. We pre-stub the sub-modules with their spec but
    actually execute them on first import — unlike the bedrock pattern
    (which expects test fixtures to ``sys.modules.pop()`` before import),
    the browser-bridge tests import directly, so leaving an unexecuted
    stub in ``sys.modules`` would mask the real module.
    """
    _ensure_extensions_pkg()
    _BB_DIR = _EXT_DIR / "browser-bridge"

    if "extensions.browser_bridge" not in sys.modules:
        mod = types.ModuleType("extensions.browser_bridge")
        mod.__path__ = [str(_BB_DIR)]
        mod.__package__ = "extensions.browser_bridge"
        sys.modules["extensions.browser_bridge"] = mod

    for sub in ("adapter", "plugin"):
        full_name = f"extensions.browser_bridge.{sub}"
        if full_name not in sys.modules:
            init = _BB_DIR / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(full_name, str(init))
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.browser_bridge"
            sys.modules[full_name] = sub_mod
            spec.loader.exec_module(sub_mod)


def _register_ambient_sensors_alias() -> None:
    """Register extensions.ambient_sensors → extensions/ambient-sensors/.

    Mirrors the browser_bridge pattern (eager exec on first import) — the
    ambient-sensors plugin dir is hyphenated, so tests import the Python
    modules via the underscore form. Only ``foreground.py`` exists in T2;
    later tasks (T3-T6) add ``sensitive_apps.py``, ``pause_state.py``,
    ``daemon.py``, and ``plugin.py``. The loop below skips files that
    don't yet exist, so this stays correct as the plugin grows.
    """
    _ensure_extensions_pkg()

    if not _AMBIENT_DIR.exists():
        # Directory does not exist yet (e.g. on a stale checkout). Register
        # nothing; later test imports will fail with a clear ModuleNotFoundError.
        return

    if "extensions.ambient_sensors" not in sys.modules:
        mod = types.ModuleType("extensions.ambient_sensors")
        mod.__path__ = [str(_AMBIENT_DIR)]
        mod.__package__ = "extensions.ambient_sensors"
        sys.modules["extensions.ambient_sensors"] = mod

    for sub in ("foreground", "sensitive_apps", "pause_state", "daemon", "plugin"):
        full_name = f"extensions.ambient_sensors.{sub}"
        if full_name in sys.modules:
            continue
        init = _AMBIENT_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.ambient_sensors"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)


def _register_skill_evolution_alias() -> None:
    """Register extensions.skill_evolution → extensions/skill-evolution/.

    Mirrors the ambient_sensors pattern (eager exec on first import) — the
    skill-evolution plugin dir is hyphenated, so tests import the Python
    modules via the underscore form. The loop below skips files that
    don't yet exist, so this stays correct as the plugin grows from T2
    (pattern_detector) onward.
    """
    _ensure_extensions_pkg()

    if not _SKILL_EVO_DIR.exists():
        return

    if "extensions.skill_evolution" not in sys.modules:
        mod = types.ModuleType("extensions.skill_evolution")
        mod.__path__ = [str(_SKILL_EVO_DIR)]
        mod.__package__ = "extensions.skill_evolution"
        sys.modules["extensions.skill_evolution"] = mod
        # Bind on parent so pytest's monkeypatch dotted-path resolver
        # (which uses getattr) can find ``extensions.skill_evolution``.
        sys.modules["extensions"].skill_evolution = mod

    parent = sys.modules["extensions.skill_evolution"]
    for sub in ("pattern_detector", "skill_extractor", "candidate_store", "subscriber", "session_metrics"):
        full_name = f"extensions.skill_evolution.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _SKILL_EVO_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.skill_evolution"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        # Bind on parent so monkeypatch.setattr("extensions.skill_evolution.X", ...) works.
        setattr(parent, sub, sub_mod)


def _register_voice_mode_alias() -> None:
    """Register extensions.voice_mode → extensions/voice-mode/.

    Mirrors the skill_evolution / ambient_sensors pattern (eager exec on
    first import) — the voice-mode plugin dir is hyphenated, so tests
    import the Python modules via the underscore form. The loop below
    skips files that don't yet exist, so this stays correct as the
    plugin grows from T1 (audio_capture) through T5 (main loop).
    """
    _ensure_extensions_pkg()

    if not _VOICE_MODE_DIR.exists():
        return

    if "extensions.voice_mode" not in sys.modules:
        mod = types.ModuleType("extensions.voice_mode")
        mod.__path__ = [str(_VOICE_MODE_DIR)]
        mod.__package__ = "extensions.voice_mode"
        sys.modules["extensions.voice_mode"] = mod
        # Bind on parent so pytest's monkeypatch dotted-path resolver
        # (which uses getattr) can find ``extensions.voice_mode``.
        sys.modules["extensions"].voice_mode = mod

    parent = sys.modules["extensions.voice_mode"]
    for sub in ("audio_capture", "vad", "stt", "tts", "tts_playback", "playback", "orchestrator", "voice_mode", "plugin"):
        full_name = f"extensions.voice_mode.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _VOICE_MODE_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.voice_mode"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        # Bind on parent so monkeypatch.setattr("extensions.voice_mode.X", ...) works.
        setattr(parent, sub, sub_mod)


def _register_browser_control_alias() -> None:
    """Register extensions.browser_control → extensions/browser-control/.

    Mirrors the voice_mode / skill_evolution pattern (eager exec on first
    import) — the browser-control plugin dir is hyphenated, so tests
    import the Python modules via the underscore form. The loop below
    skips files that don't yet exist, so this stays correct as the
    plugin grows.
    """
    _ensure_extensions_pkg()

    if not _BROWSER_CONTROL_DIR.exists():
        return

    if "extensions.browser_control" not in sys.modules:
        mod = types.ModuleType("extensions.browser_control")
        mod.__path__ = [str(_BROWSER_CONTROL_DIR)]
        mod.__package__ = "extensions.browser_control"
        sys.modules["extensions.browser_control"] = mod
        # Bind on parent so pytest's monkeypatch dotted-path resolver
        # (which uses getattr) can find ``extensions.browser_control``.
        sys.modules["extensions"].browser_control = mod

    parent = sys.modules["extensions.browser_control"]
    for sub in ("browser", "tools", "plugin"):
        full_name = f"extensions.browser_control.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _BROWSER_CONTROL_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.browser_control"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        # Bind on parent so monkeypatch.setattr("extensions.browser_control.X", ...) works.
        setattr(parent, sub, sub_mod)


_register_coding_harness_alias()
_register_aws_bedrock_provider_alias()
_register_browser_bridge_alias()
_register_ambient_sensors_alias()
_register_skill_evolution_alias()
_register_voice_mode_alias()
_register_browser_control_alias()
