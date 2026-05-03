# ruff: noqa: N999  — directory name `adapter-runner` matches OpenComputer's
# extension naming convention; the conftest alias maps to `adapter_runner`.
"""adapter-runner — public surface for adapter-pack authors.

Adapter authors write::

    from extensions.adapter_runner import adapter, Strategy

    @adapter(site="hackernews", name="top", ..., strategy=Strategy.PUBLIC)
    async def run(args, ctx):
        ...

Everything else is internal (``_decorator.py`` / ``_runner.py`` / etc.).
"""

from __future__ import annotations

from ._decorator import (
    AdapterArg,
    AdapterSpec,
    adapter,
    clear_registry_for_tests,
    get_adapter,
    get_registered_adapters,
)
from ._strategy import Strategy

__all__ = [
    "adapter",
    "AdapterArg",
    "AdapterSpec",
    "Strategy",
    "clear_registry_for_tests",
    "get_adapter",
    "get_registered_adapters",
    "register_adapter_at_runtime",
    "register_adapter_pack",
]


def register_adapter_at_runtime(spec):  # type: ignore[no-untyped-def]
    """Hot-reload entrypoint — see ``plugin.register_adapter_at_runtime``.

    Re-exported here so ``Browser(action="adapter_save")`` can call it
    via ``from extensions.adapter_runner import register_adapter_at_runtime``
    without poking at the plugin module directly.
    """
    from .plugin import register_adapter_at_runtime as _impl

    return _impl(spec)


def register_adapter_pack(api, *, adapters_dir):
    """Helper used by adapter-pack plugins' ``plugin.py``.

    Walks the pack's ``adapters/`` dir, imports each ``.py`` adapter
    file, and registers the resulting ``AdapterSpec``s as synthetic
    tools on ``api``. Idempotent — re-importing already-loaded modules
    is a no-op.
    """
    from pathlib import Path

    from ._decorator import get_registered_adapters
    from ._discovery import _import_adapter_file
    from .plugin import _register_specs_with_api

    adapters_dir = Path(adapters_dir).resolve()
    if not adapters_dir.is_dir():
        return

    before_keys = {(s.site, s.name) for s in get_registered_adapters()}
    pack_name = adapters_dir.parent.name or "pack"
    for path in sorted(adapters_dir.rglob("*.py")):
        if path.name.startswith("__"):
            continue
        if "verify" in path.parts or "fixtures" in path.parts:
            continue
        _import_adapter_file(path, prefix=f"pack.{pack_name}")

    new_specs = [
        s for s in get_registered_adapters() if (s.site, s.name) not in before_keys
    ]
    _register_specs_with_api(api, new_specs)
