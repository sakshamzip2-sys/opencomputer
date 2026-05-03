"""adapter-runner plugin entry — discovers adapters at boot + registers
each as a synthetic ``BaseTool``.

Tool naming: each adapter ``(site, name)`` becomes ``<Site><Name>`` in
PascalCase — e.g. ``hackernews/top`` → ``HackernewsTop``,
``atria/assignments`` → ``AtriaAssignments``.

Discovery sources (priority order — later wins on dup):
  1. Bundled curated pack: ``extensions/browser-control/adapters/**``
  2. Installed plugin packs: ``extensions/<plugin>/adapters/**``
  3. User-authored: ``~/.opencomputer/<profile>/adapters/**``

Doctor row reports the count of registered adapters + any import
errors encountered during discovery.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.doctor import HealthContribution, RepairResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.adapter_runner.plugin")

#: Hyphenated-on-disk → underscore alias (mirrors ``browser-control``'s
#: ``_bootstrap_package_namespace``). The decorator + ctx modules use
#: relative imports (``from ._decorator import ...``), so the package
#: must be visible under ``extensions.adapter_runner`` for them to
#: resolve.
_PKG = "extensions.adapter_runner"
_PARENT_PKG = "extensions"


def _bootstrap_package_namespace() -> None:
    """Make ``extensions.adapter_runner`` resolvable in sys.modules.

    The plugin loader puts the plugin's own directory on ``sys.path``
    and exec's ``plugin.py`` under a synthetic name — ``__init__.py``
    is never run. Without this bootstrap, an adapter file that does
    ``from extensions.adapter_runner import adapter, Strategy`` fails
    with ``ModuleNotFoundError`` (the ``extensions.adapter_runner``
    package alias is missing) or ``ImportError`` (the alias exists as
    an empty stub because ``__init__.py``'s public re-exports were
    never bound).

    Idempotent — safe to call multiple times. Mirrors
    ``tests/conftest.py::_register_adapter_runner_alias()`` so the test
    + production import shapes stay aligned.
    """
    plugin_root = Path(__file__).resolve().parent
    extensions_root = plugin_root.parent

    # Ensure ``extensions/`` is on sys.path so ``import extensions``
    # resolves as a namespace package even when the plugin loader
    # didn't add it. Idempotent.
    extensions_root_str = str(extensions_root)
    if extensions_root_str not in sys.path:
        sys.path.insert(0, extensions_root_str)

    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [extensions_root_str]
        parent.__package__ = _PARENT_PKG
        sys.modules[_PARENT_PKG] = parent

    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(plugin_root)]
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
        sys.modules[_PARENT_PKG].adapter_runner = pkg

    # Ensure the public re-exports defined in ``__init__.py`` (adapter,
    # Strategy, get_adapter, ...) are bound on the synthesized package.
    # Plain ``types.ModuleType`` creates an empty module — without this
    # exec, ``from extensions.adapter_runner import adapter`` raises
    # ImportError because ``adapter`` was never bound. Skip when the
    # symbols are already present (test conftest exec'd them, or this
    # function ran already).
    if not hasattr(pkg, "adapter"):
        init_file = plugin_root / "__init__.py"
        if init_file.is_file():
            try:
                source = init_file.read_text(encoding="utf-8")
                code = compile(source, str(init_file), "exec")
                exec(code, pkg.__dict__)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "Failed to exec adapter-runner __init__.py into the "
                    "synthesized package: %s",
                    exc,
                )


# Module-level cache for the doctor row.
_LAST_DISCOVERY: Any = None

# Stashed PluginAPI captured at boot-time ``register()`` so the runtime
# hot-reload path (``register_adapter_at_runtime``) can publish freshly-
# authored adapter specs as tools in the SAME process. ``None`` outside
# the live agent (e.g. when running unit tests that call the
# adapter-runner internals directly without going through ``register``).
#
# The actual storage lives on the synthesised ``extensions.adapter_runner``
# package's ``__dict__`` (key: ``"_LIVE_API"``) — NOT on this module. The
# core plugin loader exec's ``plugin.py`` under a synthetic name like
# ``_opencomputer_plugin_adapter_runner_plugin``, so a later ``from
# extensions.adapter_runner.plugin import register_adapter_at_runtime``
# re-imports a SECOND copy of this file under a different module name.
# Two copies → two independent ``_LIVE_API`` globals → hot-reload would
# read None on the second copy. Routing through the package keeps a
# single shared cell that both copies see.
_LIVE_API: Any = None


def _live_api_get() -> Any:
    """Read the shared ``_LIVE_API`` slot from the package namespace."""
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        return None
    return getattr(pkg, "_LIVE_API", None)


def _live_api_set(api: Any) -> None:
    """Write the shared ``_LIVE_API`` slot on the package namespace.

    Falls back to this module's global when the package isn't in
    ``sys.modules`` yet (defensive — shouldn't happen because
    ``_bootstrap_package_namespace`` always runs first).
    """
    global _LIVE_API
    _LIVE_API = api  # keep the in-module slot for backwards compat too
    pkg = sys.modules.get(_PKG)
    if pkg is not None:
        pkg._LIVE_API = api  # type: ignore[attr-defined]


def register(api: Any) -> None:
    """Plugin entrypoint: discover + register synthetic tools per adapter."""
    _bootstrap_package_namespace()

    # Stash the live PluginAPI for runtime hot-reload (Bug 2 fix).
    # ``register_adapter_at_runtime()`` reads this slot to register
    # newly-authored adapters as tools without restarting the agent.
    # ``None`` outside the live agent context (e.g. unit tests that
    # call the synthetic-tool factory directly).
    _live_api_set(api)

    from extensions.adapter_runner._discovery import (
        discover_adapters,  # type: ignore[import-not-found]
    )

    profile_home = _resolve_profile_home()
    extensions_root = Path(__file__).resolve().parent.parent
    result = discover_adapters(
        profile_home=profile_home,
        extensions_root=extensions_root,
    )

    # Stash on module for the doctor row
    global _LAST_DISCOVERY
    _LAST_DISCOVERY = result

    _register_specs_with_api(api, result.specs)

    # Doctor contribution
    try:
        api.register_doctor_contribution(
            HealthContribution(
                id="adapter-runner",
                description=(
                    "adapter-runner: count of discovered @adapter recipes + "
                    "any import errors"
                ),
                run=_doctor_run,
            )
        )
    except AttributeError:  # older PluginAPI without doctor surface
        pass
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to register doctor contribution: %s", exc)


def _register_specs_with_api(api: Any, specs: list[Any]) -> None:
    """Wrap each spec as an ``AdapterTool`` and register with the host."""
    for spec in specs:
        try:
            api.register_tool(_AdapterTool(spec))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Failed to register adapter tool %s: %s", spec.tool_name, exc
            )


def register_adapter_at_runtime(spec: Any) -> dict[str, Any]:
    """Promote a freshly-authored ``AdapterSpec`` to a live callable tool.

    Called by ``Browser(action="adapter_save")`` after writing the
    adapter file to disk and importing it (so the ``@adapter``
    decorator landed the spec in the module-level registry). The new
    synthetic ``<Site><Name>`` ``BaseTool`` is registered on the
    process-wide tool registry via the stashed ``PluginAPI``, making
    it callable in the same agent session without restart.

    Returns a small status dict the action handler bubbles up to the
    agent so it knows what to call:

      - ``{"registered": True, "tool_name": "ProbeTest"}`` — fresh registration
      - ``{"registered": False, "already_registered": True, "tool_name": "..."}``
        — a tool with that name was already registered (boot-time
        discovery, or a previous hot-reload). The existing tool keeps
        running; the file change reaches it on the next process start.
      - ``{"registered": False, "reason": "..."}`` — no live API or
        registration failed for another reason.
    """
    live_api = _live_api_get()
    if live_api is None:
        return {
            "registered": False,
            "reason": "no live PluginAPI captured (register() never ran)",
        }
    tool = _AdapterTool(spec)
    tool_name = tool.schema.name
    try:
        live_api.register_tool(tool)
    except ValueError as exc:
        # ToolRegistry.register raises ValueError on duplicate names.
        # Surface this explicitly so the agent can decide whether to
        # rename or accept the existing tool. Re-registration would
        # require unregister-then-register, which we don't do
        # silently — file as a follow-up.
        if "already registered" in str(exc).lower():
            return {
                "registered": False,
                "already_registered": True,
                "tool_name": tool_name,
            }
        return {"registered": False, "reason": str(exc), "tool_name": tool_name}
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to register adapter tool %s: %s", tool_name, exc)
        return {"registered": False, "reason": str(exc), "tool_name": tool_name}
    return {"registered": True, "tool_name": tool_name}


def _resolve_profile_home() -> Path | None:
    """Best-effort: find the active profile home directory."""
    # Explicit env override (set by core when a profile is active).
    home_str = os.environ.get("OPENCOMPUTER_HOME")
    if home_str:
        return Path(home_str)
    # Fallback: the default location.
    return Path.home() / ".opencomputer" / "default"


# ─── synthetic tool wrapper ────────────────────────────────────────


class _AdapterTool(BaseTool):
    """One synthetic ``BaseTool`` per registered adapter."""

    parallel_safe = False

    def __init__(self, spec: Any) -> None:
        self._spec = spec
        # Capability claim — adapters that need browser/network are
        # gated behind the same browser.* tier as the Browser tool.
        self.capability_claims = (
            CapabilityClaim(
                capability_id=(
                    "adapter.browser" if spec.browser else "adapter.network"
                ),
                tier_required=(
                    ConsentTier.EXPLICIT if spec.browser else ConsentTier.IMPLICIT
                ),
                human_description=(
                    f"Run the {spec.tool_name} adapter against {spec.domain}."
                ),
            ),
        )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._spec.tool_name,
            description=self._spec.description,
            parameters=self._spec.to_json_schema(),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from extensions.adapter_runner._runner import run_adapter  # type: ignore[import-not-found]

        profile_home = _resolve_profile_home() or Path.home() / ".opencomputer"
        return await run_adapter(
            self._spec,
            arguments=dict(call.arguments or {}),
            profile_home=profile_home,
            call_id=call.id,
        )


# ─── doctor ────────────────────────────────────────────────────────


async def _doctor_run(fix: bool) -> RepairResult:  # noqa: ARG001
    discovery = _LAST_DISCOVERY
    if discovery is None:
        return RepairResult(
            id="adapter-runner",
            status="warn",
            detail="adapter-runner has not run discovery yet",
        )
    n = len(discovery.specs)
    if discovery.errors:
        # Truncate noisy error lists for the doctor row.
        sample = "; ".join(discovery.errors[:3])
        return RepairResult(
            id="adapter-runner",
            status="warn",
            detail=(
                f"discovered {n} adapter(s); {len(discovery.errors)} import "
                f"error(s): {sample}"
            ),
        )
    return RepairResult(
        id="adapter-runner",
        status="pass",
        detail=f"discovered {n} adapter(s)",
    )


__all__ = ["register", "register_adapter_at_runtime"]
