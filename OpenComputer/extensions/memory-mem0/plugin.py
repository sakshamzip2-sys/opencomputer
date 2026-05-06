"""OpenComputer plugin: Mem0 memory provider (Hermes A3).

``register()`` instantiates :class:`Mem0Provider` and calls
``api.register_memory_provider``. Gracefully degrades on older cores
that don't have the method (logs a warning and exits clean).

Configuration is environment-driven so users can opt in without
touching plugin internals:

- ``MEM0_API_KEY``  — for the hosted Mem0 cloud (default).
- ``MEM0_BASE_URL`` — for self-hosted Mem0; passed through to the SDK.
- ``MEM0_USER_ID``  — namespace; defaults to the active OC profile.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any


def _derive_user_id_from_profile() -> str:
    """Mirror Honcho's host-key-from-profile derivation.

    Profile-aware namespacing is what makes "memory feels personal per
    profile" actually work: switching from default to a "work" profile
    must not show the user their personal memories. Mirrors
    ``extensions/memory-honcho/plugin.py:_derive_host_key_from_profile``.
    """
    active = os.environ.get("OPENCOMPUTER_PROFILE", "default").strip()
    if not active or active == "default":
        return "opencomputer"
    return f"opencomputer.{active}"


def _config_from_env() -> Any:
    """Build a :class:`Mem0Config` from environment variables.

    Imported lazily through the synthetic-package alias trick (see
    register() below for why) so the plugin loader's
    ``importlib.util.spec_from_file_location`` machinery sees a
    well-formed package path.
    """
    _install_module_alias()
    from extensions.memory_mem0.provider import Mem0Config

    explicit_user_id = os.environ.get("MEM0_USER_ID", "").strip()
    user_id = explicit_user_id or _derive_user_id_from_profile()

    return Mem0Config(
        api_key=os.environ.get("MEM0_API_KEY", "").strip(),
        base_url=os.environ.get("MEM0_BASE_URL", "").strip(),
        user_id=user_id,
        enabled=True,
    )


def _install_module_alias() -> None:
    """Insert ``extensions.memory_mem0`` into ``sys.modules`` if missing.

    The plugin loader uses :func:`importlib.util.spec_from_file_location`
    with synthetic names, so a relative ``from .provider`` import has
    no parent package and fails at runtime. Honcho hits the same issue
    and works around it with this alias dance — copy that pattern.
    """
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(Path(__file__).resolve().parent.parent)]
        sys.modules["extensions"] = ext_pkg
    if "extensions.memory_mem0" not in sys.modules:
        m0_pkg = types.ModuleType("extensions.memory_mem0")
        m0_pkg.__path__ = [str(Path(__file__).resolve().parent)]
        m0_pkg.__package__ = "extensions.memory_mem0"
        sys.modules["extensions.memory_mem0"] = m0_pkg


def register(api: Any) -> None:
    """Register the Mem0 memory provider with the plugin API.

    Tolerates older cores that don't have ``register_memory_provider``;
    logs a warning and skips so the agent keeps working on baseline.

    Tolerates missing ``mem0ai`` SDK; the provider self-degrades to a
    no-op (see :class:`Mem0Provider._ensure_client`).
    """
    _install_module_alias()
    from extensions.memory_mem0.provider import Mem0Provider

    provider = Mem0Provider(_config_from_env())
    register_fn = getattr(api, "register_memory_provider", None)
    if register_fn is None:
        import logging

        logging.getLogger("memory-mem0").warning(
            "core does not support register_memory_provider; Mem0 plugin "
            "installed but inactive. Upgrade OpenComputer to schema_v3+."
        )
        return
    register_fn(provider)
