"""Walks the filesystem for ``@adapter``-decorated modules and imports them.

Sources, in priority order (later sources win on duplicate ``(site, name)``):
  1. ``extensions/browser-control/adapters/**/*.py`` — bundled curated pack
  2. ``extensions/<other-plugin>/adapters/**/*.py`` — installed adapter-pack plugins
  3. ``~/.opencomputer/<profile>/adapters/**/*.py`` — user-authored

Each ``.py`` is imported under a synthetic, unique module name (mirrors
``opencomputer/plugins/loader.py``'s pattern) so the bundled, plugin,
and user copies of ``hackernews/top.py`` can all coexist in
``sys.modules`` without colliding.

Discovery is idempotent — re-running returns specs from the live
registry without re-importing already-loaded modules.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from ._decorator import AdapterSpec, get_registered_adapters

_log = logging.getLogger("opencomputer.adapter_runner.discovery")


@dataclass(slots=True)
class DiscoveryResult:
    """Outcome of one ``discover_adapters`` call."""

    specs: list[AdapterSpec]
    errors: list[str]
    sources: dict[str, list[Path]]


def discover_adapters(
    *,
    profile_home: Path | None = None,
    extensions_root: Path | None = None,
) -> DiscoveryResult:
    """Walk all known adapter source dirs + import each ``.py`` once.

    Returns a ``DiscoveryResult`` with the freshly-collected ``AdapterSpec``s
    plus any import errors (kept as a list so the caller can surface
    them in a doctor row without aborting registration).
    """
    errors: list[str] = []
    sources: dict[str, list[Path]] = {
        "bundled": [],
        "plugins": [],
        "user": [],
    }

    # 1) Bundled — extensions/browser-control/adapters/**
    if extensions_root is None:
        # ``extensions/adapter-runner/_discovery.py`` → walk up two parents
        # to reach ``extensions/`` itself.
        extensions_root = Path(__file__).resolve().parent.parent

    bundled_root = extensions_root / "browser-control" / "adapters"
    if bundled_root.is_dir():
        for path in _walk_python_files(bundled_root):
            sources["bundled"].append(path)
            err = _import_adapter_file(path, prefix="bundled")
            if err:
                errors.append(err)

    # 2) Installed adapter-pack plugins — extensions/<plugin>/adapters/**
    if not extensions_root.is_dir():
        # Test fixtures sometimes pass a non-existent root; treat as empty.
        extensions_iter: list[Path] = []
    else:
        extensions_iter = sorted(extensions_root.iterdir())
    for plugin_dir in extensions_iter:
        if not plugin_dir.is_dir():
            continue
        if plugin_dir.name in ("browser-control", "adapter-runner"):
            continue
        adapters_dir = plugin_dir / "adapters"
        if not adapters_dir.is_dir():
            continue
        for path in _walk_python_files(adapters_dir):
            sources["plugins"].append(path)
            err = _import_adapter_file(path, prefix=f"plugins.{plugin_dir.name}")
            if err:
                errors.append(err)

    # 3) User-authored — ~/.opencomputer/<profile>/adapters/**
    if profile_home is not None:
        user_root = Path(profile_home) / "adapters"
        if user_root.is_dir():
            for path in _walk_python_files(user_root):
                sources["user"].append(path)
                err = _import_adapter_file(path, prefix="user")
                if err:
                    errors.append(err)

    return DiscoveryResult(
        specs=get_registered_adapters(),
        errors=errors,
        sources=sources,
    )


def _walk_python_files(root: Path) -> list[Path]:
    """Yield every ``.py`` under ``root`` (sorted, recursive)."""
    out: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        if p.name.startswith("__"):
            continue
        if "verify" in p.parts or "fixtures" in p.parts:
            # Skip fixture dirs — they sometimes contain Python helpers
            # that aren't adapters proper.
            continue
        out.append(p)
    return out


def _import_adapter_file(path: Path, *, prefix: str) -> str | None:
    """Import a single adapter ``.py`` under a synthetic unique name.

    Returns a human-readable error string on failure, or None on
    success. Idempotent: re-importing the same path on the same
    process is a no-op (sys.modules cache hit).
    """
    # Synthetic module name: ``oc_adapter.<prefix>.<relative-path-with-dots>``
    # so ``bundled/hackernews/top.py`` becomes
    # ``oc_adapter.bundled.hackernews.top``. Slashes → dots, drop ``.py``.
    rel_parts = list(path.with_suffix("").parts)
    # Trim everything before "adapters" so the synthetic name is short.
    try:
        idx = rel_parts.index("adapters")
        rel_parts = rel_parts[idx + 1 :]
    except ValueError:
        pass  # path doesn't contain ``adapters/`` — keep as-is

    mod_name = "oc_adapter." + prefix + "." + ".".join(rel_parts) if rel_parts else (
        f"oc_adapter.{prefix}.__root__"
    )
    if mod_name in sys.modules:
        return None  # already imported

    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        return f"could not build importlib spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        # Drop the half-loaded module so a retry has a clean slate.
        sys.modules.pop(mod_name, None)
        _log.warning("Failed to import adapter %s: %s", path, exc)
        return f"{path}: {exc!r}"
    return None


__all__ = ["DiscoveryResult", "discover_adapters"]
