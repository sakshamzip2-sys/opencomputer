"""affect-injection plugin entry — Prompt B (2026-04-28).

Registers a single ``DynamicInjectionProvider`` that contributes a
``<user-state>`` block to the system prompt every chat turn. See
``provider.py`` for the read-only logic and ``CONTRACT.md`` for the
schema downstream consumers should rely on.

The plugin loader synthesises a unique top-level module name per
plugin via ``importlib.util.spec_from_file_location``, which means a
``from .provider import …`` relative import fails at runtime (no
parent package). Same caveat as ``extensions/memory-honcho/plugin.py``.
We self-install the ``extensions.affect_injection`` namespace alias
inside ``register()`` and then absolute-import through it.
"""
from __future__ import annotations

import logging
import sys as _sys
import types as _types
from pathlib import Path as _Path

_log = logging.getLogger("opencomputer.affect_injection.plugin")


def _install_alias() -> None:
    """Make ``extensions.affect_injection.provider`` importable.

    Mirrors the alias pattern in ``extensions/memory-honcho/plugin.py``
    (and ``tests/conftest.py``'s ``_register_affect_injection_alias``
    helper). Production-load goes through the plugin-loader's synthetic
    name, so we register ``extensions.affect_injection`` here as a
    namespace pointing at our hyphenated dir, then exec the provider
    submodule into that namespace. Idempotent — second call no-ops.
    """
    here = _Path(__file__).resolve().parent
    if "extensions" not in _sys.modules:
        ext_pkg = _types.ModuleType("extensions")
        ext_pkg.__path__ = [str(here.parent)]
        _sys.modules["extensions"] = ext_pkg
    if "extensions.affect_injection" not in _sys.modules:
        ai_pkg = _types.ModuleType("extensions.affect_injection")
        ai_pkg.__path__ = [str(here)]
        ai_pkg.__package__ = "extensions.affect_injection"
        _sys.modules["extensions.affect_injection"] = ai_pkg
        _sys.modules["extensions"].affect_injection = ai_pkg  # type: ignore[attr-defined]
    parent = _sys.modules["extensions.affect_injection"]
    full_name = "extensions.affect_injection.provider"
    if full_name in _sys.modules:
        setattr(parent, "provider", _sys.modules[full_name])
        return
    init = here / "provider.py"
    if not init.exists():
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location(full_name, str(init))
    if spec is None or spec.loader is None:
        return
    sub_mod = importlib.util.module_from_spec(spec)
    sub_mod.__package__ = "extensions.affect_injection"
    _sys.modules[full_name] = sub_mod
    spec.loader.exec_module(sub_mod)
    setattr(parent, "provider", sub_mod)


def register(api) -> None:  # noqa: ANN001
    """Plugin entry. Builds the provider with env-sourced config and
    registers it via the standard plugin SDK injection surface.

    Honours ``api.session_db_path`` so the provider can read
    ``SessionDB.get_session_vibe`` at the active profile's DB. When the
    SDK does not expose a path (older callers, tests), the provider
    degrades gracefully — ``_read_session_vibe`` returns ``None`` and
    the per-turn vibe still drives the block.
    """
    _install_alias()

    # Absolute import via the alias we just installed. Relative
    # ``from .provider import …`` would fail under the synthetic loader.
    from extensions.affect_injection.provider import (
        affect_injection_provider_from_env,
    )

    db_path = getattr(api, "session_db_path", None)
    provider = affect_injection_provider_from_env(db_path=db_path)

    register_fn = getattr(api, "register_injection_provider", None)
    if register_fn is None:
        _log.warning(
            "affect-injection: api has no register_injection_provider() — "
            "skipping registration (older host)"
        )
        return
    register_fn(provider)
    _log.debug(
        "affect-injection registered (provider_id=%s, min_turns=%d)",
        provider.provider_id,
        provider._min_turns,  # noqa: SLF001 — debug log only
    )
